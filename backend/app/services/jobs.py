"""Suivi d'une tâche longue : progression, journal, annulation."""

from __future__ import annotations

import threading
from collections.abc import Callable

from .errors import ManagerError


class JobCancelled(ManagerError):
    """Tâche annulée à la demande ; rien n'a été modifié."""


class Job:
    """Suivi d'une tâche : progression, journal, annulation."""

    def __init__(self, on_progress: Callable[[str, float], None] | None = None,
                 on_log: Callable[[str], None] | None = None):
        self.cancel_event = threading.Event()
        self._on_progress = on_progress
        self._on_log = on_log

    def progress(self, phase: str, fraction: float) -> None:
        if self._on_progress:
            self._on_progress(phase, max(0.0, min(1.0, fraction)))

    def log(self, message: str) -> None:
        if self._on_log:
            self._on_log(message)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("annulée")
