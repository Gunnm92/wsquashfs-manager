"""Opérations sur les images exécutées par la file de tâches.

Une édition d'autorun est décrite par ses paramètres, persistés avec la
tâche :

- ``{"text": …, "encoding": …, "base_sha": …}`` : contenu complet (fiche
  d'une image). `base_sha` est l'empreinte de l'autorun au moment de
  l'ouverture de l'éditeur : s'il a changé entre-temps, la tâche échoue au
  lieu d'écraser la modification ;
- ``{"ops": [...]}`` : opérations (action en masse), appliquées à l'autorun
  tel qu'il est AU MOMENT de l'exécution.

Le même calcul (`plan_autorun`, `plan_files`) sert à l'aperçu et à l'exécution.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import Task
from . import scan
from .analyze import scan_folder
from .autorun import Autorun, apply_ops, decode_autorun, exe_from_cmd, normalize_path
from .rebuild import (
    Changes,
    Job,
    RebuildError,
    priority_prefix,
    rebuild,
    run_progress,
    safe_rel,
    verify,
)
from .registry import prefix_reg, set_value


def autorun_sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


@dataclass
class AutorunPlan:
    before: bytes | None      # None : pas d'autorun dans l'image
    after: bytes
    after_text: str
    encoding: str
    issues: list[dict]

    @property
    def changed(self) -> bool:
        return self.before != self.after


def plan_autorun(settings: Settings, image: Path, name: str, system: str | None,
                 params: dict, validate: bool = True) -> AutorunPlan:
    before = scan.read_autorun_bytes(settings, image)
    if before is not None:
        current_text, encoding = decode_autorun(before)
        current = Autorun.parse(current_text)
    else:
        current, encoding = None, "utf-8"

    if "text" in params:
        base = params.get("base_sha")
        if base is not None and base != autorun_sha(before):
            raise RebuildError("l'autorun de l'image a changé depuis l'ouverture de "
                               "l'éditeur : rouvrir la fiche")
        encoding = params.get("encoding") or encoding
        new = Autorun.parse(params["text"])
        after_text = params["text"]
    else:
        new = apply_ops(current, params["ops"], name, system)
        after_text = new.render()
    if not new.lines:
        raise RebuildError("l'autorun obtenu est vide")
    try:
        after = after_text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise RebuildError(f"caractère impossible à écrire en {encoding} : {exc.object[exc.start]!r}"
                           ) from exc
    issues = new.validate(set(scan._list_files(settings, image))) if validate else []
    return AutorunPlan(before=before, after=after, after_text=after_text, encoding=encoding,
                       issues=issues)


def run_autorun(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « autorun »."""
    image, system = _task_image(task)
    plan = plan_autorun(settings, image, image.stem, system, task.params, validate=False)
    if not plan.changed:
        return "Autorun déjà à jour : image non reconstruite"
    result = rebuild(settings, image, Changes(write={"autorun.cmd": plan.after}), job,
                     in_use=scan.image_in_use)
    return (f"Terminé en {result.duration:.0f} s (mode {result.mode}) : "
            f"{result.size_before / 1e6:.0f} → {result.size_after / 1e6:.0f} Mo, "
            f"original conservé en {result.backup.name}")


# ------------------------------------------------------------------ fichiers et registre

def resolve_exe(files: list[str], autorun: Autorun | None) -> str | None:
    """Chemin réel (casse de l'image) de l'exécutable de CMD, résolu comme le
    lanceur : depuis la racine, puis depuis DIR. None si introuvable."""
    if autorun is None or not autorun.get("CMD"):
        return None
    by_lower = {f.lower(): f for f in files}
    exe = exe_from_cmd(autorun.get("CMD") or "")
    d = normalize_path(autorun.get("DIR") or "")
    for candidate in (normalize_path(exe), normalize_path(f"{d}/{exe}")):
        if candidate in by_lower:
            return by_lower[candidate]
    return None


_COMPARE_LIMIT = 64 * 2**20     # au-delà, le fichier est recopié sans comparaison


def _same_content(settings: Settings, image: Path, member: str, source: Path) -> bool:
    """Fichier de l'image identique à la source : une opération rejouée ne
    reconstruit pas l'image pour rien."""
    if source.stat().st_size > _COMPARE_LIMIT:
        return False
    return scan._unsquashfs_cat(settings, image, member) == source.read_bytes()


def _image_case(path: str, by_lower: dict[str, str]) -> str:
    """Windows ignore la casse : chaque composant déjà présent dans l'image
    garde la casse de l'image (pas de second dossier « Game » à côté de « game »)."""
    parts = path.split("/")
    for i in range(len(parts), 0, -1):
        found = by_lower.get("/".join(parts[:i]).lower())
        if found:
            return "/".join([found, *parts[i:]])
    return path


