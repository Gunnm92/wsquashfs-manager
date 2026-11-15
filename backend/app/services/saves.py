"""Manager de sauvegardes et synchronisation entre machines (SPEC « Manager de sauvegardes »).

- Machines : celles de config.json (`saves_machines`, associées par code
  wsq://), plus la machine de jeu en mode agent (id « game-host », d'office).
- Manager : `state_dir/saves-reserve/<jeu>/` avec `current.tar.gz`, `meta.json`
  et, avant chaque remplacement, l'ancienne version datée (3 gardées).
- Opérations : pull (machine → manager), push (manager → machines),
  push_from (pull puis push des jeux récupérés), status (états par machine),
  delete (suppression sur des machines et/ou le manager ; archivée d'abord).
- Source suggérée : la machine « la plus avancée » (`best`) : la plus lourde,
  puis la plus récente à taille égale.

Les écritures du manager passent par durable.py (fichier .part, fsync,
échange par lien physique : l'ancienne version reste sous son nom daté).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..config import (
    ICONS,
    Settings,
    save_game_host,
    save_images_source,
    save_saves_game_host_icon,
    save_saves_machines,
    saves_client_id,
    saves_machines,
)
from ..models import Task
from .durable import fsync_dir, publish_new, swap_keep_old, write_atomic
from .rebuild import Job, RebuildError
from .trial import (
    AgentHost,
    SavesBusy,
    agent_cert_path,
    fingerprint_pem,
    pair_agent,
    parse_pair_code,
)

GAME_HOST_ID = "game-host"          # machine de jeu en mode agent : toujours listée, non supprimable
KEEP_ARCHIVES = 3                   # versions datées gardées par jeu
RESERVE_DIR = "saves-reserve"
CURRENT = "current.tar.gz"
META = "meta.json"

_GAME_RE = re.compile(r"^[^./\\\x00][^/\\\x00]{0,199}$")


# ------------------------------------------------------------------ noms

def valid_game(name: object) -> bool:
    """Nom de jeu sûr pour un dossier : pas de « / », pas de « .. », pas de point initial."""
    return isinstance(name, str) and bool(_GAME_RE.match(name)) and ".." not in name


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
        rows.insert(0, {"id": GAME_HOST_ID, "name": f"machine de jeu ({_host_of(url)})", "url": url,
                        "fingerprint": _fingerprint_or_empty(pem), "kind": "game_host", "removable": False,
                        "icon": _chosen_icon(settings.saves_game_host_icon)})
    return rows


def images_source_row(settings: Settings) -> dict | None:
    """Machine associée source de l'onglet Images, None pour la bibliothèque du manager
    (ou si la machine a été oubliée)."""
    if not settings.images_source:
        return None
    return next((r for r in machine_rows(settings) if r["id"] == settings.images_source), None)


def game_host_machine(settings: Settings) -> str:
    """Machine associée qui sert de machine de jeu (même adresse), "" sinon."""
    if settings.game_host != "agent" or not settings.agent_url:
        return ""
    for machine in saves_machines(settings):
        if _norm(machine["url"]) == _norm(settings.agent_url):
            return machine["id"]
    return ""


def use_as_game_host(settings: Settings, machine_id: str) -> None:
    """Fait d'une machine associée la machine de jeu (Réglages) : même adresse, même jeton,
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
    save_images_source(settings, machine_id)        # l'onglet Images montre cette machine


def find_machine(settings: Settings, machine_id: str) -> dict:
    """Ligne de la machine ; KeyError si elle n'existe pas (ou plus)."""
    for row in machine_rows(settings):
        if row["id"] == machine_id:
            return row
    raise KeyError(machine_id)


def _label(settings: Settings, machine_id: str) -> str:
    try:
        return find_machine(settings, machine_id)["name"]
    except KeyError:
        raise RebuildError(f"machine inconnue : {machine_id}") from None


