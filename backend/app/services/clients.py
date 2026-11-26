"""Registre des clients (machines associées) et déclaration du manager.

- Clients : ceux de config.json (`saves_machines`, associés par code wsq://),
  plus le client d'essai en mode agent (id « game-host », d'office). Les
  lignes rendues ne portent ni jeton ni certificat.
- Ordre, icône choisie, client d'essai, dossiers déclarés, association et
  oubli d'un client.
- Manager : certificat auto-signé pour l'écoute des clients, et déclaration
  du manager à chaque client (adresse, jeton propre, empreinte).

Les sauvegardes elles-mêmes sont dans saves_*.py, qui s'appuient sur ce module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
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
from . import activity
from .agent import AgentHost, agent_cert_path, fingerprint_pem, pair_agent, parse_pair_code
from .durable import fsync_dir, write_atomic
from .errors import ManagerError

log = logging.getLogger("wsquashfs-manager")
GAME_HOST_ID = "game-host"          # client d'essai en mode agent : toujours listée, non supprimable
MANAGER_PEER_VERSION = 9            # premier agent qui accepte la déclaration du manager (PUT /v1/saves/manager)


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
    except ManagerError:
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


def machine_label(settings: Settings, machine_id: str) -> str:
    try:
        return find_machine(settings, machine_id)["name"]
    except KeyError:
        raise ManagerError(f"client inconnu : {machine_id}") from None


def open_machine(settings: Settings, machine_id: str, timeout: float = 3600) -> AgentHost:
    """Client HTTPS vers un client de la synchronisation (certificat épinglé)."""
    client_id = saves_client_id(settings)
    if machine_id == GAME_HOST_ID:
        if settings.game_host != "agent" or not settings.agent_url:
            raise ManagerError("client d'essai : plus en mode agent")
        return AgentHost(settings, timeout, client_id=client_id)
    for machine in saves_machines(settings):
        if machine["id"] == machine_id:
            return AgentHost(settings, timeout, pem=machine["pem"], url=machine["url"],
                             token=machine["token"], client_id=client_id)
    raise ManagerError(f"client inconnu : {machine_id}")


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
    except (ManagerError, OSError) as exc:
        log.warning("client %s associé, mais le manager n'a pas pu s'y déclarer : %s", name, exc)
    return {"id": entry["id"], "name": name, "url": paired["url"],
            "fingerprint": paired["fingerprint"], "replaced": bool(matches)}


def machine_tls(settings: Settings, create: bool = True) -> tuple[Path, Path]:
    """Certificat auto-signé du manager pour les clients (state_dir/tls/cert.pem et key.pem),
    créé une fois avec openssl. create faux : pas de création (le fichier doit exister).
    ManagerError si openssl manque ou échoue."""
    folder = settings.state_dir / "tls"
    cert, key = folder / "cert.pem", folder / "key.pem"
    if (cert.is_file() and key.is_file()) or not create:
        return cert, key
    if not shutil.which("openssl"):
        raise ManagerError("openssl absent : certificat du manager pour les clients impossible")
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
        raise ManagerError(f"certificat du manager pour les clients impossible : {exc}") from exc
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
    KeyError : client inconnu ; ManagerError : refus ou client injoignable."""
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


def ensure_declared(settings: Settings, row: dict, info: dict, manager_fp: str) -> None:
    """Déclare le manager à un client associé (agent version 9 ou plus) qui ne le déclare pas
    encore, ou dont l'adresse d'interface connue (manager_ui de /v1/info, absente des
    anciens agents) diffère. `manager_fp` : empreinte du manager sans « : », "" sans
    déclaration possible. Erreurs ignorées (journalisées)."""
    if (manager_fp and row["kind"] == "saved" and _version_at_least(info, MANAGER_PEER_VERSION)
            and (str(info.get("manager") or "").replace(":", "").upper() != manager_fp.upper()
                 or ("manager_ui" in info and info["manager_ui"] != manager_ui_url(settings)))):
        try:
            declare_manager(settings, row["id"], info)
        except (ManagerError, OSError, KeyError) as exc:
            log.warning("déclaration du manager à %s impossible : %s", row["name"], exc)


def redeclare_manager(settings: Settings) -> list[str]:
    """Déclare le manager (nouvelle adresse) à chaque client associé joignable ; erreurs
    ignorées (client éteint, agent ancien). Retourne les clients déclarés."""
    done = []
    for machine in saves_machines(settings):
        try:
            if declare_manager(settings, machine["id"]):
                done.append(machine["id"])
        except (KeyError, ManagerError):
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
    ManagerError (AgentUnreachable si injoignable) : refus de l'agent, avec son message."""
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
        raise ManagerError("réponse illisible du client (dossiers)")
    return {"dirs": [{"name": str(d.get("name", "")), "path": str(d.get("path", ""))}
                     for d in dirs if isinstance(d, dict)],
            "eden": str(data.get("eden") or "")}


def all_machine_dirs(settings: Settings) -> list[dict]:
    """Dossiers déclarés de chaque client, lus en parallèle : [{id, name, ok, dirs, message}]
    (ok False : injoignable ou agent trop ancien, `message` dit pourquoi). Sert à montrer, en
    déclarant un dossier, le chemin qu'a chaque autre client pour ce nom."""
    def one(machine: dict) -> dict:
        row = {"id": machine["id"], "name": machine["name"], "ok": False, "dirs": [], "message": ""}
        try:
            host = open_machine(settings, machine["id"], timeout=10)
            data = host.saves_dirs()
            dirs = data.get("dirs") if isinstance(data, dict) else None
            if not isinstance(dirs, list):
                raise ManagerError("réponse illisible du client (dossiers)")
            row.update(ok=True, dirs=[{"name": str(d.get("name", "")), "path": str(d.get("path", ""))}
                                      for d in dirs if isinstance(d, dict)])
        except (KeyError, ManagerError, OSError) as exc:
            row["message"] = str(exc)
        return row

    machines = machine_rows(settings)
    if not machines:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(machines))) as pool:
        return list(pool.map(one, machines))


def declare_dir(settings: Settings, machine_id: str, name: str, path: str, also: list[str]) -> dict:
    """Déclare (ou remplace) le dossier `name` sur `machine_id` avec le chemin tapé, puis sur
    chaque client de `also` avec le chemin que le premier a retenu, à partir de son dossier
    personnel (« ~/x » ou « /home/u/x » deviennent « x » : le HOME diffère d'un client à l'autre).
    Rend les dossiers du premier client (comme machine_dirs) et `others` : [{id, name, ok,
    message}]. Erreurs du premier client : comme machine_dirs (rien n'est propagé)."""
    data = machine_dirs(settings, machine_id, ("put", name, path))
    rel = next((d["path"] for d in data["dirs"] if d["name"] == name), "")
    others = []
    for other in dict.fromkeys(i for i in also if i != machine_id):
        try:
            label = find_machine(settings, other)["name"]
        except KeyError:
            others.append({"id": other, "name": other, "ok": False, "message": "client inconnu"})
            continue
        result = {"id": other, "name": label, "ok": False, "message": ""}
        if not rel or rel.startswith("/"):
            result["message"] = "chemin hors du dossier personnel : non propagé"
        else:
            try:
                machine_dirs(settings, other, ("put", name, rel))
                result.update(ok=True, message=f"déclaré : ~/{rel}")
            except ManagerError as exc:
                result["message"] = str(exc)
        others.append(result)
    return {**data, "others": others}


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
