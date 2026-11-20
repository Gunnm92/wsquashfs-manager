"""Réglages (/api/config*) : configuration, client d'essai, mode, plage de nuit,
dossiers de jeux, SteamGridDB, intervalle de synchro."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import library_index, machines
from ..config import (
    Settings,
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
from ..deps import SettingsDep
from ..services import clients as clients_svc
from ..services import trial as trial_svc
from ..services.agent import agent_cert_path, fingerprint_pem
from ..services.sgdb import tester_cle
from ..services.trial import host_name

router = APIRouter()


@router.get("/api/config")
def get_config(settings: SettingsDep):
    return {
        "mode": settings.mode,                                  # autonome : rien de connecté ; connecte : le reste
        "machine_listening": machines.machine_server is not None,        # écoute HTTPS des clients active
        "roms_dirs": settings.roms_dirs,
        "saves_dir": str(settings.saves_dir),
        "tmp_dir": str(settings.tmp_dir) if settings.tmp_dir else "à côté de chaque image",
        "roms_roots": settings.roms_roots,
        "night_start": settings.night_start,
        "night_end": settings.night_end,
        "night_now": in_night(settings),
        "concurrency": settings.task_concurrency,
        "old_retention_days": settings.old_retention_days,
        "rebuild_mode": settings.rebuild_mode,
        "tool_user": settings.tool_user,
        # Client d'essai (lancement d'essai, --doctor, runners installés)
        "game_host": settings.game_host,
        "game_host_name": _trial_name(settings),
        "session_process": settings.session_process,
        "agent_url": settings.agent_url,
        "agent_fingerprint": _agent_fingerprint(settings),
        "game_host_machine": clients_svc.game_host_machine(settings),   # client associé choisi, "" sinon
        # Manager vu par les clients (synchro automatique) : adresse réglée, port d'écoute, empreinte
        "machine_url": settings.machine_url,
        "machine_port": settings.machine_port,
        "machine_fingerprint": clients_svc.manager_fingerprint(settings, create=False),
        "auto_push_interval": settings.auto_push_interval,     # mise à jour automatique des clients en retard
        # Icônes et jaquettes SteamGridDB : la clé n'est jamais renvoyée, seulement son existence
        "sgdb_configured": bool(settings.sgdb_api_key),
        "sgdb_authors": settings.sgdb_authors,
        "sgdb_round_threshold": settings.sgdb_round_threshold,
    }


def _trial_name(settings: Settings) -> str:
    """Nom du client d'essai (celui de l'association), sinon sa description technique."""
    machine_id = clients_svc.game_host_machine(settings)
    name = next((m["name"] for m in saves_machines(settings) if m["id"] == machine_id), "")
    return name or host_name(settings)


def _agent_fingerprint(settings: Settings) -> str:
    cert = agent_cert_path(settings)
    try:
        return fingerprint_pem(cert.read_text()) if cert.exists() else ""
    except (OSError, ValueError):
        return ""


class GameHostConfig(BaseModel):
    game_host: Literal["off", "local", "agent"]
    session_process: str = ""


@router.put("/api/config/game_host")
def set_game_host(settings: SettingsDep, body: GameHostConfig):
    """Client d'essai : désactivé, ce serveur, ou le client associé déjà choisi (agent).
    Le mode agent ne associe rien : l'adresse et le jeton viennent du client choisi
    (PUT /api/config/game_host/machine), refusé tant qu'aucune machine n'est associée."""
    values = {"game_host": body.game_host, "session_process": body.session_process}
    try:
        save_game_host(settings, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    trial_svc.doctor_cache.clear()           # l'état du lanceur est celui de l'ancienne machine
    return get_config(settings)


class GameHostMachine(BaseModel):
    id: str = Field(min_length=1, max_length=64)


@router.put("/api/config/game_host/machine")
def set_game_host_machine(settings: SettingsDep, body: GameHostMachine):
    """Client d'essai = un client déjà associé (onglet Sauvegardes) : son adresse, son
    jeton et son certificat sont repris, sans nouveau code d'association."""
    try:
        clients_svc.use_as_game_host(settings, body.id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {body.id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    trial_svc.doctor_cache.clear()           # l'état du lanceur est celui de l'ancienne machine
    return get_config(settings)


class MachineUrl(BaseModel):
    url: str = Field(default="", max_length=200)    # https://hôte:port ; "" : aucune


@router.put("/api/config/machine_url")
def set_machine_url(settings: SettingsDep, body: MachineUrl):
    """Adresse du manager vue par les clients : déclarée à chaque client associé (agent v9)."""
    try:
        save_machine_url(settings, body.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Les clients déjà déclarés gardent l'ancienne adresse : redéclaration, en arrière-plan (une
    # machine éteinte ne bloque pas l'enregistrement ; elle sera déclarée à son prochain statut).
    threading.Thread(target=clients_svc.redeclare_manager, args=(settings,), daemon=True).start()
    return get_config(settings)


class SgdbConfig(BaseModel):
    api_key: str | None = Field(default=None, max_length=200)   # absent/null : clé inchangée ; "" : effacée
    authors: list[str] = Field(default_factory=list, max_length=20)
    threshold: float = Field(ge=0, le=1)


@router.put("/api/config/sgdb")
def set_sgdb(settings: SettingsDep, body: SgdbConfig):
    """Clé SteamGridDB, auteurs préférés et seuil de rondeur des icônes."""
    try:
        save_sgdb(settings, body.api_key, body.authors, body.threshold)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config(settings)


class SgdbTest(BaseModel):
    api_key: str | None = Field(default=None, max_length=200)  # clé saisie ; absente : clé enregistrée


@router.post("/api/config/sgdb/test")
def test_sgdb(settings: SettingsDep, body: SgdbTest | None = None):
    """Essai de la clé saisie (sinon de la clé enregistrée) par une recherche de « Portal » :
    rien n'est mis en cache ni enregistré."""
    ok, message = tester_cle(settings, body.api_key if body else None)
    return {"ok": ok, "message": message}


class AutoPushInterval(BaseModel):
    seconds: int = Field(ge=0, le=86400)        # 0 : désactivée


@router.put("/api/config/auto_push")
def set_auto_push(settings: SettingsDep, body: AutoPushInterval):
    """Intervalle de la mise à jour automatique des clients en retard (0 : désactivée)."""
    try:
        save_auto_push_interval(settings, body.seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config(settings)


@router.get("/api/config/systems")
def config_systems(settings: SettingsDep):
    """Systèmes des dossiers de base (WSQUASHFS_MGR_ROMS_DIRS, ex. /roms) avec
    leur nombre d'images et de dossiers de jeux, pour « Réglages »."""
    out = []
    for root in settings.roms_roots or settings.roms_dirs:
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
def set_mode(settings: SettingsDep, body: ModeConfig):
    """Mode du manager. Passer en connecté démarre l'écoute des clients, passer en autonome
    l'arrête (sans redémarrer le manager)."""
    try:
        save_mode(settings, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with machines.machine_lock:
        machines.sync_machine_listener()
    return get_config(settings)


class NightWindow(BaseModel):
    start: str
    end: str


@router.put("/api/config/night")
def set_night(settings: SettingsDep, body: NightWindow):
    """Plage horaire des tâches programmées « cette nuit »."""
    try:
        save_night(settings, body.start, body.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_config(settings)


class RomsDirs(BaseModel):
    roms_dirs: list[str]


@router.put("/api/config/roms_dirs")
def set_roms_dirs(settings: SettingsDep, body: RomsDirs):
    dirs = [d.strip() for d in body.roms_dirs if d.strip()]
    missing = [d for d in dirs if not Path(d).expanduser().is_dir()]
    if missing:
        raise HTTPException(status_code=422, detail=f"dossier(s) introuvable(s) : {missing}")
    save_roms_dirs(settings, dirs)
    library_index.refresh_library(settings, force=False)
    return get_config(settings)
