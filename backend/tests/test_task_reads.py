"""Les lectures de la file (affichage de la bibliothèque) ne attendent pas les écritures disque."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import app.services.tasks as tasks_mod
from app.services.tasks import TaskQueue


def test_reads_are_not_blocked_by_a_slow_state_write(tmp_path: Path, monkeypatch):
    """Un disque lent (fsync de tasks.json) ne doit pas figer active_for/get/list."""
    real_write = tasks_mod.write_atomic
    slow = threading.Event()

    def write(target, text, durable=False):
        if slow.is_set():
            time.sleep(1.5)                      # disque occupé par un gros envoi
        real_write(target, text, durable=durable)

    monkeypatch.setattr(tasks_mod, "write_atomic", write)
    queue = TaskQueue(tmp_path / "tasks.json", {"x": lambda task, job: None})
    queue.submit("x", "s/a", None, {})
    slow.set()

    writer = threading.Thread(target=queue.submit, args=("x", "s/b", None, {}))
    writer.start()
    time.sleep(0.1)                              # l'écriture lente est en cours
    started = time.monotonic()
    queue.active_for("s/a")
    queue.get("inconnue")
    queue.list()
    elapsed = time.monotonic() - started
    writer.join()
    assert elapsed < 0.3, f"lectures bloquées {elapsed:.2f} s par une écriture disque"
