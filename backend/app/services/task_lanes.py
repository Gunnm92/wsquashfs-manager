"""Voies (files) d'une tâche : ce qu'elle occupe pendant son exécution.

- « disque » : travail local lourd (pack, mksquashfs, vérification, relink…). Capacité
  = `task_concurrency` (1 par défaut) : comme avant, un seul travail disque à la fois.
- « client:<id> » : un transfert ou une action sur un client (sauvegardes, installation,
  essai, runner). Une seule tâche à la fois par client : les autres clients ne sont pas
  bloqués par un envoi lent.
- « client:* » : une tâche qui peut toucher n'importe quel client (synchro automatique,
  départage, restauration…). Elle attend que tous les clients soient libres, et bloque
  les tâches par client le temps de son exécution.
- « image:<id> » : une seule tâche à la fois sur une même image.

Une tâche de transfert n'occupe pas la voie « disque » : elle écrit dans la réserve, mais
ne bloque ni un pack ni une vérification sur d'autres images.
"""

from __future__ import annotations

from ..config import Settings
from ..models import Task
from . import clients as clients_svc

DISK = "disque"
ALL_CLIENTS = "client:*"
CLIENT_PREFIX = "client:"
IMAGE_PREFIX = "image:"

# Tâches qui parlent à un client (identifié par ses params), hors « saves » et « install »
_GAME_HOST_KINDS = ("trial", "runner")
# Opérations de « saves » qui touchent un client dont le nom n'est connu qu'à l'exécution
_ANY_CLIENT_OPS = ("pull_auto", "push_auto", "resolve")


def client_lane(machine_id: str) -> str:
    return f"{CLIENT_PREFIX}{machine_id}"


def image_lane(image: str) -> str:
    return f"{IMAGE_PREFIX}{image}"


def _machines(ids: object) -> set[str]:
    """Voies des clients listés (liste d'identifiants) ; un ensemble vide si absent."""
    if not isinstance(ids, list):
        return set()
    return {client_lane(str(machine_id)) for machine_id in ids if machine_id}


def _saves_lanes(params: dict) -> set[str]:
    op = params.get("op")
    if op == "pull":
        return _machines([params.get("source")])
    if op == "push":
        return _machines(params.get("targets"))
    if op == "push_from":
        return _machines([params.get("source")]) | _machines(params.get("targets"))
    if op == "delete":
        return _machines(params.get("machines"))
    if op == "restore":
        return {ALL_CLIENTS} if params.get("push") else set()
    if op in _ANY_CLIENT_OPS:
        return {ALL_CLIENTS}
    return {ALL_CLIENTS}          # opération inconnue : prudence, tous les clients


def lanes_of(task: Task, settings: Settings) -> frozenset[str]:
    """Voies occupées par la tâche pendant son exécution."""
    lanes: set[str] = set()
    if task.image:
        lanes.add(image_lane(task.image))
    params = task.params or {}
    if task.kind == "saves":
        lanes |= _saves_lanes(params)
    elif task.kind == "install":
        lanes |= _machines(params.get("machines"))
    elif task.kind in _GAME_HOST_KINDS:
        lanes.add(client_lane(clients_svc.game_host_machine(settings) or clients_svc.GAME_HOST_ID))
    else:
        lanes.add(DISK)
    return frozenset(lanes)
