"""Tests de l'autorun : mêmes conventions que wsquashfs-launcher (SPEC § 3.2).

Couvre :
- lecture : clé en début de ligne, première occurrence, \r retiré ;
- réécriture ligne à ligne : commentaires, ordre, CRLF/LF conservés ;
- modification en place vs ajout en fin ;
- détection des clés inconnues ;
- avertissement ENV=WINEDLLOVERRIDES ;
- validation DIR=/CMD= (casse ignorée).
"""

from __future__ import annotations

from app.services.autorun import KNOWN_KEYS, Autorun


# ------------------------------------------------------------------ lecture

def test_parse_crlf_and_get():
    text = "REM configuration\r\nDIR=Game\r\nCMD=game.exe -fullscreen\r\nGAME_VERSION=1.0\r\n"
    a = Autorun.parse(text)
    assert a.eol == "\r\n"
    assert a.get("DIR") == "Game"
    assert a.get("CMD") == "game.exe -fullscreen"
    assert a.get("GAME_VERSION") == "1.0"


def test_parse_lf():
    text = "DIR=Game\nCMD=game.exe\n"
    a = Autorun.parse(text)
    assert a.eol == "\n"
    assert a.get("CMD") == "game.exe"


def test_first_occurrence_wins():
    a = Autorun.parse("HIDRAW=0\nHIDRAW=1\n")
    assert a.get("HIDRAW") == "0"
    assert a.all("HIDRAW") == ["0", "1"]


def test_key_only_at_line_start():
    a = Autorun.parse("  DIR=indented\nDIR=ok\nREM CMD=comment\n")
    assert a.get("DIR") == "ok"
    assert "CMD" not in a.keys()


def test_unknown_keys():
    a = Autorun.parse("DIR=Game\nCMD=g.exe\nMONKEY=1\n")
    assert a.unknown_keys() == ["MONKEY"]
    assert "MONKEY" not in KNOWN_KEYS


# ------------------------------------------------------------------- écriture

def test_set_modifies_in_place_preserving_comments_and_order():
    text = "REM tete\nDIR=Game\nCMD=game.exe\n# commentaire\nWINE=9.0\n"
    a = Autorun.parse(text)
    a.set("WINE", "tkg")
    out = a.render()
    lines = out.split("\n")
    assert lines[0] == "REM tete"
    assert lines[2] == "CMD=game.exe"
    assert "WINE=tkg" in lines
    # la clé garde sa place : WINE reste après le commentaire
    assert lines.index("WINE=tkg") > lines.index("# commentaire")


def test_set_appends_new_key_at_end():
    a = Autorun.parse("DIR=Game\nCMD=g.exe\n")
    a.set("GAME_VERSION", "2.0")
    assert a.lines[-1] == "GAME_VERSION=2.0"


def test_remove_key():
    a = Autorun.parse("DIR=Game\nHIDRAW=1\nCMD=g.exe\n")
    assert a.remove("HIDRAW") == 1
    assert a.get("HIDRAW") is None
    assert a.remove("HIDRAW") == 0


def test_crlf_roundtrip():
    a = Autorun.parse("DIR=Game\r\nCMD=g.exe\r\n")
    a.set("DXVK", "1")
    assert a.render() == "DIR=Game\r\nCMD=g.exe\r\nDXVK=1\r\n"


def test_lf_roundtrip():
    a = Autorun.parse("DIR=Game\nCMD=g.exe\n")
    a.set("DXVK", "1")
    assert a.render() == "DIR=Game\nCMD=g.exe\nDXVK=1\n"


# ---------------------------------------------------------------- validation

def test_validate_missing_dir():
    a = Autorun.parse("CMD=g.exe\n")
    issues = a.validate()
    assert any(i["message"].startswith("DIR=") for i in issues)


def test_validate_exe_not_found_case_insensitive():
    a = Autorun.parse("DIR=Game\nCMD=Game.exe\n")
    files = {"Game/GAME.exe", "Game/data.bin"}
    issues = a.validate(files)
    # casse ignorée : Game.exe == GAME.exe → pas d'erreur exécutable
    assert not any("exécutable" in i["message"] for i in issues)


def test_validate_exe_missing():
    a = Autorun.parse("DIR=Game\nCMD=missing.exe\n")
    files = {"Game/other.exe"}
    issues = a.validate(files)
    assert any("exécutable missing.exe" in i["message"] for i in issues)


def test_validate_winedlloversides_env():
    a = Autorun.parse('ENV="WINEDLLOVERRIDES=dinput8=n"\n')
    issues = a.validate()
    assert any("WINEDLLOVERRIDES" in i["message"] for i in issues)


def test_validate_unknown_key_warns_not_blocks():
    a = Autorun.parse("MONKEY=1\n")
    issues = a.validate()
    assert any("clé inconnue : MONKEY" in i["message"] for i in issues)
    assert all(i["level"] == "warn" for i in issues)
