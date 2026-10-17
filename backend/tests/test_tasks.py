"""Tests de la file de tâches (SPEC § 4)."""

from __future__ import annotations

import json

import pytest

from app.models import Task, TaskStatus
from app.services.rebuild import Cancelled
from app.services.tasks import Busy, TaskQueue


def _queue(tmp_path, runner=lambda t, ctx: None):
    return TaskQueue(tmp_path / "tasks.json", {"x": runner})


def test_one_active_task_per_image(tmp_path):
    q = _queue(tmp_path)
    q.start = lambda: None          # pas de worker : la tâche reste en attente
    q.submit("x", "windows/jeu", {})
    with pytest.raises(Busy):
        q.submit("x", "windows/jeu", {})
    q.submit("x", "windows/autre", {})


def test_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        _queue(tmp_path).submit("inconnu", None, {})


def test_running_task_is_requeued_after_restart(tmp_path):
    t = Task(id="abc", kind="x", image="windows/jeu", status=TaskStatus.RUNNING, progress=0.4)
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": [t.model_dump(mode="json")]}))
    q = _queue(tmp_path)
    got = q.get("abc")
    assert got.status == TaskStatus.PENDING
    assert got.progress == 0.0


def test_run_one_records_outcome(tmp_path):
    def runner(task, ctx):
        ctx.log("étape")
        ctx.progress(0.5)
        if task.params.get("fail"):
            raise RuntimeError("boum")
        if task.params.get("cancel"):
            raise Cancelled("annulé")

    q = _queue(tmp_path, runner)
    q.start = lambda: None
    results = {}
    for name, params in {"ok": {}, "fail": {"fail": 1}, "cancel": {"cancel": 1}}.items():
        t = q.submit("x", name, params)
        q.run_one(q._tasks[t.id])
        results[name] = q.get(t.id)
    assert results["ok"].status == TaskStatus.DONE
    assert results["ok"].log == ["étape"]
    assert results["fail"].status == TaskStatus.FAILED
    assert results["fail"].error == "boum"
    assert results["cancel"].status == TaskStatus.CANCELLED
    # état relu depuis le disque
    assert _queue(tmp_path).get(results["fail"].id).error == "boum"


def test_cancel_pending(tmp_path):
    q = _queue(tmp_path)
    q.start = lambda: None
    t = q.submit("x", "windows/jeu", {})
    assert q.cancel(t.id).status == TaskStatus.CANCELLED
    q.submit("x", "windows/jeu", {})      # l'image est libérée


def test_worker_runs_tasks(tmp_path):
    import threading
    done = threading.Event()
    q = _queue(tmp_path, lambda t, ctx: done.set())
    q.submit("x", "windows/jeu", {})
    assert done.wait(5)
