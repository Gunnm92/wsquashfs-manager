"""Écritures durables.

Après une coupure de courant, ce qui est sur le disque doit être soit
l'ancien état, soit le nouveau, jamais un mélange : les données d'un fichier
peuvent rester en mémoire alors que son renommage est déjà enregistré. D'où :

- `fsync` du fichier avant de le mettre en place, puis du dossier après le
  renommage (le renommage lui-même est une écriture du dossier) ;
- l'original gardé par un lien physique plutôt que renommé : le nom de
  l'image ne disparaît jamais, même une fraction de seconde (un lancement à
  ce moment échouerait) ;
- une nouvelle image publiée par lien physique : l'opération échoue si la
  cible existe, sans fenêtre entre la vérification et l'écriture.

Sans lien physique (système de fichiers qui ne les gère pas), repli sur le
renommage, avec retour arrière.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

# Erreurs qui signifient « pas de lien physique ici », pas un vrai problème
_NO_LINK = {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK, errno.ENOSYS}


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Rend durables les créations, renommages et suppressions du dossier.
    Certains systèmes de fichiers (FUSE, SMB) refusent : rien de mieux à faire."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(target: Path, data: bytes | str, mode: int | None = None,
                 durable: bool = True) -> None:
    """Remplace `target` d'un coup : un lecteur voit l'ancien contenu ou le
    nouveau, jamais un fichier à moitié écrit. `durable=False` pour un cache
    (pas d'attente du disque)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if durable:
        fsync_dir(target.parent)


def append_line(path: Path, line: str) -> None:
    """Ajoute une ligne à un journal, sur le disque au retour."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def publish_new(part: Path, target: Path) -> None:
    """Met `part` en place sous le nom `target`, qui ne doit pas exister
    (FileExistsError sinon) : jamais d'écrasement."""
    fsync_file(part)
    try:
        os.link(part, target)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_LINK:
            raise
        if target.exists():
            raise FileExistsError(errno.EEXIST, "existe déjà", str(target)) from None
        os.rename(part, target)
    else:
        part.unlink()
    fsync_dir(target.parent)


def swap_keep_old(image: Path, part: Path, backup: Path) -> None:
    """`part` remplace `image` ; l'original reste sous le nom `backup`.

    Avec lien physique : `backup` est créé d'abord (même fichier que l'image),
    puis `part` remplace l'image en une opération ; le nom de l'image existe
    à tout instant. En cas d'échec, l'état de départ est rétabli."""
    fsync_file(part)
    try:
        os.link(image, backup)
        linked = True
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_LINK:
            raise
        os.rename(image, backup)
        linked = False
    try:
        os.replace(part, image)
    except BaseException:
        if linked:
            backup.unlink(missing_ok=True)
        else:
            os.rename(backup, image)
        fsync_dir(image.parent)
        raise
    fsync_dir(image.parent)


def restore(backup: Path, image: Path) -> None:
    """L'ancienne version reprend sa place (« revenir à l'ancienne »)."""
    fsync_file(backup)
    os.replace(backup, image)
    fsync_dir(image.parent)
