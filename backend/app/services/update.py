"""Montée de version d'un jeu (SPEC § 3.3).

Un dossier source (nouveau build complet ou patch) est appliqué à un point
d'ancrage de l'image (le dossier du jeu, proposé d'après DIR=) :

- fichiers **ajoutés** (absents de l'image) et **remplacés** (présents, mais
  de taille ou de date différente) ; un fichier de même taille et même date
  est considéré identique et n'est pas recopié ;
- en option (build complet), fichiers **supprimés** : présents sous le point
  d'ancrage mais absents de la source ;
- `GAME_VERSION` mise à jour dans l'autorun de l'image ;
- les fichiers du jeu modifiés dans la couche de sauvegardes masqueraient les
  nouveaux : ils sont signalés, et en option **mis de côté** dans
  `<jeu>.avant-maj-<date>/` (jamais effacés), les vraies sauvegardes restant
  en place.

La casse de l'image est conservée (Windows l'ignore) ; la reconstruction
passe par rebuild() : overlay ou extraction, vérification, .old.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models import Task
from . import scan
from .analyze import _game_version, read_exe, scan_folder
from .autorun import Autorun, decode_autorun, exe_from_cmd, normalize_path
from .rebuild import Changes, Job, RebuildError, rebuild, safe_rel, table

_LIST_LIMIT = 300          # fichiers détaillés dans l'aperçu (les totaux restent exacts)


@dataclass
class UpdatePlan:
    anchor: str
    added: list[dict] = field(default_factory=list)      # {path, size}
    replaced: list[dict] = field(default_factory=list)   # {path, before, after}
    same: int = 0
    deleted: list[str] = field(default_factory=list)
    masked: list[str] = field(default_factory=list)      # dans la couche de sauvegardes
    version_before: str | None = None
    version: str | None = None
    version_hint: str = ""
    warnings: list[str] = field(default_factory=list)
    changes: Changes = field(default_factory=Changes)

    def summary(self) -> dict:
        size = sum(a["size"] for a in self.added) + sum(r["after"] for r in self.replaced)
        return {
            "anchor": self.anchor,
            "added": self.added[:_LIST_LIMIT], "added_count": len(self.added),
            "replaced": self.replaced[:_LIST_LIMIT], "replaced_count": len(self.replaced),
            "same": self.same,
            "deleted": self.deleted[:_LIST_LIMIT], "deleted_count": len(self.deleted),
            "masked": self.masked[:_LIST_LIMIT], "masked_count": len(self.masked),
            "copy_size": size,
            "version_before": self.version_before, "version": self.version,
            "version_hint": self.version_hint, "warnings": self.warnings,
            "changed": not self.changes.is_empty(),
        }


def _image_autorun(settings: Settings, image: Path) -> tuple[Autorun | None, str]:
    data = scan.read_autorun_bytes(settings, image)
    if data is None:
        return None, "utf-8"
    text, encoding = decode_autorun(data)
    return Autorun.parse(text), encoding


def default_anchor(settings: Settings, image: Path) -> str:
    """Point d'ancrage proposé : le DIR= de l'autorun (dossier du jeu)."""
    autorun, _ = _image_autorun(settings, image)
    d = (autorun.get("DIR") if autorun else None) or ""
    return "/".join(p for p in d.strip().strip("\"'").replace("\\", "/").split("/") if p not in ("", "."))


def _source_mtime(path: Path) -> str:
    """Date au format de unsquashfs -lls (heure locale, à la minute)."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def plan_update(settings: Settings, image: Path, source: Path, anchor: str,
                delete_missing: bool = False, version: str | None = None) -> UpdatePlan:
    if not source.is_dir():
        raise RebuildError(f"dossier source introuvable : {source}")
    entries = table(settings, image)
    by_lower = {rel.lower(): rel for rel in entries}

    # Point d'ancrage : un dossier de l'image (casse de l'image), ou la racine
    anchor = anchor.strip().strip("/")
    if anchor:
        anchor = safe_rel(anchor)
        found = by_lower.get(anchor.lower())
        if found is None or entries[found][0] != "d":
            raise RebuildError(f"le dossier {anchor} n'existe pas dans l'image")
        anchor = found
    plan = UpdatePlan(anchor=anchor)
    prefix = f"{anchor}/" if anchor else ""

    src = scan_folder(source)
    in_source = set()
    for rel, size in sorted(src.sizes.items()):
        dest = f"{prefix}{rel}"
        in_source.add(dest.lower())
        existing = by_lower.get(dest.lower())
        if existing is None:
            # Parents déjà présents : casse de l'image
            parts = dest.split("/")
            for i in range(len(parts) - 1, 0, -1):
                parent = by_lower.get("/".join(parts[:i]).lower())
                if parent:
                    dest = "/".join([parent, *parts[i:]])
                    break
            plan.added.append({"path": dest, "size": size})
            plan.changes.copy[dest] = source / rel
            continue
        kind, before, date = entries[existing]
        if kind == "d":
            raise RebuildError(f"{existing} est un dossier dans l'image, un fichier dans la source")
        if before == size and date == _source_mtime(source / rel):
            plan.same += 1
            continue
        plan.replaced.append({"path": existing, "before": before, "after": size})
        plan.changes.copy[existing] = source / rel

    if delete_missing:
        if not anchor and any(r.lower() in ("system.reg", "config_info") for r in entries):
            raise RebuildError("suppression des fichiers absents refusée à la racine d'un prefix : "
                               "choisir le dossier du jeu comme point d'ancrage")
        # Dossiers qui contiennent des fichiers de la source : à garder
        source_dirs = {"/".join(path.split("/")[:i]) for path in in_source
                       for i in range(1, path.count("/") + 1)}
        for rel in sorted(entries):
            low = rel.lower()
            if (not low.startswith(prefix.lower()) or low == "autorun.cmd"
                    or low in in_source or low in source_dirs):
                continue
            if any(low.startswith(d.lower() + "/") for d in plan.deleted):
                continue                         # déjà couvert par un dossier supprimé
            plan.deleted.append(rel)
        plan.changes.delete = list(plan.deleted)

    # GAME_VERSION : saisie, sinon ressources de l'exécutable de CMD dans la source
    autorun, encoding = _image_autorun(settings, image)
    plan.version_before = autorun.get("GAME_VERSION") if autorun else None
    if version is None and autorun and autorun.get("CMD"):
        exe = normalize_path(f"{autorun.get('DIR') or ''}/{exe_from_cmd(autorun.get('CMD'))}")
        rel = next((r for r in src.sizes if f"{prefix}{r}".lower() == exe), None)
        if rel:
            found, why = _game_version(read_exe(source / rel), None)
            plan.version, plan.version_hint = found, why
    else:
        plan.version = version or None
    if plan.version and autorun is not None and plan.version != plan.version_before:
        autorun.set("GAME_VERSION", plan.version)
        plan.changes.write["autorun.cmd"] = autorun.encode(encoding)
    override = scan.read_override(image)
    if override is not None and override.get("GAME_VERSION") is not None:
        plan.warnings.append("la surcharge (.autorun) définit aussi GAME_VERSION : elle "
                             "l'emportera au lancement")

    # Fichiers du jeu dans la couche de sauvegardes : ils masqueraient les nouveaux
    saves = settings.saves_dir / image.stem
    if saves.is_dir():
        for item in [*plan.added, *plan.replaced]:
            if (saves / item["path"]).is_file():
                plan.masked.append(item["path"])
        if plan.masked:
            plan.warnings.append(f"{len(plan.masked)} fichier(s) du jeu modifiés dans les "
                                 "sauvegardes masqueraient la nouvelle version")
    return plan


