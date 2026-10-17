"""Tests des actions en masse (SPEC § 2.4)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import ImageInfo
from app.services.autorun import Autorun
from app.services.mass import AutorunOp, MassError, apply_ops, render_template, reset_saves
from app.services.tasks import TaskQueue

INFO = ImageInfo(id="windows/jeu", path=Path("/roms/windows/jeu.wsquashfs"), name="jeu",
                 system="windows")


# ------------------------------------------------------------------ opérations

def test_ops_in_order_keep_place_and_format():
    a = Autorun.parse("REM x\r\nHIDRAW=1\r\nCMD=a.exe\r\nWINE=system\r\n".encode("cp1252"))
    new = apply_ops(a, [AutorunOp(op="remove", key="HIDRAW"),
                        AutorunOp(op="set", key="WINE", value="tkg"),
                        AutorunOp(op="set", key="GAME_VERSION", value="1.0")], INFO)
    assert new.lines == ["REM x", "CMD=a.exe", "WINE=tkg", "GAME_VERSION=1.0"]
    assert new.eol == "\r\n"
    assert a.lines[1] == "HIDRAW=1"          # l'original n'est pas modifié


def test_template_variables_and_format():
    a = Autorun.parse(b"DIR=Game\r\nCMD=game.exe -x\r\n")
    new = apply_ops(a, [AutorunOp(op="template",
                                  template="REM {name} ({system})\nDIR={DIR}\nCMD={CMD}\n"
                                           "ENV=A={{b}}\n")], INFO)
    assert new.lines == ["REM jeu (windows)", "DIR=Game", "CMD=game.exe -x", "ENV=A={b}"]
    assert new.encode().endswith(b"\r\n")     # fins de ligne de l'image, pas du formulaire


def test_template_unknown_variable():
    with pytest.raises(MassError, match="GAME_VERSION"):
        apply_ops(Autorun.parse(b"CMD=a.exe\n"),
                  [AutorunOp(op="template", template="V={GAME_VERSION}")], INFO)


def test_render_template_literal_braces():
    assert render_template("{{name}} {name}", {"name": "x"}) == "{name} x"


def test_op_validation():
    with pytest.raises(ValueError):
        AutorunOp(op="set", key="BAD KEY", value="1")
    with pytest.raises(ValueError):
        AutorunOp(op="set", key="CMD", value="a\r\nHIDRAW=1")


def test_reset_saves_renames(tmp_path):
    (tmp_path / "jeu" / "pfx").mkdir(parents=True)
    dest = reset_saves(tmp_path, INFO, now=0)
    assert not (tmp_path / "jeu").exists()
    assert dest.name.startswith("jeu.avant-") and (dest / "pfx").is_dir()
    (tmp_path / "jeu").mkdir()
    dest2 = reset_saves(tmp_path, INFO, now=0)          # même seconde : pas d'écrasement
    assert dest2 != dest and dest.exists()
    assert reset_saves(tmp_path, INFO) is None


# ------------------------------------------------------------------ API

needs_tools = pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
                                 reason="squashfs-tools absent")


@pytest.fixture
def env(tmp_path, monkeypatch):
    roms = tmp_path / "roms" / "windows"
    roms.mkdir(parents=True)
    for name, autorun in {"a": b"DIR=Game\r\nCMD=game.exe\r\nHIDRAW=1\r\n",
                          "b": b"DIR=Game\nCMD=game.exe\n",
                          "c": b"DIR=Game\r\nCMD=game.exe\r\n"}.items():
        src = tmp_path / "src" / name
        (src / "Game").mkdir(parents=True)
        (src / "Game" / "game.exe").write_bytes(b"MZ")
        (src / "autorun.cmd").write_bytes(autorun)
        subprocess.run(["mksquashfs", str(src), str(roms / f"{name}.wsquashfs"), "-noappend",
                        "-quiet"], check=True)
    monkeypatch.setattr(main._settings, "roms_dirs", [str(tmp_path / "roms")])
    monkeypatch.setattr(main._settings, "tmp_dir", tmp_path / "tmp")
    monkeypatch.setattr(main._settings, "saves_dir", tmp_path / "saves")
    monkeypatch.setattr(main.scan, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(main, "_queue", TaskQueue(
        tmp_path / "tasks.json",
        {"edit_autorun": main._run_edit_autorun, "verify": main._run_verify}))
    return TestClient(main.app), roms


def _wait(client, task_id):
    for _ in range(100):
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("pending", "running"):
            return t
        time.sleep(0.1)
    raise AssertionError("tâche trop longue")


@needs_tools
def test_mass_autorun_preview_then_run(env):
    client, roms = env
    roms.joinpath("c.wsquashfs.old").write_bytes(b"x")       # c bloquée
    ids = ["windows/a", "windows/b", "windows/c"]
    ops = [{"op": "remove", "key": "HIDRAW"}, {"op": "set", "key": "GAME_VERSION",
                                                 "value": "1.0"}]
    p = client.post("/api/mass/autorun?dry_run=true", json={"ids": ids, "ops": ops}).json()
    status = {i["id"]: i["status"] for i in p["images"]}
    assert status == {"windows/a": "change", "windows/b": "change", "windows/c": "blocked"}
    assert p["space"][0]["images"] == 2 and p["space"][0]["ok"]
    assert "-HIDRAW=1" in next(i for i in p["images"] if i["id"] == "windows/a")["diff"]

    bases = {i["id"]: i["base"] for i in p["images"]}
    del bases["windows/b"]                                   # pas dans l'aperçu
    r = client.post("/api/mass/autorun", json={"ids": ids, "ops": ops, "bases": bases}).json()
    assert len(r["tasks"]) == 1
    assert [s["id"] for s in r["skipped"]] == ["windows/b"]
    assert _wait(client, r["tasks"][0])["status"] == "done"
    out = subprocess.run(["unsquashfs", "-cat", str(roms / "a.wsquashfs"), "autorun.cmd"],
                         capture_output=True, check=True).stdout
    assert out == b"DIR=Game\r\nCMD=game.exe\r\nGAME_VERSION=1.0\r\n"

    res = client.post("/api/mass/old/validate", json={"ids": ["windows/a", "windows/b"]}).json()
    assert [x["ok"] for x in res] == [True, False]
    assert not roms.joinpath("a.wsquashfs.old").exists()


@needs_tools
def test_mass_template_error_is_per_image(env):
    client, _ = env
    p = client.post("/api/mass/autorun?dry_run=true", json={
        "ids": ["windows/a", "windows/b"],
        "ops": [{"op": "template", "template": "DIR={DIR}\nCMD={CMD}\nHIDRAW={HIDRAW}\n"}],
    }).json()
    status = {i["id"]: i["status"] for i in p["images"]}
    assert status == {"windows/a": "unchanged", "windows/b": "error"}


@needs_tools
def test_mass_verify(env):
    client, roms = env
    roms.joinpath("b.wsquashfs").write_bytes(b"pas une image")
    t = _wait(client, client.post("/api/mass/verify",
                                  json={"ids": ["windows/a", "windows/b"]}).json()["task"])
    assert t["status"] == "failed"
    assert t["log"][0].startswith("✓ windows/a")
    assert t["log"][1].startswith("✗ windows/b")


@needs_tools
def test_mass_reset_saves(env, tmp_path):
    client, _ = env
    (tmp_path / "saves" / "a" / "pfx").mkdir(parents=True)
    ids = {"ids": ["windows/a", "windows/b"]}
    p = client.post("/api/mass/saves/reset?dry_run=true", json=ids).json()
    assert [x["ok"] for x in p] == [True, False]
    assert (tmp_path / "saves" / "a").exists()
    client.post("/api/mass/saves/reset", json=ids)
    assert not (tmp_path / "saves" / "a").exists()
    assert len(list((tmp_path / "saves").glob("a.avant-*"))) == 1


def test_unknown_image_is_404(env):
    client, _ = env
    r = client.post("/api/mass/verify", json={"ids": ["windows/zzz"]})
    assert r.status_code == 404
