"""Outils squashfs : exécution des commandes (`unsquashfs`, `mksquashfs`) avec priorité basse et
progression, version et mode de reconstruction possible, table de l'image, propriétaires et
vérification d'une image reconstruite, chemins dans l'image et arborescences sources."""

from __future__ import annotations

import os
import re
import shutil
import stat as statmod
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..config import Settings
from .errors import ManagerError
from .jobs import Job
from .scan import list_files_uncached

if TYPE_CHECKING:
    from .rebuild import Changes


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


def safe_rel(path: str) -> str:
    """Chemin dans l'image, normalisé ; refuse ce qui sortirait de la racine."""
    p = PurePosixPath(path.replace("\\", "/").strip("/"))
    if not p.parts or any(part in ("", ".", "..") for part in p.parts):
        raise ManagerError(f"chemin invalide dans l'image : {path!r}")
    return str(p)


# Attributs étendus gardés, espace user.* seulement : user.DOSATTRIB, où Wine
# range les attributs Windows (caché, système, archive), porté par 115 images
# sur 318 (16/11) et relu par le lanceur. Les autres espaces (security.*,
# trusted.*) ne servent pas aux jeux et un utilisateur non root ne peut pas
# les restaurer. (Retirés jusqu'au 16/11 : squashfuse 0.5 plantait en les
# lisant ; ni le lanceur ni le manager n'utilisent plus squashfuse.)
USER_XATTRS = ["-xattrs-include", r"^user\."]

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


def run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagerError(f"commande impossible : {' '.join(command)} ({exc})") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ManagerError(detail or f"commande en échec : {' '.join(command)}")
    return result


def run_progress(command: list[str], job: Job, phase: str, start: float, end: float,
                 feed: Callable[[Any], None] | None = None) -> None:
    """Commande longue (unsquashfs/mksquashfs -percentage) : chaque ligne
    numérique est un pourcentage, ramené dans [start, end] ; annulable.
    `feed(stdin)` alimente l'entrée de la commande dans un fil à part (mode
    flux), l'entrée étant fermée ensuite."""
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if feed else subprocess.DEVNULL)
    except OSError as exc:
        raise ManagerError(f"commande impossible : {command[0]} ({exc})") from exc
    if feed is not None:
        assert proc.stdin is not None
        stdin = proc.stdin

        def pump() -> None:
            try:
                feed(stdin)
            finally:
                try:
                    stdin.close()
                except OSError:
                    pass

        threading.Thread(target=pump, daemon=True).start()

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
        raise ManagerError(f"{phase} en échec (code {proc.returncode}) : " + " / ".join(tail))
    if read_errors:
        raise ManagerError(f"{phase} : {len(read_errors)} lecture(s) impossible(s), image "
                           f"incomplète refusée : " + " / ".join(read_errors[:3]))


def squashfs_version(settings: Settings) -> tuple[int, int] | None:
    """Version de unsquashfs (« unsquashfs version 4.7.5 (…) »), None si absent."""
    try:
        r = subprocess.run([settings.unsquashfs, "-version"], capture_output=True, timeout=10,
                           check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rb"version (\d+)\.(\d+)", r.stdout + r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else None


def stream_missing(settings: Settings) -> str | None:
    """Ce qui manque au mode flux, ou None s'il est possible. squashfs-tools
    4.7 : unsquashfs 4.6 plante (« buffer overflow ») sur une valeur
    d'attribut binaire en écrivant la description."""
    version = squashfs_version(settings)
    if version is None:
        return f"{settings.unsquashfs} introuvable"
    if version < (4, 7):
        return f"unsquashfs {version[0]}.{version[1]} (squashfs-tools 4.7 ou plus récent requis)"
    return None


def choose_mode(settings: Settings) -> str:
    if settings.rebuild_mode == "stream":
        if (reason := stream_missing(settings)) is not None:
            raise ManagerError(f"mode stream demandé mais impossible : {reason}")
        return "stream"
    if settings.rebuild_mode == "extract":
        return "extract"
    return "stream" if stream_missing(settings) is None else "extract"


def verify(settings: Settings, part: Path, expected: set[str], changes: Changes) -> None:
    """SPEC § 3.4 : table lisible, arborescence identique à celle attendue,
    fichiers écrits relus à l'octet près."""
    run([settings.unsquashfs, "-s", str(part)], timeout=120)
    listing = list_files_uncached(settings, part)
    if listing is None:
        raise ManagerError("vérification : arborescence de la nouvelle image illisible")
    actual = set(listing)
    if actual != expected:
        missing = sorted(expected - actual)[:5]
        extra = sorted(actual - expected)[:5]
        raise ManagerError(f"vérification : arborescence inattendue (manquants : {missing}, "
                           f"en trop : {extra})")
    for rel, data in changes.write.items():
        content = run([settings.unsquashfs, "-cat", str(part), safe_rel(rel)],
                       timeout=60).stdout
        if content != data:
            raise ManagerError(f"vérification : {rel} reconstruit différent")
    if changes.copy:
        # Une seule lecture de la table pour toutes les tailles : relire chaque
        # fichier copié (-cat) prendrait des heures sur un gros patch
        sizes = file_sizes(settings, part)
        for rel, source in changes.copy.items():
            if sizes.get(safe_rel(rel)) != source.stat().st_size:
                raise ManagerError(f"vérification : taille de {rel} différente de la source")


_LLS_RE = re.compile(r"^(\S)\S*\s+\S+\s+(\d+)\s+(\S+ \S+)\s+squashfs-root/(.+)$")


_LLN_OWNER_RE = re.compile(r"^\S+\s+(\d+)/(\d+)\s")


def image_owners(settings: Settings, image: Path) -> set[tuple[int, int]]:
    """Propriétaires (uid, gid) présents dans l'image (unsquashfs -lln)."""
    out = run([settings.unsquashfs, "-lln", str(image)], timeout=600).stdout
    owners = set()
    for line in out.decode("utf-8", "replace").splitlines():
        m = _LLN_OWNER_RE.match(line)
        if m:
            owners.add((int(m.group(1)), int(m.group(2))))
    return owners


def table(settings: Settings, image: Path) -> dict[str, tuple[str, int, str]]:
    """Table de l'image (unsquashfs -lls) : {chemin: (type, taille, date)}, type
    « - » fichier, « d » dossier, « l » lien ; date « AAAA-MM-JJ HH:MM »."""
    out = run([settings.unsquashfs, "-lls", str(image)], timeout=600).stdout
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
