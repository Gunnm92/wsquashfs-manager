"""Opérations de synchronisation et décision : pull, push, suppression, synchro automatique.

- Opérations : pull (client → manager), push (manager → clients), push_from
  (pull puis push des jeux récupérés), delete (clients et/ou manager ;
  archivée d'abord).
- Décision : `decide` (que faire d'un jeu selon l'état commun, celui du client
  et celui du manager) et `best` (le client « le plus avancé » : le plus récent
  parmi ceux de taille proche de la plus lourde, écart ≤ SIZE_TOLERANCE).
- Synchro automatique : `sync_auto` met à jour les clients en retard et garde
  les versions en conflit.

L'état exposé à l'interface est dans saves_status.py.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from ..config import Settings
from ..models import Task, TaskStatus
from .agent import AgentHost, SavesBusy
from .clients import machine_label, machine_rows, open_machine
from .durable import append_line
from .errors import ManagerError
from .jobs import Job
from .saves import clean_dir, dedupe, valid_game
from .saves_store import (
    CURRENT,
    STORE_LOCK,
    base_state,
    conflict_versions,
    current_fingerprint,
    machine_state,
    record_state,
    remove_from_manager,
    reserve_dir,
    reserve_games,
    reserve_meta,
    store,
    store_conflict,
)

log = logging.getLogger("wsquashfs-manager")


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
    source = machine_label(settings, source_id)
    listing = present_listing(host.saves_list())
    wanted = sorted(n for n in listing if valid_game(n)) if games is None else dedupe(games)
    results = []
    for index, game in enumerate(wanted):
        _check(job)
        _progress(job, f"récupération {index + 1}/{len(wanted)} : {game}", index, len(wanted))
        results.append(pull_one(settings, host, source, game, listing.get(game)))
    _progress(job, "récupéré", len(wanted), len(wanted))
    return {"source": {"id": source_id, "name": source}, "results": results}


def present_listing(games: list) -> dict[str, dict]:
    """Jeux d'une réponse de saves_list qui ont une sauvegarde : une entrée sans
    empreinte (jeu supprimé sur le client) compte comme absente."""
    return {g["name"]: g for g in games
            if isinstance(g, dict) and isinstance(g.get("name"), str) and g.get("fingerprint")}


def pull_one(settings: Settings, host: AgentHost, source: str, game: str,
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
                  size=size_of(entry), profile=profile, source=source,
                  dir_path=str(entry.get("dir") or "") or None,
                  content=str(entry.get("content") or ""))
    except SavesBusy:
        result["message"] = f"jeu en cours sur {source} : non récupéré"
        return result
    except (ManagerError, OSError) as exc:
        result["message"] = str(exc)
        return result
    finally:
        part.unlink(missing_ok=True)
    warning = ""
    try:
        host.saves_ack(game, fingerprint)
    except ManagerError as exc:
        warning = f" (état non enregistré sur {source} : {exc})"
    result.update(ok=True, fingerprint=fingerprint, newest=newest,
                  message=f"récupérée de {source}{warning}")
    return result


def push(settings: Settings, target_ids: list[str], games: list[str] | None,
         job: Job | None = None) -> dict:
    """Manager → machines cibles. `games` None : tous les jeux du manager.
    Par (jeu, machine) : {game, machine, machine_id, ok, message, archived}."""
    wanted = dedupe(games) if games is not None else reserve_games(settings)
    targets = []
    for machine_id in dedupe(target_ids):
        try:
            host = open_machine(settings, machine_id)
            error = ""
        except ManagerError as exc:
            host, error = None, str(exc)
        targets.append((machine_id, machine_label(settings, machine_id), host, error))
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
            raise ManagerError(error or "client indisponible")
        meta = reserve_meta(settings, game)
        archive = reserve_dir(settings, game) / CURRENT
        if meta is None or not archive.is_file():
            raise ManagerError("absent du manager")
        reply = host.saves_put(game, archive, meta.get("profile"), clean_dir(meta.get("dir")))
        fingerprint = str(reply.get("fingerprint") or meta.get("fingerprint") or "")
        result["archived"] = bool(reply.get("archived"))
    except SavesBusy:
        result.update(message=f"jeu en cours sur {label} : non envoyée", busy=True)
        return result
    except ManagerError as exc:
        result["message"] = str(exc)
        return result
    except OSError as exc:
        result["message"] = f"manager illisible : {exc}"
        return result
    message = f"envoyée vers {label}"
    try:
        host.saves_ack(game, fingerprint)          # POST /v1/saves/state : le client garde l'empreinte commune
    except ManagerError as exc:
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
            raise ManagerError(error or "client indisponible")
        reply = host.saves_delete(game)
    except SavesBusy:
        result["message"] = f"jeu en cours sur {label} : non supprimée"
        return result
    except ManagerError as exc:
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
    wanted = dedupe(games)
    bad = [g for g in wanted if not valid_game(g)]
    if bad:
        raise ManagerError(f"nom de jeu invalide : {bad[0]!r}")
    targets = []
    for machine_id in dedupe(machine_ids):
        try:
            host = open_machine(settings, machine_id)
            error = ""
        except ManagerError as exc:
            host, error = None, str(exc)
        targets.append((machine_id, machine_label(settings, machine_id), host, error))
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
    except (ManagerError, OSError) as exc:
        result["message"] = str(exc)
        return result
    result.update(ok=True, message=reply["message"], archived=reply["archived"] or None)
    return result


PRESENT_STATES = {"présente", "identique", "plus avancée", "en retard"}


def note(game: str, message: str) -> dict:
    """Résultat sans transfert, pour un jeu (pas de machine)."""
    return {"game": game, "machine": "", "machine_id": "", "ok": True, "message": message, "archived": None}


def size_of(entry: dict | None) -> int:
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
    - « descendre » : le client est en retard (il n'a pas bougé depuis la base, ou la base est
      inconnue et la copie du client est plus ancienne : rien n'est remplacé, le manager pousse) ;
    - « conflit » : les deux ont changé depuis la base, ou base inconnue et copies de même date.
    Base inconnue : la copie la plus récente gagne, comme decideSync du lanceur (contract/cases/
    sync-decisions.yaml, dont cette fonction et decideSync donnent les mêmes résultats)."""
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
    if client_newest == manager_newest:
        return "conflit"
    return "remonter" if client_newest > manager_newest else "descendre"


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
        except ManagerError:
            continue                # injoignable, ou sans certificat : ignorée sans bruit
        if not isinstance(info, dict) or not info.get("auto"):
            continue                # synchro auto désactivée sur le client : jamais modifiée
        try:
            listing = present_listing(host.saves_list(timeout=15))
        except ManagerError:
            continue
        clients.append((row, host, listing))
    results: list[dict] = []
    if not clients:
        return results
    if games is not None:
        names = [g for g in dedupe(games) if valid_game(g)]
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
                result = pull_one(settings, host, row["name"], game, entry)
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
                                    newest=int((entry or {}).get("newest") or 0), size=size_of(entry),
                                    source=row["name"], content=str((entry or {}).get("content") or ""),
                                    profile=profile, dir_path=str((entry or {}).get("dir") or "") or None)
        out["fingerprint"] = got
        if not stored["created"]:
            return out
    except SavesBusy:
        out.update(action="jeu en cours", busy=True,
                   message=f"jeu en cours sur {row['name']} : conflit retenté au prochain passage")
    except (ManagerError, OSError) as exc:
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
