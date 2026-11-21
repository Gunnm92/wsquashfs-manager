"""Réserve du manager : archives des sauvegardes, versions de conflit, états par client.

- Réserve : `reserve_dir/<jeu>/` (par défaut `state_dir/saves-reserve`) avec `current.tar.gz`, `meta.json`
  et, avant chaque remplacement, l'ancienne version datée (3 gardées).
- États : l'empreinte commune d'un jeu avec chaque client (`record_state`,
  `base_state`) et les dates de dernière synchro.

Les écritures passent par durable.py (fichier .part, fsync, échange par lien
physique : l'ancienne version reste sous son nom daté). Ce module ne parle à
aucun client : il lit et écrit la réserve seulement.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from .durable import fsync_dir, publish_new, swap_keep_old, write_atomic
from .errors import ManagerError
from .saves import DIR_PREFIX, check_game, clean_dir, valid_game

KEEP_ARCHIVES = 3                   # versions datées gardées par jeu
RESERVE_DIR = "saves-reserve"
CURRENT = "current.tar.gz"
META = "meta.json"
STORE_LOCK = threading.Lock()       # meta.json de la réserve : un écrit à la fois (envoi d'un client, synchro auto)


def reserve_root(settings: Settings) -> Path:
    return settings.reserve_dir or settings.state_dir / RESERVE_DIR


def reserve_dir(settings: Settings, game: str) -> Path:
    return reserve_root(settings) / check_game(game)


def reserve_games(settings: Settings) -> list[str]:
    """Jeux ayant une version courante dans le manager."""
    root = reserve_root(settings)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and valid_game(d.name) and (d / CURRENT).is_file())


def reserve_meta(settings: Settings, game: str) -> dict | None:
    try:
        data = json.loads((reserve_dir(settings, game) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def archives(settings: Settings, game: str) -> list[str]:
    """Versions datées conservées, la plus récente d'abord (AAAAMMJJ-HHMMSS)."""
    return archives_in(reserve_dir(settings, game))


def new_stamp(d: Path) -> str:
    base = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 — heure locale, pour l'utilisateur
    name, n = base, 2
    while (d / f"{name}.tar.gz").exists() or (d / f"{name}.json").exists():
        name, n = f"{base}-{n:03d}", n + 1
    return name


def prune(d: Path) -> None:
    for name in archives_in(d)[KEEP_ARCHIVES:]:
        (d / f"{name}.tar.gz").unlink(missing_ok=True)
        (d / f"{name}.json").unlink(missing_ok=True)


CONFLICT_RE = re.compile(r"^(\d{8}-\d{6}(?:-\d{3})?)-conflit-(.+)$")   # <horodatage>-conflit-<client>


def archives_in(d: Path) -> list[str]:
    """Versions datées normales (les versions de conflit, à départager, sont à part :
    conflict_versions)."""
    if not d.is_dir():
        return []
    return sorted((p.name[:-len(".tar.gz")] for p in d.glob("*.tar.gz")
                   if p.name != CURRENT and not CONFLICT_RE.match(p.name[:-len(".tar.gz")])),
                  reverse=True)


