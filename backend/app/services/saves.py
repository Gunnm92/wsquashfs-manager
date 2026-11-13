"""Réserve de sauvegardes et synchronisation entre machines (SPEC « Réserve de sauvegardes »).

- Machines : celles de config.json (`saves_machines`, associées par code
  wsq://), plus la machine de jeu en mode agent (id « game-host », d'office).
- Réserve : `state_dir/saves-reserve/<jeu>/` avec `current.tar.gz`, `meta.json`
  et, avant chaque remplacement, l'ancienne version datée (3 gardées).
- Opérations : pull (machine → réserve), push (réserve → machines),
  push_from (pull puis push des jeux récupérés), status (états par machine).

Les écritures de la réserve passent par durable.py (fichier .part, fsync,
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

from ..config import Settings, save_saves_machines, saves_client_id, saves_machines
from ..models import Task
from .durable import publish_new, swap_keep_old, write_atomic
from .rebuild import Job, RebuildError
from .trial import (
    AgentHost,
    SavesBusy,
    agent_cert_path,
    fingerprint_pem,
    parse_pair_code,
    probe_agent,
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


def machine_rows(settings: Settings) -> list[dict]:
    """Machines de la synchronisation, sans jeton ni certificat :
    {id, name, url, fingerprint, kind, removable}."""
    rows: list[dict] = []
    saved_urls = set()
    for machine in saves_machines(settings):
        saved_urls.add(_norm(machine["url"]))
        rows.append({"id": machine["id"], "name": machine["name"], "url": machine["url"],
                     "fingerprint": _fingerprint_or_empty(machine["pem"]),
                     "kind": "saved", "removable": True})
    if settings.game_host == "agent" and settings.agent_url and _norm(settings.agent_url) not in saved_urls:
        cert = agent_cert_path(settings)
        pem = cert.read_text() if cert.exists() else None
        url = settings.agent_url.rstrip("/")
        rows.insert(0, {"id": GAME_HOST_ID, "name": f"machine de jeu ({_host_of(url)})", "url": url,
                        "fingerprint": _fingerprint_or_empty(pem), "kind": "game_host", "removable": False})
    return rows


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
    """Associe une machine par son code wsq:// (même vérification que l'association
    de l'agent : certificat contre l'empreinte du code, jeton vérifié par /v1/info).
    Même empreinte ou même adresse qu'une machine enregistrée : remplacement.
    ValueError si le code est illisible ; AgentProbeError (avec son code HTTP) sinon."""
    pair = parse_pair_code(code)
    probe = probe_agent(settings, pair["url"], pair["token"], expected=pair["fingerprint"])
    info = probe["info"] or {}
    name = str(info.get("host") or "").strip() or _host_of(probe["url"])
    machines = saves_machines(settings)
    matches = [i for i, m in enumerate(machines)
               if _norm(m["url"]) == _norm(probe["url"])
               or _fingerprint_or_empty(m["pem"]).replace(":", "") == probe["fingerprint"].replace(":", "")]
    entry = {"id": secrets.token_hex(4), "name": name, "url": probe["url"],
             "token": pair["token"], "pem": probe["pem"]}
    if matches:
        entry["id"] = machines[matches[0]]["id"]          # le remplacement garde l'identifiant
        kept = [m for i, m in enumerate(machines) if i not in matches]
        kept.insert(matches[0], entry)
        machines = kept
    else:
        machines.append(entry)
    save_saves_machines(settings, machines)
    return {"id": entry["id"], "name": name, "url": probe["url"],
            "fingerprint": probe["fingerprint"], "replaced": bool(matches)}


def remove_machine(settings: Settings, machine_id: str) -> None:
    """KeyError si la machine n'est pas enregistrée ; la machine de jeu ne se retire pas."""
    if machine_id == GAME_HOST_ID:
        raise ValueError("machine de jeu : non supprimable (la désactiver dans Réglages)")
    machines = saves_machines(settings)
    kept = [m for m in machines if m["id"] != machine_id]
    if len(kept) == len(machines):
        raise KeyError(machine_id)
    save_saves_machines(settings, kept)


# ------------------------------------------------------------------ réserve

def reserve_root(settings: Settings) -> Path:
    return settings.state_dir / RESERVE_DIR


def reserve_dir(settings: Settings, game: str) -> Path:
    return reserve_root(settings) / check_game(game)


