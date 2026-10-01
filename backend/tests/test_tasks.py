"""File de tâches : persistance, verrou par image, annulation, échecs."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.models import TaskStatus
from app.services.rebuild import Job, RebuildError
from app.services.tasks import QueueLocked, TaskConflict, TaskQueue


def _wait(queue: TaskQueue, task_id: str, timeout: float = 5) -> TaskStatus:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = queue.get(task_id).status
        if status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return status
        time.sleep(0.02)
    raise AssertionError("tâche jamais terminée")


def test_one_active_task_per_image(tmp_path: Path):
    queue = TaskQueue(tmp_path / "tasks.json", {"x": lambda task, job: None})
    queue.submit("x", "win/jeu", None, {})
    with pytest.raises(TaskConflict):
        queue.submit("x", "win/jeu", None, {})
    queue.submit("x", "arcade/jeu", None, {})        # même nom, autre système


def test_state_survives_restart_and_running_is_requeued(tmp_path: Path):
    image = tmp_path / "Jeu.wsquashfs"
    part = tmp_path / "Jeu.wsquashfs.part"
    part.write_bytes(b"partiel")
    state = tmp_path / "tasks.json"
    queue = TaskQueue(state, {"x": lambda task, job: None})
    task = queue.submit("x", "win/Jeu", image, {"a": 1}, "titre")

    # Simule un arrêt brutal pendant l'exécution
    data = json.loads(state.read_text())
    data[0]["status"] = "running"
    state.write_text(json.dumps(data))

    reloaded = TaskQueue(state, {"x": lambda task, job: None})
    reloaded.load()
    again = reloaded.get(task.id)
    assert again.status == TaskStatus.PENDING
    assert again.params == {"a": 1} and again.title == "titre"
    assert "redémarrage" in again.log[-1]
    assert not part.exists()


def test_worker_runs_tasks_and_records_failures(tmp_path: Path):
    def ok(task, job):
        job.progress("mksquashfs", 0.5)
        return "fini"

    def ko(task, job):
        raise RebuildError("disque plein")

    def bug(task, job):
        raise ZeroDivisionError

    queue = TaskQueue(tmp_path / "tasks.json", {"ok": ok, "ko": ko, "bug": bug})
    queue.start()
    try:
        a = queue.submit("ok", "s/a", None, {})
        b = queue.submit("ko", "s/b", None, {})
        c = queue.submit("bug", "s/c", None, {})
        assert _wait(queue, a.id) == TaskStatus.DONE
        assert _wait(queue, b.id) == TaskStatus.FAILED
        assert _wait(queue, c.id) == TaskStatus.FAILED
    finally:
        queue.stop()
    done = queue.get(a.id)
    assert done.progress == 1.0 and done.log[-1] == "fini" and "Étape : mksquashfs" in done.log
    assert queue.get(b.id).error == "disque plein"
    assert "erreur interne" in queue.get(c.id).error
    # Une image libérée accepte une nouvelle tâche
    queue.submit("ok", "s/a", None, {})


def test_cancel_pending_and_running(tmp_path: Path):
    started = threading.Event()

    def slow(task, job: Job):
        started.set()
        while True:
            job.check()             # lève RebuildCancelled une fois annulée
            time.sleep(0.01)

    queue = TaskQueue(tmp_path / "tasks.json", {"slow": slow})
    running = queue.submit("slow", "s/a", None, {})
    pending = queue.submit("slow", "s/b", None, {})
    queue.start()
    try:
        assert started.wait(5)
        queue.cancel(pending.id)
        queue.cancel(running.id)
        assert _wait(queue, running.id) == TaskStatus.CANCELLED
        assert queue.get(pending.id).status == TaskStatus.CANCELLED
    finally:
        queue.stop()


def test_clear_finished_keeps_active(tmp_path: Path):
    queue = TaskQueue(tmp_path / "tasks.json", {"x": lambda task, job: None})
    keep = queue.submit("x", "s/a", None, {})
    gone = queue.submit("x", "s/b", None, {})
    queue.cancel(gone.id)
    assert queue.clear_finished() == 1
    assert [t.id for t in queue.list()] == [keep.id]


def test_service_stop_requeues_running_task(tmp_path: Path):
    started = threading.Event()

    def slow(task, job: Job):
        started.set()
        while True:
            job.check()
            time.sleep(0.01)

    state = tmp_path / "tasks.json"
    queue = TaskQueue(state, {"slow": slow})
    task = queue.submit("slow", "s/a", None, {})
    queue.start()
    assert started.wait(5)
    queue.stop()
    assert queue.get(task.id).status == TaskStatus.PENDING
    assert "remise en attente" in queue.get(task.id).log[-1]
    reloaded = TaskQueue(state, {"slow": slow})
    reloaded.load()
    assert reloaded.get(task.id).status == TaskStatus.PENDING


def test_second_instance_is_refused(tmp_path: Path):
    first = TaskQueue(tmp_path / "tasks.json", {})
    first.lock()
    with pytest.raises(QueueLocked):
        TaskQueue(tmp_path / "tasks.json", {}).lock()
