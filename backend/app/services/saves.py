"""Manager de sauvegardes et synchronisation entre machines (SPEC « Manager de sauvegardes »).

- Machines : celles de config.json (`saves_machines`, associées par code
  wsq://), plus le client d'essai en mode agent (id « game-host », d'office).
- Manager : `state_dir/saves-reserve/<jeu>/` avec `current.tar.gz`, `meta.json`
  et, avant chaque remplacement, l'ancienne version datée (3 gardées).
- Opérations : pull (machine → manager), push (manager → machines),
  push_from (pull puis push des jeux récupérés), status (états par machine),
  delete (suppression sur des clients et/ou le manager ; archivée d'abord).
- Source suggérée : le client « la plus avancée » (`best`) : la plus récente
  parmi celles de taille proche de la plus lourde (écart ≤ SIZE_TOLERANCE).

Les écritures du manager passent par durable.py (fichier .part, fsync,
échange par lien physique : l'ancienne version reste sous son nom daté).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..config import (
    ICONS,
    Settings,
    save_game_host,
    save_saves_game_host_icon,
    save_saves_machines,
    save_saves_order,
    saves_client_id,
    saves_machines,
    saves_order,
    set_saves_machine_field,
)
from ..models import Task, TaskStatus
from . import activity
from .durable import append_line, fsync_dir, publish_new, swap_keep_old, write_atomic
from .rebuild import Job, RebuildError
from .trial import (
    AgentHost,
    SavesBusy,
    agent_cert_path,
    fingerprint_pem,
    pair_agent,
    parse_pair_code,
)

log = logging.getLogger("wsquashfs-manager")
GAME_HOST_ID = "game-host"          # client d'essai en mode agent : toujours listée, non supprimable
KEEP_ARCHIVES = 3                   # versions datées gardées par jeu
RESERVE_DIR = "saves-reserve"
CURRENT = "current.tar.gz"
META = "meta.json"
MANAGER_PEER_VERSION = 9            # premier agent qui accepte la déclaration du manager (PUT /v1/saves/manager)
STORE_LOCK = threading.Lock()       # meta.json de la réserve : un écrit à la fois (envoi d'un client, synchro auto)

_GAME_RE = re.compile(r"^[^./\\\x00][^/\\\x00]{0,199}$")


# ------------------------------------------------------------------ noms

def valid_game(name: object) -> bool:
    """Nom de jeu sûr pour un dossier : pas de « / », pas de « .. », pas de point initial."""
    return isinstance(name, str) and bool(_GAME_RE.match(name)) and ".." not in name


def clean_dir(value: object) -> str | None:
    """Chemin d'un dossier déclaré par un client (à partir de son HOME, ex. « .local/share/x »),
    relayé aux autres clients : rendu s'il est relatif, sans « .. » ni caractère de contrôle
    (512 caractères au plus), None sinon. Le lanceur le revérifie à la réception."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if value.startswith(("/", "~")) or any(ord(c) < 32 for c in value):
        return None
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        return None
    return value


def check_game(name: object) -> str:
    if not valid_game(name):
        raise RebuildError(f"nom de jeu invalide : {name!r}")
    return str(name)


def game_name(filename: str) -> str:
    """Nom de jeu = nom de fichier de l'image sans .wsquashfs ni .pc (casse ignorée),
    comme `image.stem` dans scan._saves_size."""
    name = filename
    for suffix in (".wsquashfs", ".pc"):
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
    return name


def _dedupe(names) -> list[str]:
    return list(dict.fromkeys(names))


# ------------------------------------------------------------------ machines

def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or url


def _fingerprint_or_empty(pem: str | None) -> str:
    try:
        return fingerprint_pem(pem) if pem else ""
    except (ValueError, OSError):
        return ""


def _chosen_icon(value: object) -> str | None:
    """Icône choisie à la main, si elle est connue ; sinon None (détection)."""
    return value if isinstance(value, str) and value in ICONS else None


def machine_rows(settings: Settings) -> list[dict]:
    """Machines de la synchronisation, sans jeton ni certificat :
    {id, name, url, fingerprint, kind, removable, icon} ; `icon` est l'icône choisie
    à la main, None si elle n'est pas choisie (la détection vient de status)."""
    rows: list[dict] = []
    saved_urls = set()
    for machine in saves_machines(settings):
        saved_urls.add(_norm(machine["url"]))
        rows.append({"id": machine["id"], "name": machine["name"], "url": machine["url"],
                     "fingerprint": _fingerprint_or_empty(machine["pem"]),
                     "kind": "saved", "removable": True, "icon": _chosen_icon(machine.get("icon"))})
    if settings.game_host == "agent" and settings.agent_url and _norm(settings.agent_url) not in saved_urls:
        cert = agent_cert_path(settings)
        pem = cert.read_text() if cert.exists() else None
        url = settings.agent_url.rstrip("/")
        rows.insert(0, {"id": GAME_HOST_ID, "name": f"client d'essai ({_host_of(url)})", "url": url,
                        "fingerprint": _fingerprint_or_empty(pem), "kind": "game_host", "removable": False,
                        "icon": _chosen_icon(settings.saves_game_host_icon)})
    # Ordre choisi dans Réglages > Clients (bandeau, listes) ; les clients absents de l'ordre suivent
    rank = {machine_id: i for i, machine_id in enumerate(saves_order(settings))}
    rows.sort(key=lambda row: rank.get(row["id"], len(rank)))
    return rows


def set_machine_order(settings: Settings, ids: list[str]) -> list[str]:
    """Enregistre l'ordre des clients : doit contenir chaque client exactement une fois.
    ValueError sinon."""
    current = [row["id"] for row in machine_rows(settings)]
    if len(set(ids)) != len(ids) or sorted(ids) != sorted(current):
        raise ValueError("ordre incomplet : chaque client doit figurer une seule fois")
    save_saves_order(settings, list(ids))
    return list(ids)


def game_host_machine(settings: Settings) -> str:
    """Client associé qui sert de client d'essai (même adresse), "" sinon."""
    if settings.game_host != "agent" or not settings.agent_url:
        return ""
    for machine in saves_machines(settings):
        if _norm(machine["url"]) == _norm(settings.agent_url):
            return machine["id"]
    return ""


def use_as_game_host(settings: Settings, machine_id: str) -> None:
    """Fait d'un client associé le client d'essai (Réglages) : même adresse, même jeton,
    même certificat épinglé, sans nouvelle association. KeyError si elle est inconnue. Le
    processus de session vient de son /v1/info s'il répond, sinon le réglage actuel reste."""
    machine = next((m for m in saves_machines(settings) if m["id"] == machine_id), None)
    if machine is None:
        raise KeyError(machine_id)
    session = settings.session_process
    try:
        info = AgentHost(settings, 15, url=machine["url"], token=machine["token"], pem=machine["pem"]).info()
        session = str(info.get("session_process") or session)
    except RebuildError:
        pass
    cert = agent_cert_path(settings)
    cert.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(cert, machine["pem"].strip() + "\n")
    save_game_host(settings, {"game_host": "agent", "agent_url": machine["url"],
                              "agent_token": machine["token"], "session_process": session})


def find_machine(settings: Settings, machine_id: str) -> dict:
    """Ligne du client ; KeyError si elle n'existe pas (ou plus)."""
    for row in machine_rows(settings):
        if row["id"] == machine_id:
            return row
    raise KeyError(machine_id)


def _label(settings: Settings, machine_id: str) -> str:
    try:
        return find_machine(settings, machine_id)["name"]
    except KeyError:
        raise RebuildError(f"client inconnu : {machine_id}") from None


