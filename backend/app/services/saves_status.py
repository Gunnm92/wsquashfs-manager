"""État exposé de la synchronisation : par client et par jeu.

`status` interroge les clients (liste des jeux et info, en parallèle), saute
un moment ceux qui n'ont pas répondu, et rend pour chaque jeu son libellé, son
genre, son système, la réserve du manager et la décision `action` de chaque
client. Les clients injoignables sont retenus UNREACHABLE_TTL secondes.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import ICONS, Settings
from . import activity
from .clients import ensure_declared, machine_rows, manager_fingerprint, open_machine
from .errors import ManagerError
from .saves import DIR_PREFIX, SWITCH_PREFIX, dedupe, valid_game
from .saves_store import (
    CURRENT,
    archives,
    base_state,
    conflict_versions,
    current_fingerprint,
    reserve_dir,
    reserve_games,
    reserve_meta,
    synced_times,
)
from .saves_sync import best, decide, present_listing, size_of

log = logging.getLogger("wsquashfs-manager")


def _reserve_summary(settings: Settings, game: str) -> dict | None:
    if not (reserve_dir(settings, game) / CURRENT).is_file():
        return None
    meta = reserve_meta(settings, game) or {}
    return {"fingerprint": meta.get("fingerprint", ""), "newest": int(meta.get("newest") or 0),
            "size": size_of(meta), "source": meta.get("source", ""), "fetched_at": meta.get("fetched_at", ""),
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
                names = [entry.name[: -len(ext)] for ext in _SYSTEM_EXTS if entry.name.endswith(ext)]
                if entry.name.lower().endswith(".pc") and entry.is_dir(follow_symlinks=False):
                    names.append(entry.name[:-len(".pc")])      # dossier de jeu : même nom de jeu
                for name in names:
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
    except ManagerError as exc:             # pas de certificat (openssl absent) : pas de déclaration, l'état reste
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
            listing = present_listing(host.saves_list(timeout=15))
        except ManagerError as exc:
            with _unreachable_lock:
                _unreachable[key] = (time.monotonic(), str(exc))
            return None, str(exc), "", {}
        with _unreachable_lock:
            _unreachable.pop(key, None)
        try:
            info = host.info()
        except ManagerError:
            info = {}
        info = info if isinstance(info, dict) else {}
        ensure_declared(settings, row, info, manager_fp)
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
        wanted = dedupe(g for g in games if valid_game(g))
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
                             newest=int(entry.get("newest") or 0), size=size_of(entry),
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
        synced, synced_at = synced_times(settings, game)
        for state in states:
            state["synced"] = synced.get(state["id"], 0)
        kind = kind_of(game)
        out.append({"game": game, "synced_at": synced_at, "label": label_of(game, [listing for listing, _, _, _ in fetched]),
                    "kind": kind, "system": system_of(settings, game) if kind == "jeu" else "",
                    "reserve": _reserve_summary(settings, game), "machines": states,
                    "check": verdict["check"], "check_by": verdict["check_by"],
                    "conflict": any(state["conflict"] for state in states),
                    "conflicts": [{k: v[k] for k in ("name", "machine_id", "source", "newest", "size", "fingerprint")}
                                  for v in versions]})
    return {"machines": machines, "games": out}