def _fill(text: str, variables: dict[str, str | None], what: str) -> str:
    for var, value in variables.items():
        token = "{" + var + "}"
        if token in text:
            if value is None:
                raise RebuildError(f"{what} : {token} inconnu (exécutable de CMD introuvable "
                                   "dans l'image)")
            text = text.replace(token, value)
    return text


@dataclass
class FilesPlan:
    changes: Changes
    actions: list[dict]               # {"action": add|replace|delete|reg, "path", "detail"}
    warnings: list[str]
    saves_writes: dict[Path, bytes]   # fichiers de la couche de sauvegardes à réécrire

    @property
    def changed(self) -> bool:
        return not self.changes.is_empty() or bool(self.saves_writes)


def plan_files(settings: Settings, image: Path, name: str, system: str | None,
               params: dict) -> FilesPlan:
    """params : {"copy": [{"dest", "source"}], "delete": [chemins],
    "reg": [{"file", "key", "name", "value"}]}. Variables : {exe_dir},
    {exe_name} (exécutable de CMD), {name}, {system}.

    Une valeur de registre est aussi posée dans la couche de sauvegardes
    (saves/<jeu>/…) si le fichier y existe : Wine le réécrit à chaque partie,
    et cette copie masque celle de l'image."""
    files = scan._list_files(settings, image)
    if not files:
        raise RebuildError("arborescence de l'image illisible")
    by_lower = {f.lower(): f for f in files}
    data = scan.read_autorun_bytes(settings, image)
    autorun = Autorun.parse(decode_autorun(data)[0]) if data is not None else None
    exe = resolve_exe(files, autorun)
    variables = {"exe_dir": exe.rpartition("/")[0] if exe else None,
                 "exe_name": exe.rpartition("/")[2] if exe else None,
                 "name": name, "system": system or ""}
    saves_root = settings.saves_dir / image.stem
    changes, actions, warnings = Changes(), [], []
    saves_writes: dict[Path, bytes] = {}

    for item in params.get("copy", []):
        source = Path(item["source"])
        if not source.is_file():
            raise RebuildError(f"fichier source introuvable : {source}")
        dest = _image_case(safe_rel(_fill(item["dest"], variables, "destination")), by_lower)
        existing = by_lower.get(dest.lower())
        if existing and any(f.startswith(existing + "/") for f in files):
            raise RebuildError(f"{dest} est un dossier dans l'image")
        if existing and _same_content(settings, image, existing, source):
            actions.append({"action": "same", "path": dest, "detail": "déjà identique"})
            continue
        changes.copy[dest] = source
        actions.append({"action": "replace" if existing else "add", "path": dest,
                        "detail": f"{source} ({source.stat().st_size} o)"})
        if (saves_root / dest).exists():
            warnings.append(f"{dest} existe dans les sauvegardes et masquera le nouveau fichier")

    for raw in params.get("delete", []):
        path = safe_rel(_fill(raw, variables, "suppression"))
        existing = by_lower.get(path.lower())
        if existing is None:
            warnings.append(f"{path} absent de l'image : rien à supprimer")
            continue
        changes.delete.append(existing)
        actions.append({"action": "delete", "path": existing, "detail": ""})

    for item in params.get("reg", []):
        reg_file = prefix_reg(files, item.get("file", "user.reg"))
        if reg_file is None:
            raise RebuildError("pas de prefix Wine dans l'image (jeu seul) : registre "
                               "impossible à modifier")
        key = _fill(item["key"], variables, "clé de registre")
        before = changes.write.get(reg_file)
        if before is None:
            before = scan._unsquashfs_cat(settings, image, reg_file)
            if before is None:
                raise RebuildError(f"{reg_file} illisible")
        text = before.decode("latin-1")
        after = set_value(text, key, item["name"], item["value"]).encode("latin-1")
        detail = f'[{key}] "{item["name"]}"="{item["value"]}"'
        if after != before:
            changes.write[reg_file] = after
            actions.append({"action": "reg", "path": reg_file, "detail": detail})
        saved = saves_root / reg_file
        if saved.is_file():
            current = saves_writes.get(saved) or saved.read_bytes()
            updated = set_value(current.decode("latin-1"), key, item["name"],
                                item["value"]).encode("latin-1")
            if updated != current:
                saves_writes[saved] = updated
                actions.append({"action": "reg", "path": f"sauvegardes/{reg_file}",
                                "detail": detail})
    return FilesPlan(changes=changes, actions=actions, warnings=warnings,
                     saves_writes=saves_writes)


