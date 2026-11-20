"""Tâches de fond : synchronisation automatique des clients en retard et purge des

anciennes versions (.old). Arrêtées à l'extinction (lifespan dans main.py)."""

from __future__ import annotations

import logging
import threading

from . import deps, library_index
from .services import saves_sync
from .services.errors import ManagerError
from .services.rebuild import purge_backups

logger = logging.getLogger("wsquashfs-manager")


background_stop = threading.Event()     # arrêt des tâches de fond à l'extinction


AUTO_PUSH_IDLE = 5                      # secondes entre deux tours quand la synchro auto est désactivée


def auto_push_loop() -> None:
    """Mise à jour automatique des clients en retard (services/saves_sync.py sync_auto), toutes les
    `auto_push_interval` secondes ; 0 : désactivée (tours vides). Relu à chaque tour : un réglage
    prend effet tout de suite."""
    while not background_stop.wait(deps.settings.auto_push_interval or AUTO_PUSH_IDLE):
        if not deps.settings.auto_push_interval or deps.settings.mode == "autonome":
            continue
        try:
            saves_sync.sync_auto(deps.settings)
        except Exception:  # tâche de fond : une erreur ne tue pas la boucle
            logger.exception("mise à jour automatique des clients impossible")


def purge_old_backups() -> None:
    try:
        images = [info.path for info in library_index.refresh_library(deps.settings, force=False)]
        purge_backups(images, deps.settings.old_retention_days)
    except (OSError, ManagerError):
        # tâche de fond : un dossier inaccessible ne bloque pas le démarrage
        logger.exception("purge des .old impossible")
