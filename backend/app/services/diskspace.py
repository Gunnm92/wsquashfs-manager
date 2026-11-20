"""Espace disque nécessaire à une reconstruction : taille décompressée, besoin par point de
montage, contrôle de l'espace libre."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings
from .errors import ManagerError
from .squashfs import run, tree_entries

if TYPE_CHECKING:
    from .rebuild import Changes

# Marge sur la taille de la nouvelle image (≈ l'ancienne) : la compression
# d'un fichier remplacé peut différer.
_SPACE_MARGIN = 1.05


def uncompressed_size(settings: Settings, image: Path) -> int:
    """Somme des tailles des fichiers de l'image (`unsquashfs -lls`, table seule)."""
    out = run([settings.unsquashfs, "-lls", str(image)], timeout=300).stdout
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
        if same_fs(tmp, image_dir):
            needs[image_dir] += extract
        else:
            needs[tmp] = extract
    return needs


def same_fs(a: Path, b: Path) -> bool:
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
            raise ManagerError(f"espace insuffisant sur {path} : {human(need)} nécessaires, "
                               f"{human(free)} libres")


def human(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} To"
