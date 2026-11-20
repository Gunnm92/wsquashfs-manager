"""File de tâches : liste, état, annulation, lancement immédiat, captures d'essai,
nettoyage des tâches terminées."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..deps import QueueDep, SettingsDep
from ..models import Task
from ..services.trial import trial_dir

router = APIRouter()


@router.get("/api/tasks", response_model=list[Task])
def list_tasks(queue: QueueDep, image: str | None = None):
    return queue.list(image)


@router.get("/api/tasks/{task_id}", response_model=Task)
def get_task(queue: QueueDep, task_id: str):
    task = queue.get(task_id)
    if task is None:
        raise HTTPException(status_code=404)
    return task


@router.post("/api/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(queue: QueueDep, task_id: str):
    try:
        return queue.cancel(task_id)
    except KeyError:
        raise HTTPException(status_code=404) from None


@router.post("/api/tasks/{task_id}/now", response_model=Task)
def run_task_now(queue: QueueDep, task_id: str):
    """Lève la programmation de nuit : la tâche démarre dès que possible."""
    try:
        return queue.run_now(task_id)
    except KeyError:
        raise HTTPException(status_code=404) from None


@router.get("/api/tasks/{task_id}/screenshot")
def task_screenshot(settings: SettingsDep, task_id: str):
    shot = trial_dir(settings) / f"{task_id}.png"
    if not shot.is_file():
        raise HTTPException(status_code=404, detail="pas de capture")
    return FileResponse(shot, media_type="image/png")


@router.delete("/api/tasks/finished")
def clear_finished_tasks(queue: QueueDep):
    return {"removed": queue.clear_finished()}
