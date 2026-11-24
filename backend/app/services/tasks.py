"""File de tâches persistante (SPEC § 4).

- exécution en arrière-plan par voies (voir task_lanes) : le travail disque
  est limité à `task_concurrency` (1 par défaut), chaque client a sa propre
  voie (un envoi lent ne bloque pas les autres clients) ;
- une seule tâche active (en attente ou en cours) par image ;
- état sur disque : une tâche interrompue par un redémarrage est remise en
  attente et son ``.part`` supprimé ;
- annulation : une tâche en attente est retirée, une tâche en cours est
  arrêtée (le ``.part`` est nettoyé par la reconstruction).

Les gestionnaires (`handlers`) reçoivent la tâche et un `Job` ; ils lèvent
`ManagerError` en cas d'échec et retournent un message de fin facultatif.
"""

from __future__ import annotations

import builtins
import fcntl
import functools
import json
import logging
import os
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from ..models import Task, TaskStatus
from .durable import append_line, write_atomic
from .errors import ManagerError
from .jobs import Job, JobCancelled
from .rebuild import part_path
from .task_lanes import ALL_CLIENTS, CLIENT_PREFIX, DISK, image_lane

Handler = Callable[[Task, Job], str | None]
Lanes = Callable[[Task], frozenset[str]]


def _default_lanes(task: Task) -> frozenset[str]:
    """Sans configuration : travail disque et une voie par image."""
    return frozenset({DISK, image_lane(task.image)} if task.image else {DISK})

ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING)
NOT_RESUMED = ("trial",)        # tâches « en cours » au redémarrage : échec, pas de reprise
_HISTORY_LIMIT = 500        # tâches terminées conservées
_RETRY_DELAY = 5            # secondes avant que le worker reprenne après une erreur imprévue

log = logging.getLogger("wsquashfs-manager")


class TaskConflict(RuntimeError):
    """Une tâche est déjà active sur cette image."""


class QueueLocked(RuntimeError):
    """Une autre instance du manager utilise déjà cette file."""


