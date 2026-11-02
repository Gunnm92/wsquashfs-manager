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
import re
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
from .durable import fsync_dir, restore, swap_keep_old
from .links import _pseudo_quote, _wildcard_escape
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
    # Arborescence copiée telle quelle (liens symboliques gardés), fusionnée
    # avec l'existant : dossier de destination → dossier source
    trees: dict[str, Path] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)     # lien → nouvelle cible

    def is_empty(self) -> bool:
        return not (self.write or self.copy or self.delete or self.trees or self.links)


def tree_entries(source: Path) -> list[tuple[str, int]]:
    """Entrées d'une arborescence (relatives, liens non suivis) et taille
    des fichiers ordinaires."""
    out = []
    for cur, dirnames, filenames in os.walk(source):
        rel_dir = os.path.relpath(cur, source)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for name in list(dirnames):
            path = os.path.join(cur, name)
            out.append((prefix + name, 0))
            if os.path.islink(path):
                dirnames.remove(name)
        for name in filenames:
            st = os.lstat(os.path.join(cur, name))
            out.append((prefix + name, st.st_size if statmod.S_ISREG(st.st_mode) else 0))
    return out


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
    added = [*changes.write, *changes.copy]
    for dest, source in changes.trees.items():
        added += [f"{safe_rel(dest)}/{rel}" for rel, _ in tree_entries(source)]
        added.append(dest)
    for rel in map(safe_rel, added):
        parts = rel.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


# ------------------------------------------------------------------ processus

# Attributs étendus jamais repris dans une image : les jeux n'en ont pas besoin,
# et ceux qu'une copie Windows/Samba/macOS laisse (user.DOSATTRIB,
# user.DosStream.*) font planter squashfuse 0.5 à la lecture (01/11 : vue
# overlay morte en pleine reconstruction). L'extraction les ignore déjà.
NO_XATTRS = ["-no-xattrs"]

# Lecture impossible signalée par mksquashfs, qui continue alors avec un
# fichier vide ou sans l'entrée : l'image serait incomplète
_READ_ERROR_RE = re.compile(
    r"Failed to read|Cannot stat|creating empty file|Transport endpoint is not connected|"
    r"Input/output error|Software caused connection abort")


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
    read_errors: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if line.isdigit():
            job.progress(phase, start + (end - start) * int(line) / 100)
        elif line:
            tail.append(line)
            if _READ_ERROR_RE.search(line):
                read_errors.append(line)
    proc.wait()
    job.check()
    if proc.returncode:
        raise RebuildError(f"{phase} en échec (code {proc.returncode}) : " + " / ".join(tail))
    if read_errors:
        raise RebuildError(f"{phase} : {len(read_errors)} lecture(s) impossible(s), image "
                           f"incomplète refusée : " + " / ".join(read_errors[:3]))


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
    copies += sum(size for src in changes.trees.values() for _, size in tree_entries(src))
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
    if shutil.which(settings.squashfuse) is None:
        return f"{settings.squashfuse} introuvable"
    return None


def fuse_available(settings: Settings) -> bool:
    return fuse_missing(settings) is None


def choose_mode(settings: Settings) -> str:
    if settings.rebuild_mode == "overlay":
        if not fuse_available(settings):
            raise RebuildError("mode overlay demandé mais FUSE ou squashfuse est indisponible")
        return "overlay"
    if settings.rebuild_mode == "extract":
        return "extract"
    return "overlay" if fuse_available(settings) else "extract"


def _mounted(mountpoint: Path) -> bool:
    """Point de montage présent dans /proc/mounts. Pas os.path.ismount : sur
    un montage FUSE dont le démon est mort, lstat échoue (ENOTCONN) et
    ismount répond faux, le montage restait alors en place (01/11)."""
    escaped = str(mountpoint).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as mounts:
            return any(line.split(" ")[1] == escaped for line in mounts if " " in line)
    except OSError:
        return os.path.ismount(mountpoint)


