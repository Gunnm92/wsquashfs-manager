"""État partagé de l'application : les réglages et la file de tâches, instanciés ici une fois
pour toutes. Le reste vit dans des modules dédiés : index de la bibliothèque
(`library_index.py`), soumission de tâches (`task_submit.py`), cache du diagnostic
(`services/trial.py`) et des analyses (`services/analyze.py`).

Les routes reçoivent cet état par des dépendances FastAPI (`SettingsDep`, `QueueDep`,
`ImageDep`), qui lisent `deps.settings` et `deps.queue` à l'appel : un remplacement dans `deps`
(les tests) est vu partout. Les services gardent leurs paramètres explicites ; les threads de
fond (`background.py`, `machines.py`) lisent `deps.settings`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException

from . import library_index
from .config import Settings, get_settings, in_night
from .models import ImageInfo
from .services import analyze as analyze_mod
from .services import clients as clients_svc
from .services import scan
from .services.install import run_install
from .services.operations import (
    run_autorun,
    run_cover,
    run_files,
    run_pack,
    run_verify,
)
from .services.runners import run_relink, run_runner
from .services.saves_actions import run_saves
from .services.tasks import TaskQueue
from .services.trial import run_trial
from .services.update import run_update
from .task_submit import connected_task

settings: Settings = get_settings()


scan.LAUNCHER_CACHE = settings.launcher_cache      # verrous et montages du lanceur


# Base de connaissances locale (ajouts faits dans l'interface), dans l'appdata
analyze_mod.LOCAL_GAMES_FILE = settings.state_dir / "games.yaml"


queue = TaskQueue(
    settings.state_dir / "tasks.json",
    handlers={
        "autorun": lambda task, job: run_autorun(settings, task, job),
        "files": lambda task, job: run_files(settings, task, job),
        "verify": lambda task, job: run_verify(settings, task, job),
        "cover": lambda task, job: run_cover(settings, task, job),
        "pack": lambda task, job: run_pack(settings, task, job),
        "update": lambda task, job: run_update(settings, task, job),
        "trial": lambda task, job: connected_task(run_trial, settings)(task, job),
        "runner": lambda task, job: connected_task(run_runner, settings)(task, job),
        "relink": lambda task, job: run_relink(settings, task, job),
        "saves": lambda task, job: connected_task(run_saves, settings)(task, job),
        "install": lambda task, job: connected_task(run_install, settings)(task, job),
    },
    concurrency=settings.task_concurrency,
    night_now=lambda: in_night(settings),
)


def current_settings() -> Settings:
    """Réglages courants, lus à l'appel : un remplacement de `deps.settings` est vu partout."""
    return settings


def current_queue() -> TaskQueue:
    """File de tâches courante, lue à l'appel (comme `current_settings`)."""
    return queue


SettingsDep = Annotated[Settings, Depends(current_settings)]
QueueDep = Annotated[TaskQueue, Depends(current_queue)]


def image_by_id(id: str, settings: SettingsDep) -> ImageInfo:
    """Image désignée par le paramètre `id` de la route ; 404 si elle est inconnue."""
    return library_index.find_image(settings, id)


ImageDep = Annotated[ImageInfo, Depends(image_by_id)]


def saves_machine(settings: Settings, machine_id: str) -> dict:
    """Client (machine de jeu) par son identifiant ; 404 s'il est inconnu."""
    try:
        return clients_svc.find_machine(settings, machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
