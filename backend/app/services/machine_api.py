"""API des clients pour la synchro automatique des sauvegardes (services/saves.py).

Le manager écoute en HTTPS sur machine_port (main.py) et ne sert que ce routeur, jamais
l'interface. Il parle le même protocole que l'agent de wsquashfs-launcher (routes
/v1/saves/*), pour que le lanceur le traite comme un pair de --sync.

Chaque requête porte :
- `Authorization: Bearer <jeton>` : jeton donné au client par declare_manager ; seule
  son empreinte SHA-256 est gardée (config.json). Le jeton identifie le client ;
- `X-Wsq-Machine` : identifiant de synchro du lanceur, contrôlé seulement.

Routes : GET /v1/info, GET /v1/saves/list, GET et PUT /v1/saves/tar?game=,
POST /v1/saves/state. Les états par machine sont dans meta.json de la réserve du jeu.
Après un envoi réussi, les autres clients en retard (synchro auto) reçoivent la version du
manager (saves.sync_auto, en arrière-plan).
"""

import hashlib
import hmac
import os
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import Settings, saves_machines
from . import saves as saves_svc

MAX_ARCHIVE = 2 << 30               # 2 Gio, comme les envois vers l'agent
GZIP_MAGIC = b"\x1f\x8b"
_MACHINE_HEADER = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FINGERPRINT = re.compile(r"[0-9A-Fa-f]{64}")


def _background(target, *args, **kwargs) -> None:
    """Lance `target` dans un thread démoniaque : la réponse au client n'attend pas."""
    threading.Thread(target=target, args=args, kwargs=kwargs, name="synchro-auto", daemon=True).start()


class StateBody(BaseModel):
    game: str = Field(max_length=200)
    fingerprint: str = Field(min_length=1, max_length=128)


def _check_game(game: str) -> None:
    if not saves_svc.valid_game(game):
        raise HTTPException(status_code=400, detail="nom de jeu invalide")


