"""Tests de la création d'images (SPEC § 2.3) et des réglages."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings
from app.services import create
from app.services.rebuild import RebuildError
from app.services.tasks import TaskQueue

RULES = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"

needs_tools = pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
                                 reason="squashfs-tools absent")


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _first(analysis):
    return analysis["candidates"][0]["path"]


def test_unreal_binaries_first_then_root_launcher(tmp_path):
    folder = _tree(tmp_path / "Dispatch", {
        "Dispatch.exe": b"MZ" * 100,
        "Engine/Binaries/ThirdParty/x.dll": b"",
        "Engine/Binaries/Win64/EpicWebHelper.exe": b"MZ" * 9000,
        "start_protected_game.exe": b"MZ" * 9000,
        "_Redist/QuickSFV.EXE": b"MZ",
        "Engine/Extras/Redist/en-us/UE4PrereqSetup_x64.exe": b"MZ" * 9000,
        "Dispatch/Binaries/Win64/Dispatch-Win64-Shipping.exe": b"MZ" * 5000,
    })
    a = create.analyze(folder, RULES, tmp_path / "saves")
    paths = [c["path"] for c in a["candidates"]]
    assert paths == ["Dispatch/Binaries/Win64/Dispatch-Win64-Shipping.exe", "Dispatch.exe"]
    assert a["autorun"]["lines"] == ["DIR=Dispatch/Binaries/Win64",
                                     "CMD=Dispatch-Win64-Shipping.exe"]
    assert a["type"] == "none" and a["issues"] == []


def test_unity_and_crash_handler(tmp_path):
    folder = _tree(tmp_path / "Mon Jeu", {
        "Game/Mon Jeu.exe": b"MZ",
        "Game/UnityPlayer.dll": b"",
        "Game/UnityCrashHandler64.exe": b"MZ" * 1000,
        "Game/Mon Jeu_Data/x": b"",
    })
    a = create.analyze(folder, RULES, tmp_path / "saves")
    assert [c["path"] for c in a["candidates"]] == ["Game/Mon Jeu.exe"]
    assert a["autorun"]["lines"] == ["DIR=Game", 'CMD="Mon Jeu.exe"']


def test_batocera_prefix_win32(tmp_path):
    folder = _tree(tmp_path / "Vieux.wine", {
        "system.reg": b"WINE REGISTRY Version 2\n#arch=win32\n",
        "user.reg": b"",
        "drive_c/windows/system32/notepad.exe": b"MZ" * 10000,
        "drive_c/Vieux/vieux.exe": b"MZ",
        "drive_c/Vieux/unins000.exe": b"MZ",
    })
    a = create.analyze(folder, RULES, tmp_path / "saves")
    assert a["name"] == "Vieux" and a["type"] == "batocera" and a["arch"] == "win32"
    assert [c["path"] for c in a["candidates"]] == ["drive_c/Vieux/vieux.exe"]
    assert a["autorun"]["lines"] == ["DIR=drive_c/Vieux", "CMD=vieux.exe", "ARCH=win32"]
    assert any(s["key"] == "ARCH" for s in a["suggestions"])


def test_existing_autorun_kept_and_bat_checked(tmp_path):
    folder = _tree(tmp_path / "Jeu", {
        "autorun.cmd": b"REM fait main\r\nCMD=launch.bat\r\n",
        "launch.bat": b"taskill /f /im x.exe\r\njeu.exe\r\n",
        "jeu.exe": b"MZ",
    })
    (tmp_path / "saves" / "Jeu").mkdir(parents=True)
    a = create.analyze(folder, RULES, tmp_path / "saves")
    assert a["has_autorun"] and a["autorun"]["lines"] == ["REM fait main", "CMD=launch.bat"]
    bat = next(c for c in a["candidates"] if c["path"] == "launch.bat")
    assert bat["warnings"] and "taskkill" in bat["warnings"][0]
    assert any("sauvegardes" in w for w in a["warnings"])


@needs_tools
def test_pack_injects_autorun_without_touching_source(tmp_path):
    settings = get_settings().model_copy(update={"tmp_dir": tmp_path / "tmp"})
    folder = _tree(tmp_path / "Jeu", {"autorun.cmd": b"ancien\n", "Game/jeu.exe": b"MZ",
                                      "Game/Avec Espace.txt": b"x"})
    dest = tmp_path / "roms" / "windows" / "Jeu.wsquashfs"
    create.pack(settings, folder, dest, b"DIR=Game\r\nCMD=jeu.exe\r\n")
    out = subprocess.run(["unsquashfs", "-cat", str(dest), "autorun.cmd"], capture_output=True,
                         check=True).stdout
    assert out == b"DIR=Game\r\nCMD=jeu.exe\r\n"
    assert (folder / "autorun.cmd").read_bytes() == b"ancien\n"
    assert not dest.with_name("Jeu.wsquashfs.part").exists()
    with pytest.raises(RebuildError, match="existe déjà"):
        create.pack(settings, folder, dest, b"CMD=x.exe\r\n")


# ------------------------------------------------------------------ API

@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "roms" / "windows").mkdir(parents=True)
    s = main._settings
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    monkeypatch.setattr(s, "roms_dirs", [str(tmp_path / "roms")])
    monkeypatch.setattr(s, "tmp_dir", tmp_path / "tmp")
    monkeypatch.setattr(s, "saves_dir", tmp_path / "saves")
    monkeypatch.setattr(main.scan, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(main, "_queue", TaskQueue(tmp_path / "tasks.json",
                                                  {"pack": main._run_pack}))
    return TestClient(main.app)


def test_config_put_saved_and_validated(client, tmp_path, monkeypatch):
    other = tmp_path / "autres"
    other.mkdir()
    r = client.put("/api/config", json={"roms_dirs": [str(other), str(other)],
                                        "saves_dir": str(tmp_path / "s"),
                                        "tmp_dir": str(tmp_path / "t")})
    assert r.status_code == 200 and r.json()["roms_dirs"] == [str(other)]
    assert main._settings.roms_dirs == [str(other)]
    monkeypatch.setenv("WSQUASHFS_MGR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WSQUASHFS_MGR_ROMS_DIRS", "/ailleurs")
    assert get_settings().roms_dirs == [str(other)]        # le fichier prime
    r = client.put("/api/config", json={"roms_dirs": ["/n/existe/pas"], "saves_dir": "/s",
                                        "tmp_dir": "relatif"})
    assert r.status_code == 400 and "absolu" in r.json()["detail"]


def test_fs_dirs(client, tmp_path):
    _tree(tmp_path / "src" / "Jeu.wine", {"system.reg": b""})
    (tmp_path / "src" / ".cache").mkdir()
    r = client.get("/api/fs/dirs", params={"path": str(tmp_path / "src")}).json()
    assert [d["name"] for d in r["dirs"]] == ["Jeu.wine"] and r["dirs"][0]["prefix"]
    assert r["parent"] == str(tmp_path)


@needs_tools
def test_create_end_to_end(client, tmp_path):
    folder = _tree(tmp_path / "src" / "Nouveau", {"Game/nouveau.exe": b"MZ"})
    a = client.post("/api/create/analyze", json={"folder": str(folder)}).json()
    assert a["destinations"][0]["systems"] == ["windows"]
    body = {"folder": str(folder), "roms_dir": str(tmp_path / "roms"), "system": "windows",
            "name": a["name"], "autorun": a["autorun"]}
    r = client.post("/api/create", json=body)
    assert r.status_code == 200, r.text
    for _ in range(100):
        t = client.get(f"/api/tasks/{r.json()['task']}").json()
        if t["status"] not in ("pending", "running"):
            break
        time.sleep(0.1)
    assert t["status"] == "done", t["log"]
    assert (tmp_path / "roms" / "windows" / "Nouveau.wsquashfs").exists()
    assert client.post("/api/create", json=body).status_code == 409
    bad = {**body, "name": "../evasion"}
    assert client.post("/api/create", json=bad).status_code == 400
    assert client.post("/api/create", json={**body, "roms_dir": "/tmp"}).status_code == 400
