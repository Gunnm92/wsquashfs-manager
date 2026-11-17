"""Interface et fichiers statiques : page d'accueil, connexion, assets de frontend/,
feuille personnalisée, healthz."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from .. import auth, deps

router = APIRouter()


# routes/assets.py → app → backend → racine du dépôt (ou /app dans l'image Docker)
FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


class LoginReq(BaseModel):
    password: str


# Essais de mot de passe : au-delà de LOGIN_MAX_FAILURES échecs en LOGIN_WINDOW secondes depuis
# une même adresse, refus (429) jusqu'à la fin de la fenêtre ; chaque échec coûte LOGIN_DELAY s.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 900
LOGIN_DELAY = 1.0
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _recent_failures(address: str, now: float) -> list[float]:
    """Échecs encore dans la fenêtre pour `address` (appelant : sous _login_lock)."""
    recent = [t for t in _login_failures.get(address, []) if now - t < LOGIN_WINDOW]
    if recent:
        _login_failures[address] = recent
    else:
        _login_failures.pop(address, None)
    return recent


@router.post("/login")
def login(req: LoginReq, request: Request):
    if not deps._settings.password:
        return {"token": ""}
    address = request.client.host if request.client else "?"
    now = time.monotonic()
    with _login_lock:
        recent = _recent_failures(address, now)
        if len(recent) >= LOGIN_MAX_FAILURES:
            wait = int(LOGIN_WINDOW - (now - recent[0])) + 1
            raise HTTPException(status_code=429, detail=f"trop d'essais : réessayer dans {wait // 60 + 1} min")
    if not auth._same(req.password, deps._settings.password):
        with _login_lock:
            _login_failures.setdefault(address, []).append(now)
        time.sleep(LOGIN_DELAY)
        raise HTTPException(status_code=401, detail="mot de passe invalide")
    with _login_lock:
        _login_failures.pop(address, None)
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
