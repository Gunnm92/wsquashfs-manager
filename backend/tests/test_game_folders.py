"""Dossiers de jeu `*.pc` (mode Batocera) : entrées « dossier » de la bibliothèque, surcharge
d'autorun à côté du dossier, refus des actions de reconstruction, sauvegardes rattachées,
et `*.wine` (atelier d'empaquetage) hors des jeux."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import deps, library_index, main
from app.config import Settings
from app.services import saves_status
from app.services import scan

AUTORUN = b"REM jeu\r\nCMD=Jeu.exe\r\nDIR=bin\r\n"


@pytest.fixture
def roms(tmp_path, monkeypatch):
    """roms/win : Jeu.pc (autorun, sauvegardes), Jeu.wine (atelier), Autre.pc sans autorun,
    et une image Jeu.wsquashfs pour une même clé de sauvegardes."""
    win = tmp_path / "roms" / "win"
    (win / "Jeu.pc" / "bin").mkdir(parents=True)
    (win / "Jeu.pc" / "bin" / "Jeu.exe").write_bytes(b"MZ")
    (win / "Jeu.pc" / "autorun.cmd").write_bytes(AUTORUN)
    (win / "Autre.pc").mkdir()
    (win / "Jeu.wine").mkdir()
    (win / "Jeu.wine" / "system.reg").write_bytes(b"x")
    (win / "Avec.wsquashfs").write_bytes(b"")
    (win / "Avec.pc").mkdir()
    (tmp_path / "saves" / "Jeu").mkdir(parents=True)
    (tmp_path / "saves" / "Jeu" / "slot.sav").write_bytes(b"12345")
    monkeypatch.setattr(scan, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(scan, "unsquashfs_cat", lambda settings, image, member: None)
    for key, value in {"roms_dirs": [str(tmp_path / "roms")], "saves_dir": tmp_path / "saves",
                       "tmp_dir": tmp_path / "tmp", "state_dir": tmp_path / "state"}.items():
        monkeypatch.setattr(deps.settings, key, value)
    library_index.library.clear()
    return tmp_path / "roms" / "win"


@pytest.fixture
def client(roms):
    return TestClient(main.app, headers={"x-wsfs-token": ""})


def _by_id(client):
    return {i["id"]: i for i in client.get("/api/library").json()}


def _edit(lines, **extra):
    return {"lines": lines, "eol": "\r\n", "trailing_eol": True, **extra}


def test_folder_appears_as_game_with_its_own_id(client):
    library = _by_id(client)
    assert library["win/Jeu.pc"]["kind"] == "folder"
    assert library["win/Jeu.pc"]["name"] == "Jeu"
    assert library["win/Jeu.pc"]["cmd"] == "Jeu.exe" and library["win/Jeu.pc"]["dir"] == "bin"
    assert library["win/Jeu.pc"]["size"] == len(b"MZ") + len(AUTORUN)
    assert library["win/Autre.pc"]["kind"] == "folder" and library["win/Autre.pc"]["cmd"] is None


def test_image_and_folder_with_same_name_are_distinct(client):
    library = _by_id(client)
    assert library["win/Avec"]["kind"] == "image"
    assert library["win/Avec.pc"]["kind"] == "folder"


def test_wine_folder_is_not_a_game(client):
    library = _by_id(client)
    assert "win/Jeu.wine" not in library and "win/Jeu" not in library
    assert all(not game_id.endswith(".wine") for game_id in library)


def test_override_written_next_to_folder(client, roms):
    body = _edit(["CMD=Jeu.exe", "DIR=bin", "HIDRAW=1"])
    response = client.put("/api/image/override", params={"id": "win/Jeu.pc"}, json=body)
    assert response.status_code == 200, response.text
    assert (roms / "Jeu.pc.autorun").read_bytes() == b"HIDRAW=1\r\n"
    assert response.json()["image"]["kind"] == "folder"
    assert response.json()["image"]["override"] == ["HIDRAW"]
    assert _by_id(client)["win/Jeu.pc"]["hidraw"] is True


def test_override_changes_effective_command(client, roms):
    client.put("/api/image/override", params={"id": "win/Jeu.pc"},
               json=_edit(["CMD=Autre.exe", "DIR=bin"]))
    assert (roms / "Jeu.pc.autorun").read_bytes() == b"CMD=Autre.exe\r\n"
    assert _by_id(client)["win/Jeu.pc"]["cmd"] == "Autre.exe"
    assert not (roms / "Jeu.pc" / "Jeu.pc.autorun").exists()


def test_autorun_read_comes_from_folder(client):
    r = client.get("/api/image/autorun", params={"id": "win/Jeu.pc"}).json()
    assert r["found"] is True and "CMD=Jeu.exe" in r["text"]


def test_folder_rebuild_routes_refused_with_409(client):
    detail = "dossier : pas d'image à reconstruire"
    for response in (
        client.post("/api/image/autorun", params={"id": "win/Jeu.pc"}, json=_edit(["CMD=x.exe"])),
        client.post("/api/image/autorun/preview", params={"id": "win/Jeu.pc"}, json=_edit(["CMD=x.exe"])),
        client.post("/api/image/relink", params={"id": "win/Jeu.pc"}, json={}),
        client.post("/api/image/update", params={"id": "win/Jeu.pc"},
                    json={"source": "/tmp", "anchor": ""}),
        client.post("/api/image/old/validate", params={"id": "win/Jeu.pc"}),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]
    assert client.post("/api/image/relink", params={"id": "win/Jeu.pc"}, json={}).json()["detail"] == detail


def test_folder_files_route_refused(client):
    assert client.get("/api/image/files", params={"id": "win/Jeu.pc"}).status_code == 409


def test_saves_route_accepts_folder(client, monkeypatch):
    """L'état des sauvegardes d'un dossier : la clé du jeu est « Jeu » (sans .pc)."""
    seen = {}

    def fake_status(settings, games=None, refresh=False):
        seen["games"] = games
        return {"games": {}}

    monkeypatch.setattr(saves_status, "status", fake_status)
    monkeypatch.setattr(deps.settings, "mode", "connecte", raising=False)
    r = client.get("/api/saves/status", params={"id": "win/Jeu.pc"})
    assert r.status_code == 200, r.text
    assert seen["games"] == ["Jeu"]


def test_saves_size_shared_by_image_and_folder(client):
    library = _by_id(client)
    assert library["win/Jeu.pc"]["saves_size"] == 5
    assert library["win/Jeu.pc"]["saves_present"] is True


def test_saves_row_gets_folder_system_without_image(roms, tmp_path):
    settings = Settings(roms_dirs=[str(tmp_path / "roms")])
    assert saves_status.game_systems(settings, ttl=0)["Jeu"] == "win"
    assert saves_status.system_of(settings, "Jeu") == "win"


def test_folder_cache_refreshes_on_autorun_change(client, roms):
    assert _by_id(client)["win/Jeu.pc"]["cmd"] == "Jeu.exe"
    (roms / "Jeu.pc" / "autorun.cmd").write_bytes(b"CMD=Nouveau.exe\r\n")
    assert _by_id(client)["win/Jeu.pc"]["cmd"] == "Nouveau.exe"


def test_folder_cover_read_from_folder(client, roms):
    cover = roms / "Jeu.pc" / ".wsquashfs"
    cover.mkdir()
    (cover / "cover").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    response = client.get("/api/image/art", params={"id": "win/Jeu.pc", "kind": "icon"})
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
