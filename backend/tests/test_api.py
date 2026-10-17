"""Tests de l'API : authentification et désignation des images par identifiant."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import ImageInfo
from app.services.tasks import TaskQueue


@pytest.fixture
def client(monkeypatch):
    images = [
        ImageInfo(id="arcade/jeu", path=Path("/roms/arcade/jeu.wsquashfs"), name="jeu",
                  system="arcade"),
        ImageInfo(id="windows/jeu", path=Path("/roms/windows/jeu.wsquashfs"), name="jeu",
                  system="windows"),
    ]
    monkeypatch.setattr(main.scan, "scan_all", lambda settings, force=False: images)
    return TestClient(main.app)


def test_bad_token_gives_401_not_500(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    r = client.get("/api/config", headers={"x-wsfs-token": "faux"})
    assert r.status_code == 401
    r = client.get("/api/config")
    assert r.status_code == 401


def test_good_token_passes(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    token = client.post("/login", json={"password": "sésame"}).json()["token"]
    r = client.get("/api/config", headers={"x-wsfs-token": token.encode()})
    assert r.status_code == 200


def test_same_name_in_two_systems(client):
    r = client.get("/api/image", params={"id": "windows/jeu"})
    assert r.status_code == 200
    assert r.json()["system"] == "windows"
    assert client.get("/api/image", params={"id": "jeu"}).status_code == 404


def test_autorun_edit_rejects_bad_eol(client):
    r = client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                    json={"lines": ["CMD=a.exe"], "eol": "\r"})
    assert r.status_code == 422


@pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
                    reason="squashfs-tools absent")
def test_autorun_edit_end_to_end(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Game"
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    (src.parent / "autorun.cmd").write_bytes(b"DIR=Game\r\nCMD=game.exe\r\n")
    roms = tmp_path / "roms"
    image = roms / "windows" / "jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src.parent), str(image), "-noappend", "-quiet"],
                   check=True)
    monkeypatch.setattr(main._settings, "roms_dirs", [str(roms)])
    monkeypatch.setattr(main._settings, "tmp_dir", tmp_path / "tmp")
    monkeypatch.setattr(main._settings, "saves_dir", tmp_path / "saves")
    monkeypatch.setattr(main.scan, "_CACHE_DIR", tmp_path / "cache")
    queue = TaskQueue(tmp_path / "tasks.json", {"edit_autorun": main._run_edit_autorun})
    monkeypatch.setattr(main, "_queue", queue)
    client = TestClient(main.app)

    a = client.get("/api/image/autorun", params={"id": "windows/jeu"}).json()
    assert a["issues"] == []
    body = {"lines": [*a["lines"], "CMD=autre.exe", "HIDRAW=1"], "eol": a["eol"],
            "trailing_eol": a["trailing_eol"], "encoding": a["encoding"], "base": a["base"]}
    body["lines"][1] = "CMD=absent.exe"
    preview = client.post("/api/image/autorun", params={"id": "windows/jeu", "dry_run": True},
                          json=body).json()
    assert preview["changed"] and "task" not in preview
    assert "+HIDRAW=1" in preview["diff"]
    assert any("absent.exe" in i["message"] for i in preview["issues"])

    body["lines"][1] = "CMD=game.exe"
    r = client.post("/api/image/autorun", params={"id": "windows/jeu"}, json=body)
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]
    for _ in range(100):
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("pending", "running"):
            break
        time.sleep(0.1)
    assert t["status"] == "done", t["log"]
    assert image.with_name("jeu.wsquashfs.old").exists()

    # .old présent : nouvelle édition refusée tant qu'on n'a pas validé
    a = client.get("/api/image/autorun", params={"id": "windows/jeu"}).json()
    assert a["lines"][-1] == "HIDRAW=1"
    body = {**body, "lines": a["lines"][:-1], "base": a["base"]}
    assert client.post("/api/image/autorun", params={"id": "windows/jeu"},
                       json=body).status_code == 409
    assert client.post("/api/image/old/validate", params={"id": "windows/jeu"}).status_code == 200
    assert not image.with_name("jeu.wsquashfs.old").exists()
    # image relue entre-temps ? la base envoyée ne correspond plus
    stale = {**body, "base": {"size": 1, "mtime": 0}}
    r = client.post("/api/image/autorun", params={"id": "windows/jeu"}, json=stale)
    assert r.status_code == 409 and "changé" in r.json()["detail"]
