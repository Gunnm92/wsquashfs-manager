"""Moteur de règles YAML « tel fichier → tel réglage » (SPEC § 2.5).

Chaque règle : conditions sur l'analyse (imports, fichiers présents, type de
prefix, bits, etc.) → proposition (clé d'autorun, fichier à ajouter, ou
avertissement) + justification + niveau de confiance.

Les règles sont validées au chargement (condition inconnue = erreur
immédiate, pas un plantage au milieu de l'analyse d'une image) et évaluées
dans l'ordre ; la base de connaissances par jeu (SPEC § 2.5) est appliquée EN
PRIORITÉ et peut court-circuiter.

Motifs de fichiers : un motif SANS `/` porte sur le nom du fichier, où qu'il
soit dans l'arborescence ; un motif avec `/` porte sur le chemin complet
(relatif à la racine). Casse ignorée.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from ..models import RuleResult

CONDITIONS = {
    "file", "not_file", "all_files", "imports", "imports_any", "not_imports",
    "prefix", "arch", "exe_bits", "large_address_aware", "strings_any",
    "exe_name_glob", "dll_override", "tekno_version_below",
}
ACTIONS = {"key", "value", "file", "warning"}


class RuleError(ValueError):
    """Fichier de règles invalide."""


def validate_rules(rules: list[dict[str, Any]]) -> None:
    for i, rule in enumerate(rules):
        name = rule.get("name", f"#{i}")
        unknown = set(rule.get("when", {})) - CONDITIONS
        if unknown:
            raise RuleError(f"règle {name} : condition(s) inconnue(s) {sorted(unknown)}")
        bad = set(rule.get("then", {})) - ACTIONS
        if bad:
            raise RuleError(f"règle {name} : action(s) inconnue(s) {sorted(bad)}")
        then = rule.get("then", {})
        if not (then.get("key") or then.get("file") or then.get("warning")):
            raise RuleError(f"règle {name} : aucune proposition (key, file ou warning)")


def load_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    rules = list(data.get("rules", []))
    validate_rules(rules)
    return rules


def _dll(name: str) -> str:
    """« D3D11.DLL », « *d3d11 » (DllOverrides global) → « d3d11 »."""
    n = name.lower().lstrip("*")
    return n[:-4] if n.endswith(".dll") else n


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


class Analysis:
    """Résumé de l'analyse d'une image ou d'un dossier (lecture seule)."""

    def __init__(
        self,
        files: list[str],
        imports: set[str] | None = None,
        exe_name: str | None = None,
        exe_dir: str | None = None,       # dossier de l'exécutable (relatif à la racine)
        exe_bits: int | None = None,
        exe_large_address_aware: bool = False,
        prefix_type: str | None = None,   # "batocera" | "proton" | "other" | "none"
        proton_version: str | None = None,
        arch: str | None = None,          # "win32" | "win64" (d'après system.reg)
        tekno_version: str | None = None,
        version_resource: str | None = None,  # ProductVersion/FileVersion de l'exécutable
        strings: set[str] | None = None,
        dll_overrides: dict[str, str] | None = None,  # registre : dll → "native", "builtin"…
    ):
        self.files = files
        self.files_lower = [f.lower() for f in files]
        self.imports = {_dll(d) for d in (imports or set())}
        self.exe_name = exe_name
        self.exe_dir = exe_dir or ""
        self.exe_bits = exe_bits
        self.exe_large_address_aware = exe_large_address_aware
        self.prefix_type = prefix_type or "none"
        self.proton_version = proton_version
        self.arch = arch
        self.tekno_version = tekno_version
        self.version_resource = version_resource
        self.strings = {s.lower() for s in (strings or set())}
        self.dll_overrides = {_dll(k): v.lower() for k, v in (dll_overrides or {}).items()}

    def has_file(self, pattern: str) -> bool:
        pat = pattern.lower()
        if "/" in pat:
            return any(fnmatch.fnmatchcase(f, pat) for f in self.files_lower)
        return any(fnmatch.fnmatchcase(f.rsplit("/", 1)[-1], pat) for f in self.files_lower)


def _match(cond: dict[str, Any], a: Analysis) -> bool:
    """Évalue une condition. Toutes les clés présentes doivent être vraies."""
    for key, expected in cond.items():
        if key == "file":                      # un fichier correspond
            if not a.has_file(expected):
                return False
        elif key == "not_file":                # aucun fichier ne correspond
            patterns = expected if isinstance(expected, list) else [expected]
            if any(a.has_file(p) for p in patterns):
                return False
        elif key == "all_files":               # liste : tous présents
            if not all(a.has_file(p) for p in expected):
                return False
        elif key == "imports":                 # liste : tous importés
            if any(_dll(d) not in a.imports for d in expected):
                return False
        elif key == "imports_any":             # liste : au moins un importé
            if not any(_dll(d) in a.imports for d in expected):
                return False
        elif key == "not_imports":
            if any(_dll(d) in a.imports for d in expected):
                return False
        elif key == "prefix":
            if a.prefix_type != expected:
                return False
        elif key == "arch":
            if a.arch != expected:
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
            if not a.exe_name or not fnmatch.fnmatch(a.exe_name.lower(), expected.lower()):
                return False
        elif key == "dll_override":            # {dll: valeur} : réglage du registre,
            for dll, val in expected.items():  # 1er choix (« native,builtin » → native)
                actual = a.dll_overrides.get(_dll(dll), "").split(",")[0].strip()
                if actual != str(val).lower():
                    return False
        elif key == "tekno_version_below":     # version connue ET inférieure
            if not a.tekno_version or \
               _version_tuple(a.tekno_version) >= _version_tuple(str(expected)):
                return False
        else:                                  # protégé par validate_rules
            raise RuleError(f"condition inconnue : {key}")
    return True


def _fill(value: str | None, a: Analysis, path: bool = False) -> str | None:
    """Variables des propositions : {exe_dir}, {exe_name}."""
    if value is None:
        return None
    out = (value.replace("{exe_dir}", a.exe_dir.rstrip("/"))
                .replace("{exe_name}", a.exe_name or ""))
    return out.lstrip("/") if path else out


def run_rules(rules: list[dict[str, Any]], a: Analysis) -> list[RuleResult]:
    results: list[RuleResult] = []
    for rule in rules:
        if not _match(rule.get("when", {}), a):
            continue
        action = rule.get("then", {})
        results.append(RuleResult(
            key=action.get("key"),
            value=_fill(action.get("value"), a),
            file=_fill(action.get("file"), a, path=True),
            warning=_fill(action.get("warning"), a),
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
