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
