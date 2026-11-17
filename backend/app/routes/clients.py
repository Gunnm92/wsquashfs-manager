"""Clients associés (sauvegardes) : association, icône, dossiers synchronisés."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import deps
from ..services import saves as saves_svc
from ..services.rebuild import RebuildError
from ..services.trial import AgentProbeError, AgentUnreachable

router = APIRouter()


class SavesMachineAdd(BaseModel):
    code: str = Field(min_length=1, max_length=600)     # code wsq:// de « wsquashfs-launcher --pair »


@router.get("/api/saves/machines")
def saves_machines_list():
    """Machines de la synchronisation (jeton et certificat jamais renvoyés)."""
    return {"machines": saves_svc.machine_rows(deps._settings)}


@router.post("/api/saves/machines")
def saves_machine_add(body: SavesMachineAdd):
    """Associe un client par son code wsq:// ; même empreinte ou même adresse → remplacement."""
    try:
        return saves_svc.add_machine(deps._settings, body.code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentProbeError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.delete("/api/saves/machines/{machine_id}")
def saves_machine_remove(machine_id: str):
    try:
        saves_svc.remove_machine(deps._settings, machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    deps._doctor_cache.clear()           # le client d'essai a pu être désactivé avec lui
    return {"removed": machine_id}


class SavesDir(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    path: str = Field(min_length=1, max_length=1024)


def _machine_dirs(machine_id: str, change: tuple[str, ...] = ()) -> dict:
    """Dossiers déclarés d'un client ; 404 client inconnu, 502 injoignable, 422 refus de
    l'agent (chemin hors HOME, nom invalide, agent trop ancien...)."""
    try:
        return saves_svc.machine_dirs(deps._settings, machine_id, change)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except AgentUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RebuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dirs(machine_id: str):
    """Dossiers synchronisés en plus des jeux, déclarés sur le client (et dossier d'Eden)."""
    return _machine_dirs(machine_id)


@router.put("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dir_put(machine_id: str, body: SavesDir):
    """Déclare (ou remplace) un dossier sur le client : chemin de ce client, sous son HOME."""
    return _machine_dirs(machine_id, ("put", body.name.strip(), body.path.strip()))


@router.delete("/api/saves/machines/{machine_id}/dirs")
def saves_machine_dir_delete(machine_id: str, name: str):
    """Retire la déclaration sur le client ; ses fichiers restent."""
    return _machine_dirs(machine_id, ("delete", name))


class SavesMachineIcon(BaseModel):
    icon: str | None                    # linux, steamos, batocera ; null : retour à la détection


@router.patch("/api/saves/machines/{machine_id}")
def saves_machine_icon(machine_id: str, body: SavesMachineIcon):
    """Icône choisie à la main d'un client ; null la rend à la détection (système)."""
    try:
        return saves_svc.set_icon(deps._settings, machine_id, body.icon)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
