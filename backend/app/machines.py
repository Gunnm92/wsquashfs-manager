"""API des clients (`machine_app`, application séparée sur le port des clients, sans

interface ni documentation) et démarrage/arrêt de son écoute HTTPS selon le mode."""

from __future__ import annotations

import logging
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import deps
from .config import Settings
from .services import clients as clients_svc
from .services import machine_api
from .services.errors import ManagerError

logger = logging.getLogger("wsquashfs-manager")


def _serve_machines(server: uvicorn.Server) -> None:
    try:
        server.run()
    except (SystemExit, OSError) as exc:        # port occupé, par exemple : l'interface reste
        logger.error("clients : écoute impossible (%s)", exc)


def _start_machine_server(settings: Settings) -> uvicorn.Server | None:
    """Écoute HTTPS des clients (machine_port, certificat du manager) dans un thread démoniaque ;
    seulement l'API des clients (machine_app), jamais l'interface. None si désactivée."""
    if settings.machine_port <= 0:
        return None
    try:
        cert, key = clients_svc.machine_tls(settings)
    except ManagerError as exc:
        logger.error("clients : %s", exc)
        return None
    # log_config et log_level à None : uvicorn configure la journalisation pour tout le processus ;
    # « warning » ou access_log=False ici couperaient aussi les journaux de l'interface
    config = uvicorn.Config(machine_app, host=settings.machine_host, port=settings.machine_port,
                            ssl_certfile=str(cert), ssl_keyfile=str(key), log_config=None,
                            log_level=None)
    server = uvicorn.Server(config)
    threading.Thread(target=_serve_machines, args=(server,), name="machines", daemon=True).start()
    logger.info("clients : écoute HTTPS sur le port %s", settings.machine_port)
    return server


# Écoute HTTPS des clients : seulement en mode connecté ; suit le passage d'un mode à l'autre
_machine_server: uvicorn.Server | None = None


_machine_lock = threading.Lock()


def _sync_machine_listener() -> None:
    """Démarre l'écoute des clients si le mode est connecté, l'arrête sinon. Appelé sous
    _machine_lock, au démarrage et à chaque changement de mode."""
    global _machine_server
    if deps._settings.mode == "connecte":
        if _machine_server is None:
            _machine_server = _start_machine_server(deps._settings)
    elif _machine_server is not None:
        _machine_server.should_exit = True
        _machine_server = None


# API des clients (synchro automatique) : application distincte, sur machine_port seulement,
# sans l'interface, sans documentation, avec son propre contrôle (jeton par machine)
machine_app = FastAPI(title="wsquashfs-manager (clients)", version="0.2.0",
                      docs_url=None, redoc_url=None, openapi_url=None)


machine_app.include_router(machine_api.build_router(lambda: deps._settings))


@machine_app.exception_handler(RequestValidationError)
def _unreadable_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Corps invalide : 400 {"detail": "requête illisible"}, comme l'agent du lanceur (pas la liste
    d'erreurs de validation de FastAPI), pour que le client Go lise le même format partout."""
    return JSONResponse(status_code=400, content={"detail": "requête illisible"})
