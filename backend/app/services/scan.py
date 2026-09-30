"""Scan des dossiers roms/ avec cache.

Le cache (taille + mtime de chaque image) évite de relire le CONTENU des
images non modifiées (SPEC § 2.1) : autorun, liste des fichiers, type de
prefix. L'état qui change sans que l'image change — sauvegardes, `.old`,
`.keys`, jeu en cours — est recalculé à chaque scan.

La lecture se fait SANS monter l'image (`unsquashfs -cat`, `unsquashfs -l`),
avec repli squashfuse pour l'autorun si unsquashfs est absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..config import Settings
from ..models import ImageInfo, ImageState, ImageType, RunnerKind
from .autorun import Autorun, unquote

_CACHE_DIR = Path.home() / ".cache/wsquashfs-manager"
_CACHE_VERSION = 2          # à incrémenter quand le contenu mis en cache change

# Dossiers système du prefix : un dinput8.dll qui s'y trouve n'est pas un relais.
_SYSTEM_DIRS = ("drive_c/windows/",)


def image_id(image: Path, system: str | None) -> str:
    return f"{system}/{image.stem}" if system else image.stem


def _find_images(roms_dirs: list[str], max_depth: int) -> list[tuple[Path, str | None]]:
    """Retourne [(chemin image, système), ...], sans descendre au-delà de
    `max_depth` niveaux sous chaque dossier roms/. système = premier
    sous-dossier si l'image est dans roms/<système>/."""
    out: list[tuple[Path, str | None]] = []
    for d in roms_dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for cur, dirnames, filenames in os.walk(root):
            depth = len(Path(cur).relative_to(root).parts)
            if depth + 1 >= max_depth:
                dirnames[:] = []            # ne pas descendre plus bas
            dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
            for f in sorted(filenames):
                if f.startswith(".") or not f.endswith(".wsquashfs"):
                    continue
                p = Path(cur) / f
                rel = p.relative_to(root)
                system = rel.parts[0] if len(rel.parts) > 1 else None
                out.append((p, system))
    return out


