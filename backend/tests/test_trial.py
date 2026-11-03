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
    check = subprocess.run(["bash", "-n", str(script)], capture_output=True, check=False)
    assert check.returncode == 0


def _frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def test_demux():
    frame = _frame
    assert trial._demux(frame(1, b"abc") + frame(2, b"de")) == b"abcde"


DOCTOR = """Montage des images
  ✓ /dev/fuse accessible
  ✗ squashfuse : montage de l'image
Wine / Proton
  ✓ runners installés (--wine) :
      wine-tkg  /opt/wine-tkg
      GE-Proton-latest  /config/.config/heroic/tools/proton/GE-Proton-latest
Manettes
  – hidraw : aucune manette Sony (HIDRAW=1 retombe en XInput)

✓ Prêt à lancer des jeux
"""


def test_parse_doctor():
    d = trial.parse_doctor(DOCTOR)
    assert [s["title"] for s in d["sections"]] == ["Montage des images", "Wine / Proton", "Manettes"]
    assert [i["status"] for i in d["sections"][0]["items"]] == ["ok", "missing"]
    assert d["sections"][1]["items"][0]["details"] == [
        "wine-tkg  /opt/wine-tkg",
        "GE-Proton-latest  /config/.config/heroic/tools/proton/GE-Proton-latest"]
    assert d["sections"][2]["items"][0]["status"] == "optional"
    assert d["runners"] == [{"name": "wine-tkg", "path": "/opt/wine-tkg"},
                            {"name": "GE-Proton-latest",
                             "path": "/config/.config/heroic/tools/proton/GE-Proton-latest"}]
    assert d["verdict"] == "✓ Prêt à lancer des jeux"


# ------------------------------------------------------------------ machine de jeu

def _settings(tmp_path, monkeypatch, **env):
    from app import config
    monkeypatch.delenv("WSQUASHFS_MGR_STEAMBOX_CONTAINER", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WSQUASHFS_MGR_RUNTIME_CONFIG_FILE", str(tmp_path / "config.json"))
    return config.get_settings()


def test_game_host_off_by_default_docker_if_container_configured(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch).game_host == "off"
    s = _settings(tmp_path, monkeypatch, WSQUASHFS_MGR_STEAMBOX_CONTAINER="SteamBox")
    assert s.game_host == "docker"
    with pytest.raises(RebuildError, match="désactivée"):
        trial.game_host(_settings(tmp_path, monkeypatch))


def test_save_game_host_persists_and_validates(tmp_path, monkeypatch):
    from app import config
    s = _settings(tmp_path, monkeypatch)
    config.save_game_host(s, {"game_host": "local", "session_process": ""})
    again = _settings(tmp_path, monkeypatch)
    assert again.game_host == "local" and again.session_process == ""
    with pytest.raises(ValueError):
        config.save_game_host(s, {"game_host": "docker", "docker_host": "docker-socket-proxy"})
    with pytest.raises(ValueError):
        config.save_game_host(s, {"game_host": "ssh"})


def test_local_host_exec_stream_and_paths(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    s.game_host = "local"
    host = trial.game_host(s)
    assert host.exec(["sh", "-c", "echo out; echo err >&2; exit 3"]) == (3, b"out\nerr\n")
    assert host.exec(["commande-qui-n-existe-pas"])[0] == 127
    chunks = []
    code, err = host.exec_stream(["sh", "-c", "printf abc; echo oups >&2"], chunks.append)
    assert (code, b"".join(chunks), err) == (0, b"abc", b"oups\n")
    image = Path("/n/importe/ou/Jeu.wsquashfs")
    assert host.path_of(image) == str(image)      # pas de correspondance de chemins en local


def test_local_listing_and_fetch(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    s.game_host = "local"
    runner = tmp_path / "runner"
    (runner / "files/bin").mkdir(parents=True)
    (runner / "files/bin/wine").write_bytes(b"x" * 10)
    (runner / "proton").symlink_to("files/bin/wine")
    assert trial.steambox_listing(s, str(runner)) == {"files": 0, "files/bin": 0, "files/bin/wine": 10, "proton": 0}
    dest = tmp_path / "copie"
    trial.steambox_fetch(s, str(runner), dest)
    assert (dest / "files/bin/wine").read_bytes() == b"x" * 10
    assert (dest / "proton").is_symlink()
