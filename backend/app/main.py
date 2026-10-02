"""wsquashfs-manager — application FastAPI (SPEC § 5)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Settings, get_settings, save_roms_dirs
from .models import ImageInfo, ImageState, Task
from .services import scan
from .services.analyze import FolderAnalysis, analyze_folder, analyze_image, report
from .services.autorun import KEY_HELP, KEY_NAME_RE, Autorun, decode_autorun
from .services.operations import (
    autorun_sha,
    find_folders,
    folder_stem,
    pack_target,
    plan_autorun,
    plan_files,
    remove_tree,
    reset_saves,
    run_autorun,
    run_files,
    run_pack,
    run_verify,
)
from .services.rebuild import (
    RebuildError,
    backup_path,
    existing_parent,
    purge_backups,
    restore_backup,
    validate_backup,
)
from .services.tasks import QueueLocked, TaskConflict, TaskQueue

logger = logging.getLogger("wsquashfs-manager")

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "rules.yaml"

_settings: Settings = get_settings()
queue = TaskQueue(
    _settings.state_dir / "tasks.json",
    handlers={
        "autorun": lambda task, job: run_autorun(_settings, task, job),
        "files": lambda task, job: run_files(_settings, task, job),
        "verify": lambda task, job: run_verify(_settings, task, job),
        "pack": lambda task, job: run_pack(_settings, task, job),
    },
    concurrency=_settings.task_concurrency,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        queue.lock()
    except QueueLocked as exc:
        raise SystemExit(f"wsquashfs-manager déjà lancé : {exc}") from exc
    queue.load()
    queue.start()
    threading.Thread(target=_purge_old_backups, daemon=True).start()
    yield
    queue.stop()


app = FastAPI(title="wsquashfs-manager", version="0.2.0", lifespan=lifespan)


# ------------------------------------------------------------------- auth

def _token(password: str) -> str:
    """Jeton dérivé du mot de passe : le mot de passe lui-même ne circule
    qu'à la connexion, pas dans chaque requête."""
    return hmac.new(password.encode(), b"wsquashfs-manager", hashlib.sha256).hexdigest()


def _same(a: str | bytes, b: str) -> bool:
    """Comparaison à temps constant (compare_digest refuse les str non ASCII)."""
    return hmac.compare_digest(a if isinstance(a, bytes) else a.encode(), b.encode())


@app.middleware("http")
async def simple_auth(request: Request, call_next):
    if not _settings.password:
        return await call_next(request)
    if request.url.path in ("/", "/login", "/healthz"):
        return await call_next(request)
    # Starlette décode les en-têtes en latin-1 : on retrouve les octets reçus.
    # Une HTTPException levée dans un middleware donnerait une erreur 500.
    token = request.headers.get("x-wsfs-token", "").encode("latin-1")
    if not _same(token, _token(_settings.password)):
        return JSONResponse({"detail": "mot de passe invalide"}, status_code=401)
    return await call_next(request)


class LoginReq(BaseModel):
    password: str


@app.post("/login")
def login(req: LoginReq):
    if not _settings.password:
        return {"token": ""}
    if not _same(req.password, _settings.password):
        raise HTTPException(status_code=401, detail="mot de passe invalide")
    return {"token": _token(_settings.password)}


# ----------------------------------------------------------------- config

@app.get("/api/config")
def get_config():
    return {
        "roms_dirs": _settings.roms_dirs,
        "saves_dir": str(_settings.saves_dir),
        "tmp_dir": str(_settings.tmp_dir),
        "concurrency": _settings.task_concurrency,
        "old_retention_days": _settings.old_retention_days,
        "rebuild_mode": _settings.rebuild_mode,
        "tool_user": _settings.tool_user,
    }


class RomsDirs(BaseModel):
    roms_dirs: list[str]


@app.put("/api/config/roms_dirs")
def set_roms_dirs(body: RomsDirs):
    dirs = [d.strip() for d in body.roms_dirs if d.strip()]
    missing = [d for d in dirs if not Path(d).expanduser().is_dir()]
    if missing:
        raise HTTPException(status_code=422, detail=f"dossier(s) introuvable(s) : {missing}")
    save_roms_dirs(_settings, dirs)
    _refresh_library(force=False)
    return get_config()


