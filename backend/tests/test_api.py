"""Tests de l'API : authentification, identifiants, édition d'autorun,
actions en masse, et un parcours complet sur une vraie image."""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import ImageInfo, TaskStatus
from app.services.tasks import TaskQueue

AUTORUN = b"REM jeu\r\nCMD=game.exe\r\nDIR=Game\r\nHIDRAW=1\r\n"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = TaskQueue(tmp_path / "state" / "tasks.json", main.queue.handlers)
    monkeypatch.setattr(main, "queue", q)
    return q


@pytest.fixture
def client(monkeypatch, queue, tmp_path):
    images = [
        ImageInfo(id="arcade/jeu", path=tmp_path / "arcade/jeu.wsquashfs", name="jeu",
                  system="arcade", size=1000),
        ImageInfo(id="windows/jeu", path=tmp_path / "windows/jeu.wsquashfs", name="jeu",
                  system="windows", size=2000),
    ]
    for info in images:
        info.path.parent.mkdir(parents=True)
        info.path.write_bytes(b"img")
    monkeypatch.setattr(main.scan, "scan_all", lambda settings, force=False: images)
    monkeypatch.setattr(main.scan, "_list_files",
                        lambda settings, image: ["Game", "Game/game.exe"])
    monkeypatch.setattr(main.scan, "read_autorun_bytes", lambda settings, image: AUTORUN)
    monkeypatch.setattr(main.scan, "image_in_use", lambda image: False)
    main._library.clear()
    return TestClient(main.app)


def _edit(lines, **extra):
    return {"lines": lines, "eol": "\r\n", "trailing_eol": True, **extra}


# ------------------------------------------------------------------ auth

def test_bad_token_gives_401_not_500(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    assert client.get("/api/config", headers={"x-wsfs-token": "faux"}).status_code == 401
    assert client.get("/api/config").status_code == 401
    assert client.post("/login", json={"password": "faux"}).status_code == 401


def test_good_token_passes_and_is_not_the_password(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    token = client.post("/login", json={"password": "sésame"}).json()["token"]
    assert token and "sésame" not in token
    assert client.get("/api/config", headers={"x-wsfs-token": token}).status_code == 200


# ------------------------------------------------------------------ images

def test_same_name_in_two_systems(client):
    response = client.get("/api/image", params={"id": "windows/jeu"})
    assert response.status_code == 200
    assert response.json()["system"] == "windows"
    assert client.get("/api/image", params={"id": "jeu"}).status_code == 404


def test_files_search(client):
    data = client.get("/api/image/files", params={"id": "arcade/jeu", "q": "EXE"}).json()
    assert data == {"total": 2, "matches": 1, "files": ["Game/game.exe"]}


# ------------------------------------------------------------------ autorun

def test_autorun_read_gives_sha_and_encoding(client):
    data = client.get("/api/image/autorun", params={"id": "arcade/jeu"}).json()
    assert data["found"] and data["encoding"] == "utf-8" and data["eol"] == "\r\n"
    assert data["sha"] and data["lines"][1] == "CMD=game.exe"


def test_autorun_edit_rejects_bad_eol_and_multiline(client):
    assert client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                       json={"lines": ["CMD=a.exe"], "eol": "\r"}).status_code == 422
    assert client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                       json=_edit(["CMD=a\nDIR=b"])).status_code == 422


def test_autorun_preview_validates_without_creating_task(client, queue):
    response = client.post("/api/image/autorun/preview", params={"id": "arcade/jeu"}, json={
        "lines": ["DIR=Game", "CMD=game.exe"], "eol": "\n", "trailing_eol": False,
    })
    assert response.status_code == 200
    assert response.json()["text"] == "DIR=Game\nCMD=game.exe"
    assert response.json()["issues"] == []
    assert queue.list() == []


def test_autorun_edit_creates_one_task_per_image(client, queue):
    lines = ["REM jeu", "CMD=game.exe", "DIR=Game"]
    first = client.post("/api/image/autorun", params={"id": "arcade/jeu"}, json=_edit(lines))
    assert first.status_code == 200
    task = queue.get(first.json()["task"])
    assert task.status == TaskStatus.PENDING and task.kind == "autorun"
    assert task.params["text"] == "REM jeu\r\nCMD=game.exe\r\nDIR=Game\r\n"
    again = client.post("/api/image/autorun", params={"id": "arcade/jeu"}, json=_edit(lines))
    assert again.status_code == 409