def set_aside_masked(settings: Settings, image: Path, paths: list[str]) -> Path | None:
    """Déplace les fichiers masquants de la couche de sauvegardes dans
    <jeu>.avant-maj-<date>/ (même arborescence) : rien n'est effacé, les
    vraies sauvegardes de partie restent en place."""
    saves = settings.saves_dir / image.stem
    if not paths or not saves.is_dir():
        return None
    aside = saves.with_name(f"{saves.name}.avant-maj-{time.strftime('%Y%m%d-%H%M%S')}")
    for rel in paths:
        src = saves / safe_rel(rel)
        if src.is_file():
            target = aside / safe_rel(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
    return aside


def run_update(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « update »."""
    if not task.image_path:
        raise RebuildError("tâche sans image")
    image = Path(task.image_path)
    p = task.params
    plan = plan_update(settings, image, Path(p["source"]), p.get("anchor", ""),
                       p.get("delete_missing", False), p.get("version"))
    job.log(f"{len(plan.added)} ajouté(s), {len(plan.replaced)} remplacé(s), "
            f"{plan.same} identique(s), {len(plan.deleted)} supprimé(s)")
    if plan.changes.is_empty():
        return "Rien à mettre à jour : image déjà à jour"
    result = rebuild(settings, image, plan.changes, job, in_use=scan.image_in_use)
    message = (f"Mise à jour terminée en {result.duration:.0f} s (mode {result.mode}) : "
               f"{result.size_before / 1e9:.2f} → {result.size_after / 1e9:.2f} Go, "
               f"original conservé en {result.backup.name}")
    if plan.version:
        message += f" ; GAME_VERSION={plan.version}"
    if p.get("clean_saves") and plan.masked:
        aside = set_aside_masked(settings, image, plan.masked)
        message += f" ; {len(plan.masked)} fichier(s) masquant(s) mis de côté dans {aside.name}"
    elif plan.masked:
        message += f" ; attention : {len(plan.masked)} fichier(s) des sauvegardes masquent la mise à jour"
    return message

