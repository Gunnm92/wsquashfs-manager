"""Actions en masse (SPEC § 2.4) : opérations sur l'autorun d'une sélection,
réinitialisation des sauvegardes.

Les opérations d'autorun s'appliquent dans l'ordre, sur la même lecture que
l'édition manuelle (§ 3.2) : une clé modifiée garde sa place, une clé ajoutée
va en fin de fichier, fins de ligne et encodage sont conservés.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from ..models import ImageInfo
from .autorun import Autorun

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MassError(Exception):
    pass


class AutorunOp(BaseModel):
    """`set` : poser ou modifier une clé ; `remove` : retirer toutes ses
    occurrences ; `template` : remplacer tout l'autorun par un modèle."""
    op: Literal["set", "remove", "template"]
    key: str = ""
    value: str = ""
    template: str = ""

    @field_validator("key")
    @classmethod
    def _key_format(cls, v: str) -> str:
        if v and not _KEY_RE.match(v):
            raise ValueError(f"clé invalide : {v}")
        return v

    @field_validator("value")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("valeur sur une seule ligne")
        return v


def template_vars(info: ImageInfo, current: Autorun) -> dict[str, str]:
    """Variables d'un modèle : {name}, {system} et les clés de l'autorun
    actuel ({CMD}, {DIR}, {GAME_VERSION}…, première occurrence)."""
    out: dict[str, str] = {}
    for k, v, _ in current.items():
        out.setdefault(k, v)
    out["name"] = info.name
    out["system"] = info.system or ""
    return out


def render_template(template: str, variables: dict[str, str]) -> str:
    """`{var}` remplacé ; `{{` et `}}` donnent des accolades. Variable
    inconnue → erreur (l'image est écartée, les autres continuent)."""
    missing = sorted({m.group(1) for m in _VAR_RE.finditer(template.replace("{{", ""))
                      if m.group(1) not in variables})
    if missing:
        raise MassError("variable(s) absente(s) : " + ", ".join(missing))
    out = _VAR_RE.sub(lambda m: variables[m.group(1)], template.replace("{{", "\0L")
                      .replace("}}", "\0R"))
    return out.replace("\0L", "{").replace("\0R", "}")


def apply_ops(current: Autorun, ops: list[AutorunOp], info: ImageInfo) -> Autorun:
    """Nouvel autorun ; `current` n'est pas modifié."""
    a = Autorun(lines=list(current.lines), eol=current.eol,
                trailing_eol=current.trailing_eol, encoding=current.encoding)
    for op in ops:
        if op.op == "set":
            if not op.key:
                raise MassError("clé manquante")
            a.set(op.key, op.value)
        elif op.op == "remove":
            if not op.key:
                raise MassError("clé manquante")
            a.remove(op.key)
        else:
            text = render_template(op.template, template_vars(info, a))
            new = Autorun.parse(text)
            # Fins de ligne et encodage de l'image, pas ceux du formulaire
            a = Autorun(lines=new.lines, eol=a.eol, trailing_eol=True, encoding=a.encoding)
    return a


# ----------------------------------------------------------------- sauvegardes

def saves_dir_of(saves_root: Path, info: ImageInfo) -> Path:
    return saves_root / info.name


def reset_saves(saves_root: Path, info: ImageInfo, now: float | None = None) -> Path | None:
    """Renomme `saves/<jeu>` en `saves/<jeu>.avant-<date>` (rien n'est
    supprimé). Retourne le nouveau chemin, ou None sans sauvegardes."""
    d = saves_dir_of(saves_root, info)
    if not d.is_dir():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    dest = d.with_name(f"{d.name}.avant-{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = d.with_name(f"{d.name}.avant-{stamp}-{n}")
    d.rename(dest)
    return dest
