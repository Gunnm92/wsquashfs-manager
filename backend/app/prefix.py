"""Lecture d'un prefix Wine / Proton (SPEC § 2.5).

- `system.reg` : prefix Batocera/Wine ; `#arch=win32`/`win64` dans les
  premières lignes ;
- `config_info` : prefix Proton ; `PROTON_VERSION=…` (et à défaut la version
  dans `VulkanLayerPath`) ;
- `user.reg` : sections `[Software\\Wine\\DllOverrides]` et
  `[Software\\Wine\\AppDefaults\\<exe>\\DllOverrides]` → dictionnaire
  dll → valeur ("native", "builtin", "disabled"…).

Tous les fichiers sont lus en texte tolérant ; un fichier absent donne des
valeurs None, pas une erreur.
"""

from __future__ import annotations

import re
from pathlib import Path

_ARCH_RE = re.compile(r"^#arch=(\w+)", re.IGNORECASE)
_PROTON_VERSION_RE = re.compile(r"^PROTON_VERSION=(.+?)\s*$", re.IGNORECASE)
_VULKAN_LAYER_PATH_RE = re.compile(r"^VulkanLayerPath=(.+?)\s*$", re.IGNORECASE)

_SECTION_RE = re.compile(r"^\[([^\]]*)\]\s*$")
_REG_ENTRY_RE = re.compile(r'^"([^"]*)"\s*=\s*"(.*)"\s*$')


def detect_prefix(files: list[str], read: callable) -> dict:
    """Analyse les fichiers de RACINE d'un prefix.

    `files` : tous les fichiers (chemins relatifs) ; `read` : fonction
    (nom_fichier_racine) -> bytes|None. Retourne
    {type: batocera|proton|other|none, arch, proton_version}."""
    root = {f.lower() for f in files if "/" not in f}
    if "config_info" in root:
        d = {"type": "proton"}
    elif "system.reg" in root:
        d = {"type": "batocera"}
    else:
        return {"type": "none", "arch": None, "proton_version": None}

    arch: str | None = None
    proton: str | None = None
    if "system.reg" in root or d["type"] == "batocera":
        data = read("system.reg")
        if data:
            for line in data.decode("utf-8", errors="replace").splitlines()[:20]:
                m = _ARCH_RE.match(line.strip())
                if m:
                    arch = m.group(1).lower()
                    break
    if d["type"] == "proton":
        data = read("config_info")
        if data:
            for line in data.decode("utf-8", errors="replace").splitlines():
                m = _PROTON_VERSION_RE.match(line.strip())
                if m:
                    proton = m.group(1)
                    break
                if proton is None:
                    m = _VULKAN_LAYER_PATH_RE.match(line.strip())
                    if m:
                        # .../GE-Proton11-48/share/libreoffice... → GE-Proton11-48
                        for comp in m.group(1).split("/"):
                            if re.match(r"^[GP]-?Proton", comp):
                                proton = comp
                                break
    d["arch"] = arch
    d["proton_version"] = proton
    return d


def parse_dll_overrides(reg_text: str) -> dict[str, str]:
    """Toutes les sections DllOverrides d'un .reg → {dll: valeur}.

    La première occurrence d'une DLL gagne (comme l'autorun) ; le `*` global
    est conservé dans la clé. Clés minuscules."""
    out: dict[str, str] = {}
    section = ""
    for line in reg_text.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            section = m.group(1).strip().lower()
            continue
        if not section.endswith("dlloverrides"):
            continue
        m = _REG_ENTRY_RE.match(line.strip())
        if not m:
            continue
        dll = m.group(1).strip().lower()
        if dll and dll not in out:
            out[dll] = m.group(2)
    return out


def read_prefix_file(base: Path, name: str) -> bytes | None:
    """Lit un fichier à la racine d'un prefix (monté ou dossier)."""
    p = base / name
    if p.is_file():
        try:
            return p.read_bytes()
        except OSError:
            return None
    return None
