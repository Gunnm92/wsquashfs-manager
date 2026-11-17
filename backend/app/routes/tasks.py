"""File de tâches : liste, état, annulation, lancement immédiat, captures d'essai,
nettoyage des tâches terminées."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import deps
from ..models import Task
from ..services.trial import trial_dir

router = APIRouter()


@router.get("/api/tasks", response_model=list[Task])
def list_tasks(image: str | None = None):
    return deps.queue.list(image)


@router.get("/api/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    task = deps.queue.get(task_id)
    if task is None:
        raise HTTPException(status_code=404)
    return task


@router.post("/api/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: str):
    try:
        return deps.queue.cancel(task_id)
    except KeyError:
        raise HTTPException(status_code=404) from None


@router.post("/api/tasks/{task_id}/now", response_model=Task)
def run_task_now(task_id: str):
    """Lève la programmation de nuit : la tâche démarre dès que possible."""
    try:
        return deps.queue.run_now(task_id)
    except KeyError:
        raise HTTPException(status_code=404) from None


@router.get("/api/tasks/{task_id}/screenshot")
def task_screenshot(task_id: str):
    shot = trial_dir(deps._settings) / f"{task_id}.png"
    if not shot.is_file():
        raise HTTPException(status_code=404, detail="pas de capture")
    return FileResponse(shot, media_type="image/png")


@router.delete("/api/tasks/finished")
def clear_finished_tasks():
    return {"removed": deps.queue.clear_finished()}
