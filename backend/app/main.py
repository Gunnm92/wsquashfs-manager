"""wsquashfs-manager — application FastAPI (SPEC § 5).


Ce module ne garde que : création de l'application, démarrage et arrêt (lifespan),
middlewares (mode autonome, mot de passe) et montage des routeurs de `app/routes/`.
L'ordre d'inclusion des routeurs suit celui des routes d'origine."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import auth, background, deps, machines
from .config import Settings
from .routes import (
    assets,
    clients,
    folders,
    game_host,
    images,
    install,
    library,
    mass,
    saves,
    tasks,
)
from .routes import (
    settings as settings_routes,
)
from .services.tasks import QueueLocked

logger = logging.getLogger("wsquashfs-manager")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        deps.queue.lock()
    except QueueLocked as exc:
        raise SystemExit(f"wsquashfs-manager déjà lancé : {exc}") from exc
    deps.queue.load()
    deps.queue.start()
    threading.Thread(target=background._purge_old_backups, daemon=True).start()
    with machines._machine_lock:
        machines._sync_machine_listener()
    auto_push = threading.Thread(target=background._auto_push_loop, name="synchro-auto-boucle", daemon=True)
    auto_push.start()
    yield
    background._background_stop.set()
    with machines._machine_lock:
        if machines._machine_server is not None:
            machines._machine_server.should_exit = True
    auto_push.join(timeout=10)
    deps.queue.stop()


app = FastAPI(title="wsquashfs-manager", version="0.2.0", lifespan=lifespan)


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@app.middleware("http")
async def autonome_guard(request: Request, call_next):
    if deps._settings.mode == "autonome" and auth._needs_connection(request.url.path):
        return JSONResponse({"detail": deps.MODE_AUTONOME_DETAIL}, status_code=409)
    return await call_next(request)


@app.middleware("http")
async def simple_auth(request: Request, call_next):
    if not deps._settings.password:
        # Sans mot de passe, une page d'un autre site peut envoyer une requête « simple »
        # (POST sans corps JSON, DELETE…) au manager. Un en-tête personnalisé ne part jamais
        # d'une autre origine sans requête préalable (CORS), que le manager n'accepte pas :
        # l'exiger sur les écritures suffit. L'interface l'envoie toujours (vide ici).
        if request.method not in SAFE_METHODS and "x-wsfs-token" not in request.headers:
            return JSONResponse({"detail": "en-tête X-Wsfs-Token requis (requête d'un autre site ?)"},
                                status_code=403)
        return await call_next(request)
    # Fichiers de l'interface (styles, script, aide, polices) : rien de privé
    if request.url.path in ("/", "/login", "/healthz", "/custom.css") or request.url.path.startswith("/assets/"):
        return await call_next(request)
    # Starlette décode les en-têtes en latin-1 : on retrouve les octets reçus.
    # Une HTTPException levée dans un middleware donnerait une erreur 500.
    token = request.headers.get("x-wsfs-token", "").encode("latin-1")
    if not auth._same(token, auth._token(deps._settings.password)):
        return JSONResponse({"detail": "mot de passe invalide"}, status_code=401)
    return await call_next(request)


# Routeurs : l'ordre d'inclusion suit celui des routes d'origine
app.include_router(settings_routes.router)
app.include_router(game_host.router)
app.include_router(library.router)
app.include_router(images.router)
app.include_router(mass.router)
app.include_router(folders.router)
app.include_router(tasks.router)
app.include_router(saves.router)
app.include_router(clients.router)
app.include_router(install.router)
app.include_router(assets.router)


def check_access(settings: Settings) -> None:
    """Écoute réseau sans mot de passe : refusée, sauf renoncement explicite
    (WSQUASHFS_MGR_ALLOW_NO_PASSWORD=1, réseau de confiance)."""
    local = settings.host in ("127.0.0.1", "localhost", "::1")
    if local or settings.password:
        return
    if not settings.allow_no_password:
        raise SystemExit("Écoute réseau sans mot de passe refusée : poser "
                         "WSQUASHFS_MGR_PASSWORD (l'application réécrit les images), "
                         "ou WSQUASHFS_MGR_ALLOW_NO_PASSWORD=1 sur un réseau de confiance.")
    logger.warning("écoute sur %s sans mot de passe : tout le réseau peut modifier les images",
                   settings.host)


def run() -> None:
    # Journaux de l'application (purge des .old, synchro, tâches…) : sans handler, seuls les
    # avertissements sortaient. Les journaux d'uvicorn ont leurs propres handlers.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    check_access(deps._settings)
    uvicorn.run(app, host=deps._settings.host, port=deps._settings.port)
