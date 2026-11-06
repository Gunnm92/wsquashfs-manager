"""Lancement d'essai : rapport, verdicts, chemins, script envoyé à SteamBox."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services import trial
from app.services.rebuild import RebuildError

REPORT = {
    "code": 0, "elapsed": 60, "launcher_alive": True, "game_seen": True,
    "games": ["X:\\.cache\\wsquashfs\\wine\\Astebreed\\Astebreed.exe"],
    "windows": [{"id": "0x2000001", "name": "Astebreed [Version 3.02]",
                 "state": "_NET_WM_STATE_FULLSCREEN, _NET_WM_STATE_HIDDEN"}],
    "left": {"procs": 0, "mounts": 0}, "screenshot": True,
    "log": ("Jeu           : Astebreed\n013c:err:seh:NtRaiseException Unhandled exception code c0000005\n"
            "Assert!!(0 - shader phong.cfx Invalid call)\n"),
}


def test_report_and_minimized_verdict():
    r = trial.report_from_json(REPORT)
    assert r.elapsed == 60 and r.launcher_alive and r.game_seen
    assert r.windows[0]["name"] == "Astebreed [Version 3.02]"
    assert {e["meaning"] for e in r.errors} >= {"assertion du jeu", "exception non gérée"}
    ok, why = r.verdict
    assert not ok and "minimisée" in why


def test_verdicts():
    stopped = trial.report_from_json({"elapsed": 7, "launcher_alive": False, "game_seen": True, "log": ""})
    assert stopped.verdict == (False, "le jeu s'est arrêté au bout de 7 s")
    nothing = trial.report_from_json({"elapsed": 60, "launcher_alive": True, "game_seen": False, "log": ""})
    assert nothing.verdict[1] == "aucun processus de jeu vu"
    fine = trial.report_from_json({"elapsed": 60, "launcher_alive": True, "game_seen": True,
                                   "windows": [{"id": "0x1", "name": "Jeu", "state": "_NET_WM_STATE_FULLSCREEN"}],
                                   "log": ""})
    assert fine.verdict == (True, "le jeu a tenu 60 s")


# ------------------------------------------------------------------ machine de jeu

def _settings(tmp_path, monkeypatch, **env):
    from app import config
    monkeypatch.delenv("WSQUASHFS_MGR_STEAMBOX_CONTAINER", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WSQUASHFS_MGR_RUNTIME_CONFIG_FILE", str(tmp_path / "config.json"))
    return config.get_settings()


def test_game_host_off_by_default_and_legacy_docker_becomes_off(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch).game_host == "off"
    (tmp_path / "config.json").write_text('{"game_host": "docker", "steambox_container": "SteamBox"}')
    assert _settings(tmp_path, monkeypatch).game_host == "off"
    with pytest.raises(RebuildError, match="désactivée"):
        trial.game_host(_settings(tmp_path, monkeypatch))


def test_save_game_host_persists_and_validates(tmp_path, monkeypatch):
    from app import config
    s = _settings(tmp_path, monkeypatch)
    config.save_game_host(s, {"game_host": "local", "session_process": ""})
    again = _settings(tmp_path, monkeypatch)
    assert again.game_host == "local" and again.session_process == ""
    with pytest.raises(ValueError):
        config.save_game_host(s, {"game_host": "docker"})
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