def test_autorun_edit_refuses_unchanged_stale_or_old(client):
    same = _edit(["REM jeu", "CMD=game.exe", "DIR=Game", "HIDRAW=1"])
    assert client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                       json=same).json()["detail"] == "aucun changement"
    stale = _edit(["CMD=x.exe"], base_sha="0" * 64)
    response = client.post("/api/image/autorun", params={"id": "arcade/jeu"}, json=stale)
    assert response.status_code == 409 and "changé" in response.json()["detail"]
    main._find_image("arcade/jeu").path.with_suffix(".wsquashfs.old").write_bytes(b"")
    response = client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                           json=_edit(["CMD=x.exe"]))
    assert response.status_code == 409 and ".old" in response.json()["detail"]


# ------------------------------------------------------------------ masse

def test_mass_preview_and_submit(client, queue):
    body = {"ids": ["arcade/jeu", "windows/jeu", "nope/x"],
            "ops": [{"op": "remove", "key": "HIDRAW"}]}
    main._find_image("windows/jeu").path.with_suffix(".wsquashfs.old").write_bytes(b"")
    preview = client.post("/api/mass/autorun/preview", json=body).json()
    by_id = {item["id"]: item for item in preview["items"]}
    assert by_id["arcade/jeu"]["changed"] and by_id["arcade/jeu"]["blocked"] is None
    assert by_id["arcade/jeu"]["after"] == "REM jeu\r\nCMD=game.exe\r\nDIR=Game\r\n"
    assert ".old" in by_id["windows/jeu"]["blocked"]
    assert by_id["nope/x"]["error"] == "image inconnue"
    assert preview["ready"] == 1
    assert preview["space"][0]["needed"] == 1000

    result = client.post("/api/mass/autorun", json=body).json()
    assert len(result["tasks"]) == 1
    assert {s["id"] for s in result["skipped"]} == {"windows/jeu", "nope/x"}
    assert queue.get(result["tasks"][0]).title == "−HIDRAW"


def test_mass_ops_are_validated(client):
    bad = [{"op": "set", "key": "A B", "value": "1"}, {"op": "set", "key": "A"},
           {"op": "replace", "template": "  "}, {"op": "drop", "key": "A"}]
    for op in bad:
        response = client.post("/api/mass/autorun/preview",
                               json={"ids": ["arcade/jeu"], "ops": [op]})
        assert response.status_code == 422, op


# ------------------------------------------------------------------ parcours réel

@pytest.mark.skipif(not shutil.which("mksquashfs"), reason="squashfs-tools absent")
def test_end_to_end_edit_rebuild_validate(tmp_path, monkeypatch, queue):
    src = tmp_path / "src"
    (src / "Game").mkdir(parents=True)
    (src / "Game" / "game.exe").write_bytes(b"MZ")
    (src / "autorun.cmd").write_bytes(AUTORUN)
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(image), "-comp", "zstd", "-noappend", "-quiet"],
                   check=True)
    monkeypatch.setattr(main.scan, "_CACHE_DIR", tmp_path / "cache")
    for key, value in {"roms_dirs": [str(tmp_path / "roms")], "tmp_dir": tmp_path / "tmp",
                       "rebuild_mode": "extract", "saves_dir": tmp_path / "saves"}.items():
        monkeypatch.setattr(main._settings, key, value)
    main._library.clear()
    client = TestClient(main.app)

    library = client.get("/api/library").json()
    assert [(i["id"], i["hidraw"]) for i in library] == [("win/Jeu", True)]
    sha = client.get("/api/image/autorun", params={"id": "win/Jeu"}).json()["sha"]
    response = client.post("/api/image/autorun", params={"id": "win/Jeu"},
                           json=_edit(["REM jeu", "CMD=game.exe", "DIR=Game"], base_sha=sha))
    assert response.status_code == 200, response.text
    assert response.json()["issues"] == []

    queue.start()
    try:
        task_id = response.json()["task"]
        deadline = time.monotonic() + 60
        while queue.get(task_id).status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        queue.stop()
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "done", task

    info = client.get("/api/library").json()[0]
    assert info["hidraw"] is None and info["has_old"] and info["state"] == "has_old"
    assert client.get("/api/tasks", params={"image": "win/Jeu"}).json()[0]["id"] == task_id

    info = client.post("/api/image/old/validate", params={"id": "win/Jeu"}).json()
    assert not info["has_old"]
    assert not image.with_name("Jeu.wsquashfs.old").exists()


# ------------------------------------------------------------------ fichiers, sauvegardes

def test_mass_files_input_validation(client):
    for body in ({"ids": ["arcade/jeu"]},
                 {"ids": ["arcade/jeu"], "copy": [{"dest": "a.dll", "source": "relatif.dll"}]},
                 {"ids": ["arcade/jeu"], "reg": [{"key": "K", "name": "n\nx", "value": "v"}]}):
        assert client.post("/api/mass/files/preview", json=body).status_code == 422, body


