"""Lancement d'essai : rapport, verdicts, chemins, script envoyé à SteamBox."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.services import trial
from app.services.rebuild import RebuildError

OUTPUT = """@@ELAPSED 60
@@LAUNCHER 1
@@GAMESEEN 1
@@GAME X:\\.cache\\wsquashfs\\wine\\Astebreed\\Astebreed.exe
@@WINDOW 0x2000001|Astebreed [Version 3.02]|_NET_WM_STATE_FULLSCREEN, _NET_WM_STATE_HIDDEN
@@LEFT 0 0
@@LOG
Jeu           : Astebreed
013c:err:seh:NtRaiseException Unhandled exception code c0000005
Assert!!(0 - shader phong.cfx Invalid call)
"""


def test_parse_report_and_minimized_verdict():
    r = trial.parse_report(OUTPUT)
    assert r.elapsed == 60 and r.launcher_alive and r.game_seen
    assert r.windows[0]["name"] == "Astebreed [Version 3.02]"
    assert {e["meaning"] for e in r.errors} >= {"assertion du jeu", "exception non gérée"}
    ok, why = r.verdict
    assert not ok and "minimisée" in why


def test_verdicts():
    stopped = trial.parse_report("@@ELAPSED 7\n@@LAUNCHER 0\n@@GAMESEEN 1\n@@LOG\n")
    assert stopped.verdict == (False, "le jeu s'est arrêté au bout de 7 s")
    nothing = trial.parse_report("@@ELAPSED 60\n@@LAUNCHER 1\n@@GAMESEEN 0\n@@LOG\n")
    assert nothing.verdict[1] == "aucun processus de jeu vu"
    fine = trial.parse_report("@@ELAPSED 60\n@@LAUNCHER 1\n@@GAMESEEN 1\n"
                              "@@WINDOW 0x1|Jeu|_NET_WM_STATE_FULLSCREEN\n@@LOG\n")
    assert fine.verdict == (True, "le jeu a tenu 60 s")


def test_steambox_path():
    s = Settings()
    assert trial.steambox_path(s, Path("/roms/win/Jeu X.wsquashfs")) == \
        "/home/arcade/games/Batocera/roms/win/Jeu X.wsquashfs"
    assert trial.steambox_path(s, Path("/config/userdata/roms/win/A.wsquashfs")).endswith(
        "/Batocera/roms/win/A.wsquashfs")
    with pytest.raises(RebuildError):
        trial.steambox_path(s, Path("/ailleurs/A.wsquashfs"))


def test_script_is_valid_bash(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(trial._SCRIPT)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True, check=False).returncode == 0


def _frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def test_demux():
    frame = _frame
    assert trial._demux(frame(1, b"abc") + frame(2, b"de")) == b"abcde"
