"""Interface et fichiers statiques : page d'accueil, connexion, assets de frontend/,
feuille personnalisée, healthz."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from .. import auth, deps

router = APIRouter()


FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


class LoginReq(BaseModel):
    password: str


@router.post("/login")
def login(req: LoginReq):
    if not deps._settings.password:
        return {"token": ""}
    if not auth._same(req.password, deps._settings.password):
        raise HTTPException(status_code=401, detail="mot de passe invalide")
    return {"token": auth._token(deps._settings.password)}


@router.get("/", response_class=FileResponse)
def index():
    frontend = FRONTEND_DIR / "index.html"
    if not frontend.exists():
        return HTMLResponse("<h1>wsquashfs-manager</h1><p>OK (API : /api/)</p>")
    # Toujours revalidée : une page gardée en cache resterait sur l'ancienne
    # interface après une mise à jour
    return FileResponse(frontend, headers={"Cache-Control": "no-cache"})


ASSET_TYPES = {".css": "text/css", ".js": "text/javascript", ".html": "text/html",
               ".woff2": "font/woff2", ".png": "image/png", ".svg": "image/svg+xml"}


@router.get("/assets/{name:path}")
def asset(name: str):
    """Fichiers de frontend/assets/ : styles, script, aide, polices."""
    root = (FRONTEND_DIR / "assets").resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix not in ASSET_TYPES:
        raise HTTPException(status_code=404, detail="fichier introuvable")
    # Revalidés à chaque chargement, comme la page : jamais d'ancien script
    # avec une nouvelle page après une mise à jour
    return FileResponse(path, media_type=ASSET_TYPES[path.suffix], headers={"Cache-Control": "no-cache"})


@router.get("/custom.css")
def custom_css():
    """Feuille personnalisée : <state_dir>/custom.css (thèmes, jetons), chargée après app.css.
    Absente : réponse vide plutôt qu'un 404 dans la console du navigateur."""
    path = deps._settings.state_dir / "custom.css"
    headers = {"Cache-Control": "no-cache"}
    if not path.is_file():
        return Response("", media_type="text/css", headers=headers)
    return FileResponse(path, media_type="text/css", headers=headers)


@router.get("/healthz")
def healthz():
    return {"ok": True, "time": time.time()}
