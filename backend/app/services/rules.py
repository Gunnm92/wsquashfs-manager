"""Moteur de règles YAML « tel fichier → tel réglage » (SPEC § 2.5).

Chaque règle : conditions sur l'analyse (imports, fichiers présents, type de
prefix, bits, etc.) → proposition (clé d'autorun, fichier à ajouter, ou
avertissement) + justification + niveau de confiance.

Les règles sont évaluées dans l'ordre ; la base de connaissances par jeu
(SPEC § 2.5) est appliquée EN PRIORITÉ et peut court-circuiter.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from ..models import RuleResult


def load_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return list(data.get("rules", []))


class Analysis:
    """Résumé de l'analyse d'une image ou d'un dossier (lecture seule)."""

    def __init__(
        self,
        files: list[str],
        imports: set[str] | None = None,
        exe_name: str | None = None,
        exe_bits: int | None = None,
        exe_large_address_aware: bool = False,
        prefix_type: str | None = None,   # "batocera" | "proton" | "other" | "none"
        proton_version: str | None = None,
        arch: str | None = None,          # "win32" | "win64" (d'après system.reg)
        tekno_version: str | None = None,
        version_resource: str | None = None,  # ProductVersion/FileVersion de l'exécutables
        strings: set[str] | None = None,
    ):
        self.files = files
        self.files_lower = {f.lower() for f in files}
        self.imports = {d.lower() for d in (imports or set())}
        self.exe_name = exe_name
        self.exe_bits = exe_bits
        self.exe_large_address_aware = exe_large_address_aware
        self.prefix_type = prefix_type or "none"
        self.proton_version = proton_version
        self.arch = arch
        self.tekno_version = tekno_version
        self.version_resource = version_resource
        self.strings = {s.lower() for s in (strings or set())}

    def has_file(self, pattern: str) -> bool:
        """True si un fichier de l'analyse correspond au pattern (glob simple)."""
        import fnmatch
        return any(fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(f.lower(), pattern.lower())
                   for f in self.files)


def _match(cond: dict[str, Any], a: Analysis) -> bool:
    """Évalue une condition. Toutes les clés présentes doivent être vraies."""
    for key, expected in cond.items():
        if key == "file":                      # glob dans l'arborescence
            if not a.has_file(expected):
                return False
        elif key == "all_files":               # liste : tous présents
            if not all(a.has_file(p) for p in expected):
                return False
        elif key == "imports":                 # liste : tous importés
            missing = [d.lower() for d in expected if d.lower() not in a.imports]
            if missing:
                return False
        elif key == "imports_any":             # liste : au moins un importé
            if not any(d.lower() in a.imports for d in expected):
                return False
        elif key == "not_imports":
            if any(d.lower() in a.imports for d in expected):
                return False
        elif key == "prefix":
            if a.prefix_type != expected:
                return False
        elif key == "exe_bits":
            if a.exe_bits != expected:
                return False
        elif key == "large_address_aware":
            if a.exe_large_address_aware != expected:
                return False
        elif key == "strings_any":
            if not any(s.lower() in a.strings for s in expected):
                return False
        elif key == "exe_name_glob":
            import fnmatch
            if not a.exe_name or not fnmatch.fnmatch(a.exe_name, expected):
                return False
        else:
            raise ValueError(f"condition inconnue : {key}")
    return True


def run_rules(rules: list[dict[str, Any]], a: Analysis) -> list[RuleResult]:
    results: list[RuleResult] = []
    for rule in rules:
        cond = rule.get("when", {})
        if not _match(cond, a):
            continue
        action = rule.get("then", {})
        results.append(RuleResult(
            key=action.get("key"),
            value=action.get("value"),
            file=action.get("file"),
            warning=action.get("warning"),
            justification=rule.get("justification", ""),
            confidence=rule.get("confidence", "high"),
            source=rule.get("name", "règle"),
        ))
    return results


def exe_fingerprint(exe_path: Path) -> str | None:
    """Empreinte de l'exécutable pour la base de connaissances par jeu."""
    try:
        return hashlib.sha256(exe_path.read_bytes()).hexdigest()
    except OSError:
        return None