def open_machine(settings: Settings, machine_id: str, timeout: float = 3600) -> AgentHost:
    """Client HTTPS vers un client de la synchronisation (certificat épinglé)."""
    client_id = saves_client_id(settings)
    if machine_id == GAME_HOST_ID:
        if settings.game_host != "agent" or not settings.agent_url:
            raise RebuildError("client d'essai : plus en mode agent")
        return AgentHost(settings, timeout, client_id=client_id)
    for machine in saves_machines(settings):
        if machine["id"] == machine_id:
            return AgentHost(settings, timeout, pem=machine["pem"], url=machine["url"],
                             token=machine["token"], client_id=client_id)
    raise RebuildError(f"client inconnu : {machine_id}")


def add_machine(settings: Settings, code: str) -> dict:
    """Associe un client par son code wsq:// : le secret du code s'échange ici, une
    seule fois, contre le jeton durable de ce manager (pair_agent) ; seul ce jeton est
    enregistré. Même empreinte ou même adresse qu'un client enregistré : remplacement.
    ValueError si le code est illisible ; AgentProbeError (avec son code HTTP) sinon."""
    pair = parse_pair_code(code)
    paired = pair_agent(pair["url"], pair["secret"], pair["fingerprint"], saves_client_id(settings))
    name = paired["host"] or _host_of(paired["url"])
    machines = saves_machines(settings)
    matches = [i for i, m in enumerate(machines)
               if _norm(m["url"]) == _norm(paired["url"])
               or _fingerprint_or_empty(m["pem"]).replace(":", "") == paired["fingerprint"].replace(":", "")]
    entry = {"id": secrets.token_hex(4), "name": name, "url": paired["url"],
             "token": paired["token"], "pem": paired["pem"]}
    if matches:
        entry["id"] = machines[matches[0]]["id"]          # le remplacement garde l'identifiant
        if _chosen_icon(machines[matches[0]].get("icon")):
            entry["icon"] = machines[matches[0]]["icon"]  # et l'icône choisie à la main
        kept = [m for i, m in enumerate(machines) if i not in matches]
        kept.insert(matches[0], entry)
        machines = kept
    else:
        machines.append(entry)
    save_saves_machines(settings, machines)
    try:
        declare_manager(settings, entry["id"])
    except (RebuildError, OSError) as exc:
        log.warning("client %s associé, mais le manager n'a pas pu s'y déclarer : %s", name, exc)
    return {"id": entry["id"], "name": name, "url": paired["url"],
            "fingerprint": paired["fingerprint"], "replaced": bool(matches)}


def machine_tls(settings: Settings, create: bool = True) -> tuple[Path, Path]:
    """Certificat auto-signé du manager pour les clients (state_dir/tls/cert.pem et key.pem),
    créé une fois avec openssl. create faux : pas de création (le fichier doit exister).
    RebuildError si openssl manque ou échoue."""
    folder = settings.state_dir / "tls"
    cert, key = folder / "cert.pem", folder / "key.pem"
    if (cert.is_file() and key.is_file()) or not create:
        return cert, key
    if not shutil.which("openssl"):
        raise RebuildError("openssl absent : certificat du manager pour les clients impossible")
    folder.mkdir(parents=True, exist_ok=True)
    cert_part, key_part = folder / "cert.pem.part", folder / "key.pem.part"
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", str(key_part), "-out", str(cert_part), "-days", "3650",
                        "-subj", "/CN=wsquashfs-manager",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        key_part.unlink(missing_ok=True)
        cert_part.unlink(missing_ok=True)
        raise RebuildError(f"certificat du manager pour les clients impossible : {exc}") from exc
    os.chmod(key_part, 0o600)
    os.replace(key_part, key)
    os.replace(cert_part, cert)
    fsync_dir(folder)
    return cert, key


def manager_fingerprint(settings: Settings, create: bool = True) -> str:
    """Empreinte du certificat du manager, au format affiché (« AA:BB… ») ; "" si absent et
    create faux, ou illisible."""
    cert, _ = machine_tls(settings, create)
    try:
        return fingerprint_pem(cert.read_text()) if cert.is_file() else ""
    except (OSError, ValueError):
        return ""


def _version_at_least(info: dict | None, minimum: int) -> bool:
    version = str((info or {}).get("version") or "")
    return version.isdigit() and int(version) >= minimum


def declare_manager(settings: Settings, machine_id: str, info: dict | None = None) -> bool:
    """Déclare le manager à un client associé (agent version 9) : un jeton neuf est envoyé
    avec l'adresse (Réglages) et l'empreinte du certificat du manager. Seule l'empreinte
    SHA-256 du jeton est gardée, et seulement après l'accord de l'agent. False, sans rien
    faire, si l'adresse du manager n'est pas réglée ou si l'agent est plus ancien.
    KeyError : client inconnu ; RebuildError : refus ou client injoignable."""
    if not settings.machine_url:
        return False
    if not any(m["id"] == machine_id for m in saves_machines(settings)):
        raise KeyError(machine_id)
    host = open_machine(settings, machine_id, timeout=15)
    if info is None:
        info = host.info()
    if not _version_at_least(info, MANAGER_PEER_VERSION):
        return False
    fingerprint = manager_fingerprint(settings).replace(":", "")
    token = secrets.token_urlsafe(32)
    name = f"wsquashfs-manager ({socket.gethostname()})"[:64]
    host.saves_manager_put(name, settings.machine_url, token, fingerprint, ui=manager_ui_url(settings))
    set_saves_machine_field(settings, machine_id, "manager_token_sha256",
                            hashlib.sha256(token.encode("utf-8")).hexdigest())
    return True


def manager_ui_url(settings: Settings) -> str:
    """Adresse de l'interface donnée aux clients : ui_url (Réglages, proxy), sinon l'hôte de
    machine_url sur le port de l'interface, en http ; "" sans adresse du manager."""
    if settings.ui_url:
        return settings.ui_url
    host = urlsplit(settings.machine_url).hostname if settings.machine_url else None
    if not host:
        return ""
    return f"http://[{host}]:{settings.port}" if ":" in host else f"http://{host}:{settings.port}"


def redeclare_manager(settings: Settings) -> list[str]:
    """Déclare le manager (nouvelle adresse) à chaque client associé joignable ; erreurs
    ignorées (client éteint, agent ancien). Retourne les clients déclarés."""
    done = []
    for machine in saves_machines(settings):
        try:
            if declare_manager(settings, machine["id"]):
                done.append(machine["id"])
        except (KeyError, RebuildError):
            continue
    return done


def remove_machine(settings: Settings, machine_id: str) -> None:
    """KeyError si le client n'est pas enregistré ; le client d'essai ne se retire pas.
    Si le client oublié était le client d'essai, celui-ci n'est plus choisi et
    désactivé : sinon son adresse resterait listée comme « client d'essai », injoignable.
    """
    if machine_id == GAME_HOST_ID:
        raise ValueError("client d'essai : non supprimable (choisir un autre client d'essai dans le bandeau Clients)")
    machines = saves_machines(settings)
    kept = [m for m in machines if m["id"] != machine_id]
    if len(kept) == len(machines):
        raise KeyError(machine_id)
    was_game_host = game_host_machine(settings) == machine_id
    save_saves_machines(settings, kept)
    activity.forget(machine_id)
    if was_game_host:
        save_game_host(settings, {"game_host": "off", "agent_url": "", "agent_token": ""})


def machine_dirs(settings: Settings, machine_id: str, change: tuple[str, ...] = ()) -> dict:
    """Dossiers déclarés d'un client (agent v8) : {dirs: [{name, path}], eden}. `change` :
    ("put", nom, chemin) ou ("delete", nom) avant la lecture. KeyError : client inconnu ;
    RebuildError (AgentUnreachable si injoignable) : refus de l'agent, avec son message."""
    find_machine(settings, machine_id)
    host = open_machine(settings, machine_id, timeout=30)
    if change and change[0] == "put":
        data = host.saves_dir_put(change[1], change[2])
    elif change and change[0] == "delete":
        data = host.saves_dir_delete(change[1])
    else:
        data = host.saves_dirs()
    dirs = data.get("dirs") if isinstance(data, dict) else None
    if not isinstance(dirs, list):
        raise RebuildError("réponse illisible du client (dossiers)")
    return {"dirs": [{"name": str(d.get("name", "")), "path": str(d.get("path", ""))}
                     for d in dirs if isinstance(d, dict)],
            "eden": str(data.get("eden") or "")}


