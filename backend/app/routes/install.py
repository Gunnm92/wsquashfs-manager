"""Installation des images sur les clients associés et désinstallation (tâches « install »
de la file, résultat par machine dans task.params.result)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import deps
from ..services import install as install_svc
from ..services.install import valid_name
from ..services.rebuild import RebuildError
from ..services.tasks import TaskConflict
from ..services.trial import AgentRejected, AgentUnreachable

router = APIRouter()


class InstallIds(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)           # identifiants <système>/<nom>
    machines: list[str] = Field(min_length=1, max_length=20)


class UninstallNames(BaseModel):
    names: list[str] = Field(min_length=1, max_length=50)         # « Jeu.wsquashfs »
    machines: list[str] = Field(min_length=1, max_length=20)


class InstallDir(BaseModel):
    dir: str = Field(max_length=1024)                              # « » : dossier deviné


@router.get("/api/install/status")
def install_status():
    """Par machine : joignable, dossier des jeux, espace libre, jeux et envois partiels."""
    return install_svc.status(deps._settings)


@router.post("/api/install")
def install_submit(body: InstallIds):
    """Une tâche « install » par image, vers les clients cochés. Une image déjà en
    tâche active : 409 (rien n'est soumis)."""
    if len(set(body.ids)) != len(body.ids):
        raise HTTPException(status_code=422, detail="image en double")
    if len(set(body.machines)) != len(body.machines):
        raise HTTPException(status_code=422, detail="client en double")
    infos = [deps._find_image(image_id) for image_id in body.ids]
    for info in infos:
        if not valid_name(info.path.name):
            raise HTTPException(status_code=422, detail="seules les images .wsquashfs s'installent")
    targets = [deps._saves_machine(machine_id) for machine_id in body.machines]
    for info in infos:
        if deps.queue.active_for(info.id):
            raise HTTPException(status_code=409, detail=f"une tâche est déjà en attente ou en cours pour {info.id}")
    where = f"{len(targets)} client(s)"
    tasks = []
    for info in infos:
        params = {"op": "install", "machines": body.machines}
        try:
            task = deps.queue.submit("install", info.id, info.path, params, f"installer {info.path.name} sur {where}")
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        tasks.append(task.id)
    return {"tasks": tasks}


@router.post("/api/uninstall")
def uninstall_submit(body: UninstallNames):
    """Une tâche « install » (op uninstall) par nom de jeu, sur les clients cochés."""
    if len(set(body.names)) != len(body.names):
        raise HTTPException(status_code=422, detail="jeu en double")
    for name in body.names:
        if not valid_name(name):
            raise HTTPException(status_code=422, detail=f"nom d'image invalide : {name}")
    if len(set(body.machines)) != len(body.machines):
        raise HTTPException(status_code=422, detail="client en double")
    targets = [deps._saves_machine(machine_id) for machine_id in body.machines]
    where = f"{len(targets)} client(s)"
    tasks = []
    for name in body.names:
        params = {"op": "uninstall", "name": name, "machines": body.machines}
        tasks.append(deps.queue.submit("install", None, None, params, f"désinstaller {name} de {where}").id)
    return {"tasks": tasks}


@router.post("/api/install/machines/{machine_id}/dir")
def install_set_dir(machine_id: str, body: InstallDir):
    """Dossier des jeux du client (« » : le dossier deviné). 400 : refusé par l'agent
    (hors du HOME, inexistant), son message relayé ; 502 : client injoignable."""
    deps._saves_machine(machine_id)
    try:
        return install_svc.set_dir(deps._settings, machine_id, body.dir)
    except AgentRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (AgentUnreachable, RebuildError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
