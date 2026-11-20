"""Index en mémoire de la bibliothèque : le dernier scan, pour que la fiche d'une image ne
relance pas un scan complet. Les réglages et la file de tâches sont passés par l'appelant.
L'état « en cours » est revérifié par la file avant d'agir."""

from __future__ import annotations

import threading

from fastapi import HTTPException

from .config import Settings
from .models import ImageInfo, ImageState
from .services import scan
from .services.tasks import TaskQueue

library: dict[str, ImageInfo] = {}


library_lock = threading.Lock()


def refresh_library(settings: Settings, force: bool) -> list[ImageInfo]:
    """Rescanne les dossiers roms/ et remplace l'index."""
    images = scan.scan_all(settings, force=force)
    with library_lock:
        library.clear()
        library.update({info.id: info for info in images})
    return images


def with_task_state(queue: TaskQueue, info: ImageInfo) -> ImageInfo:
    """L'image affichée « modifiée » tant qu'une tâche est en attente ou en cours dessus."""
    if queue.active_for(info.id) and info.state != ImageState.IN_USE:
        info = info.model_copy(update={"state": ImageState.MODIFIED})
    return info


def find_image(settings: Settings, image_id: str) -> ImageInfo:
    """Image de l'index (rescannée si elle manque ou a disparu) ; 404 si inconnue."""
    with library_lock:
        info = library.get(image_id)
    if info is None or not info.path.exists():
        refresh_library(settings, force=False)
        with library_lock:
            info = library.get(image_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"image inconnue : {image_id}")
    return info


def fresh_image(settings: Settings, queue: TaskQueue, image_id: str) -> ImageInfo:
    """Image relue (après une reconstruction, un échange de .old…)."""
    refresh_library(settings, force=False)
    return with_task_state(queue, find_image(settings, image_id))
