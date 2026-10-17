"""wsquashfs-manager — application FastAPI (SPEC § 5).

Étape 1 « Socle » : service, config, scan + cache, bibliothèque (API JSON).
Étape 2 « Autorun » : édition, reconstruction par overlay dans la file de
tâches, vérification, échange, ancienne image gardée en .old.
"""

from __future__ import annotations

import difflib
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import ImageInfo, ImageState, Task
from .services import rebuild, scan
from .services.autorun import KEY_HELP, Autorun
from .services.tasks import Busy, TaskQueue

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"

_settings: Settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _queue.start()          # reprend les tâches restées en attente
    yield


app = FastAPI(title="wsquashfs-manager", version="0.1.0", lifespan=lifespan)


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

def _image_files(info: ImageInfo) -> list[str]:
    """Liste des fichiers de l'image (`unsquashfs -l`), gardée tant que
    l'image ne change pas."""
    key = (str(info.path), info.size, info.mtime)
    files = _files_cache.get(key)
    if files is None:
        files = scan.list_files(_settings, info.path)
        _files_cache.clear()                # une seule image à la fois : la fiche ouverte
        _files_cache[key] = files
    return files


_files_cache: dict[tuple, list[str]] = {}


@app.get("/api/autorun/keys")
def autorun_keys():
    """Clés connues, avec aide et valeurs possibles (formulaire)."""
    return KEY_HELP


@app.get("/api/image/autorun")
def autorun_raw(id: str):
    """Lecture brute de l'autorun (sans monter : unsquashfs -cat)."""
    info = _find_image(id)
    data = scan.read_autorun_bytes(_settings, info.path)
    base = {"size": info.size, "mtime": info.mtime}
    if data is None:
        return {"found": False, "text": "", "lines": [], "eol": "\r\n", "trailing_eol": True,
                "encoding": "utf-8", "items": [], "unknown_keys": [], "issues": [],
                "base": base}
    a = Autorun.parse(data)
    return {
        "found": True,
        "text": a.render(),
        "lines": a.lines,
        "eol": a.eol,
        "trailing_eol": a.trailing_eol,
        "encoding": a.encoding,
        "items": [{"key": k, "value": v, "line": i} for k, v, i in a.items()],
        "unknown_keys": a.unknown_keys(),
        "issues": a.validate(set(_image_files(info))),
        "base": base,
    }


class AutorunEdit(BaseModel):
    lines: list[str]
    eol: Literal["\r\n", "\n"] = "\r\n"
    trailing_eol: bool = True
    encoding: Literal["utf-8", "cp1252", "latin-1"] = "utf-8"
    # Image telle qu'elle était à la lecture : refus si elle a changé depuis
    base: dict | None = None


@app.post("/api/image/autorun")
def autorun_edit(id: str, body: AutorunEdit, dry_run: bool = False):
    """Aperçu (dry_run) ou écriture de l'autorun. L'écriture crée une tâche
    de reconstruction (SPEC § 3.1) ; l'original est gardé en .old."""
    info = _find_image(id)
    new = Autorun(lines=body.lines, eol=body.eol, trailing_eol=body.trailing_eol,
                  encoding=body.encoding)
    old_data = scan.read_autorun_bytes(_settings, info.path) or b""
    old = Autorun.parse(old_data)
    new_data = new.encode()
    diff = list(difflib.unified_diff(old.lines, new.lines, "avant", "après", lineterm=""))
    issues = new.validate(set(_image_files(info)))
    if new_data != old_data and not diff:
        diff = ["(fins de ligne ou encodage seulement)"]
    out = {"changed": new_data != old_data, "diff": diff, "issues": issues,
           "blockers": _blockers(info)}
    if dry_run:
        return out
    if not out["changed"]:
        raise HTTPException(status_code=400, detail="aucun changement")
    if out["blockers"]:
        raise HTTPException(status_code=409, detail=" ; ".join(out["blockers"]))
    if body.base and (body.base.get("size"), body.base.get("mtime")) != (info.size, info.mtime):
        raise HTTPException(status_code=409, detail="l'image a changé depuis la lecture : "
                                                    "recharger la fiche")
    task = _submit("edit_autorun", info, {
        "path": str(info.path), "size": info.size, "mtime": info.mtime,
        "autorun": new_data.decode("latin-1"),     # octets bruts, transportables en JSON
    })
    return {**out, "task": task.id}