def test_mass_verify_is_not_blocked_by_old(client, queue):
    main._find_image("arcade/jeu").path.with_suffix(".wsquashfs.old").write_bytes(b"")
    result = client.post("/api/mass/verify", json={"ids": ["arcade/jeu"]}).json()
    assert len(result["tasks"]) == 1 and queue.get(result["tasks"][0]).kind == "verify"


def test_saves_reset(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main._settings, "saves_dir", tmp_path / "saves")
    assert client.post("/api/image/saves/reset", params={"id": "arcade/jeu"}).status_code == 409
    (tmp_path / "saves" / "jeu").mkdir(parents=True)
    response = client.post("/api/image/saves/reset", params={"id": "arcade/jeu"})
    assert response.status_code == 200 and response.json()["renamed"].startswith("jeu.avant-")


# ------------------------------------------------------------------ création

@pytest.fixture
def folders_client(tmp_path, monkeypatch, queue):
    win = tmp_path / "roms" / "win"
    (win / "Jeu.pc" / "bin").mkdir(parents=True)
    (win / "Jeu.pc" / "bin" / "Jeu.exe").write_bytes(b"MZ")
    (win / "Vide.pc").mkdir()
    (win / "Fait.pc").mkdir()
    (win / "Fait.wsquashfs").write_bytes(b"")
    monkeypatch.setattr(main._settings, "roms_dirs", [str(win)])
    main._analyses.clear()
    return TestClient(main.app)


def test_folders_list_and_analyze(folders_client):
    folders = {f["id"]: f for f in folders_client.get("/api/folders").json()}
    assert set(folders) == {"win/Jeu.pc", "win/Vide.pc", "win/Fait.pc"}
    assert folders["win/Fait.pc"]["image_exists"]
    r = folders_client.get("/api/folders/analyze", params={"id": "win/Jeu.pc"}).json()
    assert r["generated"]["text"] == "DIR=bin\nCMD=Jeu.exe\n" and r["recommended"] == "generated"
    assert r["existing"] is None and r["image_id"] == "win/Jeu"


def test_folders_pack_creates_tasks_and_skips(folders_client, queue):
    body = {"items": [{"id": "win/Jeu.pc", "autorun": "DIR=bin\nCMD=Jeu.exe\n"},
                      {"id": "win/Vide.pc"}, {"id": "win/Fait.pc", "autorun": "CMD=a.exe"},
                      {"id": "win/Nope.pc", "autorun": "CMD=a.exe"}]}
    result = folders_client.post("/api/folders/pack", json=body).json()
    assert len(result["tasks"]) == 1
    task = queue.get(result["tasks"][0])
    assert task.kind == "pack" and task.image == "win/Jeu"
    assert task.params["autorun"] == "DIR=bin\r\nCMD=Jeu.exe\r\n" and task.params["rename_wine"]
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "pas d'autorun" in reasons["win/Vide.pc"] and "existe déjà" in reasons["win/Fait.pc"]
    bad = folders_client.post("/api/folders/pack", json={"items": [{"id": "win/Jeu.pc",
                                                                   "autorun": "DIR=bin"}]})
    assert bad.status_code == 422


def test_folders_delete_requires_readable_image(folders_client, tmp_path):
    result = folders_client.post("/api/folders/delete", json={"ids": ["win/Fait.pc"]}).json()
    assert result["done"] == [] and "illisible" in result["skipped"][0]["reason"]
    assert (tmp_path / "roms" / "win" / "Fait.pc").is_dir()


def test_folders_pack_after_rename_explains(folders_client, queue, tmp_path):
    win = tmp_path / "roms" / "win"
    (win / "Jeu.pc").rename(win / "Jeu.wine")
    body = {"items": [{"id": "win/Jeu.pc", "autorun": "DIR=bin\nCMD=Jeu.exe\n"}]}
    first = folders_client.post("/api/folders/pack", json=body).json()
    assert len(first["tasks"]) == 1          # retrouvé sous son nouveau nom
    again = folders_client.post("/api/folders/pack", json=body).json()
    assert again["tasks"] == [] and "déjà en attente" in again["skipped"][0]["reason"]


