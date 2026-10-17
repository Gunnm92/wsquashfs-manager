"""wsquashfs-manager — application FastAPI (SPEC § 5).

Étape 1 « Socle » : service, config, scan + cache, bibliothèque (API JSON).
Étape 2 « Autorun » : édition, reconstruction par overlay dans la file de
tâches, vérification, échange, ancienne image gardée en .old.
"""

from __future__ import annotations

import difflib
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import ImageInfo, ImageState, Task
from .services import mass, rebuild, scan
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
    out = _autorun_change(info, new, _image_files(info))
    new_data = out.pop("new_data")
    if dry_run:
        return out
    if not out["changed"]:
        raise HTTPException(status_code=400, detail="aucun changement")
    if out["blockers"]:
        raise HTTPException(status_code=409, detail=" ; ".join(out["blockers"]))
    if body.base and (body.base.get("size"), body.base.get("mtime")) != (info.size, info.mtime):
        raise HTTPException(status_code=409, detail="l'image a changé depuis la lecture : "
                                                    "recharger la fiche")
    return {**out, "task": _submit_autorun(info, new_data).id}


def _autorun_change(info: ImageInfo, new: Autorun, files: list[str],
                    old_data: bytes | None = None) -> dict:
    """Aperçu d'un nouvel autorun : diff, validations, blocages."""
    if old_data is None:
        old_data = scan.read_autorun_bytes(_settings, info.path) or b""
    old = Autorun.parse(old_data)
    new_data = new.encode()
    diff = list(difflib.unified_diff(old.lines, new.lines, "avant", "après", lineterm=""))
    if new_data != old_data and not diff:
        diff = ["(fins de ligne ou encodage seulement)"]
    return {"changed": new_data != old_data, "diff": diff,
            "issues": new.validate(set(files)), "blockers": _blockers(info),
            "new_data": new_data}


def _submit_autorun(info: ImageInfo, new_data: bytes) -> Task:
    return _submit("edit_autorun", info, {
        "path": str(info.path), "size": info.size, "mtime": info.mtime,
        "autorun": new_data.decode("latin-1"),     # octets bruts, transportables en JSON
    })


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


def _images(ids: list[str]) -> list[ImageInfo]:
    """Images de la sélection, dans l'ordre donné ; 404 si une est inconnue."""
    by_id = {i.id: i for i in scan.scan_all(_settings)}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise HTTPException(status_code=404, detail="image(s) inconnue(s) : " + ", ".join(unknown))
    return [by_id[i] for i in dict.fromkeys(ids)]


# ------------------------------------------------------------- actions en masse

class MassAutorun(BaseModel):
    ids: list[str]
    ops: list[mass.AutorunOp]
    # Images telles que vues dans l'aperçu ({id: {size, mtime}}) : au
    # lancement, seules celles-ci, inchangées, sont reconstruites.
    bases: dict[str, dict] = {}


@app.post("/api/mass/autorun")
def mass_autorun(body: MassAutorun, dry_run: bool = False):
    """Opérations d'autorun sur une sélection (SPEC § 2.4) : aperçu image par
    image et espace disque, puis une tâche de reconstruction par image."""
    if not body.ops:
        raise HTTPException(status_code=400, detail="aucune opération")
    results, to_build = [], []
    for info in _images(body.ids):
        r = {"id": info.id, "size": info.size}
        try:
            data = scan.read_autorun_bytes(_settings, info.path)
            new = mass.apply_ops(Autorun.parse(data or b""), body.ops, info)
            change = _autorun_change(info, new, scan.list_files(_settings, info.path),
                                     old_data=data or b"")
        except mass.MassError as e:
            results.append({**r, "status": "error", "message": str(e)})
            continue
        new_data = change.pop("new_data")
        status = ("unchanged" if not change["changed"]
                  else "blocked" if change["blockers"] else "change")
        results.append({**r, **change, "status": status,
                        "base": {"size": info.size, "mtime": info.mtime}})
        if status == "change":
            to_build.append((info, new_data))

    if dry_run:
        return {"images": results, "space": _space([i for i, _ in to_build])}

    tasks, skipped = [], []
    for info, new_data in to_build:
        base = body.bases.get(info.id)
        if not base or (base.get("size"), base.get("mtime")) != (info.size, info.mtime):
            skipped.append({"id": info.id, "reason": "absente de l'aperçu ou modifiée depuis"})
            continue
        try:
            tasks.append(_queue.submit("edit_autorun", info.id, {
                "path": str(info.path), "size": info.size, "mtime": info.mtime,
                "autorun": new_data.decode("latin-1"),
            }).id)
        except Busy as e:
            skipped.append({"id": info.id, "reason": str(e)})
    return {"tasks": tasks, "skipped": skipped,
            "images": [r for r in results if r["status"] != "change"]}