def open_machine(settings: Settings, machine_id: str, timeout: float = 3600) -> AgentHost:
    """Client HTTPS vers une machine de la synchronisation (certificat épinglé)."""
    client_id = saves_client_id(settings)
    if machine_id == GAME_HOST_ID:
        if settings.game_host != "agent" or not settings.agent_url:
            raise RebuildError("machine de jeu : plus en mode agent")
        return AgentHost(settings, timeout, client_id=client_id)
    for machine in saves_machines(settings):
        if machine["id"] == machine_id:
            return AgentHost(settings, timeout, pem=machine["pem"], url=machine["url"],
                             token=machine["token"], client_id=client_id)
    raise RebuildError(f"machine inconnue : {machine_id}")


def add_machine(settings: Settings, code: str) -> dict:
    """Associe une machine par son code wsq:// : le secret du code s'échange ici, une
    seule fois, contre le jeton durable de ce manager (pair_agent) ; seul ce jeton est
    enregistré. Même empreinte ou même adresse qu'une machine enregistrée : remplacement.
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
    return {"id": entry["id"], "name": name, "url": paired["url"],
            "fingerprint": paired["fingerprint"], "replaced": bool(matches)}


def remove_machine(settings: Settings, machine_id: str) -> None:
    """KeyError si la machine n'est pas enregistrée ; la machine de jeu ne se retire pas.
    Si la machine oubliée était la machine de jeu (Réglages), la machine de jeu est
    désactivée : sinon son adresse resterait listée comme « machine de jeu », injoignable.
    Elle cesse aussi d'être la source de l'onglet Images."""
    if machine_id == GAME_HOST_ID:
        raise ValueError("machine de jeu : non supprimable (la désactiver dans Réglages)")
    machines = saves_machines(settings)
    kept = [m for m in machines if m["id"] != machine_id]
    if len(kept) == len(machines):
        raise KeyError(machine_id)
    was_game_host = game_host_machine(settings) == machine_id
    save_saves_machines(settings, kept)
    if was_game_host:
        save_game_host(settings, {"game_host": "off", "agent_url": "", "agent_token": ""})
    if settings.images_source == machine_id:
        save_images_source(settings, "")


def set_icon(settings: Settings, machine_id: str, icon: str | None) -> dict:
    """Icône choisie à la main d'une machine (None : retour à la détection).
    KeyError si la machine est inconnue ; ValueError si l'icône n'est pas dans ICONS."""
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


def archives_in(d: Path) -> list[str]:
    if not d.is_dir():
        return []
    return sorted((p.name[:-len(".tar.gz")] for p in d.glob("*.tar.gz") if p.name != CURRENT),
                  reverse=True)


def _write_part(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def store(settings: Settings, game: str, part: Path, *, fingerprint: str, newest: int,
          size: int, profile: str | None, source: str) -> dict:
    """Range une archive reçue (`part`, écrite entièrement) dans le manager.

    Même empreinte que la version courante : rien n'est archivé, seules les
    informations sont rafraîchies. Sinon l'ancienne version devient
    `<AAAAMMJJ-HHMMSS>.tar.gz` (+ `.json`) et `part` prend sa place ; les
    versions datées au-delà de KEEP_ARCHIVES sont supprimées. `size` : octets
    des fichiers de sauvegarde, tels que la machine les a comptés."""
    d = reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    current, meta_path = d / CURRENT, d / META
    old = reserve_meta(settings, game) if current.exists() else None
    meta = {"fingerprint": fingerprint, "newest": int(newest), "size": int(size), "profile": profile,
            "source": source, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}
    text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    if old is not None and old.get("fingerprint") == fingerprint:
        part.unlink(missing_ok=True)
        write_atomic(meta_path, text)
        return meta
    stamp = _stamp(d)
    if current.exists():
        swap_keep_old(current, part, d / f"{stamp}.tar.gz")
    else:
        publish_new(part, current)
    if old is not None:
        meta_part = d / "meta.json.part"
        _write_part(meta_part, text)
        swap_keep_old(meta_path, meta_part, d / f"{stamp}.json")
    else:
        write_atomic(meta_path, text)
    _prune(d)
    return meta


def remove_from_manager(settings: Settings, game: str) -> dict:
    """Retire la copie du jeu du manager : la version courante devient une version
    datée (avec son .json, comme store() lors d'un remplacement), puis current.tar.gz
    et meta.json disparaissent. Les versions datées sont gardées (KEEP_ARCHIVES).
    Absent du manager : rien à faire, sans erreur. {ok, message, archived}."""
    d = reserve_dir(settings, game)
    current, meta_path = d / CURRENT, d / META
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
    """Sauvegardes de la machine source → manager. `games` None : tous ceux de la source.
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
    empreinte (jeu supprimé sur la machine) compte comme absente."""
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
        store(settings, game, part, fingerprint=fingerprint, newest=newest,
              size=_size_of(entry), profile=profile, source=source)
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
    results = []
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
              "message": "", "archived": None}
    try:
        if host is None:
            raise RebuildError(error or "machine indisponible")
        meta = reserve_meta(settings, game)
        archive = reserve_dir(settings, game) / CURRENT
        if meta is None or not archive.is_file():
            raise RebuildError("absent du manager")
        reply = host.saves_put(game, archive, meta.get("profile"))
        fingerprint = str(reply.get("fingerprint") or meta.get("fingerprint") or "")
        result["archived"] = bool(reply.get("archived"))
    except SavesBusy:
        result["message"] = f"jeu en cours sur {label} : non envoyée"
        return result
    except RebuildError as exc:
        result["message"] = str(exc)
        return result
    except OSError as exc:
        result["message"] = f"manager illisible : {exc}"
        return result
    message = f"envoyée vers {label}"
    try:
        host.saves_ack(game, fingerprint)
    except RebuildError as exc:
        message += f" (état non enregistré : {exc})"
    result.update(ok=True, message=message)
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
            raise RebuildError(error or "machine indisponible")
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
    """Supprime les sauvegardes des jeux sur les machines cibles (archivées puis effacées
    par la machine) et, si `manager`, sur le manager (version courante datée).
    Par (jeu, cible) : {game, machine, machine_id, ok, message, archived}. Pour chaque jeu,
    les machines puis le manager ; une machine injoignable ou occupée échoue seule."""
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
    results = []
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


def best(states: list[dict]) -> dict | None:
    """Machine suggérée parmi les états présents : la plus lourde, puis la plus
    récente (couple (size, newest) le plus grand). None si aucune ne l'a."""
    candidates = [s for s in states if s["state"] in PRESENT_STATES]
    return max(candidates, key=lambda s: (s["size"], s["newest"])) if candidates else None


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
    copier un jeu sur une machine qui ne l'a pas : push_from, source explicite."""
    wanted = _dedupe(target_ids)
    results: list[dict] = []
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for entry in status(settings, games)["games"]:
        game = entry["game"]
        source = best(entry["machines"])
        if source is None or source["state"] == "identique":
            identical = any(s["state"] == "identique" for s in entry["machines"])
            results.append(_note(game, "déjà identiques" if identical else "aucune machine ne l'a : rien à pousser"))
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
                                "ok": False, "message": (state or {}).get("message") or "machine injoignable",
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
        results.append({"game": game, "ok": False, "message": "absent de toutes les machines joignables",
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
    {check, check_by} : `check` vrai si la machine suggérée n'a pas la date newest
    maximale parmi les présentes ; `check_by` est alors le nom de celle-ci.

    - « injoignable » : la machine n'a pas répondu, elle ne compte pas ;
    - « absente » : la machine n'a pas le jeu (stockage plus petit, jeu pas
      installé) ; ce n'est pas une différence, elle ne compte pas ;
    - « présente » : une seule machine a le jeu, rien à comparer ;
    - « identique » : les machines qui ont le jeu (deux au moins) ont la même
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
    for s in have:
        s["state"] = "plus avancée" if s["fingerprint"] == top["fingerprint"] else "en retard"
    newest = max(have, key=lambda s: s["newest"])
    if top["newest"] >= newest["newest"]:
        return no_check
    return {"check": True, "check_by": newest["name"]}


def status(settings: Settings, games: list[str] | None = None) -> dict:
    """État de la synchronisation : par machine (une requête saves_list et une requête
    info, en parallèle) et par jeu (libellés, manager, suggestion à vérifier).
    `games` None : tous les jeux connus."""
    rows = machine_rows(settings)

    def fetch(row: dict) -> tuple[dict | None, str, str]:
        """(jeux, erreur, système) : un échec de info laisse le système vide, sans
        rendre la machine injoignable si saves_list a répondu."""
        try:
            host = open_machine(settings, row["id"], timeout=15)
            listing = _present_listing(host.saves_list(timeout=15))
        except RebuildError as exc:
            return None, str(exc), ""
        try:
            info = host.info()
            system = str(info.get("os") or "") if isinstance(info, dict) else ""
        except RebuildError:
            system = ""
        return listing, "", system

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as pool:
        fetched = list(pool.map(fetch, rows))
    if games is None:
        names = set(reserve_games(settings))
        for listing, _, _ in fetched:
            if listing:
                names.update(n for n in listing if valid_game(n))
        wanted = sorted(names)
    else:
        wanted = _dedupe(g for g in games if valid_game(g))
    machines = []
    for row, (listing, error, system) in zip(rows, fetched):
        machines.append({"id": row["id"], "name": row["name"], "url": row["url"],
                         "ok": listing is not None, "message": error,
                         "games": len(listing) if listing is not None else None,
                         "os": system, "icon": row["icon"] or (system if system in ICONS else "linux"),
                         "icon_set": row["icon"] is not None})
    out = []
    for game in wanted:
        states = []
        for row, (listing, error, _) in zip(rows, fetched):
            state = {"id": row["id"], "name": row["name"], "state": "absente",
                     "fingerprint": "", "newest": 0, "size": 0, "message": ""}
            if listing is None:
                state.update(state="injoignable", message=error)
            elif game in listing:
                entry = listing[game]
                state.update(state="présente", fingerprint=str(entry.get("fingerprint") or ""),
                             newest=int(entry.get("newest") or 0), size=_size_of(entry))
            states.append(state)
        verdict = label_states(states)
        out.append({"game": game, "reserve": _reserve_summary(settings, game), "machines": states,
                    "check": verdict["check"], "check_by": verdict["check_by"]})
    return {"machines": machines, "games": out}


# ------------------------------------------------------------------ tâche

def run_saves(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « saves » (file de tâches, comme la vérification).
    params : {op: pull|pull_auto|push|push_from|push_auto|delete, source?, targets?, games (liste ou null = tous)} ;
    delete : {machines (ids), manager (bool), games (liste)}.
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
        label = f"vers {len(p['targets'])} machine(s)"
    elif op == "push_from":
        result = push_from(settings, p["source"], p["targets"], games, job)
        items = result["pull"]["results"] + result["push"]["results"]
        label = f"depuis {result['pull']['source']['name']}"
    elif op == "pull_auto":
        result = pull_auto(settings, games, job)
        items = result["results"]
        label = "depuis les machines les plus avancées"
    elif op == "push_auto":
        result = push_auto(settings, p["targets"], games, job)
        items = result["pull"]["results"] + result["push"]["results"]
        label = f"vers {len(p['targets'])} machine(s), chacune depuis la plus avancée"
    elif op == "delete":
        machines = list(p.get("machines") or [])
        result = delete(settings, machines, games or [], bool(p.get("manager")), job)
        items = result["results"]
        where = " et ".join(part for part in (f"{len(machines)} machine(s)" if machines else "",
                                              "le manager" if p.get("manager") else "") if part)
        label = f"sur {where}"
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
