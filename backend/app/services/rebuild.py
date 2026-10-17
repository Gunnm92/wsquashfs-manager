"""Reconstruction d'une image sans l'extraire (SPEC § 3.1, § 3.4).

1. image montée en lecture seule (squashfuse) ;
2. couche des changements posée par-dessus (fuse-overlayfs en lecture seule,
   deux `lowerdir` : la vue fusionnée n'est que lue par mksquashfs, il n'y a
   donc ni `upperdir` ni « whiteout ») ; les fichiers supprimés sont exclus
   de mksquashfs (`-ef`) ;
3. mksquashfs depuis la vue fusionnée vers `<jeu>.wsquashfs.part` (mêmes
   options que `--pack` du lanceur) ;
4. vérification de la nouvelle image ;
5. échange : original → `<jeu>.wsquashfs.old`, `.part` → `<jeu>.wsquashfs` ;
6. démontage, nettoyage.

Repli sans FUSE : extraction complète (unsquashfs), changements appliqués sur
le disque, puis mksquashfs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from . import scan

# Marge d'espace disque en plus de la taille estimée.
_SPACE_MARGIN = 64 * 1024 * 1024

_PROGRESS_RE = re.compile(rb"(\d+)/(\d+)\s+(\d+)%")


class RebuildError(Exception):
    pass


class Cancelled(RebuildError):
    pass


@dataclass
class Changes:
    """Changements à appliquer : chemins relatifs à la racine de l'image."""
    files: dict[str, bytes] = field(default_factory=dict)   # ajoutés ou remplacés
    delete: list[str] = field(default_factory=list)          # retirés (fichier ou dossier)

    def size(self) -> int:
        return sum(len(b) for b in self.files.values())


@dataclass
class Context:
    """Liens avec la tâche : journal, progression, annulation."""
    log: Callable[[str], None] = lambda msg: None
    progress: Callable[[float], None] = lambda p: None
    cancel: threading.Event = field(default_factory=threading.Event)


def old_path(image: Path) -> Path:
    return image.with_name(image.name + ".old")


def part_path(image: Path) -> Path:
    return image.with_name(image.name + ".part")


# ----------------------------------------------------------------- montages

def _umount(path: Path) -> None:
    for args in (["-u"], ["-uz"]):          # puis démontage différé si occupé
        for tool in ("fusermount3", "fusermount"):
            try:
                if subprocess.run([tool, *args, str(path)], capture_output=True,
                                  timeout=30).returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                continue


def _fuse_available(settings: Settings) -> bool:
    return (Path("/dev/fuse").exists() and shutil.which(settings.squashfuse) is not None
            and shutil.which(settings.fuse_overlayfs) is not None)


def _run(cmd: list[str], what: str, timeout: int = 60) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RebuildError(f"{what} : {e}") from e
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RebuildError(f"{what} : {err or 'code ' + str(r.returncode)}")


# ----------------------------------------------------------------- étapes

def _stage(changes: Changes, top: Path, lower: Path | None) -> None:
    """Écrit les fichiers modifiés dans `top`, en gardant les droits du
    fichier d'origine s'il existe dans `lower`."""
    for rel, data in changes.files.items():
        dest = top / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()                    # fichier extrait en lecture seule
        dest.write_bytes(data)
        if lower is not None and (lower / rel).is_file():
            os.chmod(dest, (lower / rel).stat().st_mode & 0o7777)


def _space_needed(settings: Settings, image: Path, changes: Changes) -> int:
    return image.stat().st_size + changes.size() + _SPACE_MARGIN


def _uncompressed_size(settings: Settings, image: Path) -> int:
    """Taille décompressée (repli par extraction) : somme des fichiers
    réguliers de `unsquashfs -lln`."""
    r = subprocess.run([settings.unsquashfs, "-lln", str(image)], capture_output=True,
                       timeout=300)
    total = 0
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].startswith("-") and parts[2].isdigit():
            total += int(parts[2])
    return total