def _space(infos: list[ImageInfo]) -> list[dict]:
    """Espace nécessaire par système de fichiers : chaque image reconstruite
    garde son .old jusqu'à validation, il faut donc la place de toutes."""
    by_dev: dict[int, dict] = {}
    for info in infos:
        dev = info.path.parent.stat().st_dev
        d = by_dev.setdefault(dev, {"path": str(info.path.parent), "needed": 0, "images": 0,
                                    "free": shutil.disk_usage(info.path.parent).free})
        d["needed"] += info.size + rebuild._SPACE_MARGIN
        d["images"] += 1
    for d in by_dev.values():
        d["ok"] = d["free"] >= d["needed"]
    return list(by_dev.values())


class MassIds(BaseModel):
    ids: list[str]


@app.post("/api/mass/old/{action}")
def mass_old(action: Literal["validate", "revert"], body: MassIds):
    """Valider ou revenir à l'ancienne image, pour chaque image de la sélection."""
    out = []
    for info in _images(body.ids):
        try:
            if (t := _queue.active_for(info.id)) is not None:
                raise rebuild.RebuildError(f"tâche {t.id} en cours")
            if action == "validate":
                rebuild.validate_old(info.path)
            else:
                rebuild.revert_old(info.path, in_use=_in_use(info.path))
            out.append({"id": info.id, "ok": True})
        except rebuild.RebuildError as e:
            out.append({"id": info.id, "ok": False, "message": str(e)})
    return out


@app.post("/api/mass/saves/reset")
def mass_reset_saves(body: MassIds, dry_run: bool = False):
    """Réinitialiser les sauvegardes : `saves/<jeu>` renommé en
    `.avant-<date>`, rien n'est supprimé."""
    out = []
    for info in _images(body.ids):
        r = {"id": info.id, "saves_size": info.saves_size}
        if not info.saves_present:
            out.append({**r, "ok": False, "message": "pas de sauvegardes"})
        elif _in_use(info.path):
            out.append({**r, "ok": False, "message": "jeu en cours (monté ou lancé)"})
        elif dry_run:
            out.append({**r, "ok": True})
        else:
            dest = mass.reset_saves(_settings.saves_dir, info)
            out.append({**r, "ok": True, "message": f"renommées en {dest.name}" if dest else ""})
    return out


@app.post("/api/image/saves/reset")
def reset_saves(id: str):
    r = mass_reset_saves(MassIds(ids=[id]))[0]
    if not r["ok"]:
        raise HTTPException(status_code=409, detail=r["message"])
    return r


@app.post("/api/mass/verify")
def mass_verify(body: MassIds):
    """Vérifier l'intégrité de la sélection (table de l'image, autorun lisible),
    en tâche de fond."""
    infos = _images(body.ids)
    t = _queue.submit("verify", None, {"images": [[i.id, str(i.path)] for i in infos]})
    return {"task": t.id}


def _run_verify(task: Task, ctx: rebuild.Context) -> None:
    images = task.params["images"]
    bad = 0
    for n, (image_id, path) in enumerate(images):
        if ctx.cancel.is_set():
            raise rebuild.Cancelled("annulé")
        problems = verify_image(Path(path))
        if problems:
            bad += 1
            ctx.log(f"✗ {image_id} : " + " ; ".join(problems))
        else:
            ctx.log(f"✓ {image_id}")
        ctx.progress((n + 1) / len(images))
    if bad:
        raise rebuild.RebuildError(f"{bad} image(s) sur {len(images)} en défaut")


def verify_image(image: Path) -> list[str]:
    """Problèmes d'intégrité d'une image (vide si elle est saine)."""
    if not image.exists():
        return ["fichier absent"]
    try:
        rebuild._run([_settings.unsquashfs, "-s", str(image)], "table illisible")
    except rebuild.RebuildError as e:
        return [str(e)]
    if not scan.list_files(_settings, image):
        return ["liste des fichiers illisible"]
    data = scan.unsquashfs_cat(_settings, image, "autorun.cmd")
    if data is None:
        return ["autorun.cmd absent ou illisible"]
    a = Autorun.parse(data)
    if a.get("CMD") is None:
        return ["autorun sans CMD="]
    return []


# ------------------------------------------------------------- file de tâches

_queue = TaskQueue(_settings.data_dir / "tasks.json",
                   {"edit_autorun": _run_edit_autorun, "verify": _run_verify},
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
