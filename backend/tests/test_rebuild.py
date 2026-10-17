"""Tests de la reconstruction (SPEC § 3.1, § 3.4), avec les vrais outils.

Ignorés si squashfs-tools manque ; le mode overlay demande en plus
squashfuse, fuse-overlayfs et /dev/fuse.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.services import rebuild
from app.services.rebuild import Changes, Context, RebuildError
from app.services.scan import list_files, unsquashfs_cat

pytestmark = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
    reason="squashfs-tools absent",
)

needs_fuse = pytest.mark.skipif(
    not (shutil.which("squashfuse") and shutil.which("fuse-overlayfs")
         and Path("/dev/fuse").exists()),
    reason="FUSE indisponible",
)

AUTORUN = b"REM jeu\r\nDIR=Game\r\nCMD=game.exe\r\n"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_dir=tmp_path / "tmp", roms_dirs=[str(tmp_path / "roms")])


@pytest.fixture
def image(tmp_path):
    src = tmp_path / "src"
    (src / "Game" / "data").mkdir(parents=True)
    (src / "autorun.cmd").write_bytes(AUTORUN)
    (src / "Game" / "game.exe").write_bytes(b"MZ" + b"\0" * 1000)
    (src / "Game" / "data" / "a.pak").write_bytes(b"a" * 5000)
    (src / "Game" / "Mon Fichier.txt").write_text("espaces")
    out = tmp_path / "roms" / "windows" / "jeu.wsquashfs"
    out.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(out), "-comp", "zstd", "-noappend", "-quiet"],
                   check=True, capture_output=True)
    return out


def _new_autorun():
    return Changes(files={"autorun.cmd": b"REM jeu\r\nDIR=Game\r\nCMD=game.exe\r\nHIDRAW=1\r\n"})


def _check_result(settings, image, before):
    assert unsquashfs_cat(settings, image, "autorun.cmd").endswith(b"HIDRAW=1\r\n")
    assert set(list_files(settings, image)) == set(before)
    old = rebuild.old_path(image)
    assert unsquashfs_cat(settings, old, "autorun.cmd") == AUTORUN
    assert not rebuild.part_path(image).exists()
    assert list(settings.tmp_dir.iterdir()) == []       # dossier de travail nettoyé


@needs_fuse
def test_rebuild_overlay(settings, image):
    before = list_files(settings, image)
    logs = []
    progress = []
    rebuild.rebuild(settings, image, _new_autorun(),
                    Context(log=logs.append, progress=progress.append))
    _check_result(settings, image, before)
    assert any("overlay" in m for m in logs)
    assert progress[-1] == 1.0
    assert not any(p.is_mount() for p in settings.tmp_dir.rglob("*"))


def test_rebuild_extract_fallback(settings, image, monkeypatch):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    before = list_files(settings, image)
    logs = []
    rebuild.rebuild(settings, image, _new_autorun(), Context(log=logs.append))
    _check_result(settings, image, before)
    assert any("extraction" in m for m in logs)


@pytest.mark.parametrize("fuse", [pytest.param(True, marks=needs_fuse), False])
def test_rebuild_delete_and_add(settings, image, monkeypatch, fuse):
    if not fuse:
        monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    changes = Changes(files={"Game/new/patch.dll": b"patch"}, delete=["Game/data"])
    rebuild.rebuild(settings, image, changes)
    files = set(list_files(settings, image))
    assert "Game/new/patch.dll" in files and "Game/new" in files
    assert not any(f.startswith("Game/data") for f in files)
    assert "Game/Mon Fichier.txt" in files


def test_keeps_file_mode(settings, image, monkeypatch, tmp_path):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    rebuild.rebuild(settings, image, Changes(files={"Game/game.exe": b"MZ2"}))
    out = subprocess.run(["unsquashfs", "-lln", str(image), "Game/game.exe"],
                         capture_output=True, text=True, check=True).stdout
    assert "-rw-r--r--" in out


def test_refuses_when_old_exists(settings, image):
    rebuild.old_path(image).write_bytes(b"x")
    with pytest.raises(RebuildError, match=".old"):
        rebuild.rebuild(settings, image, _new_autorun())


def test_refuses_when_in_use(settings, image):
    data = image.read_bytes()
    with pytest.raises(RebuildError, match="en cours"):
        rebuild.rebuild(settings, image, _new_autorun(), in_use=lambda: True)
    assert image.read_bytes() == data


def test_game_launched_during_build_keeps_image(settings, image, monkeypatch):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    data = image.read_bytes()
    calls = iter([False, True])          # libre au début, lancé avant l'échange
    with pytest.raises(RebuildError, match="pendant"):
        rebuild.rebuild(settings, image, _new_autorun(), in_use=lambda: next(calls))
    assert image.read_bytes() == data
    assert not rebuild.old_path(image).exists()
    assert not rebuild.part_path(image).exists()


def test_cancel_leaves_image_untouched(settings, image, monkeypatch):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    data = image.read_bytes()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rebuild.Cancelled):
        rebuild.rebuild(settings, image, _new_autorun(), Context(cancel=cancel))
    assert image.read_bytes() == data
    assert not rebuild.part_path(image).exists()
    assert not rebuild.old_path(image).exists()


def test_stale_part_is_removed(settings, image, monkeypatch):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    rebuild.part_path(image).write_bytes(b"reste d'une tache interrompue")
    rebuild.rebuild(settings, image, _new_autorun())
    assert not rebuild.part_path(image).exists()


def test_verify_detects_unexpected_content(settings, image):
    expected = set(list_files(settings, image)) | {"manquant.txt"}
    with pytest.raises(RebuildError, match="manquant"):
        rebuild.verify(settings, image, expected, Changes(), Context())


def test_expected_listing():
    before = ["autorun.cmd", "Game", "Game/data", "Game/data/a.pak", "Game/database"]
    changes = Changes(files={"Game/new/x.dll": b""}, delete=["Game/data"])
    assert rebuild.expected_listing(before, changes) == {
        "autorun.cmd", "Game", "Game/database", "Game/new", "Game/new/x.dll",
    }


def test_validate_and_revert(settings, image, monkeypatch):
    monkeypatch.setattr(rebuild, "_fuse_available", lambda s: False)
    original = image.read_bytes()
    rebuild.rebuild(settings, image, _new_autorun())
    with pytest.raises(RebuildError, match="en cours"):
        rebuild.revert_old(image, in_use=True)
    rebuild.revert_old(image, in_use=False)
    assert image.read_bytes() == original
    assert not rebuild.old_path(image).exists()

    rebuild.rebuild(settings, image, _new_autorun())
    rebuild.validate_old(image)
    assert not rebuild.old_path(image).exists()
    assert unsquashfs_cat(settings, image, "autorun.cmd").endswith(b"HIDRAW=1\r\n")