def _mount_daemon(command: list[str], log: Path) -> None:
    """Lance un démon FUSE (squashfuse, fuse-overlayfs) avec sa sortie dans
    `log`. Pas de tube : le démon le garderait après le montage, et écrire
    dans un tube fermé le tuerait (SIGPIPE) au premier message."""
    with open(log, "ab") as out:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RebuildError(f"commande impossible : {' '.join(command)} ({exc})") from exc
    if result.returncode:
        raise RebuildError(_log_tail(log) or f"commande en échec : {' '.join(command)}")


def _log_tail(log: Path, lines: int = 8) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " / ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())


def _unmount(mountpoint: Path) -> None:
    """Démontage avec quelques essais (fichiers encore ouverts juste après
    mksquashfs), puis démontage différé, comme umount_retry() du lanceur."""
    for _ in range(5):
        if not _mounted(mountpoint):
            return
        for tool in ("fusermount3", "fusermount"):
            if not shutil.which(tool):
                continue
            if subprocess.run([tool, "-u", str(mountpoint)], capture_output=True,
                              check=False).returncode == 0:
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
    items: list[tuple[str, bytes | Path]] = [*changes.write.items(), *changes.copy.items()]
    for rel, source in items:
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
    for dest, source in changes.trees.items():
        base = root / safe_rel(dest)
        for rel, _ in tree_entries(source):
            src, target = source / rel, base / rel
            if src.is_dir() and not src.is_symlink():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    target.unlink()
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.is_dir() and not target.is_symlink():
                raise RebuildError(f"{dest}/{rel} est un dossier dans l'image")
            if target.exists() or target.is_symlink():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target, follow_symlinks=False)
    for rel, link_target in changes.links.items():
        target = root / safe_rel(rel)
        if target.is_dir() and not target.is_symlink():
            raise RebuildError(f"{rel} est un dossier dans l'image")
        if target.exists() or target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target, target)


def pseudo_options(changes: Changes, existing: set[str], work: Path, uid: int,
                   gid: int) -> list[str]:
    """Tous les changements appliqués par mksquashfs lui-même (exclusions et
    définitions « pseudo »), l'image d'origine étant lue en lecture seule :
    rien n'est écrit à travers une vue fuse-overlayfs.

    Pourquoi (01/11) : sur une image qui porte des attributs étendus laissés
    par Windows/macOS, squashfuse meurt dès que fuse-overlayfs l'interroge
    (lien `pfx`, attribut user.DOSATTRIB) ; et 1,5 Go de runner écrits par
    fuse-overlayfs avaient déjà fait tomber les montages.

    Un chemin supprimé ou remplacé est exclu (-ef) ; un fichier est repris
    du disque par lien dur pseudo (`l`), un lien symbolique recréé (`s`), un
    dossier nouveau créé (`d`)."""
    if changes.is_empty():
        return []
    excludes: list[str] = []
    pseudo: list[str] = []
    defined: set[str] = set()
    gone: set[str] = set()

    def present(rel: str) -> bool:
        return rel in existing and not any(rel == g or rel.startswith(g + "/") for g in gone)

    def make_dirs(rel: str) -> None:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if not present(parent) and parent not in defined:
                pseudo.append(f"{_pseudo_quote(parent)} d 755 {uid} {gid}")
                defined.add(parent)

    def replace(rel: str) -> None:
        if present(rel) and rel not in gone:
            excludes.append(_wildcard_escape(rel))
            gone.add(rel)
        make_dirs(rel)
        defined.add(rel)

    def add_file(rel: str, source: Path) -> None:
        replace(rel)
        pseudo.append(f"{_pseudo_quote(rel)} l {_pseudo_quote(str(source.resolve()))}")

    for rel in map(safe_rel, changes.delete):
        if present(rel):
            excludes.append(_wildcard_escape(rel))
            gone.add(rel)
    written = work / "written"
    for i, (rel, data) in enumerate(changes.write.items()):
        written.mkdir(exist_ok=True)
        tmp = written / str(i)
        tmp.write_bytes(data)
        tmp.chmod(0o644)
        add_file(safe_rel(rel), tmp)
    for rel, source in changes.copy.items():
        add_file(safe_rel(rel), source)
    for dest, source in changes.trees.items():
        dest = safe_rel(dest)
        if not present(dest) and dest not in defined:
            make_dirs(dest)
            pseudo.append(f"{_pseudo_quote(dest)} d 755 {uid} {gid}")
            defined.add(dest)
        for rel, _ in tree_entries(source):
            target, src = f"{dest}/{rel}", source / rel
            if src.is_symlink():
                replace(target)
                pseudo.append(f"{_pseudo_quote(target)} s 777 {uid} {gid} {os.readlink(src)}")
            elif src.is_dir():
                if not present(target) and target not in defined:
                    make_dirs(target)
                    pseudo.append(f"{_pseudo_quote(target)} d 755 {uid} {gid}")
                    defined.add(target)
            else:
                add_file(target, src)
    for rel, link_target in changes.links.items():
        rel = safe_rel(rel)
        replace(rel)
        pseudo.append(f"{_pseudo_quote(rel)} s 777 {uid} {gid} {link_target}")
    opts: list[str] = []
    if excludes:
        exclude_file = work / "changes.exclude"
        exclude_file.write_text("".join(line + "\n" for line in excludes), encoding="utf-8")
        opts += ["-wildcards", "-ef", str(exclude_file)]
    if pseudo:
        pseudo_file = work / "changes.pseudo"
        pseudo_file.write_text("".join(line + "\n" for line in pseudo), encoding="utf-8")
        opts += ["-pf", str(pseudo_file)]
    return opts


