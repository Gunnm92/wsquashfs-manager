"""Réglages (/api/config*) : configuration, client d'essai, mode, plage de nuit,
dossiers de jeux, SteamGridDB, intervalle de synchro."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import deps, machines
from ..config import (
    in_night,
    save_auto_push_interval,
    save_game_host,
    save_machine_url,
    save_mode,
    save_night,
    save_roms_dirs,
    save_sgdb,
    saves_machines,
)
from ..services import saves as saves_svc
from ..services.sgdb import tester_cle
from ..services.trial import agent_cert_path, fingerprint_pem, host_name

router = APIRouter()


@router.get("/api/config")
def get_config():
    return {
        "mode": deps._settings.mode,                                  # autonome : rien de connecté ; connecte : le reste
        "machine_listening": machines._machine_server is not None,        # écoute HTTPS des clients active
        "roms_dirs": deps._settings.roms_dirs,
        "saves_dir": str(deps._settings.saves_dir),
        "tmp_dir": str(deps._settings.tmp_dir) if deps._settings.tmp_dir else "à côté de chaque image",
        "roms_roots": deps._settings.roms_roots,
        "night_start": deps._settings.night_start,
        "night_end": deps._settings.night_end,
        "night_now": in_night(deps._settings),
        "concurrency": deps._settings.task_concurrency,
        "old_retention_days": deps._settings.old_retention_days,
        "rebuild_mode": deps._settings.rebuild_mode,
        "tool_user": deps._settings.tool_user,
        # Client d'essai (lancement d'essai, --doctor, runners installés)
        "game_host": deps._settings.game_host,
        "game_host_name": _trial_name(),
        "session_process": deps._settings.session_process,
        "agent_url": deps._settings.agent_url,
        "agent_fingerprint": _agent_fingerprint(),
        "game_host_machine": saves_svc.game_host_machine(deps._settings),   # client associé choisi, "" sinon
        # Manager vu par les clients (synchro automatique) : adresse réglée, port d'écoute, empreinte
        "machine_url": deps._settings.machine_url,
        "machine_port": deps._settings.machine_port,
        "machine_fingerprint": saves_svc.manager_fingerprint(deps._settings, create=False),
        "auto_push_interval": deps._settings.auto_push_interval,     # mise à jour automatique des clients en retard
        # Icônes et jaquettes SteamGridDB : la clé n'est jamais renvoyée, seulement son existence
        "sgdb_configured": bool(deps._settings.sgdb_api_key),
        "sgdb_authors": deps._settings.sgdb_authors,
        "sgdb_round_threshold": deps._settings.sgdb_round_threshold,
    }


def _trial_name() -> str:
    """Nom du client d'essai (celui de l'association), sinon sa description technique."""
    machine_id = saves_svc.game_host_machine(deps._settings)
    name = next((m["name"] for m in saves_machines(deps._settings) if m["id"] == machine_id), "")
    return name or host_name(deps._settings)


def _agent_fingerprint() -> str:
    cert = agent_cert_path(deps._settings)
    try:
        return fingerprint_pem(cert.read_text()) if cert.exists() else ""
    except (OSError, ValueError):
        return ""


class GameHostConfig(BaseModel):
    game_host: Literal["off", "local", "agent"]
    session_process: str = ""


@router.put("/api/config/game_host")
def set_game_host(body: GameHostConfig):
    """Client d'essai : désactivé, ce serveur, ou le client associé déjà choisi (agent).
    Le mode agent ne associe rien : l'adresse et le jeton viennent du client choisi
    (PUT /api/config/game_host/machine), refusé tant qu'aucune machine n'est associée."""
    values = {"game_host": body.game_host, "session_process": body.session_process}
    try:
        save_game_host(deps._settings, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    deps._doctor_cache.clear()           # l'état du lanceur est celui de l'ancienne machine
    return get_config()


class GameHostMachine(BaseModel):
    id: str = Field(min_length=1, max_length=64)


@router.put("/api/config/game_host/machine")
def set_game_host_machine(body: GameHostMachine):
    """Client d'essai = un client déjà associé (onglet Sauvegardes) : son adresse, son
    jeton et son certificat sont repris, sans nouveau code d'association."""
    try:
        saves_svc.use_as_game_host(deps._settings, body.id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {body.id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    deps._doctor_cache.clear()           # l'état du lanceur est celui de l'ancienne machine
    return get_config()


class MachineUrl(BaseModel):
    url: str = Field(default="", max_length=200)    # https://hôte:port ; "" : aucune


@router.put("/api/config/machine_url")
def set_machine_url(body: MachineUrl):
    """Adresse du manager vue par les clients : déclarée à chaque client associé (agent v9)."""
    try:
        save_machine_url(deps._settings, body.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Les clients déjà déclarés gardent l'ancienne adresse : redéclaration, en arrière-plan (une
    # machine éteinte ne bloque pas l'enregistrement ; elle sera déclarée à son prochain statut).
    threading.Thread(target=saves_svc.redeclare_manager, args=(deps._settings,), daemon=True).start()
    return get_config()


class SgdbConfig(BaseModel):
    api_key: str | None = Field(default=None, max_length=200)   # absent/null : clé inchangée ; "" : effacée
    authors: list[str] = Field(default_factory=list, max_length=20)
    threshold: float = Field(ge=0, le=1)


@router.put("/api/config/sgdb")
def set_sgdb(body: SgdbConfig):
    """Clé SteamGridDB, auteurs préférés et seuil de rondeur des icônes."""
    try:
        save_sgdb(deps._settings, body.api_key, body.authors, body.threshold)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config()


class SgdbTest(BaseModel):
    api_key: str | None = Field(default=None, max_length=200)  # clé saisie ; absente : clé enregistrée


@router.post("/api/config/sgdb/test")
def test_sgdb(body: SgdbTest | None = None):
    """Essai de la clé saisie (sinon de la clé enregistrée) par une recherche de « Portal » :
    rien n'est mis en cache ni enregistré."""
    ok, message = tester_cle(deps._settings, body.api_key if body else None)
    return {"ok": ok, "message": message}


class AutoPushInterval(BaseModel):
    seconds: int = Field(ge=0, le=86400)        # 0 : désactivée


@router.put("/api/config/auto_push")
def set_auto_push(body: AutoPushInterval):
    """Intervalle de la mise à jour automatique des clients en retard (0 : désactivée)."""
    try:
        save_auto_push_interval(deps._settings, body.seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config()


@router.get("/api/config/systems")
def config_systems():
    """Systèmes des dossiers de base (WSQUASHFS_MGR_ROMS_DIRS, ex. /roms) avec
    leur nombre d'images et de dossiers de jeux, pour « Réglages »."""
    out = []
    for root in deps._settings.roms_roots or deps._settings.roms_dirs:
        base = Path(root).expanduser()
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for d in entries:
            hidden_or_game = d.name.startswith(".") or d.name.lower().endswith((".pc", ".wine"))
            if not d.is_dir() or hidden_or_game:
                continue
            try:
                names = [e.name.lower() for e in d.iterdir()]
            except OSError:
                continue
            images = sum(n.endswith(".wsquashfs") for n in names)
            folders = sum(n.endswith((".pc", ".wine")) for n in names)
            if images or folders:
                out.append({"path": str(d), "name": d.name, "root": str(base),
                            "images": images, "folders": folders})
    return out


class ModeConfig(BaseModel):
    mode: Literal["autonome", "connecte"]


@router.put("/api/config/mode")
def set_mode(body: ModeConfig):
    """Mode du manager. Passer en connecté démarre l'écoute des clients, passer en autonome
    l'arrête (sans redémarrer le manager)."""
    try:
        save_mode(deps._settings, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with machines._machine_lock:
        machines._sync_machine_listener()
    return get_config()


class NightWindow(BaseModel):
    start: str
    end: str


@router.put("/api/config/night")
def set_night(body: NightWindow):
    """Plage horaire des tâches programmées « cette nuit »."""
    try:
        save_night(deps._settings, body.start, body.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config()


class RomsDirs(BaseModel):
    roms_dirs: list[str]


@router.put("/api/config/roms_dirs")
def set_roms_dirs(body: RomsDirs):
    dirs = [d.strip() for d in body.roms_dirs if d.strip()]
    missing = [d for d in dirs if not Path(d).expanduser().is_dir()]
    if missing:
        raise HTTPException(status_code=422, detail=f"dossier(s) introuvable(s) : {missing}")
    save_roms_dirs(deps._settings, dirs)
    deps._refresh_library(force=False)
    return get_config()
