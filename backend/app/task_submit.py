"""Soumission de tâches à la file et refus d'agir sur une image : tâche en cours, jeu lancé,
.old à valider, mode autonome. La file et les réglages sont passés par l'appelant."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException

from . import library_index
from .config import Settings
from .models import ImageInfo, ImageState, Task
from .services import operations, scan
from .services.errors import ManagerError
from .services.rebuild import pending_backup
from .services.tasks import TaskConflict, TaskQueue

MODE_AUTONOME_DETAIL = "mode autonome : fonction connectée désactivée (Réglages › Mode)"


def connected_task(handler, settings: Settings):
    """Gestionnaire d'une tâche qui touche un client : refusé en mode autonome (une tâche
    restée en file avant le passage en autonome échoue au lieu de se connecter)."""
    def run(task, job):
        if settings.mode == "autonome":
            raise ManagerError(MODE_AUTONOME_DETAIL)
        return handler(settings, task, job)
    return run


def blocked_reason(queue: TaskQueue, info: ImageInfo, rebuild: bool = True) -> str | None:
    """Ce qui empêche d'agir sur l'image maintenant (de la reconstruire si
    `rebuild`)."""
    if info.state == ImageState.IN_USE:
        return "jeu en cours d'utilisation"
    if rebuild and pending_backup(info.path) is not None:
        return "sauvegarde .old à valider ou restaurer d'abord"
    if queue.active_for(info.id):
        return "une tâche est déjà en attente ou en cours"
    return None


def submit(queue: TaskQueue, info: ImageInfo, kind: str, params: dict, title: str,
           rebuild: bool = True, night: bool = False) -> Task:
    reason = blocked_reason(queue, info, rebuild)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return queue.submit(kind, info.id, info.path, params, title, night=night)
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def old_action(settings: Settings, queue: TaskQueue, image_id: str, action: Callable,
               fresh: bool = True) -> ImageInfo:
    """Valide ou restaure le .old d'une image. `fresh` : bibliothèque relue après coup (une
    action en masse la relit une seule fois à la fin : un scan complet par image prend des
    secondes)."""
    info = library_index.find_image(settings, image_id)
    if queue.active_for(info.id):
        raise HTTPException(status_code=409, detail="une tâche est en cours sur cette image")
    if info.state == ImageState.IN_USE or scan.image_in_use(info.path):
        raise HTTPException(status_code=409, detail="jeu en cours d'utilisation")
    try:
        action(info.path)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not fresh:
        return info
    return library_index.fresh_image(settings, queue, image_id)


def reset_saves(settings: Settings, queue: TaskQueue, image_id: str) -> str:
    """Met de côté les sauvegardes d'une image (jeu non lancé, aucune tâche) ; nom du dossier
    renommé."""
    info = library_index.find_image(settings, image_id)
    reason = blocked_reason(queue, info, rebuild=False)
    if reason is None and scan.image_in_use(info.path):
        reason = "jeu en cours d'utilisation"
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return operations.reset_saves(settings, info.path).name
    except (ManagerError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
