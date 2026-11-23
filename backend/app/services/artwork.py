"""Illustration d'un jeu.

Affichage (routes /api/image/art et /api/folders/art) : l'illustration INTÉGRÉE seulement,
`.wsquashfs/cover` dans l'image (`embedded_cover`) ou dans le dossier à empaqueter
(`folder_cover`) ; sans elle, les initiales sont affichées par l'interface.

Ajout (run_cover, reconstruction : rebuild.add_cover) : `artwork()`, dans l'ordre
0. SteamGridDB (services/sgdb.py) : icône ronde (`kind=icon`) ou jaquette 600×900
   (`kind=cover`), avec clé API réglée dans Réglages ; cache dans `<state_dir>/art/sgdb` ;
1. Jaquette d'EmulationStation : `gamelist.xml` du dossier du système
   (vignette, boîte, puis capture), à défaut `images/<jeu>-thumb.png`… — le
   chemin doit rester dans le dossier du système ;
2. sinon, l'icône de l'exécutable de `CMD=`, lue dans l'image sans la monter
   (extrait le temps de la lecture, à côté de l'image), la plus grande
   disponible ; mise en cache dans `<state_dir>/art`, absence comprise.

Illustration intégrée lue par `unsquashfs -cat` ; cache dans `<state_dir>/art/embedded`,
absence comprise (`.none`), clé = chemin + date + taille de l'image.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
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
from . import sgdb
from .autorun import exe_from_cmd, normalize_path
from .durable import write_atomic

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
    kind: str           # "embedded" (illustration intégrée) | "sgdb" | "es" | "icon"


# ------------------------------------------------------------------ ES

def _es_media(system_dir: Path, rel: str) -> Artwork | None:
    """Média d'ES (chemin relatif au dossier du système, qu'il ne doit pas quitter)."""
    cand = (system_dir / rel).resolve()
    media = _IMAGE_EXT.get(cand.suffix.lower())
    if media and cand.is_relative_to(system_dir) and cand.is_file():
        return Artwork(cand, media, "es")
    return None


def es_artwork(image: Path) -> Artwork | None:
    system_dir = image.parent.resolve()

    def usable(rel: str) -> Artwork | None:
        return _es_media(system_dir, rel)

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


# ------------------------------------------------------------------ jeux Switch (onglet Sauvegardes)

_TITLE_IN_PATH = re.compile(r"\[([0-9A-Fa-f]{16})\]")
SWITCH_SYSTEMS = ("switch", "switch2")


def switch_artwork(roots: list[str], title_id: str) -> Artwork | None:
    """Jaquette d'un jeu Switch d'après son TitleID (celui des sauvegardes Eden, jeu de base) :
    fiche des gamelist.xml de switch/ et switch2/ sous chaque dossier roms/ dont le chemin porte
    [TitleID] (ou celui d'une mise à jour, …800), mêmes médias que la fiche d'une image."""
    want = title_id.upper()
    if not re.fullmatch(r"[0-9A-F]{16}", want):
        return None
    for root in roots:
        for system in SWITCH_SYSTEMS:
            system_dir = Path(root) / system
            if root.rstrip("/").endswith("/" + system):
                system_dir = Path(root)
            gamelist = system_dir / "gamelist.xml"
            if not gamelist.is_file():
                continue
            try:
                games = ET.parse(gamelist).getroot().iter("game")
            except (ET.ParseError, OSError):
                continue
            system_dir = system_dir.resolve()
            for game in games:
                ids = [m.upper() for m in _TITLE_IN_PATH.findall(game.findtext("path") or "")]
                if not any(i == want or i[:13] + "000" == want for i in ids):
                    continue
                for tag in _ES_TAGS:
                    rel = (game.findtext(tag) or "").strip()
                    if rel and (art := _es_media(system_dir, rel)):
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
    # L'exécutable fait partie de la clé : corrigé par une surcharge (image
    # inchangée), il donne une autre icône
    exe = hashlib.sha1(f"{dir_ or ''}|{cmd or ''}".encode()).hexdigest()[:8]
    stamp = f"{key}-{int(st.st_mtime)}-{st.st_size}-{exe}"
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
                with open(exe, "wb") as dest:
                    subprocess.run([settings.unsquashfs, "-cat", str(image), rel], stdout=dest,
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


# ------------------------------------------------------------------ illustration intégrée

# Chemin de l'illustration dans l'image (même fichier que rebuild.COVER) et dossier qui la porte
COVER_DIR = ".wsquashfs"
EMBEDDED = f"{COVER_DIR}/cover"
_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
          (b"GIF87a", ".gif", "image/gif"), (b"GIF89a", ".gif", "image/gif"))


def _sniff(data: bytes) -> tuple[str, str]:
    """(extension, type MIME) d'après les premiers octets ; inconnu → PNG."""
    for magic, ext, media in _MAGIC:
        if data.startswith(magic):
            return ext, media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".png", "image/png"


def _embedded_dir(settings: Settings) -> Path:
    return settings.state_dir / "art" / "embedded"


def embedded_cover(settings: Settings, image: Path) -> Artwork | None:
    """Illustration intégrée à l'image (`.wsquashfs/cover`), lue sans monter l'image.
    Seule source de l'affichage. Mise en cache par chemin, taille et date : une absence
    l'est aussi (`.none`), et l'image modifiée est relue. Pour un dossier de jeu (`*.pc`), c'est
    le fichier posé dans le dossier, lu tel quel."""
    if image.is_dir():
        return _folder_cover(image)
    try:
        st = image.stat()
    except OSError:
        return None
    key = hashlib.sha1(str(image).encode()).hexdigest()
    stamp = f"{key}-{st.st_mtime_ns}-{st.st_size}"
    cache = _embedded_dir(settings)
    for ext, media in ((".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp"),
                       (".gif", "image/gif")):
        if (cache / f"{stamp}{ext}").is_file():
            return Artwork(cache / f"{stamp}{ext}", media, "embedded")
    if (cache / f"{stamp}.none").exists():
        return None
    with _locks_guard:
        lock = _locks.setdefault(f"embedded:{key}", threading.Lock())
    with lock:
        for ext, media in ((".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp"),
                           (".gif", "image/gif")):
            if (cache / f"{stamp}{ext}").is_file():
                return Artwork(cache / f"{stamp}{ext}", media, "embedded")
        if (cache / f"{stamp}.none").exists():
            return None
        try:
            r = subprocess.run([settings.unsquashfs, "-cat", str(image), EMBEDDED],
                               capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None                             # outil absent ou bloqué : rien en cache
        data = r.stdout if r.returncode == 0 else b""
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.glob(f"{key}-*"):          # versions précédentes de l'image
            old.unlink(missing_ok=True)
        if not data:
            (cache / f"{stamp}.none").touch()
            return None
        ext, media = _sniff(data)
        out = cache / f"{stamp}{ext}"
        out.write_bytes(data)
        return Artwork(out, media, "embedded")


def _folder_cover(folder: Path) -> Artwork | None:
    """Illustration d'un dossier de jeu : `.wsquashfs/cover` de la racine du dossier."""
    cover = folder / EMBEDDED
    try:
        with cover.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if not head:
        return None
    _, media = _sniff(head)
    return Artwork(cover, media, "embedded")


def forget_embedded_misses(settings: Settings) -> None:
    """Actualiser : les absences d'illustration intégrée sont revalidées au prochain affichage."""
    for none in _embedded_dir(settings).glob("*.none"):
        none.unlink(missing_ok=True)


def _suivre_proprietaire(path: Path, source: os.stat_result) -> None:
    """Propriétaire du dossier à empaqueter (au mieux : refusé hors root, sans conséquence)."""
    try:
        os.chown(path, source.st_uid, source.st_gid)
    except OSError:
        pass


def ecrire_cover_dossier(folder: Path, data: bytes) -> Path:
    """Écrit l'illustration du dossier à empaqueter : `<dossier>/.wsquashfs/cover`. Le dossier
    `.wsquashfs` est créé au besoin, avec les permissions et le propriétaire du dossier ; le
    fichier est écrit d'un coup (jamais à moitié). Lève OSError : dossier absent, ou
    `.wsquashfs` qui n'est pas un dossier ordinaire."""
    if folder.is_symlink() or not folder.is_dir():
        raise OSError(f"dossier introuvable : {folder.name}")
    source = folder.stat()
    mode = stat.S_IMODE(source.st_mode)
    cover_dir = folder / COVER_DIR
    if cover_dir.is_symlink() or (cover_dir.exists() and not cover_dir.is_dir()):
        raise OSError(f"{COVER_DIR} n'est pas un dossier ordinaire")
    if not cover_dir.exists():
        cover_dir.mkdir(mode=mode)
        os.chmod(cover_dir, mode)
        _suivre_proprietaire(cover_dir, source)
    target = cover_dir / "cover"
    write_atomic(target, data, mode=(mode & 0o666) or 0o644)
    _suivre_proprietaire(target, source)
    return target


def folder_cover(folder: Path) -> Artwork | None:
    """Illustration d'un dossier à empaqueter : `<dossier>/.wsquashfs/cover`, fichier régulier
    qui reste dans le dossier."""
    cand = folder / EMBEDDED
    try:
        resolved = cand.resolve(strict=True)
        root = folder.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    try:
        with open(resolved, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    _ext, media = _sniff(head)
    return Artwork(resolved, media, "embedded")


# ------------------------------------------------------------------ illustration

def artwork(settings: Settings, image: Path, cmd: str | None, dir_: str | None,
            kind: str = "icon", cle: str | None = None) -> Artwork | None:
    """Illustration d'un jeu : SteamGridDB (icône ronde par défaut, jaquette si `kind`
    « cover », services/sgdb.py), puis icône de l'exécutable et jaquette d'ES (dans cet ordre
    pour une icône, l'inverse pour une jaquette). `cle` : id de l'image (nom mémorisé, cf. sgdb)."""
    if kind == "icon":      # une icône : celle de l'exécutable avant une jaquette
        return (sgdb.sgdb_artwork(settings, image, kind, cle) or exe_icon(settings, image, cmd, dir_)
                or es_artwork(image))
    return (sgdb.sgdb_artwork(settings, image, kind, cle) or es_artwork(image)
            or exe_icon(settings, image, cmd, dir_))