def _write_saves(writes: dict[Path, bytes]) -> None:
    for path, data in writes.items():
        mode = path.stat().st_mode
        part = path.with_name(path.name + ".wsfs-part")
        part.write_bytes(data)
        os.chmod(part, mode)
        os.replace(part, path)


def run_files(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « files »."""
    image, system = _task_image(task)
    plan = plan_files(settings, image, image.stem, system, task.params)
    for warning in plan.warnings:
        job.log(f"Attention : {warning}")
    if not plan.changed:
        return "Déjà à jour : image non reconstruite"
    message = "Image inchangée"
    if not plan.changes.is_empty():
        result = rebuild(settings, image, plan.changes, job, in_use=scan.image_in_use)
        message = (f"Terminé en {result.duration:.0f} s (mode {result.mode}), "
                   f"original conservé en {result.backup.name}")
    elif scan.image_in_use(image):
        raise RebuildError("jeu en cours d'utilisation")
    if plan.saves_writes:
        _write_saves(plan.saves_writes)
        message += f" ; registre des sauvegardes mis à jour ({len(plan.saves_writes)} fichier(s))"
    return message


def _task_image(task: Task) -> tuple[Path, str | None]:
    if not task.image_path:
        raise RebuildError("tâche sans image")
    system = task.image.split("/", 1)[0] if task.image and "/" in task.image else None
    return Path(task.image_path), system


# ------------------------------------------------------------------ vérification

def run_verify(settings: Settings, task: Task, job: Job) -> str:
    """SPEC § 2.4 : table de l'image lisible, autorun lisible et cohérent."""
    image, _ = _task_image(task)
    job.progress("table", 0.1)
    if not scan._unsquashfs_ok(settings, image):
        raise RebuildError("table de l'image illisible (unsquashfs -s)")
    job.progress("arborescence", 0.4)
    files = scan._list_files_uncached(settings, image)
    if files is None:
        raise RebuildError("arborescence illisible (unsquashfs -l)")
    job.log(f"{len(files)} entrées")
    job.progress("autorun", 0.8)
    data = scan.read_autorun_bytes(settings, image)
    if data is None:
        return "Image lisible, mais sans autorun.cmd"
    issues = Autorun.parse(decode_autorun(data)[0]).validate(set(files))
    for issue in issues:
        job.log(f"{'Erreur' if issue['level'] == 'error' else 'Attention'} : {issue['message']}")
    return f"Image intègre ; autorun : {len(issues)} remarque(s)" if issues else \
        "Image intègre, autorun cohérent"


# ------------------------------------------------------------------ sauvegardes

def reset_saves(settings: Settings, image: Path) -> Path:
    """« Réinitialiser » (SPEC § 2.2) : saves/<jeu> est renommé en
    <jeu>.avant-<date>, jamais supprimé."""
    saves = settings.saves_dir / image.stem
    if not saves.is_dir():
        raise RebuildError("aucune sauvegarde pour ce jeu")
    target = saves.with_name(f"{saves.name}.avant-{time.strftime('%Y%m%d-%H%M%S')}")
    os.rename(saves, target)
    return target


# ------------------------------------------------------------------ empaquetage (création)

FOLDER_SUFFIXES = (".pc", ".wine")


def folder_stem(name: str) -> str:
    """« Jeu.pc » / « Jeu.wine » → « Jeu » (nom de l'image et des sauvegardes)."""
    for suffix in FOLDER_SUFFIXES:
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name


def find_folders(settings: Settings) -> list[dict]:
    """Dossiers de jeux à empaqueter (`*.pc`, `*.wine`) dans roms/<système>/,
    avec la même déduction du système que les images."""
    out = []
    seen: set[Path] = set()
    for d in settings.roms_dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        is_system_dir = root.resolve().parent.name == "roms"
        parents = [(root, root.resolve().name if is_system_dir else None)]
        if not is_system_dir:
            parents += [(p, p.name) for p in sorted(root.iterdir())
                        if p.is_dir() and not p.name.startswith(".")
                        and not p.name.lower().endswith(FOLDER_SUFFIXES)]
        for parent, system in parents:
            try:
                entries = sorted(parent.iterdir())
            except OSError:
                continue
            for entry in entries:
                if (not entry.is_dir() or entry.is_symlink()
                        or not entry.name.lower().endswith(FOLDER_SUFFIXES)
                        or entry.resolve() in seen):
                    continue
                seen.add(entry.resolve())
                stem = folder_stem(entry.name)
                image = entry.with_name(stem + ".wsquashfs")
                has_autorun = any(p.name.lower() == "autorun.cmd" for p in _safe_iterdir(entry))
                out.append({
                    "id": f"{system}/{entry.name}" if system else entry.name,
                    "system": system, "name": stem, "folder": entry.name,
                    "path": str(entry), "kind": entry.name.rsplit(".", 1)[-1].lower(),
                    "has_autorun": has_autorun,
                    "image_id": scan.image_id(image, system), "image_exists": image.exists(),
                })
    return out


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def pack_target(source: Path) -> Path:
    return source.with_name(folder_stem(source.name) + ".wsquashfs")


def run_pack(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « pack » : dossier → .wsquashfs (SPEC § 2.3).

    1. renommage `<jeu>.pc` → `<jeu>.wine` (option) ;
    2. mksquashfs vers `<jeu>.wsquashfs.part`, avec l'autorun validé injecté
       sans modifier le dossier (chaque entrée de premier niveau est une
       source, l'ancien autorun.cmd n'en fait pas partie) ;
    3. vérification : table lisible, arborescence identique à celle du
       dossier, autorun relu à l'octet près ;
    4. `.part` → `<jeu>.wsquashfs` ; dossier supprimé seulement si demandé."""
    params = task.params
    source = Path(params["source"])
    stem = folder_stem(source.name)
    wine = source.with_name(stem + ".wine")
    if not source.is_dir() and wine.is_dir():
        source = wine                # tâche reprise après un renommage déjà fait
    if not source.is_dir():
        raise RebuildError(f"dossier introuvable : {source}")
    target = pack_target(source)
    part = target.with_name(target.name + ".part")
    if target.exists():
        raise RebuildError(f"{target.name} existe déjà : jamais écrasé")
    if part.exists():
        part.unlink()               # reste d'une tâche interrompue
    if not shutil.which(settings.mksquashfs):
        raise RebuildError(f"outil requis introuvable : {settings.mksquashfs}")

    job.progress("analyse", 0.0)
    listing = scan_folder(source)
    job.log(f"{len(listing.sizes)} fichiers, {listing.total_size / 1e9:.1f} Go")
    free = shutil.disk_usage(source.parent).free
    if free < listing.total_size * 1.02:
        raise RebuildError(f"espace insuffisant : {listing.total_size / 1e9:.1f} Go à écrire "
                           f"au pire, {free / 1e9:.1f} Go libres")
    job.check()

    if params.get("rename_wine") and source != wine:
        if wine.exists():
            raise RebuildError(f"{wine.name} existe déjà")
        os.rename(source, wine)
        job.log(f"Renommé : {source.name} → {wine.name}")
        source = wine
        params["source"] = str(wine)

    autorun: bytes | None = None
    if params.get("autorun") is not None:
        autorun = params["autorun"].encode(params.get("encoding") or "utf-8")
    old_autorun = [f for f in listing.files if f.lower() == "autorun.cmd"]
    expected = set(listing.files)
    if autorun is not None:
        expected -= set(old_autorun)
        expected.add("autorun.cmd")
    elif not old_autorun:
        raise RebuildError("ni autorun existant ni autorun proposé")

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    done = False
    try:
        with tempfile.TemporaryDirectory(prefix="pack-", dir=settings.tmp_dir) as tmp:
            if autorun is not None:
                injected = Path(tmp) / "autorun.cmd"
                injected.write_bytes(autorun)
                sources = [str(source / name) for name in sorted(os.listdir(source))
                           if name.lower() != "autorun.cmd"] + [str(injected)]
            else:
                sources = [str(source)]
            run_progress([*priority_prefix(settings), settings.mksquashfs, *sources, str(part),
                           *settings.mksquashfs_opts, "-noappend", "-percentage"],
                          job, "mksquashfs", 0.02, 0.92)
        job.check()
        job.progress("vérification", 0.93)
        verify(settings, part, expected, Changes(write={"autorun.cmd": autorun}
                                                 if autorun is not None else {}))
        os.chmod(part, 0o644)
        if target.exists():
            raise RebuildError(f"{target.name} est apparu pendant l'empaquetage")
        os.replace(part, target)
        done = True
    finally:
        if not done:
            part.unlink(missing_ok=True)
    size = target.stat().st_size
    message = (f"{target.name} créé : {size / 1e9:.2f} Go "
               f"({size / max(listing.total_size, 1):.0%} du dossier)")
    if params.get("delete_source"):
        job.progress("suppression du dossier", 0.97)
        shutil.rmtree(source)
        message += f" ; dossier {source.name} supprimé"
    else:
        message += f" ; dossier {source.name} conservé"
    return message
