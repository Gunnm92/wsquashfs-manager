"""Activité des clients : jeux en cours, manettes et événements de partie (agent 11).

Deux sources, sans interrogation périodique des clients :
- GET /v1/info des agents (appelé à chaque relecture des sauvegardes, saves.status) : état de
  référence (jeux en cours, manettes, version du lanceur), qui remplace l'état connu du client ;
- POST /v1/events du lanceur (machine_api) : partie lancée ou terminée, entre deux relectures.

L'interface lit l'état et les nouveaux événements ici (GET /api/clients/activity), sans contacter
les clients. Les événements de fin de partie sont aussi gardés dans activity.jsonl (historique borné).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque

from ..config import Settings

EVENT_KINDS = ("start", "exit")
CONTROLLER_KINDS = ("dualsense", "playstation", "xbox", "steam", "nintendo", "gamepad")
CRASH_SECONDS = 60                  # sortie en erreur avant ce délai : plantage probable
MAX_EVENTS = 200                    # événements gardés en mémoire pour l'interface
HISTORY_MAX = 2000                  # lignes de activity.jsonl au-delà desquelles on élague
HISTORY_KEEP = 1000

_lock = threading.Lock()
_live: dict[str, dict[str, int]] = {}            # client → {jeu: début (s)}
_controllers: dict[str, list[dict]] = {}         # client → manettes au dernier relevé
_events: deque[dict] = deque(maxlen=MAX_EVENTS)
_seq = 0


def clean_controllers(value: object) -> list[dict]:
    """Manettes valides d'une réponse d'agent ou d'un événement (16 au plus)."""
    out = []
    for c in value if isinstance(value, list) else []:
        if not isinstance(c, dict) or c.get("kind") not in CONTROLLER_KINDS:
            continue
        bus = c.get("bus") if c.get("bus") in ("usb", "bluetooth") else ""
        out.append({"kind": c["kind"], "name": str(c.get("name") or "")[:80], "bus": bus})
    return out[:16]


def clean_playing(value: object) -> dict[str, int]:
    """Jeux en cours d'une réponse d'agent : {jeu: début}."""
    out = {}
    for p in value if isinstance(value, list) else []:
        if isinstance(p, dict) and isinstance(p.get("game"), str) and 0 < len(p["game"]) <= 200:
            since = p.get("since")
            out[p["game"]] = since if isinstance(since, int) and since > 0 else 0
    return out


def from_info(machine_id: str, info: dict) -> None:
    """État de référence d'un client relu par GET /v1/info (agent 11 : champs playing, controllers)."""
    if "playing" not in info:
        return                                          # agent trop ancien : rien de connu
    with _lock:
        _live[machine_id] = clean_playing(info.get("playing"))
        _controllers[machine_id] = clean_controllers(info.get("controllers"))


def forget(machine_id: str) -> None:
    with _lock:
        _live.pop(machine_id, None)
        _controllers.pop(machine_id, None)


def record(settings: Settings, machine: dict, event: dict) -> dict:
    """Un événement envoyé par un client (déjà validé par machine_api) ; rend l'entrée du journal."""
    global _seq
    mid, game, kind = machine["id"], event["game"], event["event"]
    now = int(time.time())
    at = event.get("at") if isinstance(event.get("at"), int) and event["at"] > 0 else now
    entry = {"machine": mid, "name": machine.get("name") or mid, "event": kind, "game": game, "at": at}
    with _lock:
        if kind == "start":
            _live.setdefault(mid, {})[game] = at
            if event.get("controllers") is not None:
                _controllers[mid] = clean_controllers(event.get("controllers"))
        else:
            started = event.get("started") if isinstance(event.get("started"), int) else _live.get(mid, {}).get(game)
            _live.get(mid, {}).pop(game, None)
            code = event.get("code") if isinstance(event.get("code"), int) else None
            duration = max(0, at - started) if started else None
            entry.update(code=code, duration=duration,
                         crash=bool(code not in (0, None) and duration is not None and duration < CRASH_SECONDS))
        _seq += 1
        entry["seq"] = _seq
        _events.append(entry)
    if kind == "exit":
        _history(settings, entry)
    return entry


def _history(settings: Settings, entry: dict) -> None:
    """Ajoute une fin de partie à activity.jsonl (élagué au-delà de HISTORY_MAX lignes)."""
    path = settings.state_dir / "activity.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in entry.items() if k != "seq"}, ensure_ascii=False) + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > HISTORY_MAX:
                path.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass                                            # l'historique n'empêche jamais l'événement


def snapshot(since: int = -1) -> dict:
    """État pour l'interface : jeux en cours et manettes par client, événements après `since`
    (tous si négatif ; aucun si `since` vaut 0 et qu'il n'y en a pas encore)."""
    with _lock:
        return {
            "seq": _seq,
            "live": {mid: [{"game": g, "since": s} for g, s in sorted(games.items(), key=lambda x: x[1])]
                     for mid, games in _live.items()},
            "controllers": {mid: list(c) for mid, c in _controllers.items()},
            "events": [dict(e) for e in _events if e["seq"] > since] if since >= 0 else [dict(e) for e in _events],
        }


def reset() -> None:
    """Remise à zéro (tests)."""
    global _seq
    with _lock:
        _live.clear()
        _controllers.clear()
        _events.clear()
        _seq = 0
