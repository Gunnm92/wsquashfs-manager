"""Liens du prefix vers un runner hors de l'image (ou absent de l'image).

Dans un prefix créé par Proton, les DLL de Wine sont des liens vers le
runner (`.../GE-Proton-latest/files/lib/wine/x86_64-windows/d3d11.dll`).
Pointant hors de l'image, ils dépendent d'un runner installé sur la machine :
cassés ailleurs ou quand ce runner change, et Proton ne répare pas un lien
cassé (« failed to open umu.exe »). Vérifié sur la ludothèque le 01/11 :
Kingdom Hearts III en a 1350 vers le GE-Proton-latest de Heroic.

Ne sont PAS signalés (inoffensifs, vus sur 62 images Batocera) :
- `dosdevices/` (lecteurs, ports) ;
- les liens vers `/usr/wine/` (runners Batocera, remplacés par DXVK) ;
- les dossiers utilisateur (Documents → /userdata/system…).

Un lien est corrigeable si le runner est joint à l'image (`runners/<nom>/`)
et contient le même fichier : il est alors réécrit en relatif à
l'empaquetage, sans toucher au dossier source (lien pseudo de mksquashfs).
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Cible typique d'un fichier de runner Proton/Wine hors de l'image
_RUNNER_TARGET_RE = re.compile(
    r"(compatibilitytools\.d/|heroic/tools/(proton|wine)/|/files/(lib|lib64|share)/wine/)", re.IGNORECASE)
_BATOCERA_RE = re.compile(r"^/usr/wine/")
_LLS_LINK_RE = re.compile(r"^l\S*\s+\S+\s+\d+\s+\S+\s+\S+\s+squashfs-root/(.+?) -> (.*)$")


@dataclass
class LinkReport:
    fixable: dict[str, str] = field(default_factory=dict)   # lien → nouvelle cible relative
    external: list[tuple[str, str]] = field(default_factory=list)   # (lien, cible) hors de l'image
    missing: list[tuple[str, str]] = field(default_factory=list)    # (lien, cible) absente de l'image
    runners: list[str] = field(default_factory=list)        # runners hors image visés

    @property
    def count(self) -> int:
        return len(self.fixable) + len(self.external) + len(self.missing)

    def issues(self, image: bool = False) -> list[dict]:
        """Remarques pour un dossier à empaqueter, ou une image (`image`) :
        dans une image, un lien que runners/ aurait pu servir ne se corrige
        plus qu'en réempaquetant."""
        out = []
        if self.fixable and image:
            out.append({"level": "error", "message": (
                f"{len(self.fixable)} lien(s) du prefix vers un runner hors de l'image alors "
                f"qu'il est joint dans runners/ : ailleurs, le jeu ne démarre pas (« failed to "
                f"open umu.exe »). Réempaqueter depuis le dossier : ils seront réécrits")})
        elif self.fixable:
            out.append({"level": "warn", "message": (
                f"{len(self.fixable)} lien(s) du prefix vers un runner hors du dossier, "
                f"présent dans runners/ : réécrits en relatif dans l'image (dossier inchangé)")})
        if self.external:
            names = ", ".join(self.runners[:3]) or "runner de la machine"
            out.append({"level": "warn", "message": (
                f"{len(self.external)} lien(s) du prefix vers {names}, hors de l'image : "
                f"l'image dépend de ce runner installé (cassée ailleurs ou s'il change ; "
                f"Proton ne répare pas un lien cassé). Joindre le runner dans runners/ "
                f"pour les réécrire, ex. {self.external[0][0]} → {self.external[0][1]}")})
        if self.missing:
            rel, target = self.missing[0]
            out.append({"level": "warn", "message": (
                f"{len(self.missing)} lien(s) du prefix vers des fichiers absents de l'image "
                f"(liens cassés, ex. {rel} → {target}) : sans effet tant que le jeu n'en a "
                f"pas besoin, sinon Proton ne les recrée pas")})
        return out

    def to_dict(self) -> dict:
        return {"fixable": len(self.fixable), "external": len(self.external),
                "missing": len(self.missing), "runners": self.runners,
                "examples": [{"link": k, "target": t}
                             for k, t in [*self.external, *self.missing][:5]]}


