"""Édition minimale des fichiers de registre Wine (`user.reg`, `system.reg`).

Format (« WINE REGISTRY Version 2 ») :

    [Software\\\\Wine\\\\DllOverrides] 1695000000
    #time=1d9e3c0a1b2c3d4
    "d3d9"="native"

Seule une valeur chaîne est posée ; tout le reste du fichier (autres clés,
ordre, commentaires, fins de ligne) est conservé tel quel. Les chemins de
clé et les noms de valeur sont comparés sans tenir compte de la casse,
comme Windows. Le fichier est lu et réécrit en latin-1 : Wine n'y écrit que
de l'ASCII (le reste est échappé en \\x…), l'aller-retour est donc exact.
"""

from __future__ import annotations

import re
import time

_SECTION_RE = re.compile(r"^\[(.*)\](\s+\d+)?\s*$")
_VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"\s*$')


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _key_in_file(key: str) -> str:
    """« Software\\Wine\\DllOverrides » → forme du fichier (\\\\ doublés)."""
    return "\\\\".join(part for part in key.replace("\\\\", "\\").split("\\") if part)


def _filetime(now: float) -> str:
    return format(int((now + 11644473600) * 10**7), "x")


def get_value(text: str, key: str, name: str) -> str | None:
    """Valeur brute (échappée, sans guillemets) ou None."""
    section = _key_in_file(key).lower()
    current = None
    prefix = f'"{_escape(name)}"='.lower()
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).lower()
        elif current == section and line.lower().startswith(prefix):
            raw = line[len(prefix):]
            return raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
    return None


def section_values(text: str, key: str) -> dict[str, str]:
    """Valeurs chaîne d'une section : {nom: valeur} (échappements retirés)."""
    section = _key_in_file(key).lower()
    current = None
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).lower()
            continue
        if current != section:
            continue
        v = _VALUE_RE.match(line)
        if v:
            out[_unescape(v.group(1))] = _unescape(v.group(2))
    return out


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def set_value(text: str, key: str, name: str, value: str, now: float | None = None) -> str:
    """Pose `"name"="value"` dans la section `key` (créée si absente)."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    section = _key_in_file(key)
    entry = f'"{_escape(name)}"="{_escape(value)}"'
    prefix = f'"{_escape(name)}"='.lower()

    start = next((i for i, line in enumerate(lines)
                  if (m := _SECTION_RE.match(line)) and m.group(1).lower() == section.lower()),
                 None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")),
                   len(lines))
        for i in range(start + 1, end):
            if lines[i].lower().startswith(prefix):
                lines[i] = entry
                return eol.join(lines)
        insert = start + 1
        while insert < end and lines[insert].startswith("#"):
            insert += 1               # après #time=…
        lines.insert(insert, entry)
        return eol.join(lines)

    now = time.time() if now is None else now
    block = [f"[{section}] {int(now)}", f"#time={_filetime(now)}", entry, ""]
    while lines and lines[-1] == "":
        lines.pop()
    return eol.join([*lines, "", *block])


def prefix_reg(files: list[str], name: str = "user.reg") -> str | None:
    """Chemin du fichier de registre dans l'image : racine (prefix Batocera)
    ou pfx/ (prefix Proton). None pour un jeu seul (prefix créé au lancement)."""
    present = set(files)
    for candidate in (name, f"pfx/{name}"):
        if candidate in present:
            return candidate
    return None