def set_icon(settings: Settings, machine_id: str, icon: str | None) -> dict:
    """Icône choisie à la main d'un client (None : retour à la détection).
    KeyError si le client est inconnue ; ValueError si l'icône n'est pas dans ICONS."""
    find_machine(settings, machine_id)
    if icon is not None and icon not in ICONS:
        raise ValueError(f"icône inconnue : {icon} (valeurs : {', '.join(ICONS)})")
    if machine_id == GAME_HOST_ID:
        save_saves_game_host_icon(settings, icon)
    else:
        machines = saves_machines(settings)
        for machine in machines:
            if machine["id"] == machine_id:
                if icon is None:
                    machine.pop("icon", None)
                else:
                    machine["icon"] = icon
        save_saves_machines(settings, machines)
    return {"id": machine_id, "icon": icon, "icon_set": icon is not None}


# ------------------------------------------------------------------ manager

def reserve_root(settings: Settings) -> Path:
    return settings.state_dir / RESERVE_DIR


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


def _stamp(d: Path) -> str:
    base = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 — heure locale, pour l'utilisateur
    name, n = base, 2
    while (d / f"{name}.tar.gz").exists() or (d / f"{name}.json").exists():
        name, n = f"{base}-{n:03d}", n + 1
    return name


def _prune(d: Path) -> None:
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