def _check_space(path: Path, needed: int, what: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free
    if free < needed:
        raise RebuildError(f"espace insuffisant sur {what} : {needed / 1e9:.1f} Go nécessaires, "
                           f"{free / 1e9:.1f} Go libres")


def _priority_prefix(settings: Settings) -> list[str]:
    cmd = []
    if shutil.which("nice"):
        cmd += ["nice", "-n", str(settings.task_priority_nice)]
    if shutil.which("ionice"):
        cmd += ["ionice", "-c", str(settings.task_priority_ionice)]
    return cmd


def _mksquashfs(settings: Settings, src: Path, part: Path, exclude: Path | None,
                ctx: Context) -> None:
    cmd = _priority_prefix(settings) + [
        settings.mksquashfs, str(src), str(part), *settings.mksquashfs_opts,
        "-noappend", "-quiet", "-progress",
    ]
    if exclude is not None:
        cmd += ["-ef", str(exclude)]
    ctx.log("mksquashfs " + " ".join(cmd[cmd.index(settings.mksquashfs) + 1:]))
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    tail: list[bytes] = []

    def reader():
        buf = b""
        assert proc.stdout is not None
        while chunk := proc.stdout.read1(4096):
            buf += chunk
            # la barre de progression est réécrite avec \r
            *done, buf = re.split(rb"[\r\n]", buf)
            for piece in done:
                m = _PROGRESS_RE.search(piece)
                if m:
                    ctx.progress(int(m.group(3)) / 100)
                elif piece.strip():
                    tail.append(piece)
                    del tail[:-20]

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    while proc.poll() is None:
        if ctx.cancel.wait(0.5):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            t.join(timeout=5)
            raise Cancelled("annulé")
    t.join(timeout=5)
    for line in tail:
        ctx.log(line.decode("utf-8", errors="replace"))
    if proc.returncode != 0:
        raise RebuildError(f"mksquashfs a échoué (code {proc.returncode})")


def expected_listing(before: list[str], changes: Changes) -> set[str]:
    """Contenu attendu de la nouvelle image : liste d'origine, plus les
    fichiers ajoutés (et leurs dossiers), moins les chemins supprimés et leur
    contenu."""
    out = set(before)
    for rel in changes.files:
        parts = rel.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    for d in changes.delete:
        out = {p for p in out if p != d and not p.startswith(d + "/")}
    return out


def verify(settings: Settings, part: Path, expected: set[str], changes: Changes,
           ctx: Context) -> None:
    """Vérifications après reconstruction (SPEC § 3.4)."""
    _run([settings.unsquashfs, "-s", str(part)], "table de la nouvelle image illisible")
    after = set(scan.list_files(settings, part))
    if after != expected:
        missing = sorted(expected - after)[:5]
        extra = sorted(after - expected)[:5]
        raise RebuildError(f"contenu inattendu : manquants {missing}, en trop {extra}")
    ctx.log(f"vérifié : {len(after)} entrées")
    for rel, data in changes.files.items():
        if scan.unsquashfs_cat(settings, part, rel) != data:
            raise RebuildError(f"{rel} : contenu différent de celui attendu")
    ctx.log(f"vérifié : {len(changes.files)} fichier(s) modifié(s) identiques")


def swap(image: Path, part: Path) -> None:
    """original → .old, .part → original. Le lien physique garde l'image en
    place pendant l'échange ; sans lui (système de fichiers qui ne les gère
    pas), renommage puis retour arrière en cas d'échec."""
    old = old_path(image)
    try:
        os.link(image, old)
        linked = True
    except OSError:
        os.rename(image, old)
        linked = False
    try:
        os.replace(part, image)
    except OSError:
        if not linked:
            os.rename(old, image)
        else:
            old.unlink()
        raise


# ----------------------------------------------------------------- point d'entrée

def rebuild(settings: Settings, image: Path, changes: Changes, ctx: Context | None = None,
            in_use: Callable[[], bool] = lambda: False) -> None:
    """Reconstruit `image` avec `changes`. L'original est gardé en `.old`.

    `in_use` : test « jeu en cours », refait juste avant l'échange (le jeu a
    pu être lancé pendant la construction)."""
    ctx = ctx or Context()
    old, part = old_path(image), part_path(image)
    if old.exists():
        raise RebuildError(f"{old.name} existe : valider ou revenir à l'ancienne d'abord")
    if in_use():
        raise RebuildError("jeu en cours (monté ou lancé)")
    if not changes.files and not changes.delete:
        raise RebuildError("aucun changement")
    stat = image.stat()

    work = settings.tmp_dir / f"rebuild-{uuid.uuid4().hex[:12]}"
    lower, top, merged = work / "lower", work / "top", work / "merged"
    mounted: list[Path] = []
    part.unlink(missing_ok=True)                 # reste d'une tâche interrompue
    _check_space(image.parent, _space_needed(settings, image, changes), str(image.parent))
    try:
        for d in (lower, top, merged):
            d.mkdir(parents=True)
        before = scan.list_files(settings, image)
        if not before:
            raise RebuildError("liste des fichiers de l'image illisible")

        src = _prepare_overlay(settings, image, changes, lower, top, merged, mounted, ctx)
        if src is None:
            src = _prepare_extract(settings, image, changes, work, ctx)

        exclude = None
        if changes.delete:
            exclude = work / "exclude.txt"
            exclude.write_text("".join(p + "\n" for p in changes.delete))
        _mksquashfs(settings, src, part, exclude, ctx)
        ctx.progress(1.0)
        verify(settings, part, expected_listing(before, changes), changes, ctx)

        if ctx.cancel.is_set():
            raise Cancelled("annulé")
        cur = image.stat()
        if (cur.st_size, cur.st_mtime) != (stat.st_size, stat.st_mtime):
            raise RebuildError("l'image a changé pendant la reconstruction")
        if in_use():
            raise RebuildError("jeu lancé pendant la reconstruction : image inchangée")
        swap(image, part)
        ctx.log(f"échangée : {image.name} ({image.stat().st_size / 1e6:.0f} Mo), "
                f"ancienne gardée en {old.name}")
    finally:
        for m in reversed(mounted):
            _umount(m)
        part.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def _prepare_overlay(settings: Settings, image: Path, changes: Changes, lower: Path,
                     top: Path, merged: Path, mounted: list[Path], ctx: Context) -> Path | None:
    """Vue fusionnée image + changements, ou None si FUSE est indisponible."""
    if not _fuse_available(settings):
        ctx.log("FUSE indisponible : repli par extraction")
        return None
    try:
        _run([settings.squashfuse, "-o", "ro", str(image), str(lower)], "squashfuse")
        mounted.append(lower)
        _stage(changes, top, lower)
        # lowerdir : le premier masque le second
        _run([settings.fuse_overlayfs, "-o", f"lowerdir={top}:{lower}", str(merged)],
             "fuse-overlayfs")
        mounted.append(merged)
    except RebuildError as e:
        ctx.log(f"montage impossible ({e}) : repli par extraction")
        for m in reversed(mounted):
            _umount(m)
        mounted.clear()
        return None
    ctx.log("image montée, changements superposés (overlay)")
    return merged


def _prepare_extract(settings: Settings, image: Path, changes: Changes, work: Path,
                     ctx: Context) -> Path:
    root = work / "root"
    _check_space(work, _uncompressed_size(settings, image) + _SPACE_MARGIN, str(work))
    ctx.log("extraction complète (unsquashfs)")
    _run([settings.unsquashfs, "-q", "-no-progress", "-d", str(root), str(image)],
         "unsquashfs", timeout=3600)
    _stage(changes, root, None)
    return root


# ----------------------------------------------------------------- .old

def validate_old(image: Path) -> None:
    """« Valider » : la nouvelle image est bonne, l'ancienne est supprimée."""
    old = old_path(image)
    if not old.exists():
        raise RebuildError("pas d'ancienne image")
    old.unlink()


def revert_old(image: Path, in_use: bool) -> None:
    """« Revenir à l'ancienne » : l'ancienne remplace la nouvelle."""
    old = old_path(image)
    if not old.exists():
        raise RebuildError("pas d'ancienne image")
    if in_use:
        raise RebuildError("jeu en cours (monté ou lancé)")
    os.replace(old, image)
