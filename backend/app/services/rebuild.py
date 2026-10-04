"""Reconstruction transactionnelle des images ``.wsquashfs`` (SPEC § 3.1, § 3.4).

Une image squashfs ne se modifie pas en place. Deux façons de la reconstruire :

- **overlay** (si FUSE est disponible) : l'image est montée en lecture seule
  (squashfuse), une couche fuse-overlayfs reçoit uniquement les changements,
  et mksquashfs relit la vue fusionnée. Rien n'est extrait sur disque ;
- **extraction** (repli) : unsquashfs complet dans le dossier temporaire,
  changements appliqués, puis mksquashfs (≈ 2 × la taille décompressée).

Dans les deux cas la nouvelle image est construite en ``<jeu>.wsquashfs.part``
à côté de l'original (même disque : échange atomique), vérifiée (table,
arborescence complète comparée à celle attendue, contenu des fichiers écrits),
puis échangée : l'original devient ``<jeu>.wsquashfs.old`` et n'est supprimé
que sur validation explicite ou après le délai de rétention.
"""

from __future__ import annotations

import os
import shutil
import stat as statmod
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import Settings, work_area
from .scan import _list_files_uncached

# Marge sur la taille de la nouvelle image (≈ l'ancienne) : la compression
# d'un fichier remplacé peut différer.
_SPACE_MARGIN = 1.05


class RebuildError(RuntimeError):
    """Une reconstruction n'a pas pu être menée à son terme."""


class RebuildCancelled(RebuildError):
    """Reconstruction annulée à la demande ; rien n'a été modifié."""


@dataclass
class Changes:
    """Changements à appliquer, chemins relatifs à la racine de l'image."""
    write: dict[str, bytes] = field(default_factory=dict)   # contenu fourni
    copy: dict[str, Path] = field(default_factory=dict)     # fichier source à copier
    delete: list[str] = field(default_factory=list)         # fichiers ou dossiers

    def is_empty(self) -> bool:
        return not (self.write or self.copy or self.delete)


class Job:
    """Suivi d'une reconstruction : progression, journal, annulation."""

    def __init__(self, on_progress: Callable[[str, float], None] | None = None,
                 on_log: Callable[[str], None] | None = None):
        self.cancel_event = threading.Event()
        self._on_progress = on_progress
        self._on_log = on_log

    def progress(self, phase: str, fraction: float) -> None:
        if self._on_progress:
            self._on_progress(phase, max(0.0, min(1.0, fraction)))

    def log(self, message: str) -> None:
        if self._on_log:
            self._on_log(message)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise RebuildCancelled("annulée")


@dataclass
class RebuildResult:
    mode: str                 # "overlay" | "extract"
    size_before: int
    size_after: int
    backup: Path
    duration: float


# ------------------------------------------------------------------ chemins

def safe_rel(path: str) -> str:
    """Chemin dans l'image, normalisé ; refuse ce qui sortirait de la racine."""
    p = PurePosixPath(path.replace("\\", "/").strip("/"))
    if not p.parts or any(part in ("", ".", "..") for part in p.parts):
        raise RebuildError(f"chemin invalide dans l'image : {path!r}")
    return str(p)


def backup_path(image: Path) -> Path:
    return image.with_name(image.name + ".old")


def part_path(image: Path) -> Path:
    return image.with_name(image.name + ".part")


def expected_listing(old: list[str], changes: Changes) -> set[str]:
    """Arborescence attendue après application des changements."""
    out = set(old)
    for rel in map(safe_rel, changes.delete):
        out = {p for p in out if p != rel and not p.startswith(rel + "/")}
    for rel in map(safe_rel, [*changes.write, *changes.copy]):
        parts = rel.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


# ------------------------------------------------------------------ processus

def priority_prefix(settings: Settings) -> list[str]:
    """Priorité basse (SPEC § 4) : c'est surtout du disque, le jeu passe avant."""
    prefix: list[str] = []
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", str(settings.task_priority_ionice)]
    if shutil.which("nice"):
        prefix += ["nice", "-n", str(settings.task_priority_nice)]
    return prefix


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RebuildError(f"commande impossible : {' '.join(command)} ({exc})") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RebuildError(detail or f"commande en échec : {' '.join(command)}")
    return result