# ------------------------------------------------------------- bibliothèque

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


@app.get("/api/library", response_model=list[ImageInfo])
def library(refresh: bool = False):
    """Ludothèque : toutes les images des dossiers roms/, avec cache."""
    return [_with_task_state(info) for info in _refresh_library(force=refresh)]


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


@app.get("/api/image")
def image_detail(id: str):
    return _with_task_state(_find_image(id))


@app.get("/api/image/files")
def image_files(id: str, q: str = "", limit: int = 2000):
    """Arborescence de l'image (lecture seule), avec recherche (casse ignorée)."""
    files = scan._list_files(_settings, _find_image(id).path)
    needle = q.lower()
    matches = [f for f in files if needle in f.lower()] if needle else files
    return {"total": len(files), "matches": len(matches), "files": matches[:limit]}


# ------------------------------------------------------------- autorun

@app.get("/api/image/analyze")
def image_analyze(id: str, exe: str | None = None):
    """Proposition d'autorun pour une image existante (SPEC § 2.5), même
    moteur que pour les dossiers ; l'image n'est ni montée ni modifiée."""
    info = _find_image(id)
    key = f"image:{info.path}"
    cached = _analyses.get(key)
    if cached and cached[0] == info.mtime:
        fa = cached[1]
    else:
        fa = analyze_image(info.path, info.name, _settings.unsquashfs, _settings.tmp_dir)
        if len(_analyses) >= _ANALYSES_MAX:
            _analyses.pop(next(iter(_analyses)))
        _analyses[key] = (info.mtime, fa)
    return {"id": info.id, **report(fa, exe)}


@app.get("/api/autorun/keys")
def autorun_keys():
    return [{"key": key, "help": help_, "values": values}
            for key, (help_, values) in KEY_HELP.items()]


@app.get("/api/image/autorun")
def autorun_raw(id: str):
    """Lecture brute de l'autorun (sans monter : unsquashfs -cat)."""
    info = _find_image(id)
    data = scan.read_autorun_bytes(_settings, info.path)
    if data is None:
        return {"found": False, "text": "", "lines": [], "eol": "\r\n", "trailing_eol": True,
                "encoding": "utf-8", "sha": None, "items": [], "unknown_keys": []}
    text, encoding = decode_autorun(data)
    autorun = Autorun.parse(text)
    return {
        "found": True,
        "text": autorun.render(),
        "lines": autorun.lines,
        "eol": autorun.eol,
        "trailing_eol": autorun.trailing_eol,
        "encoding": encoding,
        "sha": autorun_sha(data),
        "items": [
            {"key": key, "value": value, "line": line} for key, value, line in autorun.items()
        ],
        "unknown_keys": autorun.unknown_keys(),
    }


class AutorunEdit(BaseModel):
    lines: list[str]
    eol: Literal["\r\n", "\n"] = "\r\n"
    trailing_eol: bool = True
    encoding: Literal["utf-8", "latin-1"] = "utf-8"
    base_sha: str | None = None     # empreinte lue à l'ouverture (édition concurrente)

    @field_validator("lines")
    @classmethod
    def _no_newline(cls, lines: list[str]) -> list[str]:
        if any("\n" in line or "\r" in line for line in lines):
            raise ValueError("une ligne ne peut pas contenir de saut de ligne")
        return lines

    def params(self) -> dict:
        text = Autorun(lines=self.lines, eol=self.eol, trailing_eol=self.trailing_eol).render()
        return {"text": text, "encoding": self.encoding, "base_sha": self.base_sha}


def _plan(info: ImageInfo, params: dict, validate: bool = True):
    try:
        return plan_autorun(_settings, info.path, info.name, info.system, params, validate)
    except RebuildError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/image/autorun/preview")
def autorun_preview(id: str, body: AutorunEdit):
    """Valide le contenu proposé sans écrire ni créer de tâche."""
    plan = _plan(_find_image(id), body.params())
    return {"text": plan.after_text, "changed": plan.changed, "issues": plan.issues}