def _dangling(rel: str, target: str, files: set[str]) -> str | None:
    """"external" : cible hors de l'image (absolue, ou relative qui en sort) ;
    "missing" : dans l'image mais absente (ancien `runner/` renommé
    `runners/`, fichier manquant du runner joint) ; None : lien valide."""
    if target.startswith("/"):
        return None if _BATOCERA_RE.match(target) else "external"
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if resolved == ".." or resolved.startswith("../"):
        return "external"
    return "missing" if files and resolved not in files else None


def _runner_name(target: str) -> str | None:
    """Nom du runner visé : dossier contenant `files/` (Proton) ou `lib/wine`."""
    parts = [p for p in target.split("/") if p not in ("", ".", "..")]
    for i, part in enumerate(parts[1:], start=1):
        if part == "files" or (part in ("lib", "lib64") and i + 1 < len(parts)
                               and parts[i + 1] == "wine"):
            return parts[i - 1]
    return None


def classify(links: Iterable[tuple[str, str]], files: Iterable[str]) -> LinkReport:
    """Liens (relatif à la racine, cible) → rapport. `files` : chemins de
    l'image ou du dossier (pour trouver le même fichier sous runners/)."""
    files = set(files)
    joined: dict[str, str] = {}            # nom en minuscules → nom réel sous runners/
    for f in files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[0] == "runners":
            joined.setdefault(parts[1].lower(), parts[1])
    report = LinkReport()
    runners: dict[str, None] = {}
    for rel, target in links:
        first = rel.split("/", 1)[0].lower()
        if first.startswith("dosdevices") or first == "runners":
            continue
        kind = _RUNNER_TARGET_RE.search(target) and _dangling(rel, target, files)
        if not kind:
            continue
        name = _runner_name(target)
        fixed = None
        if name:
            parts = [p for p in target.split("/") if p]
            suffix = "/".join(parts[parts.index(name) + 1:])
            # Runner joint du même nom, sinon le seul runner joint
            real = joined.get(name.lower()) or (next(iter(joined.values()))
                                                if len(joined) == 1 else None)
            if real and f"runners/{real}/{suffix}" in files:
                fixed = posixpath.relpath(f"runners/{real}/{suffix}", posixpath.dirname(rel) or ".")
        if fixed:
            report.fixable[rel] = fixed
        elif kind == "missing":
            report.missing.append((rel, target))
        else:
            report.external.append((rel, target))
            runners[name or target.rsplit("/", 1)[0]] = None
    report.runners = list(runners)
    return report


def folder_links(root: Path) -> list[tuple[str, str]]:
    """Liens symboliques d'un dossier (non suivis), relatifs à sa racine."""
    out = []
    for cur, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(cur, root)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for name in [*dirnames, *filenames]:
            path = os.path.join(cur, name)
            if os.path.islink(path):
                try:
                    out.append((prefix + name, os.readlink(path)))
                except OSError:
                    continue
    return out


def image_links(image: Path, unsquashfs: str = "unsquashfs") -> list[tuple[str, str]]:
    """Liens symboliques d'une image, d'après sa table (unsquashfs -lls)."""
    r = subprocess.run([unsquashfs, "-lls", str(image)], capture_output=True, timeout=600,
                       check=False)
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        m = _LLS_LINK_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


# ------------------------------------------------------------- empaquetage

def _wildcard_escape(path: str) -> str:
    """Chemin littéral pour un fichier d'exclusion de mksquashfs -wildcards."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", path)


def _pseudo_quote(path: str) -> str:
    """Nom pour une définition pseudo de mksquashfs (guillemets, échappements)."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def relink_options(fixable: dict[str, str], work: Path, uid: int, gid: int,
                   base: str = "") -> list[str]:
    """Options mksquashfs remplaçant les liens `fixable` par leur cible
    relative : exclusion du lien d'origine (-wildcards -ef) puis lien pseudo
    (-pf) au même endroit. `base` : préfixe des chemins dans l'image quand
    le dossier n'est pas la racine (sources multiples)."""
    if not fixable:
        return []
    exclude = work / "relink.exclude"
    pseudo = work / "relink.pseudo"
    exclude.write_text("".join(_wildcard_escape(base + rel) + "\n" for rel in fixable),
                       encoding="utf-8")
    pseudo.write_text("".join(f"{_pseudo_quote(rel)} s 777 {uid} {gid} {target}\n"
                              for rel, target in fixable.items()), encoding="utf-8")
    return ["-wildcards", "-ef", str(exclude), "-pf", str(pseudo)]
