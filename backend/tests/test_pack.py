"""Création : analyse d'un dossier, proposition d'autorun, empaquetage."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services.analyze import analyze_folder, bat_details, name_similarity, report
from app.services.operations import find_folders, folder_stem, run_pack
from app.services.rebuild import Job, RebuildCancelled, RebuildError

needs_tools = pytest.mark.skipif(not shutil.which("mksquashfs"), reason="squashfs-tools absent")


def _tree(root: Path, files: dict[str, int | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else b"\0" * content)
    return root


def _cat(image: Path, member: str) -> bytes:
    return subprocess.run(["unsquashfs", "-cat", str(image), member], capture_output=True,
                          check=True).stdout


def _listing(image: Path) -> set[str]:
    out = subprocess.run(["unsquashfs", "-l", str(image)], capture_output=True,
                         check=True).stdout.decode()
    return {ln[len("squashfs-root/"):] for ln in out.splitlines()
            if ln.startswith("squashfs-root/")}


# ------------------------------------------------------------------ analyse

def test_unreal_prefers_root_launcher_like_existing_autoruns(tmp_path):
    root = _tree(tmp_path / "SAND LAND.pc", {
        "SANDLAND.exe": 300_000,
        "SANDLAND/Binaries/Win64/SANDLAND-Win64-Shipping.exe": 90_000_000,
        "Engine/Binaries/Win64/CrashReportClient.exe": 20_000_000,
        "Engine/Extras/Redist/en-us/UE4PrereqSetup_x64.exe": 40_000_000,
    })
    fa = analyze_folder(root, "SAND LAND")
    assert fa.engine == "unreal"
    assert [c.path for c in fa.candidates[:2]] == [
        "SANDLAND.exe", "SANDLAND/Binaries/Win64/SANDLAND-Win64-Shipping.exe"]
    assert {e["path"] for e in fa.excluded} >= {"Engine/Binaries/Win64/CrashReportClient.exe"}
    r = report(fa)
    assert r["generated"]["text"] == "CMD=SANDLAND.exe\n"
    shipping = report(fa, "SANDLAND/Binaries/Win64/SANDLAND-Win64-Shipping.exe")
    assert shipping["generated"]["text"].startswith("DIR=SANDLAND/Binaries/Win64\n")


def test_unity_crack_and_annex_dirs(tmp_path):
    root = _tree(tmp_path / "Toki.pc", {
        "Toki.exe": 650_000, "UnityPlayer.dll": 30_000_000, "UnityCrashHandler64.exe": 1_500_000,
        "_crack/Toki.exe": 650_000, "Toki Soundtrack/Toki Soundtrack.exe": 80_000_000,
        "start_server.bat": b"server.exe",
    })
    fa = analyze_folder(root, "Toki")
    assert fa.engine == "unity"
    assert fa.candidates[0].path == "Toki.exe"
    paths = [c.path for c in fa.candidates]
    assert "_crack/Toki.exe" not in paths and "start_server.bat" not in paths
    assert fa.candidates[-1].path == "Toki Soundtrack/Toki Soundtrack.exe"


def test_spaces_quoted_and_existing_autorun_compared(tmp_path):
    root = _tree(tmp_path / "Sky Force.pc", {
        "Sky Force.exe": 5_000_000, "autorun.cmd": b"CMD='Sky Force.exe'\r\n"})
    r = report(analyze_folder(root, "Sky Force"))
    assert r["generated"]["text"] == 'CMD="Sky Force.exe"\n'
    # Guillemets simples : le lanceur ne les comprend pas → autorun existant cassé
    assert r["existing"]["issues"] and r["recommended"] == "generated"


def test_existing_valid_autorun_is_recommended(tmp_path):
    root = _tree(tmp_path / "Jeu.pc", {"bin/jeu.exe": 1000, "autorun.cmd": b"DIR=bin\nCMD=jeu.exe\n"})
    assert report(analyze_folder(root, "Jeu"))["recommended"] == "existing"


def test_no_windows_game(tmp_path):
    root = _tree(tmp_path / "Linux.pc", {"start": b"#!/bin/sh", "Linux.desktop": b""})
    r = report(analyze_folder(root, "Linux"))
    assert r["candidates"] == [] and r["generated"]["text"] == ""
    assert any("aucun exécutable" in w for w in r["generated"]["warnings"])


def test_teknoparrot_profile(tmp_path):
    root = _tree(tmp_path / "Hot.pc", {
        "drive_c/teknoparrot/TeknoParrotUi.exe": 5_000_000,
        "drive_c/teknoparrot/UserProfiles/HotWheels.xml":
            b"<GameProfile><GamePath>C:\\game\\hw.exe</GamePath></GameProfile>",
        "drive_c/game/hw.exe": 9_000_000, "system.reg": b"WINE REGISTRY Version 2\n#arch=win32\n",
    })
    r = report(analyze_folder(root, "Hot Wheels"))
    assert r["prefix"] == "batocera" and r["arch"] == "win32" and r["engine"] == "teknoparrot"
    text = r["generated"]["text"]
    assert 'CMD="TeknoParrotUi.exe" --profile=HotWheels.xml --startMinimized' in text
    assert "ARCH=win32" in text


def test_helpers():
    assert name_similarity("bs1dc.exe", "Broken Sword DC") > 0.6
    assert name_similarity("SANDLAND-Win64-Shipping.exe", "SAND LAND") == 1.0
    assert folder_stem("Jeu.pc") == "Jeu" and folder_stem("Jeu.WINE") == "Jeu"
    assert folder_stem(".pc") == ".pc"


def test_bat_typos(tmp_path):
    (tmp_path / "launch.bat").write_text("start game.exe\ntaskill /im x.exe\nC:\\Games\\a.exe\n")
    details = bat_details(tmp_path, "launch.bat")
    assert len(details["suspicious"]) == 2


def test_find_folders(tmp_path):
    win = tmp_path / "roms" / "win"
    (win / "Jeu.pc").mkdir(parents=True)
    (win / "Jeu.pc" / "autorun.cmd").write_text("CMD=a.exe")
    (win / "Autre.wine").mkdir()
    (win / "Jeu.wsquashfs").write_bytes(b"")
    (win / "dossier normal").mkdir()
    for dirs in ([str(tmp_path / "roms")], [str(win)]):
        found = {f["id"]: f for f in find_folders(Settings(roms_dirs=dirs))}
        assert set(found) == {"win/Jeu.pc", "win/Autre.wine"}
        assert found["win/Jeu.pc"]["has_autorun"] and found["win/Jeu.pc"]["image_exists"]
        assert found["win/Autre.wine"]["image_id"] == "win/Autre"


# ------------------------------------------------------------------ empaquetage

@pytest.fixture
def pack_env(tmp_path):
    folder = _tree(tmp_path / "roms" / "win" / "Mon Jeu.pc", {
        "bin/game.exe": b"MZ" * 1000, "data/a.pak": 50_000, "autorun.cmd": b"CMD=vieux.exe\n",
    })
    (folder / "lien").symlink_to("bin")
    settings = Settings(tmp_dir=tmp_path / "tmp")

    def task(**params):
        base = {"source": str(folder), "autorun": None, "rename_wine": True,
                "delete_source": False}
        return Task(id="p", kind="pack", image="win/Mon Jeu",
                    image_path=str(folder.with_name("Mon Jeu.wsquashfs")), params={**base, **params})
    return settings, folder, task


@needs_tools
def test_pack_renames_injects_autorun_and_keeps_folder(pack_env):
    settings, folder, task = pack_env
    new = "DIR=bin\r\nCMD=game.exe\r\n"
    t = task(autorun=new)
    message = run_pack(settings, t, Job())
    image = folder.with_name("Mon Jeu.wsquashfs")
    wine = folder.with_name("Mon Jeu.wine")
    assert "créé" in message and "conservé" in message
    assert not folder.exists() and wine.is_dir() and t.params["source"] == str(wine)
    assert _cat(image, "autorun.cmd") == new.encode()
    assert (wine / "autorun.cmd").read_bytes() == b"CMD=vieux.exe\n"     # dossier intact
    assert _listing(image) == {"autorun.cmd", "bin", "bin/game.exe", "data", "data/a.pak", "lien"}
    assert not list(settings.tmp_dir.iterdir())


@needs_tools
def test_pack_existing_autorun_without_rename_then_delete(pack_env):
    settings, folder, task = pack_env
    run_pack(settings, task(rename_wine=False, delete_source=True), Job())
    image = folder.with_name("Mon Jeu.wsquashfs")
    assert _cat(image, "autorun.cmd") == b"CMD=vieux.exe\n"
    assert not folder.exists()


@needs_tools
def test_pack_never_overwrites_and_cancel_cleans(pack_env):
    settings, folder, task = pack_env
    image = folder.with_name("Mon Jeu.wsquashfs")
    image.write_bytes(b"existante")
    with pytest.raises(RebuildError, match="existe déjà"):
        run_pack(settings, task(), Job())
    assert image.read_bytes() == b"existante" and folder.is_dir()
    image.unlink()
    job = Job()
    job.cancel()
    with pytest.raises(RebuildCancelled):
        run_pack(settings, task(rename_wine=False), job)
    assert not image.exists() and not image.with_name(image.name + ".part").exists()
    assert folder.is_dir()


@needs_tools
def test_pack_resumes_after_rename(pack_env):
    settings, folder, task = pack_env
    folder.rename(folder.with_name("Mon Jeu.wine"))      # interrompue après le renommage
    run_pack(settings, task(), Job())
    assert folder.with_name("Mon Jeu.wsquashfs").exists()


def test_game_version_engine_vs_game():
    from app.services.analyze import ExeInfo, _game_version
    def version(name, value, engine):
        return _game_version(ExeInfo(product_name=name, product_version=value), engine)[0]
    assert version("SAND LAND", "1.0.3.0", "unreal") == "1.0.3.0"
    assert version("Aces of the Luftwaffe Squadron", "1.0.7.0", "unity") == "1.0.7.0"
    assert version("BootstrapPackagedGame", "6566", "unreal") is None
    assert version(None, "++UE4+Release-4.27-CL-0", "unreal") is None
    assert version(None, "2022.3.31f1 (4ede2d13e8b4)", "unity") is None
    assert version(None, "2020.3.20.4310246", "unity") is None
    assert version(None, "1.671.065.0", None) == "1.671.065.0"
    assert version("BRAVELY DEFAULT II", "1.0.0.0", "unreal") is None
    assert version("Spider-Man 2", "1.0", None) is None
    assert version("Worms Crazy Golf", "1.0.0.456", None) == "1.0.0.456"
    assert version("God of War", "GoWR-6137230-Thu Sep 26 09:46:31 2024", None) is None
    assert version("Jeu", "v1.0.7", None) == "v1.0.7"


def test_knowledge_base_overrides_rules_and_flags_existing(tmp_path):
    import hashlib

    from app.services import analyze
    root = _tree(tmp_path / "Astebreed.pc", {
        "Astebreed.exe": b"MZ astebreed", "autorun.cmd": b"CMD=Astebreed.exe\r\n"})
    games = tmp_path / "games.yaml"
    digest = hashlib.sha256(b"MZ astebreed").hexdigest()
    games.write_text(f"games:\n  - exe: autre-nom.exe\n    sha256: {digest}\n"
                     "    keys: {DXVK: \"0\"}\n    note: wined3d obligatoire\n")
    fa = analyze_folder(root, "Astebreed")
    proposal = analyze.propose(fa.scan, fa.candidates[0], "none", None, None, None, {},
                               games_file=games)
    assert proposal.autorun.render() == "CMD=Astebreed.exe\r\nDXVK=0\r\n"
    assert "empreinte" in proposal.items[-1]["justification"]
    # Par le nom seulement, et une valeur égale au défaut n'est pas écrite
    games.write_text("games:\n  - exe: ASTEBREED.EXE\n    keys: {DXVK: \"1\"}\n")
    proposal = analyze.propose(fa.scan, fa.candidates[0], "none", None, None, None, {},
                               games_file=games)
    assert "DXVK" not in proposal.autorun.render()


def test_shipped_games_yaml_is_valid():
    from app.services.analyze import load_games
    assert any(g["exe"] == "Astebreed.exe" for g in load_games())


def test_knowledge_requires_file(tmp_path):
    from app.services import analyze
    games = tmp_path / "games.yaml"
    games.write_text("games:\n  - exe: jeu.exe\n    requires: [d3dx9_43.dll]\n    note: DLL native\n")
    root = _tree(tmp_path / "Jeu.pc", {"bin/jeu.exe": b"MZ"})
    fa = analyze_folder(root, "Jeu")
    proposal = analyze.propose(fa.scan, fa.candidates[0], "none", None, None, None, {},
                               games_file=games)
    assert any("d3dx9_43.dll manquant" in w for w in proposal.warnings)
    _tree(root, {"bin/D3DX9_43.DLL": b"MZ"})
    fa = analyze_folder(root, "Jeu")
    proposal = analyze.propose(fa.scan, fa.candidates[0], "none", None, None, None, {},
                               games_file=games)
    assert not any("manquant" in w for w in proposal.warnings)


def test_unreal_root_launcher_inherits_shipping_signatures(tmp_path):
    from app.services import analyze
    root = _tree(tmp_path / "Until Dawn.pc", {
        "Windows/Bates.exe": 430_000,
        "Windows/Bates/Binaries/Win64/Bates-Win64-Shipping.exe": 149_000_000,
    })
    fa = analyze_folder(root, "Until Dawn")
    stub = next(c for c in fa.candidates if c.path == "Windows/Bates.exe")
    ship = next(c for c in fa.candidates if c.path.endswith("Shipping.exe"))
    ship.info.imports.add("hid.dll")
    ship.info.markers.add("DualSense")
    analyze._merge_shipping(fa.candidates)
    assert "hid.dll" in stub.info.imports and "DualSense" in stub.info.markers


def test_existing_autorun_missing_rule_setting_is_not_recommended(tmp_path):
    root = _tree(tmp_path / "GoW.pc", {"GoWR.exe": 5_000_000, "libScePad.dll": 1000,
                                       "steam_api64.dll": 1000, "autorun.cmd": b"CMD=GoWR.exe\r\n"})
    r = report(analyze_folder(root, "God of War Ragnarok"))
    assert "HIDRAW=1" in r["generated"]["text"]
    assert r["recommended"] == "generated"
    assert any("ajoute HIDRAW=1" in i["message"] for i in r["existing"]["issues"])
