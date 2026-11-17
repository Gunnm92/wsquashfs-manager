"""Illustration ajoutée sans reconstruction (mksquashfs en ajout)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services.operations import run_cover
from app.services.rebuild import COVER, Job, RebuildError

needs_tools = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
    reason="squashfs-tools absent",
)

PNG = b"\x89PNG\r\n\x1a\n" + b"jaquette" * 100


def _image(tmp_path: Path, extra: dict[str, bytes] | None = None, xattr: bool = True) -> Path:
    src = tmp_path / "src"
    (src / "Game").mkdir(parents=True)
    (src / "Game/game.exe").write_bytes(b"MZ" * 5000)
    (src / "autorun.cmd").write_bytes(b"CMD=game.exe\r\nDIR=Game\r\n")
    for rel, data in (extra or {}).items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    if xattr:
        try:
            os.setxattr(src / "Game/game.exe", "user.DOSATTRIB", b"0x20")
        except OSError:
            pass
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(image), "-comp", "zstd", "-noappend", "-quiet",
                    "-no-progress"], check=True, capture_output=True)
    return image


def _es_cover(image: Path) -> None:
    (image.parent / "covers").mkdir()
    (image.parent / "covers/Jeu.png").write_bytes(PNG)
    (image.parent / "gamelist.xml").write_text(
        "<gameList><game><path>./Jeu.wsquashfs</path><thumbnail>./covers/Jeu.png</thumbnail>"
        "</game></gameList>", encoding="utf-8")


def _run(image: Path, tmp_path: Path) -> str:
    task = Task(id="c", kind="cover", image="win/Jeu", image_path=str(image))
    return run_cover(Settings(tmp_dir=tmp_path / "tmp"), task, Job())


def _listing(image: Path) -> list[str]:
    out = subprocess.run(["unsquashfs", "-lls", str(image)], capture_output=True, text=True,
                         check=True).stdout
    return sorted(line for line in out.splitlines() if "squashfs-root/" in line
                  and "squashfs-root/.wsquashfs" not in line and not line.endswith("squashfs-root"))


def _cat(image: Path, rel: str) -> bytes:
    return subprocess.run(["unsquashfs", "-cat", str(image), rel], capture_output=True,
                          check=True).stdout


@needs_tools
def test_cover_added_without_rebuild(tmp_path: Path):
    image = _image(tmp_path)
    _es_cover(image)
    before = _listing(image)
    inode = image.stat().st_ino
    assert "Illustration ajoutée" in _run(image, tmp_path)
    assert _cat(image, COVER) == PNG
    assert _listing(image) == before                     # existant intact (types, tailles, dates)
    assert _cat(image, "Game/game.exe") == b"MZ" * 5000
    assert image.stat().st_ino == inode                  # ajout en place, pas de nouvelle image
    assert not image.with_name("Jeu.wsquashfs.old").exists()
    dest = tmp_path / "x"
    subprocess.run(["unsquashfs", "-q", "-xattrs-include", r"^user\.", "-d", str(dest), str(image)],
                   capture_output=True, check=True)
    if os.listxattr(tmp_path / "src/Game/game.exe"):
        assert os.getxattr(dest / "Game/game.exe", "user.DOSATTRIB") == b"0x20"
    assert "déjà présente" in _run(image, tmp_path)      # second passage : rien


@needs_tools
def test_cover_without_artwork_does_nothing(tmp_path: Path):
    image = _image(tmp_path)
    data = image.read_bytes()
    assert "Aucune illustration" in _run(image, tmp_path)
    assert image.read_bytes() == data


@needs_tools
def test_cover_refuses_existing_folder(tmp_path: Path):
    """.wsquashfs/ sans illustration : mksquashfs créerait .wsquashfs_1."""
    image = _image(tmp_path, {".wsquashfs/autre": b"x"})
    _es_cover(image)
    data = image.read_bytes()
    with pytest.raises(RebuildError, match="reconstruire"):
        _run(image, tmp_path)
    assert image.read_bytes() == data


@needs_tools
def test_cover_refuses_image_in_use(tmp_path: Path, monkeypatch):
    from app.services import scan
    image = _image(tmp_path)
    _es_cover(image)
    data = image.read_bytes()
    monkeypatch.setattr(scan, "image_in_use", lambda path: True)
    with pytest.raises(RebuildError, match="en cours"):
        _run(image, tmp_path)
    assert image.read_bytes() == data


# ------------------------------------------------------------------ remplacement (sans outils)

NEW = b"\x89PNG\r\n\x1a\n" + b"steamgriddb" * 100


def _fake_image(tmp_path: Path) -> Path:
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"squashfs")
    return image


def _replace_setup(monkeypatch, tmp_path: Path, *, present: bool, current: bytes | None = None,
                   folder: bool = False, kind: str = "sgdb"):
    """Image factice : arborescence, autorun, illustration trouvée et état d'utilisation."""
    from app.services import artwork as artwork_mod
    from app.services import rebuild as rebuild_mod
    from app.services import scan
    image = _fake_image(tmp_path)
    files = ["Game/game.exe", "autorun.cmd"]
    if present:
        files += [".wsquashfs", COVER]
    elif folder:
        files += [".wsquashfs", ".wsquashfs/autre"]
    art_path = tmp_path / "sgdb.png"
    art_path.write_bytes(NEW)
    monkeypatch.setattr(scan, "_list_files_uncached", lambda settings, img: files)
    monkeypatch.setattr(scan, "read_autorun_bytes", lambda settings, img: b"CMD=game.exe\r\n")
    monkeypatch.setattr(scan, "image_in_use", lambda img: False)
    monkeypatch.setattr(scan, "_unsquashfs_cat", lambda settings, img, member: current)
    monkeypatch.setattr(artwork_mod, "artwork",
                        lambda settings, img, cmd, dir_, **kwargs: artwork_mod.Artwork(art_path, "image/png", kind))
    calls = []

    def fake_rebuild(settings, img, changes, job=None, in_use=None):
        calls.append((img, changes, in_use))
        return rebuild_mod.RebuildResult(mode="extract", size_before=1, size_after=1,
                                         backup=img.with_name(img.name + ".old"), duration=0.0)

    monkeypatch.setattr(rebuild_mod, "rebuild", fake_rebuild)
    return image, calls


