"""Édition des .reg Wine : seule la valeur visée change."""

from __future__ import annotations

from app.services.registry import get_value, prefix_reg, set_value

REG = (
    "WINE REGISTRY Version 2\n"
    ";; All keys relative to REGISTRY\\\\User\\\\S-1-5-21-0-0-0-1000\n"
    "\n#arch=win64\n\n"
    "[Software\\\\Wine\\\\AppDefaults\\\\game.exe\\\\DllOverrides] 1698115281\n"
    "#time=1da062392818782\n"
    '"atiadlxx"="builtin"\n'
    "\n"
    "[Software\\\\Wine\\\\Drivers] 1698115281\n"
    '"Audio"="pulse"\n'
)
KEY = "Software\\Wine\\AppDefaults\\game.exe\\DllOverrides"


def test_adds_value_in_existing_section_after_time():
    out = set_value(REG, KEY, "dinput8", "native,builtin")
    assert out == REG.replace('#time=1da062392818782\n',
                              '#time=1da062392818782\n"dinput8"="native,builtin"\n')
    assert get_value(out, KEY.upper(), "DINPUT8") == "native,builtin"


def test_replaces_existing_value_case_insensitive():
    out = set_value(REG, KEY.lower(), "ATIADLXX", "native")
    assert '"ATIADLXX"="native"' in out and '"atiadlxx"' not in out
    assert out.count("\n") == REG.count("\n")


def test_creates_missing_section_at_end():
    out = set_value(REG, "Software\\Wine\\AppDefaults\\other.exe\\DllOverrides", "dinput8",
                    "native,builtin", now=1_700_000_000)
    assert out.startswith(REG.rstrip("\n"))
    assert out.endswith(
        "\n\n[Software\\\\Wine\\\\AppDefaults\\\\other.exe\\\\DllOverrides] 1700000000\n"
        "#time=1da1747c66d0000\n"
        '"dinput8"="native,builtin"\n')


def test_keeps_crlf_and_escapes():
    reg = REG.replace("\n", "\r\n")
    out = set_value(reg, "Software\\Test", 'a"b', "c:\\d")
    assert "\n" not in out.replace("\r\n", "")
    assert '"a\\"b"="c:\\\\d"' in out


def test_prefix_reg_location():
    assert prefix_reg(["user.reg", "pfx/user.reg"]) == "user.reg"
    assert prefix_reg(["config_info", "pfx", "pfx/user.reg"]) == "pfx/user.reg"
    assert prefix_reg(["game.exe"]) is None
