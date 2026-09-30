"""Lecture / écriture de l'`autorun.cmd`.

Conventions identiques au lanceur (SPEC § 3.2) :
- clé en début de ligne, forme `CLÉ=Valeur` ;
- première occurrence retenue ;
- le `\r` final de ligne est retiré à la lecture ;
- la réécriture est LIGNE À LIGNE : commentaires (REM, #), ordre des lignes,
  fins de ligne (CRLF/LF détectées) et espaces sont conservés ;
- une clé modifiée garde sa place ; une clé ajoutée va en fin de fichier ;
- les clés inconnues sont signalées mais non bloquées (Batocera les ignore).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Clés connues du lanceur / Batocera (SPEC § 2.2).
KNOWN_KEYS = {
    "CMD", "DIR", "GAME_VERSION", "WINE", "PROTON", "RUNNER", "ARCH", "HIDRAW",
    "DXVK", "VKD3D", "D7VK", "ESYNC", "FSYNC", "VIRTUAL_DESKTOP", "LANG",
    "ENV", "SAVEDIR", "SAVEFILES",
}

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass
class Autorun:
    """Contenu d'un autorun, ligne à ligne."""
    lines: list[str] = field(default_factory=list)   # sans le caractère de fin de ligne
    eol: str = "\r\n"                                # "\r\n" ou "\n" (détecté)

    # ------------------------------------------------------------------ read

    @classmethod
    def parse(cls, data: bytes | str) -> "Autorun":
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        raw_lines = data.splitlines()
        # Détection de CRLF : la majorité des lignes se terminant par \r
        # (après splitlines sur le texte brut) → CRLF.
        text = data
        crlf = len(text.splitlines(keepends=True)) > 0 and all(
            line.endswith("\r\n") for line in text.splitlines(keepends=True) if line
        ) if text else False
        eol = "\r\n" if crlf else "\n"
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n") if ln or True]
        # Suppression de la dernière ligne vide due au \n final
        if lines and lines[-1] == "" and not text.endswith("\r"):
            lines = lines[:-1]
        return cls(lines=lines, eol=eol)

    def get(self, key: str) -> str | None:
        """Première occurrence de la clé (comme le lanceur)."""
        for line in self.lines:
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                return m.group(2).rstrip("\r")
        return None

    def all(self, key: str) -> list[str]:
        return [
            m.group(2).rstrip("\r")
            for line in self.lines
            if (m := _KEY_RE.match(line)) and m.group(1) == key
        ]

    def items(self) -> list[tuple[str, str, int]]:
        """Toutes les occurrences (clé, valeur, ligne 1-indexée), dans l'ordre."""
        out = []
        for i, line in enumerate(self.lines, start=1):
            m = _KEY_RE.match(line)
            if m:
                out.append((m.group(1), m.group(2).rstrip("\r"), i))
        return out

    def keys(self) -> set[str]:
        return {k for k, _, _ in self.items()}

    def unknown_keys(self) -> list[str]:
        return [k for k in sorted(self.keys()) if k not in KNOWN_KEYS]

    # ---------------------------------------------------------------- write

    def set(self, key: str, value: str) -> None:
        """Modifier la première occurrence (place conservée) ou ajouter en fin."""
        for i, line in enumerate(self.lines):
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                self.lines[i] = f"{key}={value}"
                return
        self.lines.append(f"{key}={value}")

    def remove(self, key: str) -> int:
        """Retirer toutes les occurrences de la clé. Retourne le nombre retiré."""
        kept = []
        removed = 0
        for line in self.lines:
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                removed += 1
            else:
                kept.append(line)
        self.lines = kept
        return removed

    def render(self) -> str:
        return self.eol.join(self.lines) + (self.eol if self.lines else "")

    # ------------------------------------------------------------- validation

    @staticmethod
    def is_comment(line: str) -> bool:
        stripped = line.lstrip()
        return stripped.startswith("REM") or stripped.startswith("#")

    def validate(self, file_paths: set[str] | None = None) -> list[dict]:
        """Validations (SPEC § 2.2). `file_paths` = fichiers de l'image (casse
        insensible), pour vérifier DIR= et CMD=. Retourne une liste de
        {level: warn|error, message}."""
        issues: list[dict] = []
        lower_files = {p.lower() for p in (file_paths or set())}

        d = self.get("DIR")
        c = self.get("CMD")
        if d is None:
            issues.append({"level": "warn", "message": "DIR= absent"})
        elif file_paths is not None and lower_files and f"{d.lower()}/" not in {
            p + "/" for p in lower_files if "/" in p
        }:
            issues.append({"level": "warn", "message": f"DIR={d} n'existe pas dans l'image"})

        if c is None:
            issues.append({"level": "warn", "message": "CMD= absent"})
        elif file_paths is not None and lower_files:
            exe = c.split()[0]
            base = exe.lower()
            if d:
                candidate = f"{d.lower()}/{base}"
                if candidate not in lower_files and base not in lower_files:
                    issues.append(
                        {"level": "warn", "message": f"exécutable {exe} introuvable (casse ignorée)"}
                    )

        env = self.get("ENV") or ""
        if "WINEDLLOVERRIDES" in env:
            issues.append(
                {"level": "warn", "message": "ENV= contient WINEDLLOVERRIDES : il écraserait "
                                              "les réglages DLL du lanceur"}
            )
        for k in self.unknown_keys():
            issues.append({"level": "warn", "message": f"clé inconnue : {k} (ignorée par Batocera)"})
        return issues


def read_autorun(image_dir: Path) -> Autorun | None:
    """Lit l'autorun depuis un dossier monté. None s'il n'existe pas."""
    p = image_dir / "autorun.cmd"
    if not p.exists():
        return None
    return Autorun.parse(p.read_bytes())


def write_autorun(autorun: Autorun, path: Path) -> None:
    path.write_bytes(autorun.render().encode("utf-8"))
