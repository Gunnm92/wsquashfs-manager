"""Mode du manager : autonome (construction seule) ou connecté (machines).

Défaut au chargement, persistance, garde des routes connectées (409 en autonome), écoute
des machines suivant le mode, et refus des tâches connectées restées en file."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, get_settings, save_mode
from app.services.rebuild import RebuildError

DETAIL = "mode autonome : fonction connectée désactivée (Réglages › Mode)"
MACHINE = {"id": "m1", "name": "steambox", "url": "https://10.0.0.2:8790", "token": "t", "pem": "p"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Réglages chargés depuis un config.json vide, sans machine de jeu dans l'environnement."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("WSQUASHFS_MGR_RUNTIME_CONFIG_FILE", str(path))
    monkeypatch.delenv("WSQUASHFS_MGR_GAME_HOST", raising=False)
    return path


def _settings(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("machine_port", 0)
    return Settings(state_dir=tmp_path / "state", runtime_config_file=tmp_path / "config.json", **kw)


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.mode = "autonome"
    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "_library", {})
    return TestClient(main.app)


# ------------------------------------------------------------------ défaut et persistance

def test_default_is_autonomous_without_machine(runtime):
    assert get_settings().mode == "autonome"


def test_default_is_connected_with_an_associated_machine(runtime):
    runtime.write_text(json.dumps({"saves_machines": [MACHINE]}))
    assert get_settings().mode == "connecte"


def test_default_is_connected_with_a_local_or_agent_game_host(runtime, monkeypatch):
    monkeypatch.setenv("WSQUASHFS_MGR_GAME_HOST", "local")
    assert get_settings().mode == "connecte"
    monkeypatch.setenv("WSQUASHFS_MGR_GAME_HOST", "off")
    assert get_settings().mode == "autonome"


def test_saved_mode_beats_the_default(runtime):
    runtime.write_text(json.dumps({"saves_machines": [MACHINE]}))
    save_mode(get_settings(), "autonome")
    assert get_settings().mode == "autonome"
    save_mode(get_settings(), "connecte")
    assert json.loads(runtime.read_text())["mode"] == "connecte"
    assert get_settings().mode == "connecte"


def test_save_mode_refuses_unknown_value(tmp_path):
    with pytest.raises(ValueError):
        save_mode(_settings(tmp_path), "docker")


# ------------------------------------------------------------------ route

def test_put_mode_validates_and_is_exposed(client):
    assert client.get("/api/config").json()["mode"] == "autonome"
    assert client.put("/api/config/mode", json={"mode": "docker"}).status_code == 422
    body = client.put("/api/config/mode", json={"mode": "connecte"}).json()
    assert body["mode"] == "connecte" and body["machine_listening"] is False   # machine_port 0
    assert client.put("/api/config/mode", json={"mode": "autonome"}).json()["mode"] == "autonome"


def test_put_mode_starts_and_stops_the_machine_listener(client, monkeypatch):
    started = []
    monkeypatch.setattr(main, "_start_machine_server", lambda settings: started.append(settings) or SimpleNamespace(should_exit=False))
    monkeypatch.setattr(main, "_machine_server", None)
    main._settings.machine_port = 8790
    try:
        client.put("/api/config/mode", json={"mode": "connecte"})
        client.put("/api/config/mode", json={"mode": "connecte"})          # déjà démarrée : rien de plus
        assert len(started) == 1 and main._machine_server is not None
        stopped = main._machine_server
        client.put("/api/config/mode", json={"mode": "autonome"})
        assert stopped.should_exit is True and main._machine_server is None
    finally:
        main._machine_server = None
        main._settings.machine_port = 0


# ------------------------------------------------------------------ garde

CONNECTED_CALLS = [
    ("GET", "/api/saves/status", None),
    ("GET", "/api/saves/machines", None),
    ("POST", "/api/saves/machines", {"code": "x"}),
    ("POST", "/api/saves/pull", {"games": ["Jeu"], "source": "m1"}),
    ("POST", "/api/saves/push", {"games": ["Jeu"], "targets": ["m1"]}),
    ("POST", "/api/saves/delete", {"games": ["Jeu"], "machines": ["m1"]}),
    ("GET", "/api/install/status", None),
    ("POST", "/api/install", {"ids": ["arcade/jeu"], "machines": ["m1"]}),
    ("POST", "/api/uninstall", {"names": ["Jeu.wsquashfs"], "machines": ["m1"]}),
    ("POST", "/api/game_host/maintenance", {"action": "list"}),
    ("GET", "/api/game_host/status", None),
    ("GET", "/api/launcher/doctor", None),
    ("POST", "/api/image/trial", None),
    ("POST", "/api/image/runner", None),
]


@pytest.mark.parametrize("method,path,body", CONNECTED_CALLS)
def test_autonomous_refuses_connected_routes(client, method, path, body):
    response = client.request(method, path, json=body, params={"id": "arcade/jeu"})
    assert response.status_code == 409, path
    assert response.json() == {"detail": DETAIL}


def test_autonomous_still_serves_art_and_construction(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main.scan, "scan_all", lambda settings, force=False: [])
    assert client.get("/api/library").status_code == 200
    assert client.get("/api/saves/art", params={"game": "PC:Jeu"}).status_code == 404   # pas 409
    assert client.get("/api/config/systems").status_code == 200
    assert client.put("/api/config/night", json={"start": "01:00", "end": "07:00"}).status_code == 200
    assert client.put("/api/config/auto_push", json={"seconds": 0}).status_code == 200


def test_autonomous_image_links_does_not_ask_the_game_host(client, monkeypatch):
    info = main.ImageInfo(id="arcade/jeu", path=Path("/roms/arcade/jeu.wsquashfs"), name="jeu",
                          system="arcade", size=1)
    monkeypatch.setattr(main, "_find_image", lambda image_id: info)
    monkeypatch.setattr(main, "_doctor", lambda refresh=False: pytest.fail("machine interrogée"))
    monkeypatch.setattr(main, "suggest", lambda settings, path, runners: {"links": [], "runners": runners})
    body = client.get("/api/image/links", params={"id": "arcade/jeu"}).json()
    assert body["steambox_error"] == DETAIL


def test_connected_mode_lets_connected_routes_through(client, tmp_path, monkeypatch):
    main._settings.mode = "connecte"
    assert client.get("/api/saves/machines").json() == {"machines": []}
    assert client.get("/api/install/status").status_code == 200


def test_connected_task_refused_in_autonomous_mode(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_settings", _settings(tmp_path))
    main._settings.mode = "autonome"
    handler = main._connected_task(lambda task, job: calls.append(task) or "fait")
    with pytest.raises(RebuildError, match="mode autonome"):
        handler("tâche", None)
    assert calls == []
    main._settings.mode = "connecte"
    assert handler("tâche", None) == "fait"
