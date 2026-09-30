"""Scan des dossiers roms/ avec cache.

Le cache (taille + mtime de chaque image) évite de relire les images non
modifiées (SPEC § 2.1). La lecture de `GAME_VERSION` se fait SANS monter
l'image : `unsquashfs -cat <image> autorun.cmd` quand l'outil est dispo,
sinon on monte via squashfuse.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import Settings
from ..models import ImageInfo, ImageState, ImageType, RunnerKind

_VERSION_CACHE_DIR = Path.home() / ".cache/wsquashfs-manager"


def _find_images(roms_dirs: list[str]) -> list[tuple[Path, str | None]]:
    """Retourne [(chemin image, système), ...]. système = nom du sous-dossier
    si l'image est dans roms/<système>/."""
    out: list[tuple[Path, str | None]] = []
    for d in roms_dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.wsquashfs")):
            if p.name.startswith("."):
                continue
            # système : premier niveau sous la racine roms/
            rel = p.relative_to(root)
            system = rel.parts[0] if len(rel.parts) > 1 else None
            out.append((p, system))
    return out


def _read_autorun_from_image(settings: Settings, image: Path) -> str | None:
    """Lit l'autorun sans montage complet.

    1) `unsquashfs -cat <image> autorun.cmd` (sans montage) ;
    2) repli : squashfuse + lecture du fichier.
    Retourne le contenu texte, ou None.
    """
    if shutil.which(settings.unsquashfs):
        try:
            r = subprocess.run(
                [settings.unsquashfs, "-cat", str(image), "autorun.cmd"],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            pass
    # repli squashfuse
    if shutil.which(settings.squashfuse):
        mnt = _VERSION_CACHE_DIR / "mnt" / image.stem
        mnt.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([settings.squashfuse, "-o", "ro", str(image), str(mnt)],
                           check=True, timeout=30)
            p = mnt / "autorun.cmd"
            if p.exists():
                return p.read_bytes().decode("utf-8", errors="replace")
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            try:
                subprocess.run(["fusermount", "-u", str(mnt)], timeout=10)
            except OSError:
                pass
    return None


def _version_from_autorun(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("GAME_VERSION="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _detect_type(image_dir_listing: list[str]) -> ImageType:
    """Type d'après la liste des fichiers (sans montage, `unsquashfs -l`)."""
    lower = {p.lower() for p in image_dir_listing}
    has_system_reg = any("system.reg" in p for p in lower)
    has_config_info = any("config_info" in p for p in lower)
    if has_system_reg:
        return ImageType.WINE_BATOCERA
    if has_config_info:
        return ImageType.WINE_PROTON
    return ImageType.GAME_ONLY


def _list_files(settings: Settings, image: Path) -> list[str]:
    if not shutil.which(settings.unsquashfs):
        return []
    try:
        r = subprocess.run([settings.unsquashfs, "-l", str(image)],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 7:
                out.append(parts[-1])
        return out
    except (subprocess.TimeoutExpired, OSError):
        return []


def _in_use(image: Path) -> bool:
    """Même test que le lanceur : montage actif (fusermount) ou WINEPREFIX
    dans /proc."""
    try:
        r = subprocess.run(["fusermount", "-v"], capture_output=True, timeout=10)
        if r.returncode == 0 and str(image) in r.stdout.decode(errors="replace"):
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    stem = image.stem
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            envf = pid_dir / "environ"
            try:
                env = envf.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            for var in env.split("\0"):
                if var.startswith("WINEPREFIX=") and stem.lower() in var.lower():
                    return True
    except OSError:
        pass
    return False


def _saves_size(settings: Settings, stem: str) -> tuple[bool, int]:
    d = settings.saves_dir / stem
    if not d.is_dir():
        return False, 0
    total = 0
    for p in d.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return True, total


def scan_image(settings: Settings, image: Path, system: str | None) -> ImageInfo:
    stat = image.stat()
    autorun_text = _read_autorun_from_image(settings, image)
    files = _list_files(settings, image)
    t = _detect_type(files)

    wine = proton = None
    if autorun_text:
        from .autorun import Autorun
        a = Autorun.parse(autorun_text)
        wine = a.get("WINE")
        proton = a.get("PROTON")

    info = ImageInfo(
        path=image,
        name=image.stem,
        system=system,
        size=stat.st_size,
        mtime=stat.st_mtime,
        version=_version_from_autorun(autorun_text),
        type=t,
        wine=wine,
        proton=proton,
    )

    # runner
    if autorun_text:
        from .autorun import Autorun
        a = Autorun.parse(autorun_text)
        if a.get("RUNNER"):
            info.runner = RunnerKind.CUSTOM
        elif a.get("PROTON"):
            info.runner = RunnerKind.PROTON
        elif a.get("WINE"):
            info.runner = RunnerKind.WINE
        info.cmd = a.get("CMD")
        info.dir = a.get("DIR")
        info.hidraw = a.get("HIDRAW") == "1" if a.get("HIDRAW") is not None else None
        info.xinput = "xinput" in (a.get("ENV") or "").lower()
        info.dxvk = a.get("DXVK") == "1"
        info.vkd3d = a.get("VKD3D") == "1"
        if any(p.lower().endswith(".keys") for p in files):
            info.extras.append(".keys")
        if any("fakeping" in p.lower() for p in files):
            info.extras.append("fakeping")

    saves, size = _saves_size(settings, image.stem)
    info.saves_present = saves
    info.saves_size = size
    info.has_old = image.with_name(image.name + ".old").exists()

    if _in_use(image):
        info.state = ImageState.IN_USE
    elif info.has_old:
        info.state = ImageState.HAS_OLD
    return info


def scan_all(settings: Settings, force: bool = False) -> list[ImageInfo]:
    cache_file = _VERSION_CACHE_DIR / "scan_cache.json"
    cache: dict[str, dict] = {}
    if not force and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}

    results: list[ImageInfo] = []
    new_cache: dict[str, dict] = {}
    for image, system in _find_images(settings.roms_dirs):
        key = str(image)
        stat = image.stat()
        entry = cache.get(key)
        if entry and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime:
            info = ImageInfo.model_validate_json(json.dumps(entry["info"]))
        else:
            info = scan_image(settings, image, system)
        new_cache[key] = {
            "size": info.size,
            "mtime": info.mtime,
            "info": info.model_dump(mode="json"),
        }
        results.append(info)

    _VERSION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(new_cache, indent=1))
    return results
