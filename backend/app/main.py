"""wsquashfs-manager — application FastAPI (SPEC § 5).

Étape 1 « Socle » : service, config, scan + cache, bibliothèque (API JSON).
Le frontend (HTMX) arrive dans backend/app/../frontend.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import ImageInfo, Task, TaskStatus
from .services import scan
from .services.autorun import Autorun

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"

app = FastAPI(title="wsquashfs-manager", version="0.1.0")
_settings: Settings = get_settings()

# File de tâches (état en mémoire pour l'instant ; SPEC § 4 : sur disque plus tard)
_tasks: dict[str, Task] = {}


# ------------------------------------------------------------------- auth

def _same(a: str | bytes, b: str) -> bool:
    """Comparaison à temps constant (compare_digest refuse les str non ASCII)."""
    return secrets.compare_digest(a if isinstance(a, bytes) else a.encode(), b.encode())


@app.middleware("http")
async def simple_auth(request: Request, call_next):
    if not _settings.password:
        return await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/login"):
        return await call_next(request)
    # Starlette décode les en-têtes en latin-1 : on retrouve les octets reçus
    # (mot de passe UTF-8 accentué)
    token = request.headers.get("x-wsfs-token", "").encode("latin-1")
    # Une HTTPException levée dans un middleware donnerait une erreur 500
    if not _same(token, _settings.password):
        return JSONResponse({"detail": "mot de passe invalide"}, status_code=401)
    return await call_next(request)


class LoginReq(BaseModel):
    password: str


@app.post("/login")
def login(req: LoginReq):
    if not _settings.password:
        return {"token": ""}
    if not _same(req.password, _settings.password):
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


# Les images sont désignées par leur identifiant « <système>/<nom> » en
# paramètre (?id=) : deux systèmes peuvent avoir une image du même nom, et
# l'identifiant contient un « / ».

@app.get("/api/image")
def image_detail(id: str):
    return _find_image(id)


# ------------------------------------------------------------- autorun

@app.get("/api/image/autorun")
def autorun_raw(id: str):
    """Lecture brute de l'autorun (sans monter : unsquashfs -cat)."""
    info = _find_image(id)
    text = scan._read_autorun_from_image(_settings, info.path)
    if text is None:
        return {"found": False, "text": "", "lines": [], "eol": None,
                "items": [], "unknown_keys": []}
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
    eol: Literal["\r\n", "\n"] = "\r\n"


@app.post("/api/image/autorun")
def autorun_edit(id: str, body: AutorunEdit):
    """Écriture de l'autorun → crée une tâche de reconstruction (aperçu requis
    avant, SPEC § 2.4 : à implémenter avec la file)."""
    info = _find_image(id)
    a = Autorun(lines=body.lines, eol=body.eol)
    issues = a.validate()
    task_id = _new_task("edit_autorun", info.id)
    # TODO: lancer la reconstruction via la file (SPEC § 3.1)
    return {"task": task_id, "issues": issues}


def _find_image(image_id: str) -> ImageInfo:
    # scan_all ne relit que les images modifiées (cache) ; un index en
    # mémoire viendra avec la surveillance des dossiers.
    for info in scan.scan_all(_settings):
        if info.id == image_id:
            return info
    raise HTTPException(status_code=404, detail=f"image inconnue : {image_id}")


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