class TaskQueue:
    def __init__(self, state_file: Path, handlers: dict[str, Handler], concurrency: int = 1,
                 night_now: Callable[[], bool] = lambda: True, lanes: Lanes = _default_lanes):
        self.state_file = state_file
        # Journal permanent des tâches terminées : historique de chaque image,
        # conservé quand on efface la liste des tâches
        self.history_file = state_file.with_name("history.jsonl")
        self.handlers = handlers
        # Capacité de la voie « disque » ; les voies client et image sont à 1
        self.concurrency = max(1, concurrency)
        self.lanes = lanes
        # Vrai pendant la plage horaire des tâches « de nuit »
        self.night_now = night_now
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, Job] = {}
        self._cond = threading.Condition(threading.RLock())
        self._io = threading.Lock()                     # une écriture de l'état à la fois (voir _save)
        self._threads: list[threading.Thread] = []      # répartiteur et tâches en cours
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
                if task.status == TaskStatus.RUNNING and task.kind in NOT_RESUMED:
                    # Un essai relancé seul lancerait un jeu sur un client sans que personne le demande
                    task.status = TaskStatus.FAILED
                    task.error = "interrompue par un redémarrage (non relancée)"
                    task.finished_at = time.time()
                    task.log.append("Interrompue par un redémarrage : non relancée (à relancer à la main)")
                elif task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
                    task.progress, task.phase = 0.0, ""
                    task.log.append("Interrompue par un redémarrage : remise en attente")
                    if task.image_path:
                        part_path(Path(task.image_path)).unlink(missing_ok=True)
                self._tasks[task.id] = task
        self._save()

    def _save(self) -> None:
        """Écrit l'état sur le disque (fsync). Le verrou d'état n'est tenu que pour
        la copie : l'écriture se fait hors de lui, pour que les lectures (affichage
        de la bibliothèque, état des tâches) n'attendent pas un disque occupé.
        Ne jamais l'appeler sous `_cond` : ordre des verrous = écriture puis état."""
        with self._io:
            with self._cond:
                finished = sorted((t for t in self._tasks.values() if t.status not in ACTIVE),
                                  key=lambda t: t.created_at)
                for old in finished[:-_HISTORY_LIMIT]:
                    del self._tasks[old.id]
                text = json.dumps([t.model_dump(mode="json") for t in self._tasks.values()],
                                  indent=1)
            write_atomic(self.state_file, text, durable=True)

    def _persist(self) -> None:
        """Enregistrement depuis les workers : un échec d'écriture (disque plein…)
        ne doit pas tuer le worker, sinon plus aucune tâche ne démarre jusqu'au
        redémarrage. L'état en mémoire fait foi ; il sera réécrit au prochain
        enregistrement réussi."""
        try:
            self._save()
        except OSError as exc:
            log.warning("file de tâches : état non enregistré (%s)", exc)

    # -------------------------------------------------------------- API

    def submit(self, kind: str, image: str | None, image_path: Path | None,
               params: dict, title: str = "", night: bool = False) -> Task:
        if kind not in self.handlers:
            raise ValueError(f"type de tâche inconnu : {kind}")
        with self._cond:
            if image and self.active_for(image):
                raise TaskConflict(f"une tâche est déjà en attente ou en cours pour {image}")
            task = Task(id=secrets.token_hex(6), kind=kind, title=title, image=image,
                        image_path=str(image_path) if image_path else None, params=params,
                        night=night, created_at=time.time())
            self._tasks[task.id] = task
            self._cond.notify_all()
        self._save()
        return task

    def active_for(self, image: str) -> Task | None:
        with self._cond:
            return next((t for t in self._tasks.values()
                         if t.image == image and t.status in ACTIVE), None)

    def list(self, image: str | None = None) -> list[Task]:
        with self._cond:
            tasks = [t.model_copy(deep=True) for t in self._tasks.values()
                     if image is None or t.image == image]
        if image is not None:                 # fiche : ajouter le journal permanent
            known = {t.id for t in tasks}
            tasks += [t for t in self._history(image) if t.id not in known]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def _history(self, image: str) -> builtins.list[Task]:
        try:
            lines = self.history_file.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                task = Task.model_validate_json(line)
            except ValueError:
                continue
            if task.image == image:
                out.append(task)
        return out

    def _record(self, task: Task) -> None:
        """Ajoute la tâche terminée au journal (journal abrégé aux 20
        dernières lignes : l'historique sert à savoir ce qui a été fait)."""
        entry = task.model_copy(update={"log": task.log[-20:]})
        try:
            append_line(self.history_file, entry.model_dump_json())
        except OSError:
            pass

    def get(self, task_id: str) -> Task | None:
        with self._cond:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def cancel(self, task_id: str) -> Task:
        with self._cond:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            finished = None
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                task.log.append("Annulée avant démarrage")
                finished = task.model_copy(deep=True)
            elif task.status == TaskStatus.RUNNING:
                task.log.append("Annulation demandée")
                self._jobs[task_id].cancel()
            result = task.model_copy(deep=True)
        if finished is not None:                 # écriture hors du verrou d'état
            self._record(finished)
            self._save()
        return result

    def run_now(self, task_id: str) -> Task:
        """Une tâche programmée la nuit démarre dès que possible."""
        with self._cond:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            changed = task.status == TaskStatus.PENDING and task.night
            if changed:
                task.night = False
                task.log.append("Programmation de nuit levée : lancement dès que possible")
                self._cond.notify_all()
            result = task.model_copy(deep=True)
        if changed:
            self._save()
        return result

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
        dispatcher = threading.Thread(target=self._dispatch, name="tâches-répartiteur", daemon=True)
        dispatcher.start()
        self._threads.append(dispatcher)

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            for job in self._jobs.values():
                job.cancel()
            self._cond.notify_all()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=10)
        self._threads.clear()

    def _busy(self) -> Counter[str]:
        """Nombre de tâches en cours par voie."""
        busy: Counter[str] = Counter()
        for task in self._tasks.values():
            if task.status == TaskStatus.RUNNING:
                busy.update(self.lanes(task))
        return busy

    def _free(self, lanes: frozenset[str], busy: Counter[str]) -> bool:
        """Vrai si la tâche peut démarrer : voie disque sous sa capacité, voie
        client ou image libre. « client:* » occupe tous les clients à la fois."""
        for lane in lanes:
            if lane == DISK:
                if busy[DISK] >= self.concurrency:
                    return False
            elif lane == ALL_CLIENTS:
                if any(count and key.startswith(CLIENT_PREFIX) for key, count in busy.items()):
                    return False
            elif busy[lane] or (lane.startswith(CLIENT_PREFIX) and busy[ALL_CLIENTS]):
                return False
        return True

    def _next(self) -> Task | None:
        """Plus ancienne tâche en attente dont les voies sont libres ; une tâche de nuit
        attend la plage horaire (réévalué toutes les 5 s)."""
        busy = self._busy()
        night = self.night_now()
        pending = sorted((t for t in self._tasks.values() if t.status == TaskStatus.PENDING
                          and (night or not t.night)), key=lambda t: t.created_at)
        return next((t for t in pending if self._free(self.lanes(t), busy)), None)

    def _dispatch(self) -> None:
        """Répartiteur : lance chaque tâche dont les voies sont libres, dans son propre fil.
        Un client occupé n'empêche pas les autres tâches de démarrer."""
        while not self._stopping:
            try:
                self._start_next()
            except Exception:  # noqa: BLE001 — le répartiteur doit survivre à tout
                log.exception("file de tâches : erreur imprévue, reprise dans %s s", _RETRY_DELAY)
                with self._cond:
                    self._cond.wait(timeout=_RETRY_DELAY)

    def _start_next(self) -> None:
        """Lance la prochaine tâche démarrable, ou attend (5 s, ou jusqu'à un changement)."""
        with self._cond:
            if self._stopping:
                return
            task = self._next()
            if task is None:
                self._cond.wait(timeout=5)
                return
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.log.append("Démarrée")
            job = Job(on_progress=functools.partial(self._progress, task),
                      on_log=functools.partial(self._log, task))
            self._jobs[task.id] = job
            self._threads = [t for t in self._threads if t.is_alive()]
            thread = threading.Thread(target=self._run, args=(task, job), name=f"tâche-{task.id}", daemon=True)
            self._threads.append(thread)
            thread.start()
        self._persist()                          # hors du verrou d'état

    def _run(self, task: Task, job: Job) -> None:
        try:
            self.run_one(task, job)
        except Exception:  # noqa: BLE001 — une tâche ne doit pas tuer la file
            log.exception("file de tâches : erreur imprévue pendant « %s »", task.title or task.kind)

    def run_one(self, task: Task, job: Job) -> None:
        """Exécute une tâche déjà marquée « en cours » (appelé par le répartiteur)."""
        try:
            message = self.handlers[task.kind](task, job)
        except JobCancelled:
            if self._stopping and task.kind in NOT_RESUMED:
                status, error = TaskStatus.CANCELLED, None
                message = "Interrompue par l'arrêt du service : non relancée"
            elif self._stopping:
                # Arrêt du service, pas demande de l'utilisateur : la tâche
                # reprendra au prochain démarrage (SPEC § 4)
                status, error = TaskStatus.PENDING, None
                message = "Interrompue par l'arrêt du service : remise en attente"
                task.progress, task.phase, task.started_at = 0.0, "", None
            else:
                status, error, message = TaskStatus.CANCELLED, None, "Annulée, image inchangée"
        except ManagerError as exc:
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
            finished = task.model_copy(deep=True) if status != TaskStatus.PENDING else None
            self._cond.notify_all()
        if finished is not None:                 # écritures hors du verrou d'état
            self._record(finished)
        self._persist()

    def _progress(self, task: Task, phase: str, fraction: float) -> None:
        with self._cond:
            if phase != task.phase:
                task.log.append(f"Étape : {phase}")
            task.phase, task.progress = phase, fraction

    def _log(self, task: Task, message: str) -> None:
        with self._cond:
            task.log.append(message)
