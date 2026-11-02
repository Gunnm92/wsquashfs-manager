"""Liens du prefix vers un runner hors de l'image."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services.analyze import analyze_folder, report
from app.services.links import classify, folder_links, image_links
from app.services.operations import run_pack
from app.services.rebuild import Job

needs_tools = pytest.mark.skipif(not shutil.which("mksquashfs"), reason="squashfs-tools absent")

GE = "/config/.config/heroic/tools/proton/GE-Proton-latest"
SYS32 = "drive_c/windows/system32"


def test_harmless_links_ignored():
    """Vus sur la ludothèque : lecteurs, runners Batocera, dossiers utilisateur."""
    r = classify([
        ("dosdevices/z:", "/"), ("dosdevices/com1", "/dev/ttyS0"),
        (f"{SYS32}/d3d11.dll", "/usr/wine/proton/lib/wine/x86_64-windows/d3d11.dll"),
        ("drive_c/users/root/Documents", "/userdata/system"),
        ("pfx", "."), (f"{SYS32}/a.dll", "../../runners/X/files/lib/wine/a.dll"),
    ], [])
    assert r.count == 0 and r.issues() == []


def test_external_runner_links():
    """Kingdom Hearts III : liens vers le GE-Proton de Heroic, rien de joint."""
    r = classify([(f"{SYS32}/d3d11.dll", f"{GE}/files/lib/wine/x86_64-windows/d3d11.dll"),
                  (f"{SYS32}/x.dll", "/home/u/.steam/root/compatibilitytools.d/P/files/x.dll")],
                 [f"{SYS32}/d3d11.dll"])
    assert not r.fixable and len(r.external) == 2
    assert r.runners == ["GE-Proton-latest", "P"]
    [issue] = r.issues()
    assert issue["level"] == "warn" and "GE-Proton-latest" in issue["message"]


def test_fixable_by_name_or_single_runner():
    files = ["runners/GE-Proton-latest/files/lib/wine/x86_64-windows/d3d11.dll"]
    link = (f"{SYS32}/d3d11.dll", f"{GE}/files/lib/wine/x86_64-windows/d3d11.dll")
    r = classify([link], files)
    assert r.fixable == {link[0]: "../../../runners/GE-Proton-latest/files/lib/wine/"
                                  "x86_64-windows/d3d11.dll"}
    assert r.issues()[0]["level"] == "warn"
    assert r.issues(image=True)[0]["level"] == "error"
    # Ancien emplacement (runner/ à la racine, autre nom) : seul runner joint
    old = (f"{SYS32}/d3d11.dll", "../../../runner/Autre/files/lib/wine/x86_64-windows/d3d11.dll")
    assert classify([old], files).fixable[old[0]].startswith("../../../runners/GE-Proton-latest/")
    # Fichier absent du runner joint : pas corrigeable
    missing = (f"{SYS32}/zz.dll", f"{GE}/files/lib/wine/x86_64-windows/zz.dll")
    assert classify([missing], files).external == [missing]


def test_missing_inside_image():
    """Anno 117 : liens vers des fichiers icu absents du runner joint."""
    files = ["runners/D/files/lib/wine/x86_64-windows/d3d11.dll"]
    link = (f"{SYS32}/icudt68.dll", "../../../runners/D/files/lib/wine/icu/x86_64-windows/icudt68.dll")
    r = classify([link], files)
    assert r.missing == [link] and not r.external and not r.runners
    [issue] = r.issues(image=True)
    assert "absents de l'image" in issue["message"]


def _prefix(root: Path) -> Path:
    dll = root / "runners/GE-Proton-latest/files/lib/wine/x86_64-windows/d3d11.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"dll")
    game = root / "drive_c/Mon Jeu/game.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ" * 100)
    (root / SYS32).mkdir(parents=True)
    (root / SYS32 / "d3d11.dll").symlink_to(f"{GE}/files/lib/wine/x86_64-windows/d3d11.dll")
    (root / SYS32 / "ext.dll").symlink_to(f"{GE}/files/lib/wine/x86_64-windows/ext.dll")
    (root / "config_info").write_text("GE-Proton-latest\n")
    return root


def test_folder_analysis_reports_links(tmp_path):
    root = _prefix(tmp_path / "Jeu.wine")
    assert len(folder_links(root)) == 2
    r = report(analyze_folder(root, "Jeu"))
    assert r["links"]["fixable"] == 1 and r["links"]["external"] == 1
    assert len(r["link_issues"]) == 2


@needs_tools
def test_pack_rewrites_links_without_touching_folder(tmp_path):
    root = _prefix(tmp_path / "roms" / "win" / "Jeu.wine")
    task = Task(id="p", kind="pack", image="win/Jeu",
                image_path=str(root.with_name("Jeu.wsquashfs")),
                params={"source": str(root), "autorun": "DIR=drive_c/Mon Jeu\r\nCMD=game.exe\r\n",
                        "rename_wine": True, "delete_source": False})
    job = Job()
    run_pack(Settings(tmp_dir=tmp_path / "tmp"), task, job)
    links = dict(image_links(root.with_name("Jeu.wsquashfs")))
    assert links[f"{SYS32}/d3d11.dll"] == ("../../../runners/GE-Proton-latest/files/lib/wine/"
                                           "x86_64-windows/d3d11.dll")
    assert links[f"{SYS32}/ext.dll"].startswith(GE)            # sans équivalent : gardé
    assert (root / SYS32 / "d3d11.dll").readlink().as_posix().startswith(GE)   # dossier intact
    out = subprocess.run(["unsquashfs", "-cat", str(root.with_name("Jeu.wsquashfs")),
                          "runners/GE-Proton-latest/files/lib/wine/x86_64-windows/d3d11.dll"],
                         capture_output=True, check=True).stdout
    assert out == b"dll"