def write_part(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def store(settings: Settings, game: str, part: Path, *, fingerprint: str, newest: int,
          size: int, profile: str | None, source: str, dir_path: str | None = None,
          content: str = "") -> dict:
    """Range une archive reçue (`part`, écrite entièrement) dans le manager ; renvoie les
    informations enregistrées (meta). Voir store_archived."""
    return store_archived(settings, game, part, fingerprint=fingerprint, newest=newest,
                          size=size, profile=profile, source=source, dir_path=dir_path,
                          content=content)[0]


def store_archived(settings: Settings, game: str, part: Path, *, fingerprint: str, newest: int,
                   size: int, profile: str | None, source: str,
                   dir_path: str | None = None, content: str = "") -> tuple[dict, str]:
    """Range une archive reçue (`part`, écrite entièrement) dans le manager. Renvoie (meta,
    nom de la version archivée ou "").

    Même empreinte que la version courante : rien n'est archivé, seules les
    informations sont rafraîchies. Sinon l'ancienne version devient
    `<AAAAMMJJ-HHMMSS>.tar.gz` (+ `.json`) et `part` prend sa place ; les
    versions datées au-delà de KEEP_ARCHIVES sont supprimées. `size` : octets
    des fichiers de sauvegarde, tels que le client les a comptés. Les états par
    machine (meta « states ») sont gardés. `content` : empreinte de contenu (sans dates) donnée
    par les agents version 10 ; "" si inconnue (ancien agent)."""
    d = reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    current, meta_path = d / CURRENT, d / META
    prior = reserve_meta(settings, game) or {}
    old = prior if current.exists() else None
    states = prior.get("states") if isinstance(prior.get("states"), dict) else {}
    meta = {"fingerprint": fingerprint, "newest": int(newest), "size": int(size), "profile": profile,
            "source": source, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"), "states": states,
            "content": content}
    # Dossier déclaré : son chemin à partir du HOME du client, transmis à un client qui ne l'a
    # pas déclaré (elle le déclare à la réception). Gardé d'une version à l'autre.
    dir_kept = clean_dir(dir_path) or clean_dir(prior.get("dir"))
    if game.startswith(DIR_PREFIX) and dir_kept:
        meta["dir"] = dir_kept
    text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    if old is not None and old.get("fingerprint") == fingerprint:
        if not content and old.get("content"):
            meta["content"] = str(old["content"])           # même empreinte : le contenu connu reste
            text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
        part.unlink(missing_ok=True)
        write_atomic(meta_path, text)
        return meta, ""
    stamp = new_stamp(d)
    archived = ""
    if current.exists():
        swap_keep_old(current, part, d / f"{stamp}.tar.gz")
        archived = f"{stamp}.tar.gz"
    else:
        publish_new(part, current)
    if old is not None:
        meta_part = d / "meta.json.part"
        write_part(meta_part, text)
        swap_keep_old(meta_path, meta_part, d / f"{stamp}.json")
    else:
        write_atomic(meta_path, text)
    prune(d)
    return meta, archived


def current_fingerprint(settings: Settings, game: str) -> str:
    """Empreinte de la version courante du manager ; "" s'il n'en a pas."""
    if not (reserve_dir(settings, game) / CURRENT).is_file():
        return ""
    return str((reserve_meta(settings, game) or {}).get("fingerprint") or "")


def current_content(settings: Settings, game: str) -> str:
    """Empreinte de contenu (sans dates) de la version courante ; "" si inconnue ou sans version."""
    if not (reserve_dir(settings, game) / CURRENT).is_file():
        return ""
    return str((reserve_meta(settings, game) or {}).get("content") or "")


def machine_state(settings: Settings, game: str, machine_id: str) -> str:
    """Empreinte enregistrée pour un client (synchro automatique) ; "" sinon."""
    states = (reserve_meta(settings, game) or {}).get("states")
    value = states.get(machine_id) if isinstance(states, dict) else None
    return str(value or "")


def base_state(settings: Settings, game: str, machine_id: str, entry: dict | None) -> tuple[str, str]:
    """(base, origine) d'un (jeu, client) pour `decide` : l'empreinte enregistrée par le manager
    (« manager »), à défaut celle que le client déclare avoir échangée avec ce manager
    (`state` de saves_list, « client »), sinon ("", ""). Lecture seule : rien n'est enregistré."""
    own = machine_state(settings, game, machine_id)
    if own:
        return own, "manager"
    declared = str((entry or {}).get("state") or "")
    return (declared, "client") if declared else ("", "")


def record_state(settings: Settings, game: str, machine_id: str, fingerprint: str) -> None:
    """Enregistre que le client a cette empreinte pour ce jeu, et quand (`synced`, secondes epoch :
    dernière synchro de ce client pour ce jeu) ; meta.json, écriture atomique."""
    meta = reserve_meta(settings, game) or {}
    states = dict(meta["states"]) if isinstance(meta.get("states"), dict) else {}
    states[machine_id] = fingerprint
    meta["states"] = states
    synced = dict(meta["synced"]) if isinstance(meta.get("synced"), dict) else {}
    synced[machine_id] = int(time.time())
    meta["synced"] = synced
    write_atomic(reserve_dir(settings, game) / META, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


def conflict_versions(settings: Settings, game: str) -> list[dict]:
    """Versions de conflit du jeu (copies de clients à départager), la plus récente d'abord :
    {name, machine_id, source, newest, size, fingerprint, content, path}. `name` est le nom du
    fichier sans .tar.gz (« <horodatage>-conflit-<client> »)."""
    d = reserve_dir(settings, game)
    out = []
    for name in sorted((p.name[:-len(".tar.gz")] for p in d.glob("*.tar.gz")) if d.is_dir() else (), reverse=True):
        found = CONFLICT_RE.match(name)
        if not found:
            continue
        try:
            meta = json.loads((d / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        out.append({"name": name, "machine_id": str(meta.get("machine_id") or found.group(2)),
                    "source": str(meta.get("source") or ""), "newest": int(meta.get("newest") or 0),
                    "size": int(meta.get("size") or 0), "fingerprint": str(meta.get("fingerprint") or ""),
                    "content": str(meta.get("content") or ""), "path": d / f"{name}.tar.gz"})
    return out


def store_conflict(settings: Settings, game: str, part: Path, *, machine_id: str, fingerprint: str,
                   newest: int, size: int, source: str, content: str = "",
                   profile: str | None = None, dir_path: str | None = None) -> dict:
    """Range la copie d'un client en vrai conflit SANS toucher à la version courante ni aux
    états : `<horodatage>-conflit-<client>.tar.gz` + `.json` dans le dossier du jeu. Une version
    de conflit de ce client de même empreinte existe déjà : rien n'est écrit (`part` supprimée).
    Renvoie {name, created}. Appelant : sous STORE_LOCK. Ces versions ne sont pas élaguées
    (prune) : seul resolve les départage."""
    d = reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    for known in conflict_versions(settings, game):
        if known["machine_id"] == machine_id and known["fingerprint"].lower() == fingerprint.lower():
            part.unlink(missing_ok=True)
            return {"name": known["name"], "created": False}
    name = f"{new_stamp(d)}-conflit-{machine_id}"
    publish_new(part, d / f"{name}.tar.gz")
    meta = {"machine_id": machine_id, "source": source, "newest": int(newest), "size": int(size),
            "fingerprint": fingerprint, "content": content, "profile": profile,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if clean_dir(dir_path):
        meta["dir"] = dir_path
    write_atomic(d / f"{name}.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return {"name": name, "created": True}


def remove_from_manager(settings: Settings, game: str) -> dict:
    """Retire la copie du jeu du manager : la version courante devient une version
    datée (avec son .json, comme store() lors d'un remplacement), puis current.tar.gz
    et meta.json disparaissent. Les versions datées sont gardées (KEEP_ARCHIVES).
    Absent du manager : rien à faire, sans erreur. {ok, message, archived}."""
    d = reserve_dir(settings, game)
    current, meta_path = d / CURRENT, d / META
    with STORE_LOCK:        # pas d'envoi d'un client entre le retrait de l'archive et celui de meta.json
        if not current.is_file():
            return {"ok": True, "message": "pas sur le manager", "archived": False}
        stamp = new_stamp(d)
        # os.replace : renommage atomique dans le même dossier, jamais de version à moitié écrite
        os.replace(current, d / f"{stamp}.tar.gz")
        if meta_path.is_file():
            os.replace(meta_path, d / f"{stamp}.json")
        fsync_dir(d)
        prune(d)
    return {"ok": True, "message": "supprimée du manager (version archivée)", "archived": True}


def archive_versions(settings: Settings, game: str) -> list[dict]:
    """Versions datées du jeu, la plus récente d'abord : {name, archived_at, newest, size, source,
    fetched_at}. `name` : horodatage (nom du fichier sans .tar.gz) ; `archived_at` : date à
    laquelle la version a été remplacée (lue dans le nom, heure locale), en secondes epoch."""
    d = reserve_dir(settings, game)
    out = []
    for name in archives_in(d):
        try:
            meta = json.loads((d / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        try:
            archived_at = int(datetime.strptime(name[:15], "%Y%m%d-%H%M%S").timestamp())  # noqa: DTZ007 — heure locale (new_stamp)
        except ValueError:
            archived_at = 0
        out.append({"name": name, "archived_at": archived_at, "newest": int(meta.get("newest") or 0),
                    "size": int(meta.get("size") or 0), "source": str(meta.get("source") or ""),
                    "fetched_at": str(meta.get("fetched_at") or "")})
    return out


def restore_version(settings: Settings, game: str, name: str) -> dict:
    """La version datée `name` redevient la version courante du manager ; la courante est archivée
    à son tour (comme lors d'un remplacement), on peut donc revenir en arrière. Les états par
    client sont gardés : un client qui n'a pas bougé depuis son dernier échange apparaît en retard
    et reçoit la version restaurée (synchro auto ou envoi) ; un client qui a joué depuis passe en
    conflit. ManagerError si la version est inconnue. {ok, message, archived}."""
    d = reserve_dir(settings, game)
    with STORE_LOCK:
        if name not in archives_in(d):
            raise ManagerError(f"version inconnue : {name}")
        chosen, current, meta_path = d / f"{name}.tar.gz", d / CURRENT, d / META
        prior = reserve_meta(settings, game) or {}
        try:
            old = json.loads((d / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        meta = dict(old) if isinstance(old, dict) else {}
        for key in ("states", "synced"):             # états par client : ceux d'aujourd'hui
            if isinstance(prior.get(key), dict):
                meta[key] = prior[key]
            else:
                meta.pop(key, None)
        meta["restored_from"] = name
        if not meta.get("fingerprint"):
            raise ManagerError(f"version {name} sans empreinte (.json absent) : restauration refusée")
        text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
        archived = ""
        if current.exists():
            stamp = new_stamp(d)
            swap_keep_old(current, chosen, d / f"{stamp}.tar.gz")
            archived = f"{stamp}.tar.gz"
            if meta_path.exists():
                meta_part = d / "meta.json.part"
                write_part(meta_part, text)
                swap_keep_old(meta_path, meta_part, d / f"{stamp}.json")
            else:
                write_atomic(meta_path, text)
        else:
            publish_new(chosen, current)
            write_atomic(meta_path, text)
        (d / f"{name}.json").unlink(missing_ok=True)
        fsync_dir(d)
        prune(d)
    return {"ok": True, "archived": archived,
            "message": f"version du {name[6:8]}/{name[4:6]}/{name[:4]} {name[9:11]}:{name[11:13]} restaurée"
                       + (" ; la précédente est archivée" if archived else "")}


def synced_times(settings: Settings, game: str) -> tuple[dict[str, int], int]:
    """({client: dernière synchro}, dernière synchro du jeu), en secondes epoch (0 : jamais).
    Celle du jeu est la plus récente des synchros des clients, ou, pour les échanges notés avant
    l'enregistrement de ces dates, la dernière réception d'une version par le manager (`fetched_at`)."""
    meta = reserve_meta(settings, game) or {}
    raw = meta.get("synced") if isinstance(meta.get("synced"), dict) else {}
    by_machine = {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
    fetched = 0
    try:
        fetched = int(datetime.fromisoformat(str(meta.get("fetched_at") or "")).timestamp())
    except ValueError:
        pass
    return by_machine, max([fetched, *by_machine.values()])
