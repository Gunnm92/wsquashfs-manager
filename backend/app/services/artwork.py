"""Illustration d'un jeu pour la fiche.

1. Jaquette d'EmulationStation : `gamelist.xml` du dossier du système
   (vignette, boîte, puis capture), à défaut `images/<jeu>-thumb.png`… — le
   chemin doit rester dans le dossier du système ;
2. sinon, l'icône de l'exécutable de `CMD=`, lue dans l'image sans la monter
   (extrait le temps de la lecture, à côté de l'image), la plus grande
   disponible ; mise en cache dans `<state_dir>/art`, absence comprise.
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pefile

from ..config import Settings, work_area
from .autorun import exe_from_cmd, normalize_path

# Ordre de préférence des médias d'ES : une boîte ou une vignette rend mieux
# qu'une capture d'écran dans une vignette carrée
_ES_TAGS = ("thumbnail", "boxart", "image", "titleshot", "fanart", "marquee", "wheel")
_ES_SUFFIXES = ("-thumb", "-boxart", "-image", "-titleshot", "-fanart", "-marquee")
_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}
_PNG = b"\x89PNG\r\n\x1a\n"
RT_ICON, RT_GROUP_ICON = 3, 14

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass
class Artwork:
    path: Path
    media_type: str
    kind: str           # "es" (jaquette) | "icon" (icône de l'exécutable)


# ------------------------------------------------------------------ ES

def es_artwork(image: Path) -> Artwork | None:
    system_dir = image.parent.resolve()

    def usable(rel: str) -> Artwork | None:
        cand = (system_dir / rel).resolve()
        media = _IMAGE_EXT.get(cand.suffix.lower())
        if media and cand.is_relative_to(system_dir) and cand.is_file():
            return Artwork(cand, media, "es")
        return None

    gamelist = system_dir / "gamelist.xml"
    if gamelist.is_file():
        try:
            root = ET.parse(gamelist).getroot()
        except (ET.ParseError, OSError):
            root = None
        target = normalize_path(image.name)
        for game in root.iter("game") if root is not None else ():
            if normalize_path(game.findtext("path") or "") != target:
                continue
            for tag in _ES_TAGS:
                rel = (game.findtext(tag) or "").strip()
                if rel and (art := usable(rel)):
                    return art
            break
    for suffix in _ES_SUFFIXES:
        for ext in _IMAGE_EXT:
            if art := usable(f"images/{image.stem}{suffix}{ext}"):
                return art
    return None


# ------------------------------------------------------------------ icône

def best_icon(pe: pefile.PE) -> tuple[bytes, str] | None:
    """Plus grande icône du premier groupe d'icônes : (données, type MIME).
    Une icône PNG est rendue telle quelle, une icône BMP dans un fichier .ico
    d'une seule image."""
    resources = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if resources is None:
        return None
    by_type = {e.id: e for e in resources.entries if e.id is not None}
    if RT_GROUP_ICON not in by_type or RT_ICON not in by_type:
        return None

    def first_data(entry):
        # type → nom/identifiant → langue → données
        for lang in entry.directory.entries:
            return pe.get_data(lang.data.struct.OffsetToData, lang.data.struct.Size)
        return b""

    icons = {e.id: e for e in by_type[RT_ICON].directory.entries if e.id is not None}
    for group in by_type[RT_GROUP_ICON].directory.entries:
        data = first_data(group)
        if len(data) < 6:
            continue
        count = struct.unpack_from("<H", data, 4)[0]
        entries = []
        for n in range(count):
            off = 6 + 14 * n
            if off + 14 > len(data):
                break
            w, h, colors, _res, planes, bits, _size, icon_id = struct.unpack_from(
                "<BBBBHHIH", data, off)
            if icon_id in icons:
                entries.append(((w or 256) * (h or 256), bits, w, h, colors, planes, icon_id))
        if not entries:
            continue
        _area, bits, w, h, colors, planes, icon_id = max(entries)
        raw = first_data(icons[icon_id])
        if not raw:
            continue
        if raw.startswith(_PNG):
            return raw, "image/png"
        header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, colors, 0, planes,
                                                            bits, len(raw), 22)
        return header + raw, "image/x-icon"
    return None


def _exe_path(image: Path, cmd: str | None, dir_: str | None, unsquashfs: str) -> str | None:
    """Chemin réel (casse de l'image) de l'exécutable de CMD."""
    exe = exe_from_cmd(cmd or "")
    if not exe.lower().endswith(".exe"):
        return None
    want = normalize_path(f"{dir_ or ''}/{exe}")
    try:
        r = subprocess.run([unsquashfs, "-l", str(image)], capture_output=True, timeout=300,
                           check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("squashfs-root/"):
            rel = line[len("squashfs-root/"):]
            if normalize_path(rel) == want:
                return rel
    return None


@contextmanager
def _scratch(settings: Settings, image: Path):
    """Dossier où extraire l'exécutable : à côté de l'image (même disque),
    sinon dans l'appdata si le dossier du jeu est en lecture seule."""
    if os.access(settings.tmp_dir or image.parent, os.W_OK):
        with work_area(settings, image, "art-") as tmp:
            yield tmp
        return
    base = settings.state_dir / "art"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="art-", dir=base) as tmp:
        yield Path(tmp)


def exe_icon(settings: Settings, image: Path, cmd: str | None, dir_: str | None) -> Artwork | None:
    st = image.stat()
    key = hashlib.sha1(str(image).encode()).hexdigest()[:16]
    stamp = f"{key}-{int(st.st_mtime)}-{st.st_size}"
    cache = settings.state_dir / "art"
    for ext, media in ((".png", "image/png"), (".ico", "image/x-icon")):
        if (cache / f"{stamp}{ext}").is_file():
            return Artwork(cache / f"{stamp}{ext}", media, "icon")
    if (cache / f"{stamp}.none").exists():
        return None

    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if (cache / f"{stamp}.none").exists():
            return None
        result = None
        rel = _exe_path(image, cmd, dir_, settings.unsquashfs)
        if rel:
            with _scratch(settings, image) as tmp:
                exe = tmp / "exe.bin"
                with open(exe, "wb") as out:
                    subprocess.run([settings.unsquashfs, "-cat", str(image), rel], stdout=out,
                                   stderr=subprocess.DEVNULL, timeout=900, check=False)
                try:
                    pe = pefile.PE(str(exe), fast_load=True)
                    pe.parse_data_directories(directories=[
                        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
                    result = best_icon(pe)
                    pe.close()
                except Exception:   # noqa: BLE001 — un PE illisible : pas d'icône
                    result = None
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.glob(f"{key}-*"):          # versions précédentes de l'image
            old.unlink(missing_ok=True)
        if result is None:
            (cache / f"{stamp}.none").touch()
            return None
        data, media = result
        out = cache / f"{stamp}{'.png' if media == 'image/png' else '.ico'}"
        out.write_bytes(data)
        return Artwork(out, media, "icon")


def artwork(settings: Settings, image: Path, cmd: str | None, dir_: str | None) -> Artwork | None:
    return es_artwork(image) or exe_icon(settings, image, cmd, dir_)