@app.post("/api/image/autorun")
def autorun_edit(id: str, body: AutorunEdit):
    """Planifie la reconstruction de l'image avec le nouvel autorun."""
    info = _find_image(id)
    plan = _plan(info, body.params())
    if not plan.changed:
        raise HTTPException(status_code=409, detail="aucun changement")
    task = _submit(info, "autorun", body.params(), "autorun édité")
    return {"task": task.id, "issues": plan.issues}


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
            rebuild: bool = True) -> Task:
    reason = _blocked_reason(info, rebuild)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return queue.submit(kind, info.id, info.path, params, title)
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ------------------------------------------------------------- .old

def _old_action(image_id: str, action) -> ImageInfo:
    info = _find_image(image_id)
    if queue.active_for(info.id):
        raise HTTPException(status_code=409, detail="une tâche est en cours sur cette image")
    if info.state == ImageState.IN_USE or scan.image_in_use(info.path):
        raise HTTPException(status_code=409, detail="jeu en cours d'utilisation")
    try:
        action(info.path)
    except RebuildError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _fresh_image(image_id)


@app.post("/api/image/old/validate")
def old_validate(id: str):
    """La nouvelle image convient : l'ancienne (.old) est supprimée."""
    return _old_action(id, validate_backup)


@app.post("/api/image/old/restore")
def old_restore(id: str):
    """Revenir à l'ancienne image : la reconstruction est abandonnée."""
    return _old_action(id, restore_backup)


def _purge_old_backups() -> None:
    try:
        images = [info.path for info in _refresh_library(force=False)]
        purge_backups(images, _settings.old_retention_days)
    except (OSError, RebuildError):
        # tâche de fond : un dossier inaccessible ne bloque pas le démarrage
        logger.exception("purge des .old impossible")


# ------------------------------------------------------------- actions en masse

class AutorunOp(BaseModel):
    op: Literal["set", "remove", "replace"]
    key: str | None = None
    value: str | None = None
    template: str | None = None

    @model_validator(mode="after")
    def _check(self) -> AutorunOp:
        if self.op in ("set", "remove") and not (self.key and KEY_NAME_RE.match(self.key)):
            raise ValueError(f"nom de clé invalide : {self.key!r}")
        if self.op == "set" and (self.value is None or "\n" in self.value or "\r" in self.value):
            raise ValueError("valeur absente ou sur plusieurs lignes")
        if self.op == "replace" and not (self.template or "").strip():
            raise ValueError("modèle vide")
        return self


class MassAutorun(BaseModel):
    ids: list[str] = Field(min_length=1)
    ops: list[AutorunOp] = Field(min_length=1)

    def params(self) -> dict:
        return {"ops": [op.model_dump(exclude_none=True) for op in self.ops]}

    def title(self) -> str:
        parts = []
        for op in self.ops:
            if op.op == "set":
                parts.append(f"{op.key}={op.value}")
            elif op.op == "remove":
                parts.append(f"−{op.key}")
            else:
                parts.append("modèle")
        return ", ".join(parts)


@app.post("/api/mass/autorun/preview")
def mass_autorun_preview(body: MassAutorun):
    """Aperçu image par image, et espace disque nécessaire (chaque image
    reconstruite garde son .old jusqu'à validation)."""
    items = []
    needs: dict[str, dict] = {}
    for image_id in body.ids:
        try:
            info = _find_image(image_id)
        except HTTPException:
            items.append({"id": image_id, "error": "image inconnue"})
            continue
        try:
            plan = plan_autorun(_settings, info.path, info.name, info.system, body.params())
        except RebuildError as exc:
            items.append({"id": image_id, "name": info.name, "error": str(exc)})
            continue
        before = decode_autorun(plan.before)[0] if plan.before is not None else None
        blocked = _blocked_reason(info) if plan.changed else None
        items.append({"id": image_id, "name": info.name, "before": before,
                      "after": plan.after_text, "changed": plan.changed,
                      "issues": plan.issues, "blocked": blocked})
        if plan.changed and not blocked:
            fs = str(existing_parent(info.path.parent))
            entry = needs.setdefault(fs, {"path": fs, "needed": 0,
                                          "free": shutil.disk_usage(fs).free})
            entry["needed"] += info.size
    return {"items": items, "space": list(needs.values()),
            "ready": sum(1 for i in items if i.get("changed") and not i.get("blocked")
                         and not i.get("error"))}


