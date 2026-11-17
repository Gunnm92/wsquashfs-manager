"""Tâches de fond : synchronisation automatique des clients en retard et purge des

anciennes versions (.old). Arrêtées à l'extinction (lifespan dans main.py)."""

from __future__ import annotations

import logging
import threading

from . import deps
from .services import saves as saves_svc
from .services.rebuild import RebuildError, purge_backups

logger = logging.getLogger("wsquashfs-manager")


_background_stop = threading.Event()     # arrêt des tâches de fond à l'extinction


AUTO_PUSH_IDLE = 5                      # secondes entre deux tours quand la synchro auto est désactivée


def _auto_push_loop() -> None:
    """Mise à jour automatique des clients en retard (services/saves.py push_behind), toutes les
    `auto_push_interval` secondes ; 0 : désactivée (tours vides). Relu à chaque tour : un réglage
    prend effet tout de suite."""
    while not _background_stop.wait(deps._settings.auto_push_interval or AUTO_PUSH_IDLE):
        if not deps._settings.auto_push_interval or deps._settings.mode == "autonome":
            continue
        try:
            saves_svc.push_behind(deps._settings)
        except Exception:  # tâche de fond : une erreur ne tue pas la boucle
            logger.exception("mise à jour automatique des clients impossible")


def _purge_old_backups() -> None:
    try:
        images = [info.path for info in deps._refresh_library(force=False)]
        purge_backups(images, deps._settings.old_retention_days)
    except (OSError, RebuildError):
        # tâche de fond : un dossier inaccessible ne bloque pas le démarrage
        logger.exception("purge des .old impossible")
