"""Runner joint depuis SteamBox : propositions et reconstruction."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services import runners, scan
from app.services.links import classify, image_links
from app.services.rebuild import Job

needs_tools = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")), reason="squashfs-tools absent")

GE = "/box/heroic/tools/proton/GE-Proton-latest"
SLR = "/box/compatibilitytools.d/proton-cachyos-slr"
DOCTOR = [{"name": "GE-Proton-latest", "path": GE}, {"name": "proton-cachyos-slr", "path": SLR}]
SYS32 = "drive_c/windows/system32"


def _write(root: Path, rel: str, data: bytes = b"x") -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(data)


@pytest.fixture
def box(tmp_path, monkeypatch) -> Path:
    """« SteamBox » : runners dans un dossier local ; listage et copie simulés."""
    base = tmp_path / "box"
    ge = base / GE.lstrip("/")
    _write(ge, "proton", b"#!/usr/bin/env python3")
    _write(ge, "files/lib/wine/x86_64-windows/d3d11.dll", b"ge-d3d11")
    (ge / "files/lib/libz.so").parent.mkdir(parents=True, exist_ok=True)
    (ge / "files/lib/libz.so").symlink_to("libz.so.1")          # lien interne au runner
    slr = base / SLR.lstrip("/")
    for arch in ("x86_64-windows", "i386-windows"):
        _write(slr, f"files/lib/wine/icu/{arch}/icudt68.dll", b"icu")

    def listing(settings, path):
        root = base / path.lstrip("/")
        return {str(p.relative_to(root)): (p.stat().st_size if p.is_file() and not p.is_symlink()
                                           else 0) for p in root.rglob("*")}

    def fetch(settings, path, dest, job=None, total=0):
        shutil.copytree(base / path.lstrip("/"), dest, symlinks=True)

    monkeypatch.setattr(runners, "steambox_listing", listing)
    monkeypatch.setattr(runners, "steambox_fetch", fetch)
    monkeypatch.setattr(scan, "image_in_use", lambda image: False)
    return base


def _image(tmp_path: Path, name: str, build) -> Path:
    src = tmp_path / f"src-{name}"
    _write(src, "autorun.cmd", b"DIR=drive_c/Jeu\r\nCMD=jeu.exe\r\n")
    _write(src, "drive_c/Jeu/jeu.exe", b"MZ")
    _write(src, "config_info", b"GE-Proton11-5\n")
    build(src)
    out = tmp_path / "roms" / "win" / f"{name}.wsquashfs"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mksquashfs", str(src), str(out), "-noappend", "-quiet"], check=True,
                   capture_output=True)
    return out


@needs_tools
def test_join_whole_runner(tmp_path, box):
    """Kingdom Hearts III : liens vers le GE-Proton de la machine."""
    def build(src):
        (src / SYS32).mkdir(parents=True)
        (src / SYS32 / "d3d11.dll").symlink_to(f"{GE}/files/lib/wine/x86_64-windows/d3d11.dll")
    image = _image(tmp_path, "KH3", build)
    settings = Settings(tmp_dir=tmp_path / "tmp", rebuild_mode="extract")

    [s] = runners.suggest(settings, image, DOCTOR)["suggestions"]
    assert s["action"] == "join" and s["dest"] == "GE-Proton-latest" and s["key"] == "PROTON"
    assert s["rewritten"] == 1 and s["remaining"] == 0

    task = Task(id="r", kind="runner", image="win/KH3", image_path=str(image),
                params={"source": GE, "sub": "", "dest": "GE-Proton-latest", "set_key": True})
    message = runners.run_runner(settings, task, Job())
    assert "1 lien(s) réécrit(s)" in message and "PROTON=GE-Proton-latest" in message
    links = dict(image_links(image))
    assert links[f"{SYS32}/d3d11.dll"] == ("../../../runners/GE-Proton-latest/files/lib/wine/"
                                           "x86_64-windows/d3d11.dll")
    assert links["runners/GE-Proton-latest/files/lib/libz.so"] == "libz.so.1"   # gardé tel quel
    autorun = subprocess.run(["unsquashfs", "-cat", str(image), "autorun.cmd"],
                             capture_output=True, check=True).stdout
    assert b"PROTON=GE-Proton-latest" in autorun
    files = scan._list_files_uncached(settings, image)
    assert classify(image_links(image), files).count == 0
    assert image.with_name("KH3.wsquashfs.old").exists()


@needs_tools
def test_complete_joined_runner(tmp_path, box):
    """Anno 117 : DenuvOwO joint sans files/lib/wine/icu."""
    den = "runners/DenuvOwO-cachyos-11.0"

    def build(src):
        _write(src, f"{den}/proton", b"#!")
        _write(src, f"{den}/files/lib/wine/x86_64-windows/d3d11.dll", b"d")
        (src / SYS32).mkdir(parents=True)
        (src / SYS32 / "icudt68.dll").symlink_to(
            f"../../../{den}/files/lib/wine/icu/x86_64-windows/icudt68.dll")
    image = _image(tmp_path, "Anno", build)
    settings = Settings(tmp_dir=tmp_path / "tmp", rebuild_mode="extract")

    result = runners.suggest(settings, image, DOCTOR)
    assert result["report"]["missing"] == 1
    # GE a aussi icu ? non dans ce test ; seul proton-cachyos-slr convient
    [s] = result["suggestions"]
    assert s["action"] == "complete" and s["runner"] == "proton-cachyos-slr"
    assert s["sub"] == "files/lib/wine/icu" and s["dest"] == "DenuvOwO-cachyos-11.0"
    assert s["repaired"] == 1 and s["key"] is None

    task = Task(id="r", kind="runner", image="win/Anno", image_path=str(image),
                params={"source": SLR, "sub": s["sub"], "dest": s["dest"], "set_key": False})
    runners.run_runner(settings, task, Job())
    files = scan._list_files_uncached(settings, image)
    assert f"{den}/files/lib/wine/icu/i386-windows/icudt68.dll" in files
    assert f"{den}/files/lib/wine/x86_64-windows/d3d11.dll" in files          # existant gardé
    assert classify(image_links(image), files).count == 0
    autorun = subprocess.run(["unsquashfs", "-cat", str(image), "autorun.cmd"],
                             capture_output=True, check=True).stdout
    assert autorun == b"DIR=drive_c/Jeu\r\nCMD=jeu.exe\r\n"                    # inchangé


def test_names_refused(tmp_path):
    with pytest.raises(runners.RebuildError):
        runners._check_names("../x", "")
    with pytest.raises(runners.RebuildError):
        runners._check_names("GE", "files/../../etc")
