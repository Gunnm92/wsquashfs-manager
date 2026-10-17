"""File de tâches (SPEC § 4).

- exécution en arrière-plan, `task_concurrency` tâches à la fois ;
- jamais deux tâches actives sur la même image ;
- état sur disque (`<data_dir>/tasks.json`) : une tâche interrompue par un
  redémarrage est remise en attente (son `.part` est supprimé par la
  reconstruction suivante) ;
- annulation : une tâche en attente est retirée, une tâche en cours est
  interrompue (mksquashfs arrêté, `.part` supprimé).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..models import Task, TaskStatus
from .rebuild import Cancelled, Context

_ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING)
_MAX_LOG = 500
_KEEP_FINISHED = 200

Runner = Callable[[Task, Context], None]


class Busy(Exception):
    """Une tâche est déjà active sur cette image."""


class TaskQueue:
    def __init__(self, state_file: Path, runners: dict[str, Runner], concurrency: int = 1):
        self.state_file = state_file
        self.runners = runners
        self.concurrency = max(1, concurrency)
        self._tasks: dict[str, Task] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._started = False
        self._load()

    # ------------------------------------------------------------ état disque

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("tasks", []):
            t = Task.model_validate(raw)
            if t.status == TaskStatus.RUNNING:
                t.status = TaskStatus.PENDING
                t.progress = 0.0
                t.log.append("interrompue par un redémarrage : remise en attente")
            self._tasks[t.id] = t

    def _save(self) -> None:
        """Appelé verrou pris."""
        finished = sorted((t for t in self._tasks.values() if t.status not in _ACTIVE),
                          key=lambda t: t.created_at)
        for t in finished[:-_KEEP_FINISHED]:
            del self._tasks[t.id]
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"tasks": [t.model_dump(mode="json") for t in self._tasks.values()]}, indent=1))
        tmp.replace(self.state_file)

    # ------------------------------------------------------------ API

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        for _ in range(self.concurrency):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, kind: str, image: str | None, params: dict) -> Task:
        if kind not in self.runners:
            raise ValueError(f"type de tâche inconnu : {kind}")
        with self._lock:
            if image and any(t.image == image and t.status in _ACTIVE
                             for t in self._tasks.values()):
                raise Busy(f"une tâche est déjà en cours sur {image}")
            t = Task(id=uuid.uuid4().hex[:12], kind=kind, image=image, params=params,
                     created_at=time.time())
            self._tasks[t.id] = t
            self._save()
            self._wake.notify()
        self.start()
        return t

    def list(self) -> list[Task]:
        with self._lock:
            return sorted((t.model_copy(deep=True) for t in self._tasks.values()),
                          key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t.model_copy(deep=True) if t else None

    def active_for(self, image: str) -> Task | None:
        with self._lock:
            for t in self._tasks.values():
                if t.image == image and t.status in _ACTIVE:
                    return t.model_copy(deep=True)
        return None

    def cancel(self, task_id: str) -> Task | None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return None
            if t.status == TaskStatus.PENDING:
                t.status = TaskStatus.CANCELLED
                t.finished_at = time.time()
                self._save()
            elif t.status == TaskStatus.RUNNING:
                self._cancels[t.id].set()       # le worker termine et enregistre
                t.log.append("annulation demandée")
            return t.model_copy(deep=True)

    # ------------------------------------------------------------ exécution

    def _next(self) -> Task:
        with self._lock:
            while True:
                pending = sorted((t for t in self._tasks.values()
                                  if t.status == TaskStatus.PENDING),
                                 key=lambda t: t.created_at)
                if pending:
                    t = pending[0]
                    t.status = TaskStatus.RUNNING
                    t.started_at = time.time()
                    self._cancels[t.id] = threading.Event()
                    self._save()
                    return t
                self._wake.wait()

    def _worker(self) -> None:
        while True:
            t = self._next()
            self.run_one(t)

    def run_one(self, t: Task) -> None:
        cancel = self._cancels.setdefault(t.id, threading.Event())
        last_save = [0.0]

        def log(msg: str) -> None:
            with self._lock:
                t.log.append(msg)
                del t.log[:-_MAX_LOG]
                self._save()

        def progress(p: float) -> None:
            with self._lock:
                t.progress = p
                if time.time() - last_save[0] > 2:      # pas une écriture par pourcent
                    last_save[0] = time.time()
                    self._save()

        status, error = TaskStatus.DONE, None
        try:
            self.runners[t.kind](t, Context(log=log, progress=progress, cancel=cancel))
        except Cancelled:
            status = TaskStatus.CANCELLED
        except Exception as e:      # noqa: BLE001 — l'erreur va dans la tâche
            status, error = TaskStatus.FAILED, str(e) or type(e).__name__
        with self._lock:
            t.status = status
            t.error = error
            if error:
                t.log.append(f"échec : {error}")
            t.finished_at = time.time()
            self._cancels.pop(t.id, None)
            self._save()