def build_router(settings_of: Callable[[], Settings]) -> APIRouter:
    """Routeur de l'API des clients ; `settings_of` donne les réglages à chaque requête."""
    router = APIRouter()

    def caller(request: Request) -> dict:
        """Machine appelante : jeton reconnu (401), puis en-tête X-Wsq-Machine (400)."""
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found = None
        for machine in saves_machines(settings_of()):
            stored = machine.get("manager_token_sha256")
            if token and isinstance(stored, str) and hmac.compare_digest(stored, digest):
                found = machine
        if found is None:
            raise HTTPException(status_code=401, detail="jeton refusé")
        if not _MACHINE_HEADER.fullmatch(request.headers.get("x-wsq-machine", "")):
            raise HTTPException(status_code=400, detail="en-tête X-Wsq-Machine absent ou invalide")
        return found

    Machine = Annotated[dict, Depends(caller)]

    @router.get("/v1/info")
    def info(machine: Machine):
        return {"host": socket.gethostname(), "version": "manager"}

    @router.get("/v1/saves/list")
    def saves_list(machine: Machine):
        settings = settings_of()
        games = []
        for name in saves_svc.reserve_games(settings):
            meta = saves_svc.reserve_meta(settings, name) or {}
            games.append({"name": name, "fingerprint": str(meta.get("fingerprint") or ""),
                          "newest": int(meta.get("newest") or 0), "size": int(meta.get("size") or 0),
                          "state": saves_svc.machine_state(settings, name, machine["id"]),
                          **({"dir": meta["dir"]} if meta.get("dir") else {})})
        return {"games": games}

    @router.get("/v1/saves/tar")
    def saves_tar(machine: Machine, game: str = ""):
        settings = settings_of()
        _check_game(game)
        current = saves_svc.reserve_dir(settings, game) / saves_svc.CURRENT
        meta = saves_svc.reserve_meta(settings, game) or {}
        if not current.is_file() or not meta.get("fingerprint"):
            raise HTTPException(status_code=404, detail="aucune sauvegarde")
        headers = {"X-Wsq-Fingerprint": str(meta["fingerprint"])}
        if meta.get("profile"):
            headers["X-Wsq-Profile"] = str(meta["profile"])
        return FileResponse(current, media_type="application/gzip", headers=headers)

    def _header_int(request: Request, name: str, default: int) -> int:
        """Entier positif d'un en-tête (taille, date en secondes), sinon `default`."""
        value = request.headers.get(name, "")
        return int(value) if value.isdigit() and len(value) <= 18 else default

    @router.put("/v1/saves/tar")
    async def saves_put(request: Request, machine: Machine, game: str = ""):
        settings = settings_of()
        _check_game(game)
        fingerprint = request.headers.get("x-wsq-fingerprint", "")
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise HTTPException(status_code=400, detail="empreinte absente ou invalide (X-Wsq-Fingerprint)")
        profile = request.headers.get("x-wsq-profile") or None
        if profile is not None:
            try:
                profile.encode("ascii")
            except UnicodeEncodeError:
                raise HTTPException(status_code=400, detail="profil non ASCII") from None
        content = request.headers.get("x-wsq-content", "")      # empreinte de contenu (agent version 10)
        if content and not _FINGERPRINT.fullmatch(content):
            raise HTTPException(status_code=400, detail="empreinte de contenu invalide (X-Wsq-Content)")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_ARCHIVE:
            raise HTTPException(status_code=413, detail="archive trop volumineuse")
        folder = saves_svc.reserve_dir(settings, game)
        folder.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=folder, prefix="upload-", suffix=".part")
        part = Path(name)
        size, head = 0, b""
        behind = False
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    head = (head + chunk)[:2]
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise HTTPException(status_code=413, detail="archive trop volumineuse")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if size == 0 or head != GZIP_MAGIC:
                raise HTTPException(status_code=400, detail="archive refusée : pas un tar.gz")
            # Taille des fichiers de sauvegarde et date du plus récent, envoyées par le lanceur (comme
            # les donne la liste d'un agent : elles départagent les conflits) ; à défaut, l'archive
            # et l'heure de réception.
            files_size = _header_int(request, "x-wsq-size", size)
            dir_path = unquote(request.headers.get("x-wsq-dir", "")) or None
            newest = _header_int(request, "x-wsq-newest", int(time.time()))
            with saves_svc.STORE_LOCK:      # un envoi à la fois dans la réserve (pas d'entrelacement)
                meta = saves_svc.reserve_meta(settings, game) or {}
                current = saves_svc.current_fingerprint(settings, game)
                action = saves_svc.decide(
                    saves_svc.machine_state(settings, game, machine["id"]), fingerprint, current,
                    client_newest=newest, manager_newest=int(meta.get("newest") or 0),
                    client_content=content, manager_content=saves_svc.current_content(settings, game))
                if action == "conflit":     # les deux ont changé : la copie est rangée à part, rien n'est remplacé
                    stored = saves_svc.store_conflict(
                        settings, game, part, machine_id=machine["id"], fingerprint=fingerprint,
                        newest=newest, size=files_size, source=machine["name"], content=content,
                        profile=profile, dir_path=dir_path)
                    return {"fingerprint": fingerprint, "archived": "", "conflict": True,
                            "version": stored["name"]}
                if action == "à jour":      # même contenu : rien n'est écrit, seule la base est notée
                    saves_svc.record_state(settings, game, machine["id"], current)
                    return {"fingerprint": current, "archived": "", "same": True}
                if action == "descendre":   # copie plus ancienne que celle du manager : rien n'est remplacé
                    behind = True
                else:
                    _, archived = saves_svc.store_archived(settings, game, part, fingerprint=fingerprint,
                                                           newest=newest, size=files_size, profile=profile,
                                                           source=machine["name"], dir_path=dir_path,
                                                           content=content)
                    saves_svc.record_state(settings, game, machine["id"], fingerprint)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"écriture dans le manager impossible : {exc}") from exc
        finally:
            part.unlink(missing_ok=True)
        if behind:                  # ce client reçoit la version du manager (pas d'exclusion)
            if settings.auto_push_interval:
                _background(saves_svc.sync_auto, settings, [game])
            return {"fingerprint": current, "archived": "", "behind": True}
        # Les autres clients en retard (synchro auto) reçoivent cette version, sans retarder la réponse
        if settings.auto_push_interval:
            _background(saves_svc.sync_auto, settings, [game], exclude={machine["id"]})
        return {"fingerprint": fingerprint, "archived": archived}

    @router.post("/v1/saves/state")
    def saves_state(machine: Machine, body: StateBody):
        settings = settings_of()
        _check_game(body.game)
        current = saves_svc.current_fingerprint(settings, body.game)
        if not current or current.lower() != body.fingerprint.lower():
            raise HTTPException(status_code=409, detail="la version du manager a changé entre-temps : "
                                                        "relancer la synchronisation")
        saves_svc.record_state(settings, body.game, machine["id"], body.fingerprint)
        return {"fingerprint": body.fingerprint}

    return router
