"""Illustration d'un jeu pour la fiche.

0. Illustration Steam (Steam ROM Manager) : demandée aux machines de la
   synchronisation (cache dans `<state_dir>/art/steam`, voir steam_artwork) ;
1. Jaquette d'EmulationStation : `gamelist.xml` du dossier du système
   (vignette, boîte, puis capture), à défaut `images/<jeu>-thumb.png`… — le
   chemin doit rester dans le dossier du système ;
2. sinon, l'icône de l'exécutable de `CMD=`, lue dans l'image sans la monter
   (extrait le temps de la lecture, à côté de l'image), la plus grande
   disponible ; mise en cache dans `<state_dir>/art`, absence comprise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pefile

from ..config import Settings, work_area
from .autorun import exe_from_cmd, normalize_path
from .rebuild import RebuildError
from .saves import game_host_machine, machine_rows, open_machine

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
    kind: str           # "steam" (Steam ROM Manager) | "es" (jaquette) | "icon" (icône de l'exécutable)


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


# ------------------------------------------------------------------ Steam (machines)
# Illustration posée dans Steam par Steam ROM Manager, lue sur la machine (agent v10,
# GET /v1/games/art). Source prioritaire : jaquette d'ES et icône viennent ensuite.

STEAM_TTL = 600         # s : au-delà, la machine est réinterrogée (If-None-Match)
STEAM_DOWN = 60         # s : machine injoignable, pas réinterrogée avant ce délai
STEAM_TIMEOUT = 5       # s
_STEAM_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
              "image/x-icon": ".ico"}
_steam_down: dict[str, float] = {}     # machine id -> instant où la réinterroger


def _steam_dir(settings: Settings) -> Path:
    return settings.state_dir / "art" / "steam"


def _read_meta(path: Path) -> dict | None:
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _write_atomic(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(path.name + ".part")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def _steam_hit(base: Path, key: str, meta: dict) -> Artwork | None:
    media = meta.get("media_type", "")
    path = base / f"{key}{_STEAM_EXT.get(media, '.img')}"
    return Artwork(path, media, "steam") if path.is_file() else None


def _steam_fresh(base: Path, key: str, now: float) -> tuple[bool, Artwork | None]:
    """(vrai si le cache répond sans interroger de machine, illustration ou None)."""
    meta = _read_meta(base / f"{key}.json")
    if meta and now - float(meta.get("checked", 0)) < STEAM_TTL and (art := _steam_hit(base, key, meta)):
        return True, art
    none = base / f"{key}.none"
    if none.is_file():
        try:
            checked = float(none.read_text())
        except ValueError:
            checked = 0.0
        if now - checked < STEAM_TTL:
            return True, None
    return False, None


def _drop_steam_files(base: Path, key: str) -> None:
    for old in base.glob(f"{key}.*"):
        old.unlink(missing_ok=True)


def forget_steam_cache(settings: Settings) -> None:
    """Actualiser : tout le cache Steam est à revalider au prochain appel (les illustrations
    modifiées dans Steam sont alors relues ; une absence est à nouveau demandée)."""
    base = _steam_dir(settings)
    if not base.is_dir():
        return
    for meta_file in base.glob("*.json"):
        meta = _read_meta(meta_file)
        if meta is not None:
            meta["checked"] = 0
            _write_atomic(meta_file, json.dumps(meta))
    for none in base.glob("*.none"):
        none.unlink(missing_ok=True)
    _steam_down.clear()


def _steam_fetch(settings: Settings, image: Path, kind: str, key: str, ids: list[str]) -> Artwork | None:
    base = _steam_dir(settings)
    base.mkdir(parents=True, exist_ok=True)
    meta_path = base / f"{key}.json"
    meta = _read_meta(meta_path) or {}
    stale = _steam_hit(base, key, meta) if meta else None
    answered_absent = True          # toutes les machines joignables répondent 404
    unreachable = False
    for machine in ids:
        now = time.time()
        if _steam_down.get(machine, 0.0) > now:
            unreachable = True
            continue
        # L'ETag ne vaut que pour la machine qui l'a donné
        etag = meta.get("etag", "") if machine == meta.get("machine") else ""
        host = None
        try:
            host = open_machine(settings, machine, timeout=STEAM_TIMEOUT)
            got = host.games_art(image.name, kind, etag=etag, timeout=STEAM_TIMEOUT)
        except (RebuildError, httpx.HTTPError, OSError, ValueError):
            _steam_down[machine] = time.time() + STEAM_DOWN
            unreachable = True
            answered_absent = False
            continue
        finally:
            if host is not None:
                host.client.close()
        if got is None:
            continue                                  # 404 : Steam n'a rien sur cette machine
        media, data, new_etag = got
        if media == "not-modified":
            if stale is not None:
                _write_atomic(meta_path, json.dumps({**meta, "checked": now}))
                return stale
            answered_absent = False
            continue
        if not media.startswith("image/") or not data:
            answered_absent = False
            continue
        _drop_steam_files(base, key)
        path = base / f"{key}{_STEAM_EXT.get(media, '.img')}"
        _write_atomic(path, data)
        _write_atomic(meta_path, json.dumps({"etag": new_etag, "machine": machine,
                                             "media_type": media, "checked": now}))
        return Artwork(path, media, "steam")
    if unreachable or not answered_absent:
        # Pas de réponse sûre : on sert ce qui est en cache, sinon rien (repli ES / icône)
        return stale
    _drop_steam_files(base, key)
    _write_atomic(base / f"{key}.none", str(time.time()))
    return None


def steam_artwork(settings: Settings, image: Path, kind: str = "cover") -> Artwork | None:
    """Illustration Steam du jeu (`kind` : "cover" ou "icon"), d'abord de la machine d'essai
    puis des autres machines associées. Cache : servi tel quel moins de STEAM_TTL secondes
    après la dernière vérification, sinon revalidé (ETag) ; absence mise en cache aussi."""
    host = game_host_machine(settings)
    ids = [host] if host else []
    ids += [row["id"] for row in machine_rows(settings) if row["id"] not in ids]
    if not ids:
        return None
    base = _steam_dir(settings)
    key = hashlib.sha1(f"{image.name.lower()}|{kind}".encode()).hexdigest()
    fresh, art = _steam_fresh(base, key, time.time())
    if fresh:
        return art
    with _locks_guard:
        lock = _locks.setdefault(f"steam:{key}", threading.Lock())
    with lock:
        fresh, art = _steam_fresh(base, key, time.time())
        if fresh:
            return art
        return _steam_fetch(settings, image, kind, key, ids)


def artwork(settings: Settings, image: Path, cmd: str | None, dir_: str | None,
            kind: str = "cover") -> Artwork | None:
    return steam_artwork(settings, image, kind) or es_artwork(image) or exe_icon(settings, image, cmd, dir_)
