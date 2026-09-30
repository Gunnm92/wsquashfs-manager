"""wsquashfs-manager — application FastAPI (SPEC § 5).

Étape 1 « Socle » : service, config, scan + cache, bibliothèque (API JSON).
Le frontend (HTMX) arrive dans backend/app/../frontend.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import ImageInfo, Task, TaskStatus
from .services import scan

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"

app = FastAPI(title="wsquashfs-manager", version="0.1.0")
_settings: Settings = get_settings()

# File de tâches (état en mémoire pour l'instant ; SPEC § 4 : sur disque plus tard)
_tasks: dict[str, Task] = {}


# ------------------------------------------------------------------- auth

@app.middleware("http")
async def simple_auth(request: Request, call_next):
    if not _settings.password:
        return await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/login"):
        return await call_next(request)
    token = request.headers.get("x-wsfs-token", "")
    if not secrets.compare_digest(token, _settings.password):
        raise HTTPException(status_code=401, detail="mot de passe invalide")
    return await call_next(request)


class LoginReq(BaseModel):
    password: str


@app.post("/login")
def login(req: LoginReq):
    if not _settings.password:
        return {"token": ""}
    if not secrets.compare_digest(req.password, _settings.password):
        raise HTTPException(status_code=401)
    return {"token": req.password}


# ----------------------------------------------------------------- config

@app.get("/api/config")
def get_config():
    return {
        "roms_dirs": _settings.roms_dirs,
        "saves_dir": str(_settings.saves_dir),
        "tmp_dir": str(_settings.tmp_dir),
        "concurrency": _settings.task_concurrency,
        "old_retention_days": _settings.old_retention_days,
        "tool_user": _settings.tool_user,
    }


# ------------------------------------------------------------- bibliothèque

@app.get("/api/library", response_model=list[ImageInfo])
def library(refresh: bool = False):
    """Ludothèque : toutes les images des dossiers roms/, avec cache."""
    return scan.scan_all(_settings, force=refresh)


@app.get("/api/images/{name}")
def image_detail(name: str):
    results = scan.scan_all(_settings)
    for info in results:
        if info.name == name:
            return info
    raise HTTPException(status_code=404, detail=f"image inconnue : {name}")


# ------------------------------------------------------------- autorun

@app.get("/api/images/{name}/autorun")
def autorun_raw(name: str):
    """Lecture brute de l'autorun (sans monter : unsquashfs -cat)."""
    info = _find_image(name)
    from .services.autorun import Autorun
    text = scan._read_autorun_from_image(_settings, info.path)
    if text is None:
        return {"found": False, "text": "", "lines": [], "eol": None, "issues": []}
    a = Autorun.parse(text)
    return {
        "found": True,
        "text": a.render(),
        "lines": a.lines,
        "eol": a.eol,
        "items": [{"key": k, "value": v, "line": i} for k, v, i in a.items()],
        "unknown_keys": a.unknown_keys(),
    }


class AutorunEdit(BaseModel):
    lines: list[str]
    eol: str = "\r\n"


@app.post("/api/images/{name}/autorun")
def autorun_edit(name: str, body: AutorunEdit):
    """Écriture de l'autorun → crée une tâche de reconstruction (aperçu requis
    avant, SPEC § 2.4 : à implémenter avec la file)."""
    info = _find_image(name)
    from .services.autorun import Autorun
    a = Autorun.parse(Autorun(lines=body.lines, eol=body.eol).render())
    issues = a.validate()
    task_id = _new_task("edit_autorun", info.name)
    # TODO: lancer la reconstruction via la file (SPEC § 3.1)
    return {"task": task_id, "issues": issues}


def _find_image(name: str) -> ImageInfo:
    for info in scan.scan_all(_settings):
        if info.name == name:
            return info
    raise HTTPException(status_code=404, detail=f"image inconnue : {name}")


# ------------------------------------------------------------- file de tâches

@app.get("/api/tasks", response_model=list[Task])
def list_tasks():
    return sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True)


@app.post("/api/tasks")
def create_task(kind: str, image: str | None = None):
    return _new_task(kind, image)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    t = _tasks.get(task_id)
    if not t:
        raise HTTPException(status_code=404)
    if t.status == TaskStatus.RUNNING:
        raise HTTPException(status_code=409, detail="en cours (annulation = nettoyage .part)")
    t.status = TaskStatus.CANCELLED
    return t


def _new_task(kind: str, image: str | None) -> str:
    task_id = hashlib.sha1(f"{kind}:{image}:{time.time()}".encode()).hexdigest()[:12]
    _tasks[task_id] = Task(id=task_id, kind=kind, image=image, created_at=time.time())
    return task_id


# ------------------------------------------------------------- frontend

@app.get("/", response_class=FileResponse)
def index():
    f = FRONTEND_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>wsquashfs-manager</h1><p>OK (API : /api/)</p>")
    return FileResponse(f)


@app.get("/healthz")
def healthz():
    return {"ok": True, "time": time.time()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=_settings.host, port=_settings.port)