def test_interface_choice_beats_environment(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setenv("WSQUASHFS_MGR_ROMS_DIRS", "/roms")
    monkeypatch.setenv("WSQUASHFS_MGR_RUNTIME_CONFIG_FILE", str(tmp_path / "config.json"))
    first = config.get_settings()
    assert first.roms_dirs == ["/roms"] and first.roms_roots == ["/roms"]
    config.save_roms_dirs(first, ["/roms/win"])
    again = config.get_settings()
    assert again.roms_dirs == ["/roms/win"] and again.roms_roots == ["/roms"]


def test_config_systems_lists_game_dirs(client, monkeypatch, tmp_path):
    roms = tmp_path / "base"
    (roms / "win" / "Jeu.pc").mkdir(parents=True)
    (roms / "win" / "A.wsquashfs").write_bytes(b"")
    (roms / "snes").mkdir()
    (roms / "snes" / "mario.sfc").write_bytes(b"")
    monkeypatch.setattr(main._settings, "roms_roots", [str(roms)])
    assert client.get("/api/config/systems").json() == [
        {"path": str(roms / "win"), "name": "win", "root": str(roms), "images": 1, "folders": 1}]


def test_night_window(client, monkeypatch, tmp_path, queue):
    from datetime import datetime

    from app import config
    monkeypatch.setattr(main._settings, "runtime_config_file", tmp_path / "cfg.json")
    monkeypatch.setattr(main._settings, "night_start", main._settings.night_start)
    monkeypatch.setattr(main._settings, "night_end", main._settings.night_end)
    s = main._settings
    for start, end, hour, expected in [("01:00", "07:00", "03:30", True), ("01:00", "07:00", "07:00", False),
                                       ("22:00", "06:00", "23:15", True), ("22:00", "06:00", "05:59", True),
                                       ("22:00", "06:00", "12:00", False)]:
        s.night_start, s.night_end = start, end
        assert config.in_night(s, datetime.fromisoformat(f"2026-10-04T{hour}")) is expected, (start, end, hour)
    assert client.put("/api/config/night", json={"start": "25:00", "end": "06:00"}).status_code == 422
    cfg = client.put("/api/config/night", json={"start": "23:30", "end": "05:00"}).json()
    assert (cfg["night_start"], cfg["night_end"]) == ("23:30", "05:00")
    # Une action programmée « cette nuit » crée une tâche de nuit
    body = {"ids": ["arcade/jeu"], "ops": [{"op": "remove", "key": "HIDRAW"}], "when": "night"}
    task = queue.get(client.post("/api/mass/autorun", json=body).json()["tasks"][0])
    assert task.night is True
    assert client.post(f"/api/tasks/{task.id}/now").json()["night"] is False


def test_mass_override_preview_has_no_space(client, monkeypatch):
    monkeypatch.setattr(main, "effective_autorun",
                        lambda settings, image: main.Autorun.parse("CMD=game.exe\r\n"))
    body = {"ids": ["arcade/jeu"], "ops": [{"op": "set", "key": "HIDRAW", "value": "1"}],
            "target": "override"}
    data = client.post("/api/mass/autorun/preview", json=body).json()
    assert data["space"] == [] and data["ready"] == 1
    assert data["items"][0]["after"] == "CMD=game.exe\r\nHIDRAW=1\r\n"


def test_knowledge_add_goes_to_local_base(client, monkeypatch, tmp_path):
    from app.services import analyze
    monkeypatch.setattr(analyze, "LOCAL_GAMES_FILE", tmp_path / "games.yaml")
    bad = client.post("/api/knowledge", json={"note": "rien"})
    assert bad.status_code == 422
    ok = client.post("/api/knowledge", json={"game": "Mon Jeu", "keys": {"DXVK": "0"},
                                             "prefer": "bin/jeu.exe", "note": "testé"})
    assert ok.status_code == 200 and ok.json()["keys"] == {"DXVK": "0"}
    entries = client.get("/api/knowledge").json()
    assert entries[0]["game"] == "Mon Jeu" and entries[0]["_source"] == "locale"
    assert any(e.get("exe") == "Astebreed.exe" for e in entries)        # base livrée ensuite


def test_local_knowledge_prefers_exe(tmp_path, monkeypatch):
    from app.services import analyze
    monkeypatch.setattr(analyze, "LOCAL_GAMES_FILE", tmp_path / "games.yaml")
    root = tmp_path / "Mon Jeu.pc"
    for rel, size in {"MonJeu.exe": 50_000_000, "tools/Start.exe": 100_000}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"\0" * size)
    assert analyze.report(analyze.analyze_folder(root, "Mon Jeu"))["chosen"] == "MonJeu.exe"
    analyze.add_local_game({"game": "mon jeu", "prefer": "start.exe", "note": "lanceur requis"})
    r = analyze.report(analyze.analyze_folder(root, "Mon Jeu"))
    assert r["chosen"] == "tools/Start.exe"
    assert "lanceur requis" in r["candidates"][0]["reasons"][0]


def test_update_endpoints_validate_input(client):
    assert client.post("/api/image/update/preview", params={"id": "arcade/jeu"},
                       json={"source": "relatif"}).status_code == 422
    r = client.post("/api/image/update/preview", params={"id": "arcade/jeu"},
                    json={"source": "/nexiste/pas"})
    assert r.status_code == 409 and "introuvable" in r.json()["detail"]
