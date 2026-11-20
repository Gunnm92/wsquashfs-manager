"""Mode du manager : autonome (construction seule) ou connecté (machines).

Défaut au chargement, persistance, garde des routes connectées (409 en autonome), écoute
des machines suivant le mode, et refus des tâches connectées restées en file."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import deps, library_index, machines, main, task_submit
from app.config import Settings, get_settings, save_mode
from app.models import ImageInfo
from app.routes import images
from app.services import scan
from app.services import trial as trial_svc
from app.services.errors import ManagerError

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
    monkeypatch.setattr(deps, "settings", settings)
    monkeypatch.setattr(library_index, "library", {})
    return TestClient(main.app, headers={"x-wsfs-token": ""})


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
    monkeypatch.setattr(machines, "start_machine_server", lambda settings: started.append(settings) or SimpleNamespace(should_exit=False))
    monkeypatch.setattr(machines, "machine_server", None)
    deps.settings.machine_port = 8790
    try:
        client.put("/api/config/mode", json={"mode": "connecte"})
        client.put("/api/config/mode", json={"mode": "connecte"})          # déjà démarrée : rien de plus
        assert len(started) == 1 and machines.machine_server is not None
        stopped = machines.machine_server
        client.put("/api/config/mode", json={"mode": "autonome"})
        assert stopped.should_exit is True and machines.machine_server is None
    finally:
        machines.machine_server = None
        deps.settings.machine_port = 0


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
    monkeypatch.setattr(scan, "scan_all", lambda settings, force=False: [])
    assert client.get("/api/library").status_code == 200
    assert client.get("/api/saves/art", params={"game": "PC:Jeu"}).status_code == 404   # pas 409
    assert client.get("/api/config/systems").status_code == 200
    assert client.put("/api/config/night", json={"start": "01:00", "end": "07:00"}).status_code == 200
    assert client.put("/api/config/auto_push", json={"seconds": 0}).status_code == 200


def test_autonomous_image_links_does_not_ask_the_game_host(client, monkeypatch):
    info = ImageInfo(id="arcade/jeu", path=Path("/roms/arcade/jeu.wsquashfs"), name="jeu",
                          system="arcade", size=1)
    monkeypatch.setattr(library_index, "find_image", lambda settings, image_id: info)
    monkeypatch.setattr(trial_svc, "doctor", lambda settings, refresh=False: pytest.fail("machine interrogée"))
    monkeypatch.setattr(images, "suggest", lambda settings, path, runners: {"links": [], "runners": runners})
    body = client.get("/api/image/links", params={"id": "arcade/jeu"}).json()
    assert body["steambox_error"] == DETAIL


def test_connected_mode_lets_connected_routes_through(client, tmp_path, monkeypatch):
    deps.settings.mode = "connecte"
    assert client.get("/api/saves/machines").json() == {"machines": []}
    assert client.get("/api/install/status").status_code == 200


def test_connected_task_refused_in_autonomous_mode(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(deps, "settings", _settings(tmp_path))
    deps.settings.mode = "autonome"
    handler = task_submit.connected_task(lambda settings, task, job: calls.append((settings, task)) or "fait",
                                         deps.settings)
    with pytest.raises(ManagerError, match="mode autonome"):
        handler("tâche", None)
    assert calls == []
    deps.settings.mode = "connecte"
    assert handler("tâche", None) == "fait"
    assert calls == [(deps.settings, "tâche")]      # le gestionnaire reçoit les réglages en premier


def test_every_queue_handler_receives_settings_task_job(tmp_path, monkeypatch):
    """Chaque gestionnaire de la file appelle sa fonction métier avec (réglages, tâche, job)."""
    monkeypatch.setattr(deps, "settings", _settings(tmp_path))
    deps.settings.mode = "connecte"
    appels: dict[str, tuple] = {}

    def espion(nom):
        def f(*args):
            appels[nom] = args
            return nom
        return f

    fonctions = {
        "autorun": "run_autorun", "files": "run_files", "verify": "run_verify",
        "cover": "run_cover", "pack": "run_pack", "update": "run_update",
        "trial": "run_trial", "runner": "run_runner", "relink": "run_relink",
        "saves": "run_saves", "install": "run_install",
    }
    assert set(deps.queue.handlers) == set(fonctions)
    for nom in fonctions.values():
        monkeypatch.setattr(deps, nom, espion(nom))
    task, job = object(), object()
    for kind, nom in fonctions.items():
        assert deps.queue.handlers[kind](task, job) == nom
        assert appels[nom] == (deps.settings, task, job), kind