def _build_overlay(settings: Settings, image: Path, part: Path, work: Path,
                   changes: Changes, job: Job, owner_opts: list[str],
                   existing: set[str] | None = None) -> None:
    """Mode « overlay » (sans extraction) : l'image est montée en lecture
    seule et mksquashfs la relit en appliquant lui-même les changements
    (pseudo_options). Plus de fuse-overlayfs : rien ne s'écrit dans un
    montage FUSE, seul squashfuse est lu."""
    lower = work / "lower"
    lower.mkdir()
    log = work / "fuse.log"
    mounted = False
    try:
        job.progress("montage", 0.0)
        _mount_daemon([settings.squashfuse, "-o", "ro", str(image), str(lower)], log)
        mounted = True
        for rel in [*changes.write, *changes.copy]:
            target = lower / safe_rel(rel)
            if target.is_dir() and not target.is_symlink():
                raise RebuildError(f"{rel} est un dossier dans l'image")
        extra = pseudo_options(changes, existing or set(), work, os.getuid(), os.getgid())
        job.check()
        try:
            run_progress([*priority_prefix(settings), settings.mksquashfs, str(lower), str(part),
                          *settings.mksquashfs_opts, *owner_opts, *NO_XATTRS, "-noappend",
                          "-percentage", *extra],
                         job, "mksquashfs", 0.02, 0.92)
        except RebuildError as exc:
            tail = _log_tail(log)
            raise RebuildError(f"{exc}{f' — montage FUSE : {tail}' if tail else ''}") from exc
    finally:
        if mounted:
            _unmount(lower)


