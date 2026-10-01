"""File de tâches persistante (SPEC § 4).

- exécution en arrière-plan, concurrence limitée (1 par défaut) ;
- une seule tâche active (en attente ou en cours) par image ;
- état sur disque : une tâche interrompue par un redémarrage est remise en
  attente et son ``.part`` supprimé ;
- annulation : une tâche en attente est retirée, une tâche en cours est
  arrêtée (le ``.part`` est nettoyé par la reconstruction).

Les gestionnaires (`handlers`) reçoivent la tâche et un `Job` ; ils lèvent
`RebuildError` en cas d'échec et retournent un message de fin facultatif.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..models import Task, TaskStatus
from .rebuild import Job, RebuildCancelled, RebuildError, part_path
from .scan import _write_atomic

Handler = Callable[[Task, Job], str | None]

ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING)
_HISTORY_LIMIT = 500        # tâches terminées conservées


class TaskConflict(RuntimeError):
    """Une tâche est déjà active sur cette image."""


class QueueLocked(RuntimeError):
    """Une autre instance du manager utilise déjà cette file."""


class TaskQueue:
    def __init__(self, state_file: Path, handlers: dict[str, Handler], concurrency: int = 1):
        self.state_file = state_file
        self.handlers = handlers
        self.concurrency = max(1, concurrency)
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, Job] = {}
        self._cond = threading.Condition(threading.RLock())
        self._threads: list[threading.Thread] = []
        self._stopping = False

    # -------------------------------------------------------------- état

    def lock(self) -> None:
        """Verrou exclusif sur la file : deux instances prendraient la même
        tâche et reconstruiraient la même image en même temps. Libéré à la
        fin du processus (y compris en cas de plantage)."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_file.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise QueueLocked(f"une autre instance utilise déjà {self.state_file}") from None
        self._lock_fd = fd

    def load(self) -> None:
        """Relit l'état ; une tâche « en cours » a été interrompue."""
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            data = []
        with self._cond:
            for raw in data:
                try:
                    task = Task.model_validate(raw)
                except ValueError:
                    continue
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
                    task.progress, task.phase = 0.0, ""
                    task.log.append("Interrompue par un redémarrage : remise en attente")
                    if task.image_path:
                        part_path(Path(task.image_path)).unlink(missing_ok=True)
                self._tasks[task.id] = task
            self._save()

    def _save(self) -> None:
        with self._cond:
            finished = sorted((t for t in self._tasks.values() if t.status not in ACTIVE),
                              key=lambda t: t.created_at)
            for old in finished[:-_HISTORY_LIMIT]:
                del self._tasks[old.id]
            text = json.dumps([t.model_dump(mode="json") for t in self._tasks.values()],
                              indent=1)
        _write_atomic(self.state_file, text)

    # -------------------------------------------------------------- API

    def submit(self, kind: str, image: str | None, image_path: Path | None,
               params: dict, title: str = "") -> Task:
        if kind not in self.handlers:
            raise ValueError(f"type de tâche inconnu : {kind}")
        with self._cond:
            if image and self.active_for(image):
                raise TaskConflict(f"une tâche est déjà en attente ou en cours pour {image}")
            task = Task(id=secrets.token_hex(6), kind=kind, title=title, image=image,
                        image_path=str(image_path) if image_path else None, params=params,
                        created_at=time.time())
            self._tasks[task.id] = task
            self._save()
            self._cond.notify_all()
            return task

    def active_for(self, image: str) -> Task | None:
        with self._cond:
            return next((t for t in self._tasks.values()
                         if t.image == image and t.status in ACTIVE), None)

    def list(self, image: str | None = None) -> list[Task]:
        with self._cond:
            tasks = [t.model_copy(deep=True) for t in self._tasks.values()
                     if image is None or t.image == image]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        with self._cond:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def cancel(self, task_id: str) -> Task:
        with self._cond:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                task.log.append("Annulée avant démarrage")
                self._save()
            elif task.status == TaskStatus.RUNNING:
                task.log.append("Annulation demandée")
                self._jobs[task_id].cancel()
            return task.model_copy(deep=True)

    def clear_finished(self) -> int:
        with self._cond:
            done = [k for k, t in self._tasks.items() if t.status not in ACTIVE]
            for k in done:
                del self._tasks[k]
            self._save()
            return len(done)

    # -------------------------------------------------------------- exécution

    def start(self) -> None:
        self._stopping = False
        for i in range(self.concurrency):
            thread = threading.Thread(target=self._worker, name=f"tâches-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            for job in self._jobs.values():
                job.cancel()
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=10)
        self._threads.clear()

    def _next(self) -> Task | None:
        """Plus ancienne tâche en attente dont l'image n'est pas déjà occupée."""
        busy = {t.image for t in self._tasks.values() if t.status == TaskStatus.RUNNING}
        pending = sorted((t for t in self._tasks.values() if t.status == TaskStatus.PENDING),
                         key=lambda t: t.created_at)
        return next((t for t in pending if t.image is None or t.image not in busy), None)

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._stopping and (task := self._next()) is None:
                    self._cond.wait(timeout=5)
                if self._stopping:
                    return
                assert task is not None
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()
                task.log.append("Démarrée")
                job = Job(on_progress=lambda phase, f, t=task: self._progress(t, phase, f),
                          on_log=lambda msg, t=task: self._log(t, msg))
                self._jobs[task.id] = job
                self._save()
            self.run_one(task, job)

    def run_one(self, task: Task, job: Job) -> None:
        """Exécute une tâche déjà marquée « en cours » (appelé par les workers)."""
        try:
            message = self.handlers[task.kind](task, job)
        except RebuildCancelled:
            if self._stopping:
                # Arrêt du service, pas demande de l'utilisateur : la tâche
                # reprendra au prochain démarrage (SPEC § 4)
                status, error = TaskStatus.PENDING, None
                message = "Interrompue par l'arrêt du service : remise en attente"
                task.progress, task.phase, task.started_at = 0.0, "", None
            else:
                status, error, message = TaskStatus.CANCELLED, None, "Annulée, image inchangée"
        except RebuildError as exc:
            status, error, message = TaskStatus.FAILED, str(exc), f"Échec : {exc}"
        except Exception as exc:  # noqa: BLE001 — un bug ne doit pas tuer la file
            status, error = TaskStatus.FAILED, f"erreur interne : {exc!r}"
            message = f"Échec : {error}"
        else:
            status, error = TaskStatus.DONE, None
            task.progress = 1.0
        with self._cond:
            task.status = status
            task.error = error
            task.finished_at = time.time() if status != TaskStatus.PENDING else None
            if message:
                task.log.append(message)
            self._jobs.pop(task.id, None)
            self._save()
            self._cond.notify_all()

    def _progress(self, task: Task, phase: str, fraction: float) -> None:
        with self._cond:
            if phase != task.phase:
                task.log.append(f"Étape : {phase}")
            task.phase, task.progress = phase, fraction

    def _log(self, task: Task, message: str) -> None:
        with self._cond:
            task.log.append(message)
