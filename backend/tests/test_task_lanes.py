"""Voies des tâches : quel client ou quel travail disque chaque type de tâche occupe."""

from __future__ import annotations

from app.config import Settings
from app.models import Task
from app.services.task_lanes import ALL_CLIENTS, DISK, lanes_of


def _lanes(kind: str, params: dict, image: str | None = None) -> frozenset[str]:
    return lanes_of(Task(id="t", kind=kind, image=image, params=params), Settings())


def test_pack_and_local_work_use_the_disk_lane():
    assert _lanes("pack", {}, "win/Jeu") == {DISK, "image:win/Jeu"}
    assert _lanes("relink", {}, "win/Jeu") == {DISK, "image:win/Jeu"}


def test_pull_and_push_hold_only_their_clients():
    assert _lanes("saves", {"op": "pull", "source": "a"}) == {"client:a"}
    assert _lanes("saves", {"op": "push", "targets": ["a", "b"]}) == {"client:a", "client:b"}
    assert _lanes("saves", {"op": "push_from", "source": "a", "targets": ["b"]}) == {"client:a", "client:b"}


def test_operations_that_may_touch_any_client_take_every_client():
    assert _lanes("saves", {"op": "pull_auto"}) == {ALL_CLIENTS}
    assert _lanes("saves", {"op": "push_auto", "targets": ["a"]}) == {ALL_CLIENTS}
    assert _lanes("saves", {"op": "resolve", "keep": "manager", "push": False}) == {ALL_CLIENTS}
    assert _lanes("saves", {"op": "restore", "push": True}) == {ALL_CLIENTS}
    assert _lanes("saves", {"op": "restore", "push": False}) == frozenset()


def test_install_holds_the_image_and_its_clients():
    assert _lanes("install", {"op": "install", "machines": ["a"]}, "win/Jeu") == {"image:win/Jeu", "client:a"}
    assert _lanes("install", {"op": "uninstall", "name": "Jeu", "machines": ["a", "b"]}) == {"client:a", "client:b"}


def test_trial_and_runner_use_the_game_host_lane():
    assert _lanes("trial", {"duration": 60}, "win/Jeu") == {"image:win/Jeu", "client:game-host"}