def _build_extract(settings: Settings, image: Path, part: Path, work: Path,
                   changes: Changes, job: Job, owner_opts: list[str],
                   existing: set[str] | None = None) -> None:
    root = work / "root"
    # -no-xattrs : un utilisateur non root ne peut pas les restaurer ; les
    # images de jeux n'en ont pas besoin.
    run_progress([*priority_prefix(settings), settings.unsquashfs, "-no-xattrs",
                   "-percentage", "-d", str(root), str(image)],
                  job, "extraction", 0.0, 0.45)
    apply_changes(root, changes)
    job.check()
    run_progress([*priority_prefix(settings), settings.mksquashfs, str(root), str(part),
                   *settings.mksquashfs_opts, *owner_opts, *NO_XATTRS, "-noappend", "-percentage"],
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
    if changes.copy:
        # Une seule lecture de la table pour toutes les tailles : relire chaque
        # fichier copié (-cat) prendrait des heures sur un gros patch
        sizes = file_sizes(settings, part)
        for rel, source in changes.copy.items():
            if sizes.get(safe_rel(rel)) != source.stat().st_size:
                raise RebuildError(f"vérification : taille de {rel} différente de la source")


_LLS_RE = re.compile(r"^(\S)\S*\s+\S+\s+(\d+)\s+(\S+ \S+)\s+squashfs-root/(.+)$")


_LLN_OWNER_RE = re.compile(r"^\S+\s+(\d+)/(\d+)\s")


def image_owners(settings: Settings, image: Path) -> set[tuple[int, int]]:
    """Propriétaires (uid, gid) présents dans l'image (unsquashfs -lln)."""
    out = _run([settings.unsquashfs, "-lln", str(image)], timeout=600).stdout
    owners = set()
    for line in out.decode("utf-8", "replace").splitlines():
        m = _LLN_OWNER_RE.match(line)
        if m:
            owners.add((int(m.group(1)), int(m.group(2))))
    return owners


def _owner_opts(settings: Settings, image: Path, job: Job) -> list[str]:
    """La nouvelle image appartient à l'utilisateur du service (PUID/PGID,
    99:100 = arcade de SteamBox). Une image root (faite par Batocera) gêne le
    lanceur, qui tourne sans privilèges dans SteamBox : fuse-overlayfs n'y
    peut pas recopier un dossier root pour le modifier (EPERM). Batocera,
    qui lance les jeux en root, ne dépend pas du propriétaire."""
    uid, gid = os.getuid(), os.getgid()
    before = image_owners(settings, image)
    if before and before != {(uid, gid)}:
        owners = ", ".join(f"{u}:{g}" for u, g in sorted(before))
        job.log(f"Propriétaire : {owners} → {uid}:{gid} (utilisateur du service)")
    return ["-force-uid", str(uid), "-force-gid", str(gid)]


def table(settings: Settings, image: Path) -> dict[str, tuple[str, int, str]]:
    """Table de l'image (unsquashfs -lls) : {chemin: (type, taille, date)}, type
    « - » fichier, « d » dossier, « l » lien ; date « AAAA-MM-JJ HH:MM »."""
    out = _run([settings.unsquashfs, "-lls", str(image)], timeout=600).stdout
    entries: dict[str, tuple[str, int, str]] = {}
    for line in out.decode("utf-8", "replace").splitlines():
        m = _LLS_RE.match(line)
        if m:
            kind, size, date, rel = m.groups()
            if kind == "l":
                rel = rel.split(" -> ", 1)[0]
            entries[rel] = (kind, int(size), date)
    return entries


def file_sizes(settings: Settings, image: Path) -> dict[str, int]:
    return {rel: size for rel, (kind, size, _) in table(settings, image).items() if kind == "-"}


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
    owner_opts = _owner_opts(settings, image, job)
    old_listing = _list_files_uncached(settings, image)
    if old_listing is None:
        raise RebuildError("arborescence de l'image illisible (unsquashfs -l)")
    expected = expected_listing(old_listing, changes)

    swapped = False
    try:
        with work_area(settings, image, "rebuild-") as tmp:
            build = _build_overlay if mode == "overlay" else _build_extract
            build(settings, image, part, tmp, changes, job, owner_opts, set(old_listing))
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
        # .part sur le disque avant l'échange ; l'original reste en .old
        swap_keep_old(image, part, backup)
    except OSError as exc:
        raise RebuildError(f"échange final impossible : {exc}") from exc


# ------------------------------------------------------------------ .old

def validate_backup(image: Path) -> None:
    """« Valider » : la nouvelle image convient, l'ancienne est supprimée."""
    backup = backup_path(image)
    if not backup.exists():
        raise RebuildError("aucune sauvegarde .old")
    backup.unlink()
    fsync_dir(image.parent)


def restore_backup(image: Path) -> None:
    """« Revenir à l'ancienne » : la nouvelle image est remplacée par l'.old."""
    backup = backup_path(image)
    if not backup.exists():
        raise RebuildError("aucune sauvegarde .old")
    restore(backup, image)


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