def _run_replace(image: Path, tmp_path: Path, replace: bool) -> str:
    task = Task(id="c", kind="cover", image="win/Jeu", image_path=str(image),
                params={"replace": replace})
    return run_cover(Settings(tmp_dir=tmp_path / "tmp"), task, Job())


def test_replace_false_with_cover_changes_nothing(monkeypatch, tmp_path: Path):
    image, calls = _replace_setup(monkeypatch, tmp_path, present=True, current=b"autre")
    assert "déjà présente" in _run_replace(image, tmp_path, replace=False)
    assert calls == []


def test_replace_true_rebuilds_with_new_cover(monkeypatch, tmp_path: Path):
    from app.services import scan
    image, calls = _replace_setup(monkeypatch, tmp_path, present=True, current=b"ancienne")
    message = _run_replace(image, tmp_path, replace=True)
    assert "Illustration remplacée (icône SteamGridDB)" in message
    assert "Jeu.wsquashfs.old" in message
    assert len(calls) == 1
    img, changes, in_use = calls[0]
    assert img == image
    assert changes.copy == {COVER: tmp_path / "sgdb.png"}
    assert changes.write == {} and changes.delete == []
    assert in_use is scan.image_in_use


def test_replace_true_identical_cover_does_not_rebuild(monkeypatch, tmp_path: Path):
    image, calls = _replace_setup(monkeypatch, tmp_path, present=True, current=NEW)
    assert _run_replace(image, tmp_path, replace=True) == "Illustration déjà à jour : rien à faire"
    assert calls == []


def test_replace_true_with_folder_but_no_cover_rebuilds(monkeypatch, tmp_path: Path):
    image, calls = _replace_setup(monkeypatch, tmp_path, present=False, folder=True)
    assert "Illustration remplacée" in _run_replace(image, tmp_path, replace=True)
    assert len(calls) == 1 and COVER in calls[0][1].copy


def test_replace_true_without_cover_appends(monkeypatch, tmp_path: Path):
    """Sans illustration : chemin d'ajout normal (mksquashfs en ajout), pas de reconstruction."""
    from app.services import operations
    image, calls = _replace_setup(monkeypatch, tmp_path, present=False)
    commands = []
    listings = iter([["Game/game.exe", "autorun.cmd"],
                     ["Game/game.exe", "autorun.cmd", ".wsquashfs", COVER]])
    monkeypatch.setattr(operations.scan, "_list_files_uncached", lambda s, i: next(listings))
    monkeypatch.setattr(operations, "run_progress",
                        lambda command, job, phase, start, end: commands.append(command))
    monkeypatch.setattr(operations.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=NEW))
    message = _run_replace(image, tmp_path, replace=True)
    assert message.startswith("Illustration ajoutée")
    assert calls == []
    assert any(str(c).endswith("mksquashfs") for c in commands[0])

