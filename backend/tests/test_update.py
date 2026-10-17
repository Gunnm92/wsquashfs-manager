"""Montée de version : différence source/image, reconstruction, sauvegardes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services import scan
from app.services.rebuild import Job, RebuildError
from app.services.update import default_anchor, plan_update, run_update

pytestmark = pytest.mark.skipif(not shutil.which("mksquashfs"), reason="squashfs-tools absent")


def _cat(image: Path, member: str) -> bytes:
    return subprocess.run(["unsquashfs", "-cat", str(image), member], capture_output=True,
                          check=True).stdout


def _listing(image: Path) -> set[str]:
    out = subprocess.run(["unsquashfs", "-l", str(image)], capture_output=True,
                         check=True).stdout.decode()
    return {ln[len("squashfs-root/"):] for ln in out.splitlines() if ln.startswith("squashfs-root/")}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    game = src / "drive_c" / "Game"
    (game / "data").mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ v1")
    (game / "data" / "level1.pak").write_bytes(b"A" * 100)
    (game / "data" / "old.pak").write_bytes(b"O" * 10)
    (game / "same.dll").write_bytes(b"S" * 50)
    (src / "system.reg").write_text("WINE REGISTRY Version 2\n")
    (src / "autorun.cmd").write_bytes(b"DIR=drive_c/Game\r\nCMD=Game.exe\r\nGAME_VERSION=1.0\r\n")
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(image), "-noappend", "-quiet"], check=True)

    patch = tmp_path / "patch"
    (patch / "DATA").mkdir(parents=True)                      # casse différente de l'image
    (patch / "Game.exe").write_bytes(b"MZ v2 plus long")
    (patch / "DATA" / "level2.pak").write_bytes(b"B" * 200)
    shutil.copy2(game / "same.dll", patch / "same.dll")        # même taille, même date
    (patch / "data_unused").mkdir()

    monkeypatch.setattr(scan, "_CACHE_DIR", tmp_path / "cache")
    settings = Settings(rebuild_mode="extract", saves_dir=tmp_path / "saves")
    return settings, image, patch, game


def test_default_anchor_from_dir(env):
    settings, image, *_ = env
    assert default_anchor(settings, image) == "drive_c/Game"


def test_plan_partial_patch(env):
    settings, image, patch, _ = env
    plan = plan_update(settings, image, patch, "DRIVE_C/game", version="2.0")
    assert plan.anchor == "drive_c/Game"                          # casse de l'image
    assert [a["path"] for a in plan.added] == ["drive_c/Game/data/level2.pak"]
    assert [r["path"] for r in plan.replaced] == ["drive_c/Game/Game.exe"]
    assert plan.same == 1 and plan.deleted == []
    assert plan.version_before == "1.0" and plan.version == "2.0"
    assert b"GAME_VERSION=2.0" in plan.changes.write["autorun.cmd"]


def test_plan_full_build_deletes_missing_but_protects(env):
    settings, image, patch, _ = env
    plan = plan_update(settings, image, patch, "drive_c/Game", delete_missing=True, version="2.0")
    assert plan.deleted == ["drive_c/Game/data/level1.pak", "drive_c/Game/data/old.pak"]
    with pytest.raises(RebuildError, match="racine d'un prefix"):
        plan_update(settings, image, patch, "", delete_missing=True)
    with pytest.raises(RebuildError, match="n'existe pas"):
        plan_update(settings, image, patch, "drive_c/Nope")


def test_run_update_rebuilds_and_sets_aside_masked_saves(env):
    settings, image, patch, _ = env
    saves = settings.saves_dir / "Jeu"
    (saves / "drive_c" / "Game").mkdir(parents=True)
    (saves / "drive_c" / "Game" / "Game.exe").write_bytes(b"ancienne copie")    # masquerait
    (saves / "drive_c" / "Game" / "save1.sav").write_bytes(b"partie")           # vraie sauvegarde
    task = Task(id="u", kind="update", image="win/Jeu", image_path=str(image),
                params={"source": str(patch), "anchor": "drive_c/Game", "version": "2.0",
                        "clean_saves": True})
    message = run_update(settings, task, Job())
    assert "GAME_VERSION=2.0" in message and "mis de côté" in message
    assert _cat(image, "drive_c/Game/Game.exe") == b"MZ v2 plus long"
    assert _cat(image, "drive_c/Game/data/level2.pak") == b"B" * 200
    assert "drive_c/Game/data/old.pak" in _listing(image)             # patch : rien supprimé
    assert b"GAME_VERSION=2.0" in _cat(image, "autorun.cmd")
    assert not (saves / "drive_c/Game/Game.exe").exists()
    assert (saves / "drive_c/Game/save1.sav").read_bytes() == b"partie"
    aside = next(settings.saves_dir.glob("Jeu.avant-maj-*"))
    assert (aside / "drive_c/Game/Game.exe").read_bytes() == b"ancienne copie"
    # Rejouée sur l'image à jour : plus rien à faire
    image.with_name("Jeu.wsquashfs.old").unlink()
    assert run_update(settings, task, Job()).startswith("Rien à mettre à jour")