def run_progress(command: list[str], job: Job, phase: str, start: float, end: float) -> None:
    """Commande longue (unsquashfs/mksquashfs -percentage) : chaque ligne
    numérique est un pourcentage, ramené dans [start, end] ; annulable."""
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise RebuildError(f"commande impossible : {command[0]} ({exc})") from exc

    def watch() -> None:
        while proc.poll() is None:
            if job.cancel_event.wait(0.5):
                proc.terminate()
                return

    threading.Thread(target=watch, daemon=True).start()
    tail: deque[str] = deque(maxlen=15)
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if line.isdigit():
            job.progress(phase, start + (end - start) * int(line) / 100)
        elif line:
            tail.append(line)
    proc.wait()
    job.check()
    if proc.returncode:
        raise RebuildError(f"{phase} en échec (code {proc.returncode}) : " + " / ".join(tail))


# ------------------------------------------------------------------ espace disque

def uncompressed_size(settings: Settings, image: Path) -> int:
    """Somme des tailles des fichiers de l'image (`unsquashfs -lls`, table seule)."""
    out = _run([settings.unsquashfs, "-lls", str(image)], timeout=300).stdout
    total = 0
    for line in out.decode("utf-8", errors="replace").splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3 and fields[0].startswith("-") and fields[2].isdigit():
            total += int(fields[2])
    return total


def space_needed(settings: Settings, image: Path, changes: Changes,
                 mode: str) -> dict[Path, int]:
    """Octets nécessaires par point de montage (le .part à côté de l'image,
    l'extraction dans le dossier de travail, à côté de l'image par défaut)."""
    copies = sum(src.stat().st_size for src in changes.copy.values())
    writes = sum(len(data) for data in changes.write.values())
    needs: dict[Path, int] = {}
    image_dir = image.parent
    needs[image_dir] = int(image.stat().st_size * _SPACE_MARGIN) + copies + writes
    if mode == "extract":
        tmp = settings.tmp_dir or image_dir
        extract = uncompressed_size(settings, image) + copies + writes
        if _same_fs(tmp, image_dir):
            needs[image_dir] += extract
        else:
            needs[tmp] = extract
    return needs


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return existing_parent(a).stat().st_dev == existing_parent(b).stat().st_dev
    except OSError:
        return False


def existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_space(needs: dict[Path, int]) -> None:
    for path, need in needs.items():
        free = shutil.disk_usage(existing_parent(path)).free
        if free < need:
            raise RebuildError(f"espace insuffisant sur {path} : {_human(need)} nécessaires, "
                               f"{_human(free)} libres")


