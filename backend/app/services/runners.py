"""Runner joint à une image, depuis les runners installés dans SteamBox.

Deux réparations des liens du prefix (voir links.py) :
- **joindre** un runner entier dans `runners/<nom>/` (Kingdom Hearts III :
  1350 liens vers le GE-Proton-latest de Heroic) ; `PROTON=` (ou `WINE=`)
  le désigne ;
- **compléter** un runner déjà joint avec le dossier qui lui manque, pris
  dans un autre runner (Anno 117 : DenuvOwO livré sans `files/lib/wine/icu`,
  repris de proton-cachyos-slr, même ICU 68).

Dans les deux cas, les liens qui retrouvent leur fichier dans `runners/`
sont réécrits en relatif, puis l'image est reconstruite (original en `.old`).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, work_area
from ..models import Task
from . import scan
from .links import LinkReport, classify, image_links
from .operations import _task_image, image_autorun
from .rebuild import Changes, Job, RebuildError, rebuild
from .trial import steambox_fetch, steambox_listing

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ARCH_DIRS = {"x86_64-windows", "i386-windows", "x86_64-unix", "i386-unix", "aarch64-windows"}
_SUB_RE = re.compile(r"^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")


@dataclass
class RunnerPlan:
    source: str                     # dossier du runner dans SteamBox
    sub: str                        # sous-dossier copié ("" : runner entier)
    dest: str                       # nom sous runners/
    dest_prefix: str                # runners/<dest>[/<sub>]
    listing: dict[str, int]         # entrées copiées → taille
    before: LinkReport
    after: LinkReport
    autorun: bytes | None = None    # nouvel autorun (PROTON=/WINE=), None : inchangé
    key: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(self.listing.values())

    @property
    def repaired(self) -> int:
        return self.before.count - (len(self.after.external) + len(self.after.missing))

    def to_dict(self) -> dict:
        return {"source": self.source, "sub": self.sub, "dest": self.dest,
                "dest_prefix": self.dest_prefix, "entries": len(self.listing),
                "size": self.size, "rewritten": len(self.after.fixable),
                "repaired": self.repaired, "before": self.before.to_dict(),
                "remaining": len(self.after.external) + len(self.after.missing),
                "key": self.key, "warnings": self.warnings}


def _check_names(dest: str, sub: str) -> None:
    if not _NAME_RE.match(dest):
        raise RebuildError(f"nom de runner invalide : {dest!r}")
    if sub and (not _SUB_RE.match(sub) or any(p in (".", "..") for p in sub.split("/"))):
        raise RebuildError(f"sous-dossier invalide : {sub!r}")


def _with_parents(paths: set[str]) -> set[str]:
    out = set(paths)
    for p in paths:
        parts = p.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return out


def image_state(settings: Settings, image: Path) -> tuple[list[str], list[tuple[str, str]]]:
    files = scan._list_files_uncached(settings, image)
    if files is None:
        raise RebuildError("arborescence de l'image illisible (unsquashfs -l)")
    return files, image_links(image, settings.unsquashfs)


def plan_runner(settings: Settings, image: Path, source: str, sub: str, dest: str,
                set_key: bool, listing: dict[str, int] | None = None,
                state: tuple[list[str], list[tuple[str, str]]] | None = None) -> RunnerPlan:
    """Effet de la copie de `source[/sub]` dans `runners/<dest>[/sub]`."""
    sub = sub.strip("/")
    _check_names(dest, sub)
    files, links = state or image_state(settings, image)
    if listing is None:
        listing = steambox_listing(settings, posixpath.join(source, sub) if sub else source)
    prefix = f"runners/{dest}" + (f"/{sub}" if sub else "")
    after_files = _with_parents(set(files) | {f"{prefix}/{rel}" for rel in listing})
    plan = RunnerPlan(source=source, sub=sub, dest=dest, dest_prefix=prefix, listing=listing,
                      before=classify(links, files), after=classify(links, after_files))
    if set_key and not sub:
        plan.key = "PROTON" if "proton" in listing else "WINE"
        autorun, _, encoding = image_autorun(settings, image)
        if autorun is None:
            plan.warnings.append(f"pas d'autorun.cmd : {plan.key}= non posé")
            plan.key = None
        elif autorun.get(plan.key) != dest:
            other = "WINE" if plan.key == "PROTON" else "PROTON"
            autorun.set(plan.key, dest)
            autorun.remove(other)
            plan.autorun = autorun.encode(encoding)
    return plan


# ------------------------------------------------------------------ propositions

def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 2}


def _suffix(target: str, after: str) -> str | None:
    parts = [p for p in target.split("/") if p not in ("", ".", "..")]
    if after in parts:
        return "/".join(parts[parts.index(after) + 1:])
    return None


def suggest(settings: Settings, image: Path, runners: list[dict]) -> dict:
    """Liens de l'image et réparations proposées avec les runners de SteamBox
    (`runners` : [{name, path}] de --doctor)."""
    state = image_state(settings, image)
    files, links = state
    report = classify(links, files)
    suggestions: list[dict] = []
    out = {"report": report.to_dict(), "issues": report.issues(image=True), "suggestions": suggestions}
    if report.fixable and not report.external and not report.missing:
        # Runner déjà joint à l'image : seuls les liens restent à réécrire en relatif
        suggestions.append({
            "action": "relink", "title": "Réécrire les liens en relatif",
            "detail": (f"{len(report.fixable)} lien(s) pointent vers un dossier absent de "
                       f"l'image ; le runner est déjà joint dans runners/"),
            "rewritten": len(report.fixable), "repaired": len(report.fixable), "remaining": 0,
            "size": 0, "warnings": [], "source": "", "sub": "", "dest": "", "set_key": False,
            "runner": ""})
        return out
    if not report.external and not report.missing:
        return out
    listings: dict[str, dict[str, int]] = {}

    def listing(path: str) -> dict[str, int]:
        if path not in listings:
            try:
                listings[path] = steambox_listing(settings, path)
            except RebuildError:
                listings[path] = {}
        return listings[path]

    by_name = {r["name"].lower(): r for r in runners}
    # Liens vers un runner de la machine : le joindre entier, s'il est dans SteamBox
    wanted: dict[str, list[str]] = {}
    for _, target in report.external:
        name = next((n for n in report.runners if f"/{n}/" in target), None)
        if name and (suffix := _suffix(target, name)):
            wanted.setdefault(name, []).append(suffix)
    for name, suffixes in wanted.items():
        runner = by_name.get(name.lower())
        if not runner:
            continue
        found = listing(runner["path"])
        plan = plan_runner(settings, image, runner["path"], "", runner["name"], True,
                           listing=found, state=state)
        suggestions.append({
            "action": "join", "runner": runner["name"],
            "title": f"Joindre {runner['name']} à l'image",
            "detail": (f"copié de SteamBox ({runner['path']}) dans runners/{runner['name']}/, "
                       f"{plan.key or 'PROTON'}={runner['name']} ; "
                       f"{sum(s in found for s in suffixes)}/{len(suffixes)} liens retrouvent "
                       f"leur fichier"),
            **plan.to_dict(), "set_key": True})
    # Fichiers absents d'un runner joint : compléter depuis un runner qui les a
    missing: dict[str, list[str]] = {}
    for rel, target in report.missing:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
        parts = resolved.split("/")
        if len(parts) > 2 and parts[0] == "runners":
            missing.setdefault(parts[1], []).append("/".join(parts[2:]))
    for joined, suffixes in missing.items():
        sub = posixpath.commonpath([posixpath.dirname(s) for s in suffixes])
        # Dossier d'architecture (icu/x86_64-windows) : l'autre architecture aussi
        if posixpath.basename(sub) in _ARCH_DIRS:
            sub = posixpath.dirname(sub)
        if not sub:
            continue
        # Runners qui ont tous ces fichiers ; le plus proche par le nom d'abord
        candidates = [r for r in runners
                      if all(posixpath.relpath(s, sub) in listing(posixpath.join(r["path"], sub))
                             for s in suffixes)]
        candidates.sort(key=lambda r: -len(_tokens(r["name"]) & _tokens(joined)))
        if not candidates:
            continue
        runner = candidates[0]
        plan = plan_runner(settings, image, runner["path"], sub, joined, False,
                           listing=listing(posixpath.join(runner["path"], sub)), state=state)
        suggestions.append({
            "action": "complete", "runner": runner["name"],
            "title": f"Compléter {joined} avec {sub} de {runner['name']}",
            "detail": (f"{len(suffixes)} fichier(s) absents du runner joint ; {sub} copié de "
                       f"SteamBox ({runner['name']}) dans runners/{joined}/"),
            **plan.to_dict(), "set_key": False})
    return out


# ------------------------------------------------------------------ tâche

def run_relink(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « relink » : réécrit en relatif les liens du
    prefix qui pointent vers un dossier absent de l'image mais présent dans
    runners/ (ex. un runner joint, dont les liens gardent un chemin absolu)."""
    image, _ = _task_image(task)
    files, links = image_state(settings, image)
    report = classify(links, files)
    if not report.fixable:
        return "aucun lien à réécrire"
    job.log(f"{len(report.fixable)} lien(s) à réécrire en relatif")
    result = rebuild(settings, image, Changes(links=dict(report.fixable)), job,
                     in_use=scan.image_in_use)
    files, links = image_state(settings, image)
    left = classify(links, files)
    message = (f"{len(report.fixable)} lien(s) réécrit(s) ; {result.size_before / 1e9:.1f} → "
               f"{result.size_after / 1e9:.1f} Go en {result.duration:.0f} s, original conservé "
               f"en {result.backup.name}")
    if left.count:
        message += f" ; {left.count} lien(s) encore à revoir"
    return message

