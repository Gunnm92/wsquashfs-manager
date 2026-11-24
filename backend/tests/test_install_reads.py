"""Pendant l'envoi lent d'un jeu vers une machine, les lectures locales (autorun d'un dossier,
liste des dossiers) restent rapides."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import deps, main
from app.services import install
from app.services.tasks import TaskQueue
from tests.test_install import BLOCK, NAME, games_agents, make_image  # noqa: F401 (fixture)
from tests.test_saves import associate, machine_ids, make_settings


def test_local_reads_stay_fast_during_a_slow_send(games_agents, tmp_path, monkeypatch):
    deck = games_agents("steamdeck", slow=0.4)               # ~0,4 s par bloc de 1 Mio
    settings = make_settings(tmp_path)
    settings.roms_dirs = [str(tmp_path / "roms")]
    associate(settings, deck)
    mid = machine_ids(settings)["steamdeck"]
    image = make_image(tmp_path, 12 * BLOCK, NAME)
    pc = tmp_path / "roms" / "arcade" / "Autre.pc"
    pc.mkdir(parents=True)
    (pc / "autorun.cmd").write_text("game.exe\r\n")

    monkeypatch.setattr(install, "BLOCK_SIZE", BLOCK)
    queue = TaskQueue(tmp_path / "state" / "tasks.json",
                      {"install": lambda task, job: install.run_install(settings, task, job)})
    monkeypatch.setattr(deps, "settings", settings)
    monkeypatch.setattr(deps, "queue", queue)
    monkeypatch.setattr("app.library_index.find_image",
                        lambda s, i: SimpleNamespace(id=i, path=image))
    client = TestClient(main.app, headers={"x-wsfs-token": ""})
    queue.start()
    try:
        task = queue.submit("install", "arcade/Jeu", image, {"op": "install", "machines": [mid]}, "envoi")
        time.sleep(0.5)                                      # l'envoi est en cours
        slowest = 0.0
        for _ in range(3):
            started = time.monotonic()
            response = client.get("/api/folders/autorun", params={"id": "arcade/Autre.pc"})
            slowest = max(slowest, time.monotonic() - started)
            assert response.status_code == 200 and response.json()["found"] is True
        assert queue.get(task.id).status.value == "running"  # le transfert est toujours en cours
        assert slowest < 1.0, f"autorun lu en {slowest:.2f} s pendant l'envoi"
    finally:
        queue.stop()