@app.post("/api/mass/autorun")
def mass_autorun(body: MassAutorun):
    """Crée une tâche par image modifiée ; les autres sont listées avec la raison."""
    created, skipped = [], []
    params, title = body.params(), body.title()
    for image_id in body.ids:
        try:
            info = _find_image(image_id)
            plan = plan_autorun(_settings, info.path, info.name, info.system, params,
                                validate=False)
        except (HTTPException, RebuildError) as exc:
            skipped.append({"id": image_id, "reason": getattr(exc, "detail", str(exc))})
            continue
        if not plan.changed:
            skipped.append({"id": image_id, "reason": "aucun changement"})
            continue
        try:
            created.append(_submit(info, "autorun", params, title).id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


class MassIds(BaseModel):
    ids: list[str] = Field(min_length=1)


class FileCopy(BaseModel):
    dest: str = Field(min_length=1)       # chemin dans l'image ({exe_dir}, {name}…)
    source: str = Field(min_length=1)     # fichier sur le serveur

    @field_validator("source")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("chemin source absolu attendu (fichier sur le serveur)")
        return value


class RegValue(BaseModel):
    file: Literal["user.reg", "system.reg"] = "user.reg"
    key: str = Field(min_length=1)        # ex. Software\\Wine\\AppDefaults\\{exe_name}\\DllOverrides
    name: str = Field(min_length=1)
    value: str

    @field_validator("key", "name", "value")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("une seule ligne attendue")
        return value


class MassFiles(BaseModel):
    ids: list[str] = Field(min_length=1)
    copy_: list[FileCopy] = Field(default_factory=list, alias="copy")
    delete: list[str] = Field(default_factory=list)
    reg: list[RegValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self) -> MassFiles:
        if not (self.copy_ or self.delete or self.reg):
            raise ValueError("aucune opération")
        return self

    def params(self) -> dict:
        return {"copy": [c.model_dump() for c in self.copy_], "delete": self.delete,
                "reg": [r.model_dump() for r in self.reg]}

    def title(self) -> str:
        parts = [f"+{Path(c.dest).name}" for c in self.copy_]
        parts += [f"−{Path(d).name}" for d in self.delete]
        parts += [f"reg {r.name}={r.value}" for r in self.reg]
        return ", ".join(parts)


@app.post("/api/mass/files/preview")
def mass_files_preview(body: MassFiles):
    """Aperçu : fichiers ajoutés/remplacés/supprimés et valeurs de registre,
    image par image, avec les sauvegardes qui masqueraient un changement."""
    items, needs = [], {}
    for image_id in body.ids:
        try:
            info = _find_image(image_id)
        except HTTPException:
            items.append({"id": image_id, "error": "image inconnue"})
            continue
        try:
            plan = plan_files(_settings, info.path, info.name, info.system, body.params())
        except RebuildError as exc:
            items.append({"id": image_id, "name": info.name, "error": str(exc)})
            continue
        rebuilds = not plan.changes.is_empty()
        blocked = _blocked_reason(info, rebuilds) if plan.changed else None
        items.append({"id": image_id, "name": info.name, "changed": plan.changed,
                      "actions": plan.actions, "warnings": plan.warnings, "blocked": blocked})
        if rebuilds and not blocked:
            fs = str(existing_parent(info.path.parent))
            entry = needs.setdefault(fs, {"path": fs, "needed": 0,
                                          "free": shutil.disk_usage(fs).free})
            entry["needed"] += info.size
    return {"items": items, "space": list(needs.values()),
            "ready": sum(1 for i in items if i.get("changed") and not i.get("blocked")
                         and not i.get("error"))}


@app.post("/api/mass/files")
def mass_files(body: MassFiles):
    created, skipped = [], []
    params, title = body.params(), body.title()
    for image_id in body.ids:
        try:
            info = _find_image(image_id)
            plan = plan_files(_settings, info.path, info.name, info.system, params)
        except (HTTPException, RebuildError) as exc:
            skipped.append({"id": image_id, "reason": getattr(exc, "detail", str(exc))})
            continue
        if not plan.changed:
            skipped.append({"id": image_id, "reason": "aucun changement"})
            continue
        try:
            created.append(_submit(info, "files", params, title,
                                   rebuild=not plan.changes.is_empty()).id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


@app.post("/api/mass/verify")
def mass_verify(body: MassIds):
    """Vérification d'intégrité (lecture seule), une tâche par image."""
    created, skipped = [], []
    for image_id in body.ids:
        try:
            info = _find_image(image_id)
            created.append(_submit(info, "verify", {}, "vérification", rebuild=False).id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


def _reset_saves(image_id: str) -> str:
    info = _find_image(image_id)
    reason = _blocked_reason(info, rebuild=False)
    if reason is None and scan.image_in_use(info.path):
        reason = "jeu en cours d'utilisation"
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        return reset_saves(_settings, info.path).name
    except (RebuildError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/image/saves/reset")
def saves_reset(id: str):
    """Sauvegardes renommées en <jeu>.avant-<date> (jamais supprimées)."""
    renamed = _reset_saves(id)
    return {"renamed": renamed, "image": _fresh_image(id)}


@app.post("/api/mass/saves/reset")
def mass_saves_reset(body: MassIds):
    done, skipped = [], []
    for image_id in body.ids:
        try:
            done.append({"id": image_id, "renamed": _reset_saves(image_id)})
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"done": done, "skipped": skipped}


@app.post("/api/mass/old/validate")
def mass_old_validate(body: MassIds):
    done, skipped = [], []
    for image_id in body.ids:
        try:
            _old_action(image_id, validate_backup)
            done.append(image_id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"done": done, "skipped": skipped}


# ------------------------------------------------------------- création (dossiers → images)

def _folders() -> dict[str, dict]:
    folders = {f["id"]: f for f in find_folders(_settings)}
    for folder in folders.values():
        task = queue.active_for(folder["image_id"])
        folder["task"] = task.status.value if task else None
    return folders


def _find_folder(folder_id: str) -> dict:
    folder = _folders().get(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"dossier inconnu : {folder_id}")
    return folder


@app.get("/api/folders")
def list_folders():
    """Dossiers de jeux à empaqueter (*.pc, *.wine) des dossiers roms/."""
    return list(_folders().values())


# Analyses récentes : changer d'exécutable ne relit pas tout le dossier
_analyses: dict[str, tuple[float, FolderAnalysis]] = {}
_ANALYSES_MAX = 64


def _analysis(folder: dict, refresh: bool) -> FolderAnalysis:
    path = Path(folder["path"])
    mtime = path.stat().st_mtime
    cached = _analyses.get(folder["path"])
    if cached and cached[0] == mtime and not refresh:
        return cached[1]
    fa = analyze_folder(path, folder["name"])
    if len(_analyses) >= _ANALYSES_MAX:
        _analyses.pop(next(iter(_analyses)))
    _analyses[folder["path"]] = (mtime, fa)
    return fa


@app.get("/api/folders/analyze")
def folder_analyze(id: str, exe: str | None = None, refresh: bool = False):
    """Analyse et proposition d'autorun justifiée (SPEC § 2.5) ; `exe`
    choisit un autre candidat."""
    folder = _find_folder(id)
    result = report(_analysis(folder, refresh), exe)
    return {**folder, **result}


class PackItem(BaseModel):
    id: str
    autorun: str | None = None          # None : garder l'autorun du dossier
    encoding: Literal["utf-8", "latin-1"] = "utf-8"

    @field_validator("autorun")
    @classmethod
    def _has_cmd(cls, value: str | None) -> str | None:
        if value is None:
            return None
        autorun = Autorun.parse(value.replace("\r\n", "\n"))
        if not autorun.get("CMD"):
            raise ValueError("autorun sans CMD")
        # Fins de ligne Windows, comme les autoruns Batocera
        return Autorun(lines=autorun.lines, eol="\r\n", trailing_eol=True).render()


class PackRequest(BaseModel):
    items: list[PackItem] = Field(min_length=1)
    rename_wine: bool = True
    delete_source: bool = False


def _renamed_folder(folders: dict[str, dict], folder_id: str) -> dict | None:
    """« Jeu.pc » déjà renommé en « Jeu.wine » par un empaquetage précédent."""
    system, _, name = folder_id.rpartition("/")
    stem = folder_stem(name)
    return next((f for f in folders.values()
                 if f["name"] == stem and (f["system"] or "") == system), None)


@app.post("/api/folders/pack")
def folders_pack(body: PackRequest):
    """Une tâche d'empaquetage par dossier ; les refus sont listés avec la raison."""
    folders = _folders()
    created, skipped = [], []
    for item in body.items:
        folder = folders.get(item.id) or _renamed_folder(folders, item.id)
        if folder is None:
            skipped.append({"id": item.id, "reason": "dossier introuvable (renommé ou supprimé ?)"})
            continue
        if folder["task"]:
            skipped.append({"id": item.id, "reason": "empaquetage déjà en attente ou en cours"})
            continue
        if folder["image_exists"]:
            skipped.append({"id": item.id, "reason": f"{folder['name']}.wsquashfs existe déjà"})
            continue
        if item.autorun is None and not folder["has_autorun"]:
            skipped.append({"id": item.id, "reason": "pas d'autorun : en générer un"})
            continue
        source = Path(folder["path"])
        params = {"source": str(source), "autorun": item.autorun, "encoding": item.encoding,
                  "rename_wine": body.rename_wine, "delete_source": body.delete_source}
        title = "empaquetage" + (" + .wine" if body.rename_wine else "") + \
            (" + suppression du dossier" if body.delete_source else "")
        try:
            task = queue.submit("pack", folder["image_id"], pack_target(source), params, title)
        except TaskConflict as exc:
            skipped.append({"id": item.id, "reason": str(exc)})
            continue
        created.append(task.id)
    return {"tasks": created, "skipped": skipped}


@app.post("/api/folders/delete")
def folders_delete(body: MassIds):
    """Supprime des dossiers DÉJÀ empaquetés (l'image existe et se lit)."""
    folders = _folders()
    done, skipped = [], []
    for folder_id in body.ids:
        folder = folders.get(folder_id)
        if folder is None:
            skipped.append({"id": folder_id, "reason": "dossier inconnu"})
            continue
        image = pack_target(Path(folder["path"]))
        if not image.exists() or not scan._unsquashfs_ok(_settings, image):
            skipped.append({"id": folder_id, "reason": "image absente ou illisible : dossier gardé"})
            continue
        if folder["task"]:
            skipped.append({"id": folder_id, "reason": "tâche en cours"})
            continue
        try:
            remove_tree(Path(folder["path"]))
        except OSError as exc:
            skipped.append({"id": folder_id, "reason": f"suppression incomplète : {exc}"})
            continue
        done.append(folder_id)
    return {"done": done, "skipped": skipped}


# ------------------------------------------------------------- file de tâches

@app.get("/api/tasks", response_model=list[Task])
def list_tasks(image: str | None = None):
    return queue.list(image)


@app.get("/api/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    task = queue.get(task_id)
    if task is None:
        raise HTTPException(status_code=404)
    return task


@app.post("/api/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: str):
    try:
        return queue.cancel(task_id)
    except KeyError:
        raise HTTPException(status_code=404) from None


@app.delete("/api/tasks/finished")
def clear_finished_tasks():
    return {"removed": queue.clear_finished()}


# ------------------------------------------------------------- frontend

@app.get("/", response_class=FileResponse)
def index():
    frontend = FRONTEND_DIR / "index.html"
    if not frontend.exists():
        return HTMLResponse("<h1>wsquashfs-manager</h1><p>OK (API : /api/)</p>")
    return FileResponse(frontend)


@app.get("/healthz")
def healthz():
    return {"ok": True, "time": time.time()}


def run() -> None:
    import uvicorn

    if _settings.host not in ("127.0.0.1", "localhost", "::1") and not _settings.password:
        raise SystemExit("Écoute réseau sans mot de passe refusée : poser "
                         "WSQUASHFS_MGR_PASSWORD (l'application réécrit les images).")
    uvicorn.run(app, host=_settings.host, port=_settings.port)


if __name__ == "__main__":
    run()