def run_runner(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « runner »."""
    image, _ = _task_image(task)
    p = task.params
    plan = plan_runner(settings, image, p["source"], p.get("sub", ""), p["dest"],
                       bool(p.get("set_key")))
    job.log(f"{plan.source}{'/' + plan.sub if plan.sub else ''} → {plan.dest_prefix} "
            f"({len(plan.listing)} entrées, {plan.size / 1e6:.0f} Mo)")
    for warning in plan.warnings:
        job.log(f"Attention : {warning}")
    with work_area(settings, image, "runner-") as tmp:
        tree = tmp / "tree"
        source = posixpath.join(plan.source, plan.sub) if plan.sub else plan.source
        steambox_fetch(settings, source, tree, job, plan.size)
        job.check()
        changes = Changes(trees={plan.dest_prefix: tree}, links=dict(plan.after.fixable))
        if plan.autorun is not None:
            changes.write["autorun.cmd"] = plan.autorun
        result = rebuild(settings, image, changes, job, in_use=scan.image_in_use)
    files, links = image_state(settings, image)
    left = classify(links, files)
    for issue in left.issues(image=True):
        job.log(f"Reste : {issue['message']}")
    message = (f"{plan.dest_prefix} ajouté, {len(plan.after.fixable)} lien(s) réécrit(s)"
               f"{f', {plan.key}={plan.dest}' if plan.autorun else ''} ; "
               f"{result.size_before / 1e9:.1f} → {result.size_after / 1e9:.1f} Go en "
               f"{result.duration:.0f} s, original conservé en {result.backup.name}")
    if left.count:
        message += f" ; {left.count} lien(s) encore à revoir"
    return message
