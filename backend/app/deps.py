"""État partagé de l'application : réglages, file de tâches, index de la bibliothèque,
caches de module et fonctions communes aux routeurs (recherche d'image, soumission de
tâche…). Les routeurs et les tests accèdent à ces noms par le module (`deps.X`), lus à
l'appel : un remplacement dans `deps` est vu partout."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import HTTPException

from .config import Settings, get_settings, in_night
from .models import ImageInfo, ImageState, Task
from .services import analyze as analyze_mod
from .services import clients as clients_svc
from .services import scan
from .services.analyze import FolderAnalysis
from .services.errors import ManagerError
from .services.install import run_install
from .services.operations import (
    reset_saves,
    run_autorun,
    run_cover,
    run_files,
    run_pack,
    run_verify,
)
from .services.rebuild import backup_path
from .services.runners import run_relink, run_runner
from .services.saves import run_saves
from .services.tasks import TaskConflict, TaskQueue
from .services.trial import run_doctor, run_trial
from .services.update import run_update

logger = logging.getLogger("wsquashfs-manager")


RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"


_settings: Settings = get_settings()


scan.LAUNCHER_CACHE = _settings.launcher_cache      # verrous et montages du lanceur


# Base de connaissances locale (ajouts faits dans l'interface), dans l'appdata
analyze_mod.LOCAL_GAMES_FILE = _settings.state_dir / "games.yaml"


queue = TaskQueue(
    _settings.state_dir / "tasks.json",
    handlers={
        "autorun": lambda task, job: run_autorun(_settings, task, job),
        "files": lambda task, job: run_files(_settings, task, job),
        "verify": lambda task, job: run_verify(_settings, task, job),
        "cover": lambda task, job: run_cover(_settings, task, job),
        "pack": lambda task, job: run_pack(_settings, task, job),
        "update": lambda task, job: run_update(_settings, task, job),
        "trial": lambda task, job: _connected_task(run_trial)(task, job),
        "runner": lambda task, job: _connected_task(run_runner)(task, job),
        "relink": lambda task, job: run_relink(_settings, task, job),
        "saves": lambda task, job: _connected_task(run_saves)(task, job),
        "install": lambda task, job: _connected_task(run_install)(task, job),
    },
    concurrency=_settings.task_concurrency,
    night_now=lambda: in_night(_settings),
)


MODE_AUTONOME_DETAIL = "mode autonome : fonction connectée désactivée (Réglages › Mode)"


def _connected_task(handler):
    """Gestionnaire d'une tâche qui touche un client : refusé en mode autonome (une tâche
    restée en file avant le passage en autonome échoue au lieu de se connecter)."""
    def run(task, job):
        if _settings.mode == "autonome":
            raise ManagerError(MODE_AUTONOME_DETAIL)
        return handler(_settings, task, job)
    return run


_doctor_cache: dict = {}


_DOCTOR_TTL = 300           # s : --doctor prend quelques secondes


def _doctor(refresh: bool = False) -> dict:
    now = time.monotonic()
    cached = _doctor_cache.get("result")
    if cached and not refresh and now - cached[0] < _DOCTOR_TTL:
        return {**cached[1], "age": round(now - cached[0])}
    result = run_doctor(_settings)
    _doctor_cache["result"] = (now, result)
    return {**result, "age": 0}


# Index en mémoire du dernier scan : la fiche d'une image ne relance pas un
# scan complet. L'état « en cours » est revérifié par la file avant d'agir.
_library: dict[str, ImageInfo] = {}


_library_lock = threading.Lock()


def _refresh_library(force: bool) -> list[ImageInfo]:
    images = scan.scan_all(_settings, force=force)
    with _library_lock:
        _library.clear()
        _library.update({info.id: info for info in images})
    return images


def _with_task_state(info: ImageInfo) -> ImageInfo:
    if queue.active_for(info.id) and info.state != ImageState.IN_USE:
        info = info.model_copy(update={"state": ImageState.MODIFIED})
    return info


def _find_image(image_id: str) -> ImageInfo:
    with _library_lock:
        info = _library.get(image_id)
    if info is None or not info.path.exists():
        _refresh_library(force=False)
        with _library_lock:
            info = _library.get(image_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"image inconnue : {image_id}")
    return info


def _fresh_image(image_id: str) -> ImageInfo:
    """Image relue (après une reconstruction, un échange de .old…)."""
    _refresh_library(force=False)
    return _with_task_state(_find_image(image_id))


def _blocked_reason(info: ImageInfo, rebuild: bool = True) -> str | None:
    """Ce qui empêche d'agir sur l'image maintenant (de la reconstruire si
    `rebuild`)."""
    if info.state == ImageState.IN_USE:
        return "jeu en cours d'utilisation"
    if rebuild and backup_path(info.path).exists():
        return "sauvegarde .old à valider ou restaurer d'abord"
    if queue.active_for(info.id):
        return "une tâche est déjà en attente ou en cours"
    return None


def _submit(info: ImageInfo, kind: str, params: dict, title: str,
            rebuild: bool = True, night: bool = False) -> Task:
    reason = _blocked_reason(info, rebuild)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return queue.submit(kind, info.id, info.path, params, title, night=night)
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _old_action(image_id: str, action, fresh: bool = True) -> ImageInfo:
    """Valide ou restaure le .old d'une image. `fresh` : bibliothèque relue après coup (une
    action en masse la relit une seule fois à la fin : un scan complet par image prend des
    secondes)."""
    info = _find_image(image_id)
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
    return _fresh_image(image_id)


def _reset_saves(image_id: str) -> str:
    info = _find_image(image_id)
    reason = _blocked_reason(info, rebuild=False)
    if reason is None and scan.image_in_use(info.path):
        reason = "jeu en cours d'utilisation"
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return reset_saves(_settings, info.path).name
    except (ManagerError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# Analyses récentes : changer d'exécutable ne relit pas tout le dossier
_analyses: dict[str, tuple[float, FolderAnalysis]] = {}


_ANALYSES_MAX = 64


def _saves_machine(machine_id: str) -> dict:
    try:
        return clients_svc.find_machine(_settings, machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
