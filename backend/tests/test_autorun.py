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
    assert "CMD" not in a.keys()  # noqa: SIM118 — Autorun.keys(), pas un dict


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

def test_validate_without_dir_looks_at_root_like_launcher():
    a = Autorun.parse("CMD=CT3.exe\n")
    assert a.validate({"CT3.exe", "Media/a.bmp"}) == []
    assert any("CT3.exe introuvable" in i["message"] for i in a.validate({"Media/CT3.exe"}))


def test_validate_cmd_with_path_from_root():
    a = Autorun.parse("DIR=Game\nCMD=Game/bin/g.exe\n")
    assert a.validate({"Game/bin/g.exe"}) == []


def test_validate_missing_cmd_is_error():
    issues = Autorun.parse("DIR=Game\n").validate()
    assert [i["level"] for i in issues if "CMD" in i["message"]] == ["error"]


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
    a = Autorun.parse("CMD=g.exe\nMONKEY=1\n")
    issues = a.validate()
    assert any("clé inconnue : MONKEY" in i["message"] for i in issues)
    assert all(i["level"] == "warn" for i in issues)


# ------------------------------------------------------ corrections de la revue

def test_crlf_without_final_newline_is_preserved():
    text = "DIR=Game\r\nCMD=game.exe"
    a = Autorun.parse(text)
    assert a.eol == "\r\n"
    assert a.get("CMD") == "game.exe"
    assert a.render() == text


def test_mixed_eol_keeps_majority():
    a = Autorun.parse("A=1\r\nB=2\r\nC=3\n")
    assert a.eol == "\r\n"


def test_empty_autorun_renders_empty():
    assert Autorun.parse("").render() == ""


def _path_issues(issues):
    return [i for i in issues if "introuvable" in i["message"] or "n'existe pas" in i["message"]]


def test_quoted_cmd_with_spaces_validates():
    a = Autorun.parse('DIR=My Game\nCMD="Game Launcher.exe" -w\n')
    files = {"My Game/Game Launcher.exe"}
    assert not _path_issues(a.validate(files))


def test_dir_backslashes_and_quotes_are_normalised():
    a = Autorun.parse('DIR="drive_c\\Games\\GTI\\"\nCMD=gti.exe\n')
    files = {"drive_c/games/gti/GTI.EXE"}
    assert not _path_issues(a.validate(files))


def test_missing_exe_is_reported():
    a = Autorun.parse("DIR=Game\nCMD=absent.exe\n")
    assert _path_issues(a.validate({"Game/game.exe"}))


# ------------------------------------------------------------------ opérations

def test_apply_ops_set_remove_keep_layout():
    from app.services.autorun import apply_ops
    a = Autorun.parse("REM jeu\r\nCMD=game.exe\r\nHIDRAW=1\r\n")
    out = apply_ops(a, [{"op": "remove", "key": "HIDRAW"},
                        {"op": "set", "key": "CMD", "value": "other.exe"},
                        {"op": "set", "key": "GAME_VERSION", "value": "{name} 1.0"}],
                    "Jeu", "win")
    assert out.render() == "REM jeu\r\nCMD=other.exe\r\nGAME_VERSION=Jeu 1.0\r\n"
    assert a.render() == "REM jeu\r\nCMD=game.exe\r\nHIDRAW=1\r\n"     # original intact


def test_apply_ops_replace_keeps_eol_of_image():
    from app.services.autorun import apply_ops
    a = Autorun.parse("CMD=a.exe\n")
    out = apply_ops(a, [{"op": "replace", "template": "CMD={name}.exe\r\nDIR={system}"}],
                    "Jeu", "win")
    assert out.render() == "CMD=Jeu.exe\nDIR=win\n"
    assert apply_ops(None, [{"op": "set", "key": "CMD", "value": "x"}], "J", None).render() \
        == "CMD=x\r\n"


def test_decode_autorun_latin1_round_trip():
    from app.services.autorun import decode_autorun
    data = "REM réglé à la main\r\nCMD=jeu.exe\r\n".encode("cp1252")
    text, encoding = decode_autorun(data)
    assert encoding == "latin-1"
    assert Autorun.parse(text).encode(encoding) == data
    assert decode_autorun("REM é\n".encode())[1] == "utf-8"
