"""Tests du scan : lecture de `unsquashfs -l`, type de prefix, jeu en cours."""

from __future__ import annotations

import fcntl

from app.config import Settings
from app.models import ImageInfo, ImageType
from app.services.scan import (
    _apply_dynamic,
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


def test_autorun_attach_is_reported():
    """ATTACH= (commande lancée pendant le jeu) remonte dans la bibliothèque : étiquette ATTACH."""
    from pathlib import Path

    from app.models import ImageInfo
    from app.services.autorun import Autorun
    from app.services.scan import _autorun_fields

    def info_for(text: str) -> ImageInfo:
        info = ImageInfo(id="win/Jeu", name="Jeu", system="win", path=Path("/x/Jeu.wsquashfs"),
                         size=1, mtime=0)
        _autorun_fields(info, Autorun.parse(text))
        return info

    assert info_for("CMD=Jeu.exe\nATTACH=sh attach.sh\n").attach == "sh attach.sh"
    assert info_for("CMD=Jeu.exe\n").attach is None
    assert info_for("CMD=Jeu.exe\nATTACH=\n").attach is None


def test_old_size_is_the_backup_size_next_to_the_image(tmp_path):
    """old_size : taille de <image>.old à côté de l'image, 0 sans .old ; calculée au scan
    (comme has_old), sans relire le squashfs."""
    image = tmp_path / "Jeu.wsquashfs"
    image.write_bytes(b"img")
    settings = Settings(saves_dir=tmp_path / "saves", state_dir=tmp_path / "state")

    def scanne():
        return _apply_dynamic(settings, ImageInfo(id="win/Jeu", path=image, name="Jeu"),
                              set(), [], set())

    info = scanne()
    assert (info.has_old, info.old_size) == (False, 0)
    image.with_name("Jeu.wsquashfs.old").write_bytes(b"x" * 1234)
    info = scanne()
    assert (info.has_old, info.old_size) == (True, 1234)


def test_saves_size_cached_until_game_ends_or_forgotten(tmp_path, monkeypatch):
    """Taille des sauvegardes : mise en cache d'un scan à l'autre ; recalculée quand le jeu
    vient de se terminer, après forget_saves, sur force, ou au-delà de SAVES_TTL."""
    from app.services import scan
    saves = tmp_path / "saves" / "Jeu"
    (saves / "pfx" / "drive_c").mkdir(parents=True)
    (saves / "pfx" / "drive_c" / "save.dat").write_bytes(b"x" * 100)
    (saves / "lien").symlink_to(saves / "pfx" / "drive_c" / "save.dat")    # pas compté
    settings = Settings(saves_dir=tmp_path / "saves", state_dir=tmp_path / "state")
    walks = []
    real = scan._saves_size_uncached
    monkeypatch.setattr(scan, "_saves_size_uncached",
                        lambda s, stem: walks.append(stem) or real(s, stem))

    assert scan._saves_size(settings, "Jeu") == (True, 100)
    (saves / "pfx" / "drive_c" / "save2.dat").write_bytes(b"y" * 50)
    assert scan._saves_size(settings, "Jeu") == (True, 100)          # cache : pas de parcours
    assert len(walks) == 1
    assert scan._saves_size(settings, "Jeu", in_use=True) == (True, 100)
    assert scan._saves_size(settings, "Jeu", in_use=False) == (True, 150)   # partie terminée
    assert len(walks) == 2
    (saves / "pfx" / "drive_c" / "save3.dat").write_bytes(b"z")
    scan.forget_saves("Jeu")
    assert scan._saves_size(settings, "Jeu") == (True, 151)
    (saves / "pfx" / "drive_c" / "save3.dat").write_bytes(b"zz")
    assert scan._saves_size(settings, "Jeu", force=True) == (True, 152)
    monkeypatch.setattr(scan, "SAVES_TTL", 0)
    (saves / "pfx" / "drive_c" / "save3.dat").write_bytes(b"zzz")
    assert scan._saves_size(settings, "Jeu") == (True, 153)
    assert scan._saves_size(settings, "Absent") == (False, 0)
