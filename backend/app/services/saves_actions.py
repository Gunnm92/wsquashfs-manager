"""Actions de sauvegardes demandées par l'utilisateur ou la tâche « saves ».

Ce sont les opérations qui s'appuient sur l'état des clients (`status`) :
départage d'un conflit (`resolve`), copie ou envoi depuis le client « le plus
avancé » (`pull_auto`, `push_auto`) et le gestionnaire de tâche `run_saves`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from ..config import Settings
from ..models import Task
from .clients import find_machine, open_machine
from .durable import fsync_dir, publish_new, swap_keep_old, write_atomic
from .errors import ManagerError
from .jobs import Job
from .saves import DIR_PREFIX, check_game, clean_dir, dedupe
from .saves_status import status
from .saves_store import (
    CURRENT,
    META,
    STORE_LOCK,
    conflict_versions,
    new_stamp,
    prune,
    record_state,
    reserve_dir,
    reserve_meta,
    write_part,
)
from .saves_sync import best, delete, note, present_listing, pull, pull_one, push, push_from


def resolve(settings: Settings, game: str, keep: str, job: Job | None = None) -> dict:
    """Départage un jeu en conflit. `keep` : « manager » (la version courante reste), le nom d'une
    version de conflit rangée (elle devient la courante ; l'ancienne courante est archivée comme
    lors d'un remplacement) ou « client:<id> » (la copie actuelle de ce client est récupérée et
    devient la courante). Les autres versions de conflit deviennent des versions datées normales
    (puis prune). Tout client encore en conflit après relecture (action « conflit », qu'une
    version soit rangée ou non) et dont la copie n'a pas été retenue est noté « à l'empreinte de sa
    copie » : il apparaît alors en retard et reçoit la version du manager à la synchro suivante (au
    lieu de repasser en conflit). Rien à départager : ok, sans rien changer. ManagerError si `keep`
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
            raise ManagerError(f"client inconnu : {client_id}") from None
        row = next((m for m in machines if m["id"] == client_id), None)
        if row is None or row["state"] == "injoignable":
            raise ManagerError(f"client injoignable : {(row or {}).get('name') or client_id}"
                               + (f" ({row['message']})" if row and row["message"] else ""))
        if row["state"] != "présente":
            raise ManagerError(f"{row['name']} n'a pas ce jeu")
        host = open_machine(settings, client_id)
        got = pull_one(settings, host, row["name"], game, present_listing(host.saves_list()).get(game))
        if not got["ok"]:
            raise ManagerError(f"{row['name']} : {got['message']}")
        received = got["fingerprint"]
    with STORE_LOCK:
        d = reserve_dir(settings, game)
        versions = conflict_versions(settings, game)
        chosen = None
        if keep != "manager" and not client_id:
            chosen = next((v for v in versions if v["name"] == keep), None)
            if chosen is None:
                raise ManagerError(f"version de conflit inconnue : {keep}")
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
            stamp = new_stamp(d)
            if current.exists():
                swap_keep_old(current, chosen["path"], d / f"{stamp}.tar.gz")
                if prior:
                    meta_part = d / "meta.json.part"
                    write_part(meta_part, text)
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
                stamp = new_stamp(d)
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
        prune(d)
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
    wanted = dedupe(target_ids)
    results: list[dict] = []
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for entry in status(settings, games)["games"]:
        game = entry["game"]
        source = best(entry["machines"])
        if source is None or source["state"] == "identique":
            identical = any(s["state"] == "identique" for s in entry["machines"])
            results.append(note(game, "déjà identiques" if identical else "aucun client ne l'a : rien à pousser"))
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
            results.append(note(game, "aucune cible joignable autre que la source"))
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
        raise ManagerError(f"opération de sauvegardes inconnue : {op}")
    task.params["result"] = result
    for item in items:
        job.log(f"{item['game']} : {item['message']}")
    failed = [item for item in items if not item["ok"]]
    if not items:
        return "rien à faire"
    if failed:
        raise ManagerError(f"{len(failed)} échec(s) sur {len(items)} ({label}) : "
                           f"{failed[0]['game']} : {failed[0]['message']}")
    return f"{len(items)} opération(s) réussie(s) {label}"
