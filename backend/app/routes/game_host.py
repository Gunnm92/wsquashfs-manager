"""Client d'essai : maintenance du lanceur, voyant de statut, état du lanceur
(`--doctor`, mis en cache)."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import deps
from ..services.rebuild import RebuildError
from ..services.trial import MAINTENANCE_COMMANDS, host_status, maintenance, maintenance_game_rule

router = APIRouter()


class MaintenanceRequest(BaseModel):
    action: Literal["list", "umount", "clean", "save", "saves", "sync", "sync-status"]
    id: str | None = None           # save : requis (fiche du jeu) ; sync : facultatif ; les autres : interdit


@router.post("/api/game_host/maintenance")
def game_host_maintenance(body: MaintenanceRequest):
    """Maintenance du lanceur sur le client d'essai : lister, démonter, nettoyer,
    copier les sauvegardes d'un jeu (save) ou de tous (saves), synchroniser les
    sauvegardes avec les autres clients associés (sync, un jeu ou tous) et
    afficher l'état de la synchro (sync-status, sans rien changer)."""
    rule = maintenance_game_rule(body.action)
    shown = " ".join(MAINTENANCE_COMMANDS[body.action][0])
    if rule == "obligatoire" and not body.id:
        raise HTTPException(status_code=422, detail=f"{shown} demande un jeu")
    if rule == "interdit" and body.id:
        raise HTTPException(status_code=422, detail=f"{shown} ne prend pas de jeu")
    image = deps._find_image(body.id).path if body.id else None
    try:
        return maintenance(deps._settings, body.action, image)
    except RebuildError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


_status_cache: dict = {}


_STATUS_TTL = 20            # s : le voyant ne refait pas le test à chaque ouverture


@router.get("/api/game_host/status")
def game_host_status(refresh: bool = False):
    """Voyant du client d'essai (connecté, injoignable, désactivé)."""
    now = time.monotonic()
    cached = _status_cache.get("value")
    if cached and not refresh and now - cached[0] < _STATUS_TTL and cached[2] == deps._settings.game_host:
        return cached[1]
    result = host_status(deps._settings)
    _status_cache["value"] = (now, result, deps._settings.game_host)
    return result


@router.get("/api/launcher/doctor")
def launcher_doctor(refresh: bool = False):
    """État du lanceur sur le client d'essai (`wsquashfs-launcher --doctor`) :
    sections vérifiées et runners installés. Mis en cache quelques minutes."""
    try:
        return deps._doctor(refresh)
    except RebuildError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