def reserve_games(settings: Settings) -> list[str]:
    """Jeux ayant une version courante dans la réserve."""
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
          profile: str | None, source: str) -> dict:
    """Range une archive reçue (`part`, écrite entièrement) dans la réserve.

    Même empreinte que la version courante : rien n'est archivé, seules les
    informations sont rafraîchies. Sinon l'ancienne version devient
    `<AAAAMMJJ-HHMMSS>.tar.gz` (+ `.json`) et `part` prend sa place ; les
    versions datées au-delà de KEEP_ARCHIVES sont supprimées."""
    d = reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    current, meta_path = d / CURRENT, d / META
    old = reserve_meta(settings, game) if current.exists() else None
    meta = {"fingerprint": fingerprint, "newest": int(newest), "profile": profile,
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


# ------------------------------------------------------------------ opérations

def _progress(job: Job | None, phase: str, done: int, total: int) -> None:
    if job:
        job.progress(phase, done / total if total else 1.0)


def _check(job: Job | None) -> None:
    if job:
        job.check()


def pull(settings: Settings, source_id: str, games: list[str] | None,
         job: Job | None = None) -> dict:
    """Sauvegardes de la machine source → réserve. `games` None : tous ceux de la source.
    Par jeu : {game, ok, message, fingerprint, newest}. Accusé (saves_ack) après
    chaque réception réussie."""
    host = open_machine(settings, source_id)
    source = _label(settings, source_id)
    listing = {g["name"]: g for g in host.saves_list()
               if isinstance(g, dict) and isinstance(g.get("name"), str)}
    wanted = sorted(n for n in listing if valid_game(n)) if games is None else _dedupe(games)
    results = []
    for index, game in enumerate(wanted):
        _check(job)
        _progress(job, f"récupération {game}", index, len(wanted))
        results.append(_pull_one(settings, host, source, game, listing.get(game)))
    _progress(job, "récupéré", len(wanted), len(wanted))
    return {"source": {"id": source_id, "name": source}, "results": results}


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
              profile=profile, source=source)
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
    """Réserve → machines cibles. `games` None : tous les jeux de la réserve.
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
            _progress(job, f"envoi {game} → {label}", len(results), total)
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
            raise RebuildError("absent de la réserve")
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
        result["message"] = f"réserve illisible : {exc}"
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


def _reserve_summary(settings: Settings, game: str) -> dict | None:
    if not (reserve_dir(settings, game) / CURRENT).is_file():
        return None
    meta = reserve_meta(settings, game) or {}
    return {"fingerprint": meta.get("fingerprint", ""), "newest": int(meta.get("newest") or 0),
            "source": meta.get("source", ""), "fetched_at": meta.get("fetched_at", ""),
            "archives": len(archives(settings, game))}


def label_states(states: list[dict]) -> None:
    """Libellés par machine, pour un jeu (modifie `state` en place).

    - « injoignable » : la machine n'a pas répondu ; « absente » : pas ce jeu ;
    - « identique » : toutes les machines qui ont le jeu ont la même version ;
    - sinon, « plus récente » pour les machines qui ont la version la plus récente
      (newest max : la source à choisir), « plus ancienne » pour les autres."""
    present = [s for s in states if s["state"] == "présente"]
    if not present:
        return
    if len({s["fingerprint"] for s in present}) == 1:
        for s in present:
            s["state"] = "identique"
        return
    newest = max(present, key=lambda s: s["newest"])["fingerprint"]
    for s in present:
        s["state"] = "plus récente" if s["fingerprint"] == newest else "plus ancienne"


def status(settings: Settings, games: list[str] | None = None) -> dict:
    """État de la synchronisation : par machine (une seule requête saves_list, en
    parallèle) et par jeu (libellés, réserve). `games` None : tous les jeux connus."""
    rows = machine_rows(settings)

    def fetch(row: dict):
        try:
            host = open_machine(settings, row["id"], timeout=15)
            listing = {g["name"]: g for g in host.saves_list(timeout=15)
                       if isinstance(g, dict) and isinstance(g.get("name"), str)}
            return listing, ""
        except RebuildError as exc:
            return None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as pool:
        fetched = list(pool.map(fetch, rows))
    if games is None:
        names = set(reserve_games(settings))
        for listing, _ in fetched:
            if listing:
                names.update(n for n in listing if valid_game(n))
        wanted = sorted(names)
    else:
        wanted = _dedupe(g for g in games if valid_game(g))
    machines = [{"id": row["id"], "name": row["name"], "url": row["url"],
                 "ok": listing is not None, "message": error,
                 "games": len(listing) if listing is not None else None}
                for row, (listing, error) in zip(rows, fetched)]
    out = []
    for game in wanted:
        states = []
        for row, (listing, error) in zip(rows, fetched):
            state = {"id": row["id"], "name": row["name"], "state": "absente",
                     "fingerprint": "", "newest": 0, "message": ""}
            if listing is None:
                state.update(state="injoignable", message=error)
            elif game in listing:
                entry = listing[game]
                state.update(state="présente", fingerprint=str(entry.get("fingerprint") or ""),
                             newest=int(entry.get("newest") or 0))
            states.append(state)
        label_states(states)
        out.append({"game": game, "reserve": _reserve_summary(settings, game), "machines": states})
    return {"machines": machines, "games": out}


# ------------------------------------------------------------------ tâche

def run_saves(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « saves » (file de tâches, comme la vérification).
    params : {op: pull|push|push_from, source?, targets?, games (liste ou null = tous)}.
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