def _unsquashfs_cat(settings: Settings, image: Path, member: str) -> bytes | None:
    if not shutil.which(settings.unsquashfs):
        return None
    try:
        r = subprocess.run([settings.unsquashfs, "-cat", str(image), member],
                           capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def _read_autorun_from_image(settings: Settings, image: Path) -> str | None:
    """Lit l'autorun sans montage complet.

    1) `unsquashfs -cat <image> autorun.cmd` (sans montage) ;
    2) repli : squashfuse + lecture du fichier.
    Retourne le contenu texte, ou None.
    """
    data = _unsquashfs_cat(settings, image, "autorun.cmd")
    if data:
        return data.decode("utf-8", errors="replace")
    if shutil.which(settings.squashfuse):
        mnt = _CACHE_DIR / "mnt" / image.stem
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
            for tool in ("fusermount3", "fusermount"):
                try:
                    if subprocess.run([tool, "-u", str(mnt)], capture_output=True,
                                      timeout=10).returncode == 0:
                        break
                except OSError:
                    continue
    return None


def parse_listing(text: str) -> list[str]:
    """Sortie de `unsquashfs -l` (un chemin par ligne, préfixé par
    `squashfs-root`) → chemins relatifs à la racine de l'image, dossiers
    compris. Les noms avec espaces sont conservés tels quels."""
    out = []
    for line in text.splitlines():
        if line == "squashfs-root" or not line.startswith("squashfs-root/"):
            continue
        out.append(line[len("squashfs-root/"):])
    return out


def _list_files(settings: Settings, image: Path) -> list[str]:
    if not shutil.which(settings.unsquashfs):
        return []
    try:
        r = subprocess.run([settings.unsquashfs, "-l", str(image)],
                           capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    return parse_listing(r.stdout.decode("utf-8", errors="replace"))


def detect_type(files: list[str]) -> ImageType:
    """Type d'après les fichiers À LA RACINE de l'image. Un prefix Proton
    (Heroic, Steam) contient aussi system.reg : `config_info` est testé
    d'abord."""
    root = {p.lower() for p in files if "/" not in p}
    if "config_info" in root:
        return ImageType.WINE_PROTON
    if "system.reg" in root:
        return ImageType.WINE_BATOCERA
    return ImageType.GAME_ONLY


def _fakeping_candidates(files: list[str]) -> list[str]:
    """dinput8.dll hors des dossiers système : relais fakeping possible."""
    return [p for p in files
            if p.lower().endswith("/dinput8.dll")
            and not p.lower().startswith(_SYSTEM_DIRS)]


def _has_fakeping(settings: Settings, image: Path, files: list[str]) -> bool:
    for member in _fakeping_candidates(files):
        data = _unsquashfs_cat(settings, image, member)
        if data and b"fakeping" in data:
            return True
    return False


# ----------------------------------------------------------------- état dynamique

def _running_prefixes() -> set[str]:
    """WINEPREFIX de tous les processus, lus une seule fois par scan."""
    out: set[str] = set()
    try:
        pids = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return out
    for pid_dir in pids:
        try:
            env = (pid_dir / "environ").read_bytes()
        except OSError:
            continue
        for var in env.split(b"\0"):
            if var.startswith(b"WINEPREFIX="):
                out.add(var[len(b"WINEPREFIX="):].decode("utf-8", errors="replace"))
    return out


def _mount_points() -> list[str]:
    try:
        text = Path("/proc/mounts").read_text(errors="replace")
    except OSError:
        return []
    # 2e champ ; les espaces y sont encodés en \040
    return [ln.split()[1].replace("\\040", " ") for ln in text.splitlines() if len(ln.split()) > 1]


def is_in_use(stem: str, prefixes: set[str], mounts: list[str]) -> bool:
    """Même test que le lanceur : montage actif du jeu, ou processus dont le
    WINEPREFIX est …/wsquashfs/wine/<jeu> (avec ou sans sous-dossier, ex.
    pfx/ de Proton). Le nom exact est exigé : « Frogger » ≠ « Frogger 2 »."""
    pat = re.compile(r"/wsquashfs/(wine|mnt)/" + re.escape(stem) + r"(/.*)?$")
    return any(pat.search(p) for p in prefixes) or any(pat.search(m) for m in mounts)


def _saves_size(settings: Settings, stem: str) -> tuple[bool, int]:
    d = settings.saves_dir / stem
    if not d.is_dir():
        return False, 0
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return True, total


def _apply_dynamic(settings: Settings, info: ImageInfo, prefixes: set[str],
                   mounts: list[str]) -> ImageInfo:
    """État qui change sans que l'image change : recalculé à chaque scan."""
    image = info.path
    info.saves_present, info.saves_size = _saves_size(settings, image.stem)
    info.has_old = image.with_name(image.name + ".old").exists()
    info.extras = [e for e in info.extras if e != ".keys"]
    if image.with_name(image.name + ".keys").exists():    # fichier À CÔTÉ de l'image
        info.extras.insert(0, ".keys")
    if is_in_use(image.stem, prefixes, mounts):
        info.state = ImageState.IN_USE
    elif info.has_old:
        info.state = ImageState.HAS_OLD
    else:
        info.state = ImageState.OK
    return info


# ----------------------------------------------------------------- scan

def scan_image(settings: Settings, image: Path, system: str | None) -> ImageInfo:
    """Contenu de l'image (mis en cache) ; l'état dynamique est ajouté par
    scan_all."""
    stat = image.stat()
    autorun_text = _read_autorun_from_image(settings, image)
    files = _list_files(settings, image)
    a = Autorun.parse(autorun_text) if autorun_text else None

    info = ImageInfo(
        id=image_id(image, system),
        path=image,
        name=image.stem,
        system=system,
        size=stat.st_size,
        mtime=stat.st_mtime,
        type=detect_type(files),
    )
    if a:
        version = a.get("GAME_VERSION")
        info.version = unquote(version.strip()) if version else None
        info.wine = a.get("WINE")
        info.proton = a.get("PROTON")
        if a.get("RUNNER"):
            info.runner = RunnerKind.CUSTOM
        elif info.proton:
            info.runner = RunnerKind.PROTON
        elif info.wine:
            info.runner = RunnerKind.WINE
        info.cmd = a.get("CMD")
        info.dir = a.get("DIR")
        hidraw = a.get("HIDRAW")
        info.hidraw = (hidraw.strip() == "1") if hidraw is not None else None
        # DXVK/VKD3D actifs par défaut dans le lanceur : seul "0" les coupe
        info.dxvk = (a.get("DXVK") or "1").strip() != "0"
        info.vkd3d = (a.get("VKD3D") or "1").strip() != "0"
    info.xinput = info.hidraw is not True
    if _has_fakeping(settings, image, files):
        info.extras.append("fakeping")
    return info


def scan_all(settings: Settings, force: bool = False) -> list[ImageInfo]:
    cache_file = _CACHE_DIR / "scan_cache.json"
    cache: dict = {}
    if not force and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}
    if cache.get("version") != _CACHE_VERSION:
        cache = {}
    entries = cache.get("images", {})

    prefixes, mounts = _running_prefixes(), _mount_points()
    results: list[ImageInfo] = []
    new_entries: dict[str, dict] = {}
    for image, system in _find_images(settings.roms_dirs, settings.scan_max_depth):
        key = str(image)
        try:
            stat = image.stat()
        except OSError:
            continue
        entry = entries.get(key)
        if entry and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime:
            info = ImageInfo.model_validate(entry["info"])
        else:
            info = scan_image(settings, image, system)
        new_entries[key] = {
            "size": info.size,
            "mtime": info.mtime,
            "info": info.model_dump(mode="json"),
        }
        results.append(_apply_dynamic(settings, info, prefixes, mounts))

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"version": _CACHE_VERSION, "images": new_entries},
                                     indent=1))
    return results