def _human(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


# ------------------------------------------------------------------ montages

def fuse_missing(settings: Settings) -> str | None:
    """Ce qui manque au mode overlay, ou None s'il est possible."""
    if not Path("/dev/fuse").exists():
        return "/dev/fuse absent (conteneur sans le périphérique FUSE)"
    for tool in (settings.squashfuse, settings.fuse_overlayfs):
        if shutil.which(tool) is None:
            return f"{tool} introuvable"
    return None


def fuse_available(settings: Settings) -> bool:
    return fuse_missing(settings) is None


def choose_mode(settings: Settings) -> str:
    if settings.rebuild_mode == "overlay":
        if not fuse_available(settings):
            raise RebuildError("mode overlay demandé mais FUSE, squashfuse ou fuse-overlayfs "
                               "est indisponible")
        return "overlay"
    if settings.rebuild_mode == "extract":
        return "extract"
    return "overlay" if fuse_available(settings) else "extract"


def _unmount(mountpoint: Path) -> None:
    """Démontage avec quelques essais (fichiers encore ouverts juste après
    mksquashfs), puis démontage différé, comme umount_retry() du lanceur."""
    for _ in range(5):
        if not os.path.ismount(mountpoint):
            return
        for tool in ("fusermount3", "fusermount"):
            if shutil.which(tool) and subprocess.run([tool, "-u", str(mountpoint)],
                                                     capture_output=True, check=False).returncode == 0:
                return
        time.sleep(1)
    for tool in ("fusermount3", "fusermount"):
        if shutil.which(tool):
            subprocess.run([tool, "-uz", str(mountpoint)], capture_output=True, check=False)


# ------------------------------------------------------------------ application

def apply_changes(root: Path, changes: Changes) -> None:
    """Applique les changements dans `root` (vue fusionnée ou extraction).

    Un fichier remplacé est d'abord supprimé : sur l'overlay, l'écraser
    déclencherait la copie complète de l'original dans la couche haute."""
    for rel in map(safe_rel, changes.delete):
        target = root / rel
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    for rel, source in [*changes.write.items(), *changes.copy.items()]:
        target = root / safe_rel(rel)
        if target.is_dir() and not target.is_symlink():
            raise RebuildError(f"{rel} est un dossier dans l'image")
        if target.exists() or target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            shutil.copy2(source, target)


def _build_overlay(settings: Settings, image: Path, part: Path, work: Path,
                   changes: Changes, job: Job) -> None:
    lower, upper, ovl_work, merged = (work / n for n in ("lower", "upper", "work", "merged"))
    for d in (lower, upper, ovl_work, merged):
        d.mkdir()
    mounted: list[Path] = []
    try:
        job.progress("montage", 0.0)
        _run([settings.squashfuse, "-o", "ro", str(image), str(lower)], timeout=60)
        mounted.append(lower)
        _run([settings.fuse_overlayfs, "-o",
              f"lowerdir={lower},upperdir={upper},workdir={ovl_work}", str(merged)], timeout=60)
        mounted.append(merged)
        apply_changes(merged, changes)
        job.check()
        run_progress([*priority_prefix(settings), settings.mksquashfs, str(merged), str(part),
                       *settings.mksquashfs_opts, "-noappend", "-percentage"],
                      job, "mksquashfs", 0.02, 0.92)
    finally:
        for mountpoint in reversed(mounted):
            _unmount(mountpoint)


def _build_extract(settings: Settings, image: Path, part: Path, work: Path,
                   changes: Changes, job: Job) -> None:
    root = work / "root"
    # -no-xattrs : un utilisateur non root ne peut pas les restaurer ; les
    # images de jeux n'en ont pas besoin.
    run_progress([*priority_prefix(settings), settings.unsquashfs, "-no-xattrs",
                   "-percentage", "-d", str(root), str(image)],
                  job, "extraction", 0.0, 0.45)
    apply_changes(root, changes)
    job.check()
    run_progress([*priority_prefix(settings), settings.mksquashfs, str(root), str(part),
                   *settings.mksquashfs_opts, "-noappend", "-percentage"],
                  job, "mksquashfs", 0.45, 0.92)


# ------------------------------------------------------------------ vérification

def verify(settings: Settings, part: Path, expected: set[str], changes: Changes) -> None:
    """SPEC § 3.4 : table lisible, arborescence identique à celle attendue,
    fichiers écrits relus à l'octet près."""
    _run([settings.unsquashfs, "-s", str(part)], timeout=120)
    listing = _list_files_uncached(settings, part)
    if listing is None:
        raise RebuildError("vérification : arborescence de la nouvelle image illisible")
    actual = set(listing)
    if actual != expected:
        missing = sorted(expected - actual)[:5]
        extra = sorted(actual - expected)[:5]
        raise RebuildError(f"vérification : arborescence inattendue (manquants : {missing}, "
                           f"en trop : {extra})")
    for rel, data in changes.write.items():
        content = _run([settings.unsquashfs, "-cat", str(part), safe_rel(rel)],
                       timeout=60).stdout
        if content != data:
            raise RebuildError(f"vérification : {rel} reconstruit différent")
    for rel, source in changes.copy.items():
        size = len(_run([settings.unsquashfs, "-cat", str(part), safe_rel(rel)],
                        timeout=600).stdout)
        if size != source.stat().st_size:
            raise RebuildError(f"vérification : taille de {rel} différente de la source")


# ------------------------------------------------------------------ reconstruction

def rebuild(settings: Settings, image: Path, changes: Changes, job: Job | None = None,
            in_use: Callable[[Path], bool] | None = None) -> RebuildResult:
    """Applique `changes` à l'image ; l'original est gardé en ``.old``.

    `in_use(image)` est rappelé juste avant l'échange : un jeu lancé pendant
    la reconstruction garde son image d'origine."""
    job = job or Job()
    began = time.monotonic()
    if not image.is_file():
        raise RebuildError(f"image introuvable : {image}")
    if changes.is_empty():
        raise RebuildError("aucun changement à appliquer")
    backup, part = backup_path(image), part_path(image)
    if backup.exists():
        raise RebuildError(f"sauvegarde existante à valider ou restaurer : {backup.name}")
    if part.exists():
        raise RebuildError(f"reconstruction inachevée présente : {part.name}")
    for tool in (settings.unsquashfs, settings.mksquashfs):
        if not shutil.which(tool):
            raise RebuildError(f"outil requis introuvable : {tool}")
    if in_use and in_use(image):
        raise RebuildError("jeu en cours d'utilisation")

    mode = choose_mode(settings)
    if mode == "overlay":
        job.log("Mode : overlay (sans extraction)")
    elif settings.rebuild_mode == "extract":
        job.log("Mode : extraction (imposé par la configuration)")
    else:
        job.log(f"Mode : extraction complète — overlay impossible : {fuse_missing(settings)}")
    check_space(space_needed(settings, image, changes, mode))
    before = image.stat()
    old_listing = _list_files_uncached(settings, image)
    if old_listing is None:
        raise RebuildError("arborescence de l'image illisible (unsquashfs -l)")
    expected = expected_listing(old_listing, changes)

    swapped = False
    try:
        with work_area(settings, image, "rebuild-") as tmp:
            build = _build_overlay if mode == "overlay" else _build_extract
            build(settings, image, part, tmp, changes, job)
        job.check()
        job.progress("vérification", 0.93)
        verify(settings, part, expected, changes)
        job.check()
        job.progress("échange", 0.98)
        _swap(image, part, backup, before, in_use)
        swapped = True
    finally:
        if not swapped:
            part.unlink(missing_ok=True)
    job.progress("terminé", 1.0)
    return RebuildResult(mode=mode, size_before=before.st_size, size_after=image.stat().st_size,
                         backup=backup, duration=time.monotonic() - began)


def _swap(image: Path, part: Path, backup: Path, before: os.stat_result,
          in_use: Callable[[Path], bool] | None) -> None:
    now = image.stat()
    if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RebuildError("l'image a été modifiée pendant la reconstruction")
    if in_use and in_use(image):
        raise RebuildError("jeu lancé pendant la reconstruction : image d'origine conservée")
    os.chmod(part, statmod.S_IMODE(before.st_mode))
    try:
        os.chown(part, before.st_uid, before.st_gid)
    except PermissionError:
        pass        # service lancé sous le propriétaire des images : déjà bon
    try:
        os.replace(image, backup)
        try:
            os.replace(part, image)
        except OSError:
            os.replace(backup, image)
            raise
    except OSError as exc:
        raise RebuildError(f"échange final impossible : {exc}") from exc


# ------------------------------------------------------------------ .old

def validate_backup(image: Path) -> None:
    """« Valider » : la nouvelle image convient, l'ancienne est supprimée."""
    backup = backup_path(image)
    if not backup.exists():
        raise RebuildError("aucune sauvegarde .old")
    backup.unlink()


def restore_backup(image: Path) -> None:
    """« Revenir à l'ancienne » : la nouvelle image est remplacée par l'.old."""
    backup = backup_path(image)
    if not backup.exists():
        raise RebuildError("aucune sauvegarde .old")
    os.replace(backup, image)


def purge_backups(images: list[Path], retention_days: int | None) -> list[Path]:
    """Supprime les .old plus vieux que la rétention. L'âge est celui de
    l'échange (ctime : mis à jour par le renommage), pas celui du jeu."""
    if retention_days is None:
        return []
    limit = time.time() - retention_days * 86400
    removed = []
    for image in images:
        backup = backup_path(image)
        try:
            if backup.stat().st_ctime < limit:
                backup.unlink()
                removed.append(backup)
        except OSError:
            continue
    return removed
