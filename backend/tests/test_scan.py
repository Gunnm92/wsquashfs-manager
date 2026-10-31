"""Tests du scan : lecture de `unsquashfs -l`, type de prefix, jeu en cours."""

from __future__ import annotations

import fcntl

from app.models import ImageType
from app.services.scan import (
    _fakeping_candidates,
    _held_lock_inodes,
    detect_type,
    is_in_use,
    parse_listing,
)

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


def test_is_in_use_with_custom_launcher_cache(tmp_path):
    cache = tmp_path / "cache-ailleurs"
    prefixes = {f"{cache}/wine/Ride 6/pfx"}
    assert is_in_use("Ride 6", prefixes, [], cache)
    assert not is_in_use("Ride 6", prefixes, [])          # cache inconnu : pas reconnu
    assert is_in_use("Ride 6", set(), [f"{cache}/mnt/Ride 6"], cache)
    assert not is_in_use("Ride", set(), [f"{cache}/mnt/Ride 6"], cache)


def test_launcher_lock_seen_without_taking_it(tmp_path):
    """Lancement en préparation (verrou tenu, Wine pas encore démarré, montages
    de l'overlay du noyau invisibles) : jeu en cours."""
    cache = tmp_path / "cache"
    (cache / "locks").mkdir(parents=True)
    lock = cache / "locks" / "Hell is Us.lock"
    lock.touch()
    assert not is_in_use("Hell is Us", set(), [], cache, _held_lock_inodes())
    with open(lock) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)      # comme `flock -n` du lanceur
        assert is_in_use("Hell is Us", set(), [], cache, _held_lock_inodes())
        assert not is_in_use("Hell", set(), [], cache, _held_lock_inodes())
    assert not is_in_use("Hell is Us", set(), [], cache, _held_lock_inodes())


# ----------------------------------------------------------------- runner joint

PROTON_FILES = ["runners/DenuvOwO-11", "runners/DenuvOwO-11/proton",
                "runners/DenuvOwO-11/files/bin/wine"]
WINE_FILES = ["runners/wine-mine/bin/wine"]


def _info(**kw):
    from pathlib import Path

    from app.models import ImageInfo
    return ImageInfo(path=Path("/x/Jeu.wsquashfs"), name="Jeu", **kw)


def test_joined_runners_lists_dirs_with_wine():
    from app.services.scan import joined_runners
    files = PROTON_FILES + WINE_FILES + ["runners/vide/readme.txt", "drive_c/runners/x/bin/wine"]
    assert joined_runners(files) == ["DenuvOwO-11", "wine-mine"]


def test_joined_runner_named_by_proton_or_wine():
    from app.models import RunnerKind
    from app.services.scan import _apply_joined
    info = _info(runner=RunnerKind.PROTON, proton="denuvowo-11", joined_runners=["DenuvOwO-11"])
    _apply_joined(info, PROTON_FILES)
    assert info.runner_joined
    info = _info(runner=RunnerKind.WINE, wine="mine", joined_runners=["wine-mine"])
    _apply_joined(info, WINE_FILES)
    assert info.runner_joined
    info = _info(runner=RunnerKind.PROTON, proton="GE-Proton9-1", joined_runners=["DenuvOwO-11"])
    _apply_joined(info, PROTON_FILES)
    assert not info.runner_joined


def test_single_joined_runner_used_without_key():
    from app.models import RunnerKind
    from app.services.scan import _apply_joined
    info = _info(joined_runners=["DenuvOwO-11"])
    _apply_joined(info, PROTON_FILES)
    assert (info.runner, info.proton, info.runner_joined) == (RunnerKind.PROTON, "DenuvOwO-11", True)
    info = _info(joined_runners=["wine-mine"])
    _apply_joined(info, WINE_FILES)
    assert (info.runner, info.wine, info.runner_joined) == (RunnerKind.WINE, "wine-mine", True)
    info = _info(joined_runners=["a", "b"])
    _apply_joined(info, [])
    assert (info.runner, info.runner_joined) == (RunnerKind.DEFAULT, False)
