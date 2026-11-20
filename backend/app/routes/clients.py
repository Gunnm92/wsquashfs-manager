"""Clients associés (sauvegardes) : association, icône, dossiers synchronisés."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import Settings
from ..deps import SettingsDep
from ..services import activity
from ..services import clients as clients_svc
from ..services import trial as trial_svc
from ..services.agent import AgentProbeError, AgentUnreachable
from ..services.errors import ManagerError

router = APIRouter()


class SavesMachineAdd(BaseModel):
    code: str = Field(min_length=1, max_length=600)     # code wsq:// de « wsquashfs-launcher --pair »


@router.get("/api/saves/machines")
def saves_machines_list(settings: SettingsDep):
    """Machines de la synchronisation (jeton et certificat jamais renvoyés)."""
    return {"machines": clients_svc.machine_rows(settings)}


@router.post("/api/saves/machines")
def saves_machine_add(settings: SettingsDep, body: SavesMachineAdd):
    """Associe un client par son code wsq:// ; même empreinte ou même adresse → remplacement."""
    try:
        return clients_svc.add_machine(settings, body.code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentProbeError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.delete("/api/saves/machines/{machine_id}")
def saves_machine_remove(settings: SettingsDep, machine_id: str):
    try:
        clients_svc.remove_machine(settings, machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    trial_svc.doctor_cache.clear()           # le client d'essai a pu être désactivé avec lui
    return {"removed": machine_id}


class SavesDir(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    path: str = Field(min_length=1, max_length=1024)


def _machine_dirs(settings: Settings, machine_id: str, change: tuple[str, ...] = ()) -> dict:
    """Dossiers déclarés d'un client ; 404 client inconnu, 502 injoignable, 422 refus de
    l'agent (chemin hors HOME, nom invalide, agent trop ancien...)."""
    try:
        return clients_svc.machine_dirs(settings, machine_id, change)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except AgentUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ManagerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dirs(settings: SettingsDep, machine_id: str):
    """Dossiers synchronisés en plus des jeux, déclarés sur le client (et dossier d'Eden)."""
    return _machine_dirs(settings, machine_id)


@router.put("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dir_put(settings: SettingsDep, machine_id: str, body: SavesDir):
    """Déclare (ou remplace) un dossier sur le client : chemin de ce client, sous son HOME."""
    return _machine_dirs(settings, machine_id, ("put", body.name.strip(), body.path.strip()))


@router.delete("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dir_delete(settings: SettingsDep, machine_id: str, name: str):
    """Retire la déclaration sur le client ; ses fichiers restent."""
    return _machine_dirs(settings, machine_id, ("delete", name))


class SavesMachineIcon(BaseModel):
    icon: str | None                    # linux, steamos, steambox, batocera ; null : retour à la détection


class SavesMachineOrder(BaseModel):
    ids: list[str] = Field(max_length=50)       # tous les clients, du plus à gauche dans le bandeau


@router.put("/api/saves/machines/order")
def saves_machines_order(settings: SettingsDep, body: SavesMachineOrder):
    """Ordre des clients dans le bandeau et les listes (chaque client une seule fois)."""
    try:
        return {"ids": clients_svc.set_machine_order(settings, body.ids)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/saves/machines/{machine_id}")
def saves_machine_icon(settings: SettingsDep, machine_id: str, body: SavesMachineIcon):
    """Icône choisie à la main d'un client ; null la rend à la détection (système)."""
    try:
        return clients_svc.set_icon(settings, machine_id, body.icon)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/clients/activity")
def clients_activity(since: int = -1):
    """Jeux en cours et manettes par client, événements de partie après `since` : lu par l'interface
    sans contacter les clients (l'état vient des relectures et des événements qu'ils envoient)."""
    return activity.snapshot(since)


@router.post("/api/saves/machines/{machine_id}/update")
def saves_machine_update(settings: SettingsDep, machine_id: str):
    """Met à jour le lanceur du client (agent 11, binaire vérifié par son empreinte) ; l'agent se relance."""
    try:
        return clients_svc.open_machine(settings, machine_id, timeout=330).update()
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except AgentUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ManagerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