def _write_part(path: Path, text: str) -> None:
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
    stamp = _stamp(d)
    archived = ""
    if current.exists():
        swap_keep_old(current, part, d / f"{stamp}.tar.gz")
        archived = f"{stamp}.tar.gz"
    else:
        publish_new(part, current)
    if old is not None:
        meta_part = d / "meta.json.part"
        _write_part(meta_part, text)
        swap_keep_old(meta_path, meta_part, d / f"{stamp}.json")
    else:
        write_atomic(meta_path, text)
    _prune(d)
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
    """Enregistre que le client a cette empreinte pour ce jeu (meta.json, écriture atomique)."""
    meta = reserve_meta(settings, game) or {}
    states = dict(meta["states"]) if isinstance(meta.get("states"), dict) else {}
    states[machine_id] = fingerprint
    meta["states"] = states
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
    (_prune) : seul resolve les départage."""
    d = reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    for known in conflict_versions(settings, game):
        if known["machine_id"] == machine_id and known["fingerprint"].lower() == fingerprint.lower():
            part.unlink(missing_ok=True)
            return {"name": known["name"], "created": False}
    name = f"{_stamp(d)}-conflit-{machine_id}"
    publish_new(part, d / f"{name}.tar.gz")
    meta = {"machine_id": machine_id, "source": source, "newest": int(newest), "size": int(size),
            "fingerprint": fingerprint, "content": content, "profile": profile,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if clean_dir(dir_path):
        meta["dir"] = dir_path
    write_atomic(d / f"{name}.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return {"name": name, "created": True}


def resolve(settings: Settings, game: str, keep: str, job: Job | None = None) -> dict:
    """Départage un jeu en conflit. `keep` : « manager » (la version courante reste), le nom d'une
    version de conflit rangée (elle devient la courante ; l'ancienne courante est archivée comme
    lors d'un remplacement) ou « client:<id> » (la copie actuelle de ce client est récupérée et
    devient la courante). Les autres versions de conflit deviennent des versions datées normales
    (puis _prune). Tout client encore en conflit après relecture (action « conflit », qu'une
    version soit rangée ou non) et dont la copie n'a pas été retenue est noté « à l'empreinte de sa
    copie » : il apparaît alors en retard et reçoit la version du manager à la synchro suivante (au
    lieu de repasser en conflit). Rien à départager : ok, sans rien changer. RebuildError si `keep`
    est inconnu, ou si le client choisi est injoignable ou n'a pas le jeu."""
    check_game(game)
    entries = status(settings, [game], refresh=True)["games"]
    machines = entries[0]["machines"] if entries else []
    conflicted = {m["id"]: m for m in machines if m["action"] == "conflit"}
    names = {m["id"]: m["name"] for m in machines}
    received = ""                                       # empreinte reçue du client choisi
    client_id = keep[len("client:"):] if keep.startswith("client:") else ""
    if client_id:
        try:
            find_machine(settings, client_id)
        except KeyError:
            raise RebuildError(f"client inconnu : {client_id}") from None
        row = next((m for m in machines if m["id"] == client_id), None)
        if row is None or row["state"] == "injoignable":
            raise RebuildError(f"client injoignable : {(row or {}).get('name') or client_id}"
                               + (f" ({row['message']})" if row and row["message"] else ""))
        if row["state"] != "présente":
            raise RebuildError(f"{row['name']} n'a pas ce jeu")
        host = open_machine(settings, client_id)
        got = _pull_one(settings, host, row["name"], game, _present_listing(host.saves_list()).get(game))
        if not got["ok"]:
            raise RebuildError(f"{row['name']} : {got['message']}")
        received = got["fingerprint"]
    with STORE_LOCK:
        d = reserve_dir(settings, game)
        versions = conflict_versions(settings, game)
        chosen = None
        if keep != "manager" and not client_id:
            chosen = next((v for v in versions if v["name"] == keep), None)
            if chosen is None:
                raise RebuildError(f"version de conflit inconnue : {keep}")
        elif keep == "manager" and not versions and not conflicted:
            return {"ok": True, "message": "rien à départager", "kept": "manager", "archived": [], "behind": []}
        current, meta_path = d / CURRENT, d / META
        if chosen is not None:
            prior = reserve_meta(settings, game) or {}
            states = prior.get("states") if isinstance(prior.get("states"), dict) else {}
            try:
                chosen_meta = json.loads((d / f"{chosen['name']}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                chosen_meta = {}
            meta = {"fingerprint": chosen["fingerprint"], "newest": chosen["newest"], "size": chosen["size"],
                    "profile": chosen_meta.get("profile"), "source": chosen["source"],
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"), "states": states,
                    "content": chosen["content"]}
            dir_kept = clean_dir(chosen_meta.get("dir")) or clean_dir(prior.get("dir"))
            if game.startswith(DIR_PREFIX) and dir_kept:
                meta["dir"] = dir_kept
            text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
            stamp = _stamp(d)
            if current.exists():
                swap_keep_old(current, chosen["path"], d / f"{stamp}.tar.gz")
                if prior:
                    meta_part = d / "meta.json.part"
                    _write_part(meta_part, text)
                    swap_keep_old(meta_path, meta_part, d / f"{stamp}.json")
                else:
                    write_atomic(meta_path, text)
            else:
                publish_new(chosen["path"], current)
                write_atomic(meta_path, text)
            (d / f"{chosen['name']}.json").unlink(missing_ok=True)
        archived = []
        for version in versions:
            if chosen is not None and version["name"] == chosen["name"]:
                continue
            stamp = version["name"].split("-conflit-", 1)[0]       # horodatage (CONFLICT_RE)
            if (d / f"{stamp}.tar.gz").exists() or (d / f"{stamp}.json").exists():
                stamp = _stamp(d)
            os.replace(version["path"], d / f"{stamp}.tar.gz")
            if (d / f"{version['name']}.json").exists():
                os.replace(d / f"{version['name']}.json", d / f"{stamp}.json")
            archived.append(f"{stamp}.tar.gz")
            record_state(settings, game, version["machine_id"], version["fingerprint"])
        behind = []
        for machine_id, row in conflicted.items():
            if machine_id == client_id or (chosen is not None and machine_id == chosen["machine_id"]):
                continue
            record_state(settings, game, machine_id, row["fingerprint"])
            behind.append(machine_id)
        if client_id:
            record_state(settings, game, client_id, received)
        fsync_dir(d)
        _prune(d)
    if client_id:
        where = f"version de {names.get(client_id) or client_id} retenue"
    elif chosen is None:
        where = "version du manager gardée"
    else:
        where = f"version de {chosen['source'] or chosen['machine_id']} retenue"
    if behind:
        who = " et ".join(names.get(m) or m for m in behind)
        where += f" ; {who} {'recevra' if len(behind) == 1 else 'recevront'} cette version"
    return {"ok": True, "message": f"{where} ; {len(archived)} version(s) de conflit archivée(s)",
            "kept": keep, "archived": archived, "behind": behind}


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
        stamp = _stamp(d)
        # os.replace : renommage atomique dans le même dossier, jamais de version à moitié écrite
        os.replace(current, d / f"{stamp}.tar.gz")
        if meta_path.is_file():
            os.replace(meta_path, d / f"{stamp}.json")
        fsync_dir(d)
        _prune(d)
    return {"ok": True, "message": "supprimée du manager (version archivée)", "archived": True}


# ------------------------------------------------------------------ opérations

def _progress(job: Job | None, phase: str, done: int, total: int) -> None:
    if job:
        job.progress(phase, done / total if total else 1.0)


def _check(job: Job | None) -> None:
    if job:
        job.check()


def pull(settings: Settings, source_id: str, games: list[str] | None,
         job: Job | None = None) -> dict:
    """Sauvegardes du client source → manager. `games` None : tous ceux de la source.
    Par jeu : {game, ok, message, fingerprint, newest}. Accusé (saves_ack) après
    chaque réception réussie."""
    host = open_machine(settings, source_id)
    source = _label(settings, source_id)
    listing = _present_listing(host.saves_list())
    wanted = sorted(n for n in listing if valid_game(n)) if games is None else _dedupe(games)
    results = []
    for index, game in enumerate(wanted):
        _check(job)
        _progress(job, f"récupération {index + 1}/{len(wanted)} : {game}", index, len(wanted))
        results.append(_pull_one(settings, host, source, game, listing.get(game)))
    _progress(job, "récupéré", len(wanted), len(wanted))
    return {"source": {"id": source_id, "name": source}, "results": results}


def _present_listing(games: list) -> dict[str, dict]:
    """Jeux d'une réponse de saves_list qui ont une sauvegarde : une entrée sans
    empreinte (jeu supprimé sur le client) compte comme absente."""
    return {g["name"]: g for g in games
            if isinstance(g, dict) and isinstance(g.get("name"), str) and g.get("fingerprint")}


def _pull_one(settings: Settings, host: AgentHost, source: str, game: str,
              entry: dict | None) -> dict:
    result = {"game": game, "ok": False, "message": "", "fingerprint": "", "newest": 0}
    if not valid_game(game):
        result["message"] = "nom de jeu invalide"
        return result
    if entry is None:
        result["message"] = f"aucune sauvegarde sur {source}"
        return result
    d = reserve_dir(settings, game)
    part = d / "fetch.part"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fingerprint, profile = host.saves_get(game, part)
        newest = int(entry.get("newest") or 0)
        with STORE_LOCK:
            store(settings, game, part, fingerprint=fingerprint, newest=newest,
                  size=_size_of(entry), profile=profile, source=source,
                  dir_path=str(entry.get("dir") or "") or None,
                  content=str(entry.get("content") or ""))
    except SavesBusy:
        result["message"] = f"jeu en cours sur {source} : non récupéré"
        return result
    except (RebuildError, OSError) as exc:
        result["message"] = str(exc)
        return result
    finally:
        part.unlink(missing_ok=True)
    warning = ""
    try:
        host.saves_ack(game, fingerprint)
    except RebuildError as exc:
        warning = f" (état non enregistré sur {source} : {exc})"
    result.update(ok=True, fingerprint=fingerprint, newest=newest,
                  message=f"récupérée de {source}{warning}")
    return result


def push(settings: Settings, target_ids: list[str], games: list[str] | None,
         job: Job | None = None) -> dict:
    """Manager → machines cibles. `games` None : tous les jeux du manager.
    Par (jeu, machine) : {game, machine, machine_id, ok, message, archived}."""
    wanted = _dedupe(games) if games is not None else reserve_games(settings)
    targets = []
    for machine_id in _dedupe(target_ids):
        try:
            host = open_machine(settings, machine_id)
            error = ""
        except RebuildError as exc:
            host, error = None, str(exc)
        targets.append((machine_id, _label(settings, machine_id), host, error))
    total = len(wanted) * len(targets)
    results: list[dict] = []
    for game in wanted:
        for machine_id, label, host, error in targets:
            _check(job)
            _progress(job, f"envoi {len(results) + 1}/{total} : {game} → {label}", len(results), total)
            results.append(_push_one(settings, game, machine_id, label, host, error))
    _progress(job, "envoyé", total, total)
    return {"results": results}


def _push_one(settings: Settings, game: str, machine_id: str, label: str,
              host: AgentHost | None, error: str) -> dict:
    result = {"game": game, "machine": label, "machine_id": machine_id, "ok": False,
              "message": "", "archived": None, "fingerprint": "", "busy": False}
    try:
        if host is None:
            raise RebuildError(error or "client indisponible")
        meta = reserve_meta(settings, game)
        archive = reserve_dir(settings, game) / CURRENT
        if meta is None or not archive.is_file():
            raise RebuildError("absent du manager")
        reply = host.saves_put(game, archive, meta.get("profile"), clean_dir(meta.get("dir")))
        fingerprint = str(reply.get("fingerprint") or meta.get("fingerprint") or "")
        result["archived"] = bool(reply.get("archived"))
    except SavesBusy:
        result.update(message=f"jeu en cours sur {label} : non envoyée", busy=True)
        return result
    except RebuildError as exc:
        result["message"] = str(exc)
        return result
    except OSError as exc:
        result["message"] = f"manager illisible : {exc}"
        return result
    message = f"envoyée vers {label}"
    try:
        host.saves_ack(game, fingerprint)          # POST /v1/saves/state : le client garde l'empreinte commune
    except RebuildError as exc:
        message += f" (état non enregistré : {exc})"
    result.update(ok=True, message=message, fingerprint=fingerprint)
    return result


def push_from(settings: Settings, source_id: str, target_ids: list[str],
              games: list[str] | None, job: Job | None = None) -> dict:
    """Pull depuis la source, puis push vers les cibles des seuls jeux récupérés."""
    pulled = pull(settings, source_id, games, job)
    ok_games = [r["game"] for r in pulled["results"] if r["ok"]]
    pushed: dict = {"results": []}
    if ok_games:
        pushed = push(settings, target_ids, ok_games, job)
    return {"pull": pulled, "push": pushed}


def _delete_one(game: str, machine_id: str, label: str, host: AgentHost | None, error: str) -> dict:
    result = {"game": game, "machine": label, "machine_id": machine_id, "ok": False,
              "message": "", "archived": None}
    try:
        if host is None:
            raise RebuildError(error or "client indisponible")
        reply = host.saves_delete(game)
    except SavesBusy:
        result["message"] = f"jeu en cours sur {label} : non supprimée"
        return result
    except RebuildError as exc:
        result["message"] = str(exc)
        return result
    if not int(reply.get("removed") or 0):
        result.update(ok=True, message=f"rien à supprimer sur {label}")
        return result
    archived = bool(reply.get("archived"))
    result.update(ok=True, archived=archived,
                  message=f"supprimée sur {label}{' (archivée)' if archived else ''}")
    return result


def delete(settings: Settings, machine_ids: list[str], games: list[str], manager: bool,
           job: Job | None = None) -> dict:
    """Supprime les sauvegardes des jeux sur les clients cibles (archivées puis effacées
    par le client) et, si `manager`, sur le manager (version courante datée).
    Par (jeu, cible) : {game, machine, machine_id, ok, message, archived}. Pour chaque jeu,
    les clients puis le manager ; un client injoignable ou occupé échoue seul."""
    wanted = _dedupe(games)
    bad = [g for g in wanted if not valid_game(g)]
    if bad:
        raise RebuildError(f"nom de jeu invalide : {bad[0]!r}")
    targets = []
    for machine_id in _dedupe(machine_ids):
        try:
            host = open_machine(settings, machine_id)
            error = ""
        except RebuildError as exc:
            host, error = None, str(exc)
        targets.append((machine_id, _label(settings, machine_id), host, error))
    per_game = len(targets) + (1 if manager else 0)
    total = len(wanted) * per_game
    results: list[dict] = []
    for game in wanted:
        for machine_id, label, host, error in targets:
            _check(job)
            _progress(job, f"suppression {len(results) + 1}/{total} : {game} → {label}", len(results), total)
            results.append(_delete_one(game, machine_id, label, host, error))
        if manager:
            _check(job)
            _progress(job, f"suppression {len(results) + 1}/{total} : {game} → Manager", len(results), total)
            results.append(_delete_manager(settings, game))
    _progress(job, "supprimé", total, total)
    return {"results": results}


def _delete_manager(settings: Settings, game: str) -> dict:
    result = {"game": game, "machine": "Manager", "machine_id": "manager", "ok": False,
              "message": "", "archived": None}
    try:
        reply = remove_from_manager(settings, game)
    except (RebuildError, OSError) as exc:
        result["message"] = str(exc)
        return result
    result.update(ok=True, message=reply["message"], archived=reply["archived"] or None)
    return result


PRESENT_STATES = {"présente", "identique", "plus avancée", "en retard"}


def _note(game: str, message: str) -> dict:
    """Résultat sans transfert, pour un jeu (pas de machine)."""
    return {"game": game, "machine": "", "machine_id": "", "ok": True, "message": message, "archived": None}


def _size_of(entry: dict | None) -> int:
    """Taille (octets) d'une entrée de saves_list ; 0 si absente ou illisible."""
    try:
        return int((entry or {}).get("size") or 0)
    except (TypeError, ValueError):
        return 0


SIZE_TOLERANCE = 0.10    # tailles à moins de 10 % de la plus lourde : comptées comme égales


def best(states: list[dict]) -> dict | None:
    """Machine suggérée parmi les états présents : la plus récente parmi celles dont la
    taille est proche de la plus lourde (écart ≤ SIZE_TOLERANCE), pour qu'une version plus
    ancienne mais plus lourde de quelques octets ne l'emporte pas ; une version nettement
    plus petite (partie recommencée) ne gagne pas. None si aucune ne l'a."""
    candidates = [s for s in states if s["state"] in PRESENT_STATES]
    if not candidates:
        return None
    floor = max(s["size"] for s in candidates) * (1 - SIZE_TOLERANCE)
    close = [s for s in candidates if s["size"] >= floor]
    return max(close, key=lambda s: (s["newest"], s["size"]))


def _pull_sources(settings: Settings, games: list[str] | None) -> tuple[dict, list[str]]:
    """Machine source par jeu pour une copie dans le manager : la « plus avancée »
    (`best`). Renvoie ({identifiant: [jeux]}, [jeux sans source])."""
    sources: dict[str, list[str]] = {}
    none: list[str] = []
    for entry in status(settings, games)["games"]:
        pick = best(entry["machines"])
        if pick is None:
            none.append(entry["game"])
        else:
            sources.setdefault(pick["id"], []).append(entry["game"])
    return sources, none


def push_auto(settings: Settings, target_ids: list[str], games: list[str] | None,
              job: Job | None = None) -> dict:
    """Chaque jeu part de sa machine « plus avancée » (`best`) vers les cibles qui n'ont
    pas sa version (« en retard ») ; une cible qui n'a pas le jeu (« absente ») ou
    déjà à la même empreinte est sautée, une cible injoignable échoue. Un jeu
    identique partout, ou seulement dans le manager, n'est pas transféré. Pour
    copier un jeu sur un client qui ne l'a pas : push_from, source explicite."""
    wanted = _dedupe(target_ids)
    results: list[dict] = []
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for entry in status(settings, games)["games"]:
        game = entry["game"]
        source = best(entry["machines"])
        if source is None or source["state"] == "identique":
            identical = any(s["state"] == "identique" for s in entry["machines"])
            results.append(_note(game, "déjà identiques" if identical else "aucun client ne l'a : rien à pousser"))
            continue
        by_id = {s["id"]: s for s in entry["machines"]}
        to_push: list[str] = []
        emitted = False
        for target in wanted:
            if target == source["id"]:
                continue
            state = by_id.get(target)
            if state is None or state["state"] == "injoignable":
                results.append({"game": game, "machine": state["name"] if state else target, "machine_id": target,
                                "ok": False, "message": (state or {}).get("message") or "client injoignable",
                                "archived": None})
                emitted = True
            elif state["state"] == "absente":
                results.append({"game": game, "machine": state["name"], "machine_id": target, "ok": True,
                                "message": "absente : non envoyée (le jeu n'y est pas)", "archived": None})
                emitted = True
            elif source["fingerprint"] and state["fingerprint"] == source["fingerprint"]:
                results.append({"game": game, "machine": state["name"], "machine_id": target, "ok": True,
                                "message": "déjà identique", "archived": None})
                emitted = True
            else:
                to_push.append(target)
        if to_push:
            groups.setdefault((source["id"], tuple(to_push)), []).append(game)
        elif not emitted:
            results.append(_note(game, "aucune cible joignable autre que la source"))
    pulls: list[dict] = []
    pushes: list[dict] = list(results)
    for (source_id, targets), group in groups.items():
        done = push_from(settings, source_id, list(targets), group, job)
        pulls.extend(done["pull"]["results"])
        pushes.extend(done["push"]["results"])
    return {"pull": {"source": {"id": "", "name": "les plus avancées"}, "results": pulls},
            "push": {"results": pushes}}


def pull_auto(settings: Settings, games: list[str] | None, job: Job | None = None) -> dict:
    """Mettre dans le manager, chaque jeu depuis sa machine « plus avancée » (`best`).
    Un jeu qu'aucune machine joignable n'a : échec explicite."""
    sources, none = _pull_sources(settings, games)
    results: list[dict] = []
    for source_id, source_games in sources.items():
        results.extend(pull(settings, source_id, source_games, job)["results"])
    for game in none:
        results.append({"game": game, "ok": False, "message": "absent de tous les clients joignables",
                        "fingerprint": "", "newest": 0})
    return {"source": {"id": "", "name": "les plus avancées"}, "results": results}


def _reserve_summary(settings: Settings, game: str) -> dict | None:
    if not (reserve_dir(settings, game) / CURRENT).is_file():
        return None
    meta = reserve_meta(settings, game) or {}
    return {"fingerprint": meta.get("fingerprint", ""), "newest": int(meta.get("newest") or 0),
            "size": _size_of(meta), "source": meta.get("source", ""), "fetched_at": meta.get("fetched_at", ""),
            "archives": len(archives(settings, game))}


def label_states(states: list[dict]) -> dict:
    """Libellés par machine, pour un jeu (modifie `state` en place). Renvoie
    {check, check_by} : `check` vrai si le client suggéré n'a pas la date newest
    maximale parmi les présentes ; `check_by` est alors le nom de celle-ci.

    - « injoignable » : le client n'a pas répondu, il ne compte pas ;
    - « absente » : le client n'a pas le jeu (stockage plus petit, jeu pas
      installé) ; ce n'est pas une différence, elle ne compte pas ;
    - « présente » : un seul client a le jeu, rien à comparer ;
    - « identique » : les clients qui ont le jeu (deux au moins) ont la même
      empreinte ;
    - sinon : « plus avancée » pour celles qui ont l'empreinte de `best` (la source
      suggérée), « en retard » pour les autres."""
    no_check = {"check": False, "check_by": ""}
    reachable = [s for s in states if s["state"] != "injoignable"]
    have = [s for s in reachable if s["state"] == "présente"]
    if len(have) < 2:
        return no_check
    if len({s["fingerprint"] for s in have}) == 1:
        for s in have:
            s["state"] = "identique"
        return no_check
    top = best(have)
    assert top is not None          # au moins deux clients présents
    for s in have:
        s["state"] = "plus avancée" if s["fingerprint"] == top["fingerprint"] else "en retard"
    newest = max(have, key=lambda s: s["newest"])
    if top["newest"] >= newest["newest"]:
        return no_check
    return {"check": True, "check_by": newest["name"]}


DIR_PREFIX = "dossier:"         # dossier déclaré sur les clients (agent v8)
SWITCH_PREFIX = "switch:"       # sauvegarde Eden d'un jeu Switch, par TitleID (agent v8)


def kind_of(game: str) -> str:
    """Nature d'une ligne : « dossier », « switch » ou « jeu » (jeu Windows)."""
    if game.startswith(DIR_PREFIX):
        return "dossier"
    if game.startswith(SWITCH_PREFIX):
        return "switch"
    return "jeu"


SWITCH_LABEL = "Switch · "      # préfixe du nom donné par l'agent : inutile ici (section Switch)


def label_of(game: str, listings: list[dict | None]) -> str:
    """Nom lisible : celui que donne un client (nom du dossier, nom du jeu Switch, sans le préfixe
    « Switch · » puisque le tableau a sa section Switch), sinon déduit du nom (TitleID d'un jeu
    Switch) ; un jeu Windows garde son nom."""
    for listing in listings:
        label = str(((listing or {}).get(game) or {}).get("label") or "").strip()
        if label.startswith(SWITCH_LABEL):
            label = label[len(SWITCH_LABEL):].strip()
        if label:
            return label[:160]
    if game.startswith(DIR_PREFIX):
        return game[len(DIR_PREFIX):]
    if game.startswith(SWITCH_PREFIX):
        return game[len(SWITCH_PREFIX):]
    return game


_SYSTEM_EXTS = (".wsquashfs", ".squashfs")
_systems_cache: dict = {"at": 0.0, "roots": None, "map": {}, "loose": {}}
_systems_lock = threading.Lock()


def _loose_name(name: str) -> str:
    """Nom rapproché : sans casse, ponctuation ni passages entre parenthèses ou crochets
    (« After Dark (demul) » → « afterdark »)."""
    return re.sub(r"[^0-9a-z]+", "", re.sub(r"\([^)]*\)|\[[^\]]*\]", "", name.lower()))


def game_systems(settings: Settings, ttl: float = 300) -> dict[str, str]:
    """Jeu → système Batocera (dossier sous roms/) de son image .wsquashfs ou .squashfs, pour ranger
    l'onglet Sauvegardes par plateforme (arcadepc, type-x2…). Un même nom dans plusieurs systèmes :
    « win » d'abord. Gardé `ttl` secondes (un parcours de roms/ : quelques dixièmes de seconde)."""
    return _systems_scan(settings, ttl)["map"]


def system_of(settings: Settings, game: str) -> str:
    """Système de l'image d'un jeu : nom exact, sinon nom rapproché (« After Dark » pour
    « After Dark (demul) ») s'il ne désigne qu'un seul système ; "" sinon."""
    scan_ = _systems_scan(settings)
    if game in scan_["map"]:
        return scan_["map"][game]
    systems = scan_["loose"].get(_loose_name(game), set())
    return next(iter(systems)) if len(systems) == 1 else ""


def _systems_scan(settings: Settings, ttl: float = 300) -> dict:
    roots = tuple(settings.roms_roots or settings.roms_dirs)
    with _systems_lock:
        cache = _systems_cache
        if cache["roots"] == roots and time.monotonic() - cache["at"] < ttl:
            return cache
        found: dict[str, str] = {}
        loose: dict[str, set[str]] = {}

        def scan_dir(path: str, system: str) -> None:
            try:
                entries = list(os.scandir(path))
            except OSError:
                return
            for entry in entries:
                for ext in _SYSTEM_EXTS:
                    if entry.name.endswith(ext):
                        name = entry.name[: -len(ext)]
                        if name not in found or (system == "win" and found[name] != "win"):
                            found[name] = system
                        loose.setdefault(_loose_name(name), set()).add(system)
        for root in roots:
            scan_dir(root, Path(root).name)          # un dossier de système donné directement
            try:
                systems = [e for e in os.scandir(root) if e.is_dir()]
            except OSError:
                continue
            for system_dir in systems:
                scan_dir(system_dir.path, system_dir.name)
        cache.update(at=time.monotonic(), roots=roots, map=found, loose=loose)
        return cache


UNREACHABLE_TTL = 30                # secondes pendant lesquelles un client injoignable est sauté par status()
_unreachable: dict[str, tuple[float, str]] = {}      # identifiant du client → (instant, message)
_unreachable_lock = threading.Lock()


def status(settings: Settings, games: list[str] | None = None, refresh: bool = False) -> dict:
    """État de la synchronisation : par machine (une requête saves_list et une requête
    info, en parallèle) et par jeu (libellés, manager, suggestion à vérifier, décision
    `action`, versions de conflit). `games` None : tous les jeux connus. Un client qui n'a pas
    répondu est sauté (réponse immédiate « injoignable », même message) pendant UNREACHABLE_TTL
    secondes, pour qu'un client éteint ne ralentisse pas chaque lecture ; `refresh` vrai l'interroge
    quand même."""
    rows = machine_rows(settings)

    try:
        manager_fp = manager_fingerprint(settings).replace(":", "") if settings.machine_url else ""
    except RebuildError as exc:             # pas de certificat (openssl absent) : pas de déclaration, l'état reste
        log.warning("%s", exc)
        manager_fp = ""

    def fetch(row: dict) -> tuple[dict | None, str, str, dict]:
        """(jeux, erreur, système, info) : un échec de info laisse le système vide, sans
        rendre le client injoignable si saves_list a répondu. Un client (version 9 ou plus)
        qui ne déclare pas ce manager, ou dont l'adresse d'interface connue (manager_ui de
        /v1/info, absente des anciens agents) diffère, reçoit la déclaration ; erreurs ignorées."""
        key = row["id"]
        if not refresh:
            with _unreachable_lock:
                seen = _unreachable.get(key)
            if seen and time.monotonic() - seen[0] < UNREACHABLE_TTL:
                return None, seen[1], "", {}
        try:
            host = open_machine(settings, row["id"], timeout=15)
            listing = _present_listing(host.saves_list(timeout=15))
        except RebuildError as exc:
            with _unreachable_lock:
                _unreachable[key] = (time.monotonic(), str(exc))
            return None, str(exc), "", {}
        with _unreachable_lock:
            _unreachable.pop(key, None)
        try:
            info = host.info()
        except RebuildError:
            info = {}
        info = info if isinstance(info, dict) else {}
        if (manager_fp and row["kind"] == "saved" and _version_at_least(info, MANAGER_PEER_VERSION)
                and (str(info.get("manager") or "").replace(":", "").upper() != manager_fp.upper()
                     or ("manager_ui" in info and info["manager_ui"] != manager_ui_url(settings)))):
            try:
                declare_manager(settings, row["id"], info)
            except (RebuildError, OSError, KeyError) as exc:
                log.warning("déclaration du manager à %s impossible : %s", row["name"], exc)
        return listing, "", str(info.get("os") or ""), info

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as pool:
        fetched = list(pool.map(fetch, rows))
    if games is None:
        names = set(reserve_games(settings))
        for listing, _, _, _ in fetched:
            if listing:
                names.update(n for n in listing if valid_game(n))
        wanted = sorted(names)
    else:
        wanted = _dedupe(g for g in games if valid_game(g))
    machines = []
    for row, (listing, error, system, info) in zip(rows, fetched):
        if listing is not None:
            activity.from_info(row["id"], info)          # agent 11 : jeux en cours, manettes
        machines.append({"id": row["id"], "name": row["name"], "url": row["url"],
                         "agent": str(info.get("version") or ""), "build": str(info.get("build") or ""),
                         "ok": listing is not None, "message": error,
                         "games": len(listing) if listing is not None else None,
                         "os": system, "icon": row["icon"] or (system if system in ICONS else "linux"),
                         "icon_set": row["icon"] is not None, "auto": bool(info.get("auto"))})
    out = []
    for game in wanted:
        states = []
        entries = {}
        for row, (listing, error, _, _) in zip(rows, fetched):
            state = {"id": row["id"], "name": row["name"], "state": "absente",
                     "fingerprint": "", "newest": 0, "size": 0, "message": "", "content": ""}
            if listing is None:
                state.update(state="injoignable", message=error)
            elif game in listing:
                entry = listing[game]
                entries[row["id"]] = entry
                state.update(state="présente", fingerprint=str(entry.get("fingerprint") or ""),
                             newest=int(entry.get("newest") or 0), size=_size_of(entry),
                             content=str(entry.get("content") or ""))
            states.append(state)
        verdict = label_states(states)
        current = current_fingerprint(settings, game)
        manager_meta = (reserve_meta(settings, game) or {}) if current else {}
        manager_newest = int(manager_meta.get("newest") or 0)
        manager_content = str(manager_meta.get("content") or "")
        versions = conflict_versions(settings, game)
        in_conflict = {v["machine_id"] for v in versions}
        for state in states:
            base, state["base_source"] = base_state(settings, game, state["id"], entries.get(state["id"]))
            state["action"] = "" if state["state"] == "injoignable" else decide(
                base, state["fingerprint"], current,
                client_newest=state["newest"], manager_newest=manager_newest,
                client_content=state["content"], manager_content=manager_content)
            state["conflict"] = state["action"] == "conflit" or state["id"] in in_conflict
        kind = kind_of(game)
        out.append({"game": game, "label": label_of(game, [listing for listing, _, _, _ in fetched]),
                    "kind": kind, "system": system_of(settings, game) if kind == "jeu" else "",
                    "reserve": _reserve_summary(settings, game), "machines": states,
                    "check": verdict["check"], "check_by": verdict["check_by"],
                    "conflict": any(state["conflict"] for state in states),
                    "conflicts": [{k: v[k] for k in ("name", "machine_id", "source", "newest", "size", "fingerprint")}
                                  for v in versions]})
    return {"machines": machines, "games": out}


# ------------------------------------------------------------------ synchronisation automatique

BEHIND_LOCK = threading.Lock()      # un seul passage de sync_auto à la fois
HISTORY_FILE = "history.jsonl"      # même journal que la file de tâches (TaskQueue.history_file)


def decide(base: str, client: str, manager: str, *, client_newest: int, manager_newest: int,
           client_content: str = "", manager_content: str = "") -> str:
    """Décision à trois points (modèle d'Unison) pour un client : `base` est l'empreinte au dernier
    échange du client avec le manager ("" si inconnue), `client` celle de sa copie ("" s'il n'a pas
    le jeu), `manager` celle du manager ("" s'il n'a pas de version). `*_content` : empreintes de
    contenu (sans dates), "" si inconnues (ancien agent). Valeurs :
    - « rien » : ni le manager ni le client n'ont le jeu ;
    - « absente » : le client n'a pas le jeu ;
    - « à jour » : même empreinte, ou même contenu (faux conflit : l'appelant note la base) ;
    - « remonter » : le client est en avance (le manager n'a pas bougé depuis la base, ou n'a pas
      le jeu, ou la base est inconnue et la copie du client est plus récente : le manager archive) ;
    - « descendre » : le client est en retard (il n'a pas bougé depuis la base) ;
    - « conflit » : les deux ont changé depuis la base, ou base inconnue et copie du client pas plus
      récente."""
    if not client:
        return "absente" if manager else "rien"
    if not manager:
        return "remonter"
    base, client, manager = base.lower(), client.lower(), manager.lower()
    if client == manager or (client_content and manager_content
                             and client_content.lower() == manager_content.lower()):
        return "à jour"
    if base:
        if client != base:
            return "remonter" if manager == base else "conflit"
        return "descendre"
    return "remonter" if client_newest > manager_newest else "conflit"


def sync_auto(settings: Settings, games: list[str] | None = None,
              exclude: set[str] | None = None) -> list[dict]:
    """Synchronisation automatique des clients qui ont la synchro auto activée (`auto` de
    /v1/info), par comparaison à trois points (decide). Par jeu et par client, d'abord toutes les
    remontées (client en avance → manager ; un jeu absent du manager mais présent chez un client
    aussi), puis les descentes (manager → clients en retard) : une remontée peut rendre les autres
    clients « en retard » dans le même passage. Un vrai conflit range la copie du client sur le
    manager (store_conflict) sans rien remplacer. `games` : None pour toute la réserve et les jeux des
    clients ; `exclude` : clients à ne pas viser (celui qui vient d'envoyer). Un client sans auto ou
    injoignable n'est pas touché. Un seul passage à la fois : si un passage tourne, celui-ci est
    sauté (liste vide). Résultats : {game, machine, machine_id, ok, message, archived, fingerprint,
    busy, action} avec action « remontée », « poussée », « conflit », « échec » ou « jeu en cours » ;
    tout sauf « jeu en cours » va au journal des tâches (un conflit déjà rangé, une seule fois)."""
    if not BEHIND_LOCK.acquire(blocking=False):
        return []
    try:
        return _sync_auto(settings, games, set(exclude or ()))
    finally:
        BEHIND_LOCK.release()


push_behind = sync_auto             # ancien nom (machine_api)


def _result(game: str, row: dict, **kw) -> dict:
    return {"game": game, "machine": row["name"], "machine_id": row["id"], "ok": False, "message": "",
            "archived": None, "fingerprint": "", "busy": False, **kw}


def _decision(settings: Settings, game: str, row: dict, entry: dict | None) -> str:
    meta = (reserve_meta(settings, game) or {}) if current_fingerprint(settings, game) else {}
    return decide(base_state(settings, game, row["id"], entry)[0], str((entry or {}).get("fingerprint") or ""),
                  current_fingerprint(settings, game),
                  client_newest=int((entry or {}).get("newest") or 0),
                  manager_newest=int(meta.get("newest") or 0),
                  client_content=str((entry or {}).get("content") or ""),
                  manager_content=str(meta.get("content") or ""))


def _sync_auto(settings: Settings, games: list[str] | None, exclude: set[str]) -> list[dict]:
    clients = []
    for row in machine_rows(settings):
        if row["id"] in exclude:
            continue
        try:
            host = open_machine(settings, row["id"], timeout=15)
            info = host.info()
        except RebuildError:
            continue                # injoignable, ou sans certificat : ignorée sans bruit
        if not isinstance(info, dict) or not info.get("auto"):
            continue                # synchro auto désactivée sur le client : jamais modifiée
        try:
            listing = _present_listing(host.saves_list(timeout=15))
        except RebuildError:
            continue
        clients.append((row, host, listing))
    results: list[dict] = []
    if not clients:
        return results
    if games is not None:
        names = [g for g in _dedupe(games) if valid_game(g)]
    else:
        names = sorted({*reserve_games(settings), *(n for _, _, listing in clients for n in listing if valid_game(n))})
    for game in names:                                  # 1) remontées, états, conflits
        for row, host, listing in clients:
            entry = listing.get(game)
            action = _decision(settings, game, row, entry)
            if action == "à jour":
                current = current_fingerprint(settings, game)
                if machine_state(settings, game, row["id"]).lower() != current.lower():
                    with STORE_LOCK:
                        record_state(settings, game, row["id"], current)
            elif action == "remonter":
                result = _pull_one(settings, host, row["name"], game, entry)
                out = _result(game, row, ok=result["ok"], fingerprint=result["fingerprint"])
                if result["ok"]:
                    with STORE_LOCK:
                        record_state(settings, game, row["id"], result["fingerprint"])
                    out.update(action="remontée", message=f"synchro auto : {result['message']}")
                elif "en cours" in result["message"]:
                    out.update(action="jeu en cours", busy=True,
                               message=f"jeu en cours sur {row['name']} : remontée retentée au prochain passage")
                else:
                    out.update(action="échec", message=f"synchro auto : {result['message']}")
                results.append(_finish(settings, out))
            elif action == "conflit":
                results.append(_keep_conflict(settings, host, row, game, entry))
    for game in names:                                  # 2) descentes
        for row, host, listing in clients:
            entry = listing.get(game)
            if _decision(settings, game, row, entry) != "descendre":
                continue
            current = current_fingerprint(settings, game)
            result = _push_one(settings, game, row["id"], row["name"], host, "")
            out = _result(game, row, ok=result["ok"], archived=result["archived"],
                          fingerprint=result["fingerprint"], busy=result["busy"])
            if result["ok"]:
                # Empreinte du manager, pas celle que renvoie le client : si elles différaient (fichier
                # hors du profil…), le client paraîtrait encore en retard et recevrait la même version
                # à chaque passage ; elle apparaît « à départager » à la place.
                with STORE_LOCK:
                    record_state(settings, game, row["id"], current)
                out.update(action="poussée", message=f"mise à jour auto : {result['message']}")
            elif result["busy"]:
                out.update(action="jeu en cours", message=f"jeu en cours sur {row['name']} : "
                                                          "mise à jour retentée au prochain passage")
            else:
                out.update(action="échec", message=f"mise à jour auto : {result['message']}")
            results.append(_finish(settings, out))
    return results


def _finish(settings: Settings, result: dict) -> dict:
    """Journalise le résultat (sauf « jeu en cours », retenté au prochain passage) et le rend."""
    if result["action"] != "jeu en cours":
        log.info("%s : %s", result["game"], result["message"])
        _journal_behind(settings, result)
    return result


def _keep_conflict(settings: Settings, host: AgentHost, row: dict, game: str, entry: dict | None) -> dict:
    """Vrai conflit : la copie du client est rangée sur le manager comme version de conflit (sans
    rien remplacer, nulle part). Déjà rangée (même client, même empreinte) : pas de doublon, ni
    de téléchargement, ni de nouvelle entrée au journal."""
    fingerprint = str((entry or {}).get("fingerprint") or "")
    out = _result(game, row, action="conflit", fingerprint=fingerprint,
                  message="conflit : à départager (copie du client rangée sur le manager)")
    with STORE_LOCK:
        known = any(v["machine_id"] == row["id"] and v["fingerprint"].lower() == fingerprint.lower()
                    for v in conflict_versions(settings, game))
    if known:
        return out
    d = reserve_dir(settings, game)
    part = d / f"conflit-{row['id']}.part"
    try:
        d.mkdir(parents=True, exist_ok=True)
        got, profile = host.saves_get(game, part)
        with STORE_LOCK:
            stored = store_conflict(settings, game, part, machine_id=row["id"], fingerprint=got,
                                    newest=int((entry or {}).get("newest") or 0), size=_size_of(entry),
                                    source=row["name"], content=str((entry or {}).get("content") or ""),
                                    profile=profile, dir_path=str((entry or {}).get("dir") or "") or None)
        out["fingerprint"] = got
        if not stored["created"]:
            return out
    except SavesBusy:
        out.update(action="jeu en cours", busy=True,
                   message=f"jeu en cours sur {row['name']} : conflit retenté au prochain passage")
    except (RebuildError, OSError) as exc:
        out.update(action="échec", message=f"conflit : copie de {row['name']} non rangée : {exc}")
    finally:
        part.unlink(missing_ok=True)
    return _finish(settings, out)


def _journal_behind(settings: Settings, result: dict) -> None:
    """Une entrée du journal des tâches pour une remontée, une poussée, un conflit ou un échec de la
    synchro auto."""
    now = time.time()
    ok = result["ok"]
    arrow = "←" if result["action"] in ("remontée", "conflit") else "→"
    task = Task(id=secrets.token_hex(6), kind="saves",
                title=f"sauvegardes : synchro auto ({result['action']}) {arrow} {result['machine']}",
                params={"op": "sync_auto", "action": result["action"], "game": result["game"],
                        "machine_id": result["machine_id"]},
                status=TaskStatus.DONE if ok else TaskStatus.FAILED, error=None if ok else result["message"],
                log=[result["message"]], progress=1.0 if ok else 0.0,
                created_at=now, started_at=now, finished_at=now)
    try:
        append_line(settings.state_dir / HISTORY_FILE, task.model_dump_json())
    except OSError as exc:
        log.warning("journal des tâches non écrit : %s", exc)


# ------------------------------------------------------------------ tâche

def run_saves(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « saves » (file de tâches, comme la vérification).
    params : {op: pull|pull_auto|push|push_from|push_auto|delete|resolve, source?, targets?, games (liste ou null = tous)} ;
    delete : {machines (ids), manager (bool), games (liste)} ;
    resolve : {game, keep (« manager », version de conflit ou « client:<id> »), push (bool)}.
    Le résultat détaillé est gardé dans params.result ; un échec partiel fait
    échouer la tâche (le détail reste lisible)."""
    p = task.params
    op, games = p.get("op"), p.get("games")
    if op == "pull":
        result = pull(settings, p["source"], games, job)
        items = result["results"]
        label = f"depuis {result['source']['name']}"
    elif op == "push":
        result = push(settings, p["targets"], games, job)
        items = result["results"]
        label = f"vers {len(p['targets'])} client(s)"
    elif op == "push_from":
        result = push_from(settings, p["source"], p["targets"], games, job)
        items = result["pull"]["results"] + result["push"]["results"]
        label = f"depuis {result['pull']['source']['name']}"
    elif op == "pull_auto":
        result = pull_auto(settings, games, job)
        items = result["results"]
        label = "depuis les clients les plus avancés"
    elif op == "push_auto":
        result = push_auto(settings, p["targets"], games, job)
        items = result["pull"]["results"] + result["push"]["results"]
        label = f"vers {len(p['targets'])} client(s), chacun depuis le plus avancé"
    elif op == "delete":
        machines = list(p.get("machines") or [])
        result = delete(settings, machines, games or [], bool(p.get("manager")), job)
        items = result["results"]
        where = " et ".join(part for part in (f"{len(machines)} client(s)" if machines else "",
                                              "le manager" if p.get("manager") else "") if part)
        label = f"sur {where}"
    elif op == "resolve":
        game, keep = str(p.get("game") or ""), str(p.get("keep") or "")
        done = resolve(settings, game, keep, job)
        result = {"resolve": done, "push": {"results": []}}
        items = [{"game": game, "ok": done["ok"], "message": done["message"]}]
        label = f"de {game}"
        if p.get("push"):
            behind = [m["id"] for entry in status(settings, [game], refresh=True)["games"]
                      for m in entry["machines"] if m["action"] == "descendre"]
            if behind:
                result["push"] = push(settings, behind, [game], job)
                items += result["push"]["results"]
    else:
        raise RebuildError(f"opération de sauvegardes inconnue : {op}")
    task.params["result"] = result
    for item in items:
        job.log(f"{item['game']} : {item['message']}")
    failed = [item for item in items if not item["ok"]]
    if not items:
        return "rien à faire"
    if failed:
        raise RebuildError(f"{len(failed)} échec(s) sur {len(items)} ({label}) : "
                           f"{failed[0]['game']} : {failed[0]['message']}")
    return f"{len(items)} opération(s) réussie(s) {label}"