def _blockers(info: ImageInfo) -> list[str]:
    """Ce qui empêche de reconstruire l'image maintenant."""
    out = []
    if info.state == ImageState.IN_USE:
        out.append("jeu en cours (monté ou lancé)")
    if info.has_old:
        out.append("ancienne image (.old) présente : valider ou revenir à l'ancienne d'abord")
    if (t := _queue.active_for(info.id)) is not None:
        out.append(f"tâche {t.id} déjà en cours sur cette image")
    return out


def _submit(kind: str, info: ImageInfo, params: dict) -> Task:
    try:
        return _queue.submit(kind, info.id, params)
    except Busy as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _in_use(image: Path) -> bool:
    return scan.is_in_use(image.stem, scan._running_prefixes(), scan._mount_points())


def _run_edit_autorun(task: Task, ctx: rebuild.Context) -> None:
    p = task.params
    image = Path(p["path"])
    st = image.stat()
    if (st.st_size, st.st_mtime) != (p["size"], p["mtime"]):
        raise rebuild.RebuildError("l'image a changé depuis la demande")
    ctx.log(f"reconstruction de {image.name} : autorun.cmd")
    rebuild.rebuild(_settings, image,
                    rebuild.Changes(files={"autorun.cmd": p["autorun"].encode("latin-1")}),
                    ctx, in_use=lambda: _in_use(image))


@app.get("/api/image/files")
def image_files(id: str, q: str = "", limit: int = 2000):
    """Arborescence de l'image (lecture seule), avec recherche (casse ignorée)."""
    files = _image_files(_find_image(id))
    if q:
        ql = q.lower()
        files = [f for f in files if ql in f.lower()]
    return {"total": len(files), "files": files[:limit]}


# ------------------------------------------------------------- ancienne image

@app.post("/api/image/old/validate")
def old_validate(id: str):
    """La nouvelle image est bonne : l'ancienne (.old) est supprimée."""
    info = _find_image(id)
    _no_active_task(info)
    try:
        rebuild.validate_old(info.path)
    except rebuild.RebuildError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


@app.post("/api/image/old/revert")
def old_revert(id: str):
    """Revenir à l'ancienne image : elle remplace la nouvelle."""
    info = _find_image(id)
    _no_active_task(info)
    try:
        rebuild.revert_old(info.path, in_use=_in_use(info.path))
    except rebuild.RebuildError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


def _no_active_task(info: ImageInfo) -> None:
    if (t := _queue.active_for(info.id)) is not None:
        raise HTTPException(status_code=409, detail=f"tâche {t.id} en cours sur cette image")


def _find_image(image_id: str) -> ImageInfo:
    # scan_all ne relit que les images modifiées (cache) ; un index en
    # mémoire viendra avec la surveillance des dossiers.
    for info in scan.scan_all(_settings):
        if info.id == image_id:
            return info
    raise HTTPException(status_code=404, detail=f"image inconnue : {image_id}")


# ------------------------------------------------------------- file de tâches

_queue = TaskQueue(_settings.data_dir / "tasks.json", {"edit_autorun": _run_edit_autorun},
                   concurrency=_settings.task_concurrency)


@app.get("/api/tasks", response_model=list[Task])
def list_tasks():
    return _queue.list()


@app.get("/api/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    t = _queue.get(task_id)
    if not t:
        raise HTTPException(status_code=404)
    return t


@app.post("/api/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: str):
    t = _queue.cancel(task_id)
    if not t:
        raise HTTPException(status_code=404)
    return t


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
