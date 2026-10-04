"""Opérations « fichiers », vérification et sauvegardes, sur vraies images."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Task
from app.services import scan
from app.services.autorun import Autorun
from app.services.operations import (
    plan_files,
    reset_saves,
    resolve_exe,
    run_files,
    run_verify,
)
from app.services.rebuild import Job, RebuildError

pytestmark = pytest.mark.skipif(not shutil.which("mksquashfs"), reason="squashfs-tools absent")

USER_REG = ("WINE REGISTRY Version 2\n\n#arch=win32\n\n"
            "[Software\\\\Wine\\\\Drivers] 1698115281\n\"Audio\"=\"pulse\"\n")
FAKEPING = {
    "copy": [{"dest": "{exe_dir}/dinput8.dll", "source": ""}],
    "reg": [{"file": "user.reg", "key": "Software\\Wine\\AppDefaults\\{exe_name}\\DllOverrides",
             "name": "dinput8", "value": "native,builtin"}],
}


def _cat(image: Path, member: str) -> bytes:
    return subprocess.run(["unsquashfs", "-cat", str(image), member], capture_output=True,
                          check=True).stdout


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    game = src / "drive_c" / "game" / "Bin"
    game.mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ")
    (src / "user.reg").write_text(USER_REG)
    (src / "system.reg").write_text("WINE REGISTRY Version 2\n")
    (src / "autorun.cmd").write_bytes(b"DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\n")
    image = tmp_path / "roms" / "arcade" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(image), "-noappend", "-quiet"], check=True)
    dll = tmp_path / "dinput8.dll"
    dll.write_bytes(b"MZ fakeping")
    monkeypatch.setattr(scan, "_CACHE_DIR", tmp_path / "cache")
    settings = Settings(tmp_dir=tmp_path / "tmp", rebuild_mode="extract",
                        saves_dir=tmp_path / "saves")
    params = {**FAKEPING, "copy": [{"dest": "{exe_dir}/dinput8.dll", "source": str(dll)}]}
    task = Task(id="t", kind="files", image="arcade/Jeu", image_path=str(image), params=params)
    return settings, image, task


def test_resolve_exe_like_launcher():
    files = ["drive_c", "drive_c/game", "drive_c/game/Bin", "drive_c/game/Bin/Game.exe"]
    a = Autorun.parse("DIR=drive_c/GAME/bin\nCMD=game.exe -x\n")
    assert resolve_exe(files, a) == "drive_c/game/Bin/Game.exe"
    assert resolve_exe(files, Autorun.parse("CMD=nope.exe\n")) is None


def test_plan_fakeping_uses_image_case(env):
    settings, image, task = env
    plan = plan_files(settings, image, "Jeu", "arcade", task.params)
    assert list(plan.changes.copy) == ["drive_c/game/Bin/dinput8.dll"]
    assert [a["action"] for a in plan.actions] == ["add", "reg"]
    reg = plan.changes.write["user.reg"].decode()
    assert "[Software\\\\Wine\\\\AppDefaults\\\\Game.exe\\\\DllOverrides]" in reg
    assert reg.startswith(USER_REG.rstrip("\n"))


def test_run_fakeping_rebuilds_and_updates_saves_layer(env):
    settings, image, task = env
    saved_reg = settings.saves_dir / "Jeu" / "user.reg"
    saved_reg.parent.mkdir(parents=True)
    saved_reg.write_text(USER_REG + "[Software\\\\Joueur] 1\n\"x\"=\"y\"\n")
    message = run_files(settings, task, Job())
    assert "registre des sauvegardes" in message
    assert _cat(image, "drive_c/game/Bin/dinput8.dll") == b"MZ fakeping"
    assert b'"dinput8"="native,builtin"' in _cat(image, "user.reg")
    saved = saved_reg.read_text()
    assert '"dinput8"="native,builtin"' in saved and '"x"="y"' in saved
    # Deuxième passage : tout est déjà en place (le .old n'est pas en cause)
    image.with_name(image.name + ".old").unlink()
    assert run_files(settings, task, Job()) == "Déjà à jour : image non reconstruite"


def test_saves_masking_file_is_reported(env):
    settings, image, task = env
    masked = settings.saves_dir / "Jeu" / "drive_c/game/Bin/dinput8.dll"
    masked.parent.mkdir(parents=True)
    masked.write_bytes(b"ancien")
    plan = plan_files(settings, image, "Jeu", "arcade", task.params)
    assert any("masquera" in w for w in plan.warnings)


def test_registry_refused_without_prefix(env, tmp_path):
    settings, _, task = env
    src = tmp_path / "seul"
    src.mkdir()
    (src / "game.exe").write_bytes(b"MZ")
    (src / "autorun.cmd").write_bytes(b"CMD=game.exe\r\n")
    image = tmp_path / "roms" / "win" / "Seul.wsquashfs"
    image.parent.mkdir(parents=True)
    subprocess.run(["mksquashfs", str(src), str(image), "-noappend", "-quiet"], check=True)
    with pytest.raises(RebuildError, match="pas de prefix"):
        plan_files(settings, image, "Seul", "win", task.params)


def test_delete_and_missing_source(env):
    settings, image, _ = env
    plan = plan_files(settings, image, "Jeu", None, {"delete": ["SYSTEM.REG", "absent.txt"]})
    assert plan.changes.delete == ["system.reg"]
    assert any("absent.txt" in w for w in plan.warnings)
    with pytest.raises(RebuildError, match="source introuvable"):
        plan_files(settings, image, "Jeu", None, {"copy": [{"dest": "a", "source": "/nope"}]})


def test_verify(env):
    settings, image, _ = env
    task = Task(id="v", kind="verify", image="arcade/Jeu", image_path=str(image))
    assert run_verify(settings, task, Job()) == "Image intègre, autorun cohérent"
    image.write_bytes(b"pas une image")
    with pytest.raises(RebuildError, match="table"):
        run_verify(settings, task, Job())


def test_reset_saves_renames(env):
    settings, image, _ = env
    with pytest.raises(RebuildError):
        reset_saves(settings, image)
    (settings.saves_dir / "Jeu").mkdir(parents=True)
    renamed = reset_saves(settings, image)
    assert renamed.name.startswith("Jeu.avant-") and renamed.is_dir()
    assert not (settings.saves_dir / "Jeu").exists()


# ------------------------------------------------------------------ surcharge

def test_override_write_effective_and_commit(env):
    from app.services import operations
    from app.services.autorun import override_path
    settings, image, _ = env                 # autorun : DIR=drive_c/GAME/bin, CMD=game.exe
    wanted = Autorun.parse("DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\nGAME_VERSION=1.4\r\nHIDRAW=1\r\n")
    keys = operations.write_override(settings, image, wanted)
    assert keys == ["GAME_VERSION", "HIDRAW"]
    assert override_path(image).read_bytes() == b"GAME_VERSION=1.4\r\nHIDRAW=1\r\n"
    assert operations.effective_autorun(settings, image).get("HIDRAW") == "1"

    # Valeurs effectives dans la bibliothèque, sans toucher au cache de l'image
    info = scan.scan_image(settings, image, "arcade")
    assert info.version is None
    info = scan._apply_dynamic(settings, info, set(), [])
    assert info.override == ["GAME_VERSION", "HIDRAW"]
    assert info.version == "1.4" and info.hidraw is True

    # « Inscrire dans l'image » : reconstruction puis suppression de la surcharge
    params = operations.commit_params(settings, image)
    task = Task(id="c", kind="autorun", image="arcade/Jeu", image_path=str(image), params=params)
    message = operations.run_autorun(settings, task, Job())
    assert "surcharge supprimée" in message and not override_path(image).exists()
    assert _cat(image, "autorun.cmd") == \
        b"DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\nGAME_VERSION=1.4\r\nHIDRAW=1\r\n"


def test_override_removed_when_identical_to_image(env):
    from app.services import operations
    from app.services.autorun import override_path
    settings, image, _ = env
    operations.write_override(settings, image, Autorun.parse("DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\nX=1\r\n"))
    assert override_path(image).exists()
    assert operations.write_override(settings, image,
                                     Autorun.parse("DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\n")) == []
    assert not override_path(image).exists()


def test_mass_ops_start_from_effective_autorun(env):
    from app.services import operations
    settings, image, _ = env
    operations.write_override(settings, image,
                              Autorun.parse("DIR=drive_c/GAME/bin\r\nCMD=game.exe\r\nHIDRAW=1\r\n"))
    plan = operations.plan_autorun(settings, image, "Jeu", "arcade",
                                   {"ops": [{"op": "set", "key": "GAME_VERSION", "value": "2"}]},
                                   validate=False)
    assert b"HIDRAW=1" in plan.after and b"GAME_VERSION=2" in plan.after
