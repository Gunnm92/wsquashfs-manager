"""Tests du moteur de règles et du fichier rules.yaml livré."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.rules import Analysis, RuleError, load_rules, run_rules, validate_rules

RULES = load_rules(Path(__file__).resolve().parent.parent / "rules" / "rules.yaml")


def _sources(a: Analysis) -> set[str]:
    return {r.source for r in run_rules(RULES, a)}


def test_shipped_rules_load():
    assert RULES


def test_unknown_condition_rejected_at_load():
    with pytest.raises(RuleError):
        validate_rules([{"name": "x", "when": {"bidule": 1}, "then": {"warning": "w"}}])


def test_batocera_win32_prefix_does_not_crash():
    a = Analysis(files=["system.reg"], prefix_type="batocera", arch="win32")
    results = run_rules(RULES, a)
    assert any(r.key == "ARCH" and r.value == "win32" for r in results)


def test_imports_with_dll_suffix():
    a = Analysis(files=[], imports={"D3D11.DLL"})
    assert "dxvk-d3d" in _sources(a)


def test_root_level_file_matches_basename_pattern():
    a = Analysis(files=["libScePad.dll"])
    assert "hidraw-sony" in _sources(a)


def test_hidraw_not_proposed_with_steam_api():
    a = Analysis(files=["game/libScePad.dll", "game/steam_api64.dll"])
    sources = _sources(a)
    assert "hidraw-sony" not in sources
    assert "steam-api-pas-hidraw" in sources


def test_fakeping_without_env_override():
    a = Analysis(files=["drive_c/game/libavs-win32.dll", "drive_c/game/game.exe"],
                 imports={"dinput8.dll"}, exe_name="game.exe", exe_dir="drive_c/game")
    r = next(r for r in run_rules(RULES, a) if r.source == "konami-fakeping")
    assert r.key is None
    assert r.file == "drive_c/game/dinput8.dll"
    assert "AppDefaults\\game.exe\\DllOverrides" in r.warning


def test_tekno_d3dx9_only_when_native():
    base = dict(files=["TeknoParrotUi.exe"], exe_name="TeknoParrotUi.exe")
    assert "tekno-d3dx9-43" not in _sources(Analysis(**base))
    assert "tekno-d3dx9-43" not in _sources(
        Analysis(**base, dll_overrides={"*d3dx9_43": "builtin"}))
    assert "tekno-d3dx9-43" in _sources(Analysis(**base, dll_overrides={"d3dx9_43": "native"}))
    assert "tekno-d3dx9-43" in _sources(
        Analysis(**base, dll_overrides={"*d3dx9_43": "native,builtin"}))


def test_tekno_old_only_when_version_known_and_lower():
    base = dict(files=[], exe_name="TeknoParrotUi.exe")
    assert "tekno-ancien" not in _sources(Analysis(**base))
    assert "tekno-ancien" in _sources(Analysis(**base, tekno_version="1.0.0.90"))
    assert "tekno-ancien" not in _sources(Analysis(**base, tekno_version="1.0.0.200"))


def test_no_placeholder_values():
    a = Analysis(files=["config_info"], prefix_type="proton")
    for r in run_rules(RULES, a):
        assert r.value is None or "(" not in r.value
