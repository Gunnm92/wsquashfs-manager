"""Tests du scan : lecture de `unsquashfs -l`, type de prefix, jeu en cours."""

from __future__ import annotations

from app.models import ImageType
from app.services.scan import _fakeping_candidates, detect_type, is_in_use, parse_listing

LISTING = """Parallel unsquashfs: Using 8 processors
squashfs-root
squashfs-root/autorun.cmd
squashfs-root/system.reg
squashfs-root/drive_c
squashfs-root/drive_c/Game Dir
squashfs-root/drive_c/Game Dir/game.exe
"""


def test_parse_listing_strips_prefix_and_keeps_spaces():
    assert parse_listing(LISTING) == [
        "autorun.cmd", "system.reg", "drive_c", "drive_c/Game Dir",
        "drive_c/Game Dir/game.exe",
    ]


def test_detect_type_batocera():
    assert detect_type(["system.reg", "user.reg", "drive_c"]) is ImageType.WINE_BATOCERA


def test_detect_type_proton_wins_over_system_reg():
    assert detect_type(["config_info", "pfx/system.reg", "system.reg"]) is ImageType.WINE_PROTON


def test_detect_type_ignores_nested_files():
    assert detect_type(["game/system.reg", "game/config_info"]) is ImageType.GAME_ONLY


def test_fakeping_candidates_skip_system_dirs():
    files = ["drive_c/windows/system32/dinput8.dll", "drive_c/game/dinput8.dll"]
    assert _fakeping_candidates(files) == ["drive_c/game/dinput8.dll"]


def test_is_in_use_exact_name():
    prefixes = {"/userdata/saves/windows/wsquashfs/wine/Frogger 2/pfx"}
    assert is_in_use("Frogger 2", prefixes, [])
    assert not is_in_use("Frogger", prefixes, [])


def test_is_in_use_by_mount():
    mounts = ["/tmp/wsquashfs/mnt/gticlub"]
    assert is_in_use("gticlub", set(), mounts)
    assert not is_in_use("gti", set(), mounts)


def test_find_images_system_from_roms_parent(tmp_path):
    from app.services.scan import _find_images
    win = tmp_path / "roms" / "win"
    win.mkdir(parents=True)
    (win / "Jeu.wsquashfs").write_bytes(b"")
    (tmp_path / "ailleurs").mkdir()
    (tmp_path / "ailleurs" / "Autre.wsquashfs").write_bytes(b"")
    assert _find_images([str(tmp_path / "roms")], 3) == [(win / "Jeu.wsquashfs", "win")]
    assert _find_images([str(win) + "/"], 3) == [(win / "Jeu.wsquashfs", "win")]
    assert _find_images([str(tmp_path / "ailleurs")], 3) == \
        [(tmp_path / "ailleurs" / "Autre.wsquashfs", None)]


def test_find_images_no_duplicates_when_root_and_system_chosen(tmp_path):
    from app.services.scan import _find_images
    win = tmp_path / "roms" / "win"
    win.mkdir(parents=True)
    (win / "Jeu.wsquashfs").write_bytes(b"")
    found = _find_images([str(tmp_path / "roms"), str(win)], 3)
    assert found == [(win / "Jeu.wsquashfs", "win")]
