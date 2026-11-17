"""Dossiers à empaqueter (*.pc, *.wine) : liste, analyse, empaquetage, suppression ;
base de connaissances des propositions d'autorun."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .. import deps
from ..services import analyze as analyze_mod
from ..services import scan
from ..services.analyze import FolderAnalysis, add_local_game, analyze_folder, report
from ..services.artwork import folder_cover
from ..services.autorun import KEY_NAME_RE, Autorun
from ..services.operations import find_folders, folder_stem, pack_target, remove_tree
from ..services.tasks import TaskConflict
from .mass import MassIds

router = APIRouter()


class KnowledgeEntry(BaseModel):
    game: str | None = None              # motif du nom du jeu (casse ignorée)
    exe: str | None = None               # nom de l'exécutable
    prefer: str | None = None            # exécutable à retenir pour ce jeu
    keys: dict[str, str] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("keys")
    @classmethod
    def _valid_keys(cls, keys: dict[str, str]) -> dict[str, str]:
        for key, value in keys.items():
            if not KEY_NAME_RE.match(key) or "\n" in value or "\r" in value:
                raise ValueError(f"clé invalide : {key}")
        return keys


@router.get("/api/knowledge")
def knowledge_list():
    """Base de connaissances : entrées locales (prioritaires) puis livrées."""
    return analyze_mod.load_games()


@router.post("/api/knowledge")
def knowledge_add(body: KnowledgeEntry):
    """« Ajouter à la base » : enregistre un réglage validé pour ce jeu dans
    la base locale (appdata), appliqué aux prochaines propositions."""
    try:
        entry = add_local_game(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    deps._analyses.clear()                    # les propositions en cache en tiennent compte
    return entry


def _folders() -> dict[str, dict]:
    folders = {f["id"]: f for f in find_folders(deps._settings)}
    for folder in folders.values():
        task = deps.queue.active_for(folder["image_id"])
        folder["task"] = task.status.value if task else None
    return folders


def _find_folder(folder_id: str) -> dict:
    folder = _folders().get(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"dossier inconnu : {folder_id}")
    return folder


@router.get("/api/folders")
def list_folders():
    """Dossiers de jeux à empaqueter (*.pc, *.wine) des dossiers roms/."""
    return list(_folders().values())


def _analysis(folder: dict, refresh: bool) -> FolderAnalysis:
    path = Path(folder["path"])
    mtime = path.stat().st_mtime
    cached = deps._analyses.get(folder["path"])
    if cached and cached[0] == mtime and not refresh:
        return cached[1]
    fa = analyze_folder(path, folder["name"])
    if len(deps._analyses) >= deps._ANALYSES_MAX:
        deps._analyses.pop(next(iter(deps._analyses)))
    deps._analyses[folder["path"]] = (mtime, fa)
    return fa


@router.get("/api/folders/art")
def folder_art(id: str):
    """Illustration d'un dossier à empaqueter (`<dossier>/.wsquashfs/cover`). Sans cache HTTP
    durable : le fichier peut être ajouté à tout moment (revalidé à chaque affichage)."""
    art = folder_cover(Path(_find_folder(id)["path"]))
    if art is None:
        raise HTTPException(status_code=404, detail="aucune illustration")
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, no-cache"})


@router.get("/api/folders/analyze")
def folder_analyze(id: str, exe: str | None = None, refresh: bool = False):
    """Analyse et proposition d'autorun justifiée (SPEC § 2.5) ; `exe`
    choisit un autre candidat."""
    folder = _find_folder(id)
    result = report(_analysis(folder, refresh), exe)
    return {**folder, **result}


class PackItem(BaseModel):
    id: str
    autorun: str | None = None          # None : garder l'autorun du dossier
    encoding: Literal["utf-8", "latin-1"] = "utf-8"

    @field_validator("autorun")
    @classmethod
    def _has_cmd(cls, value: str | None) -> str | None:
        if value is None:
            return None
        autorun = Autorun.parse(value.replace("\r\n", "\n"))
        if not autorun.get("CMD"):
            raise ValueError("autorun sans CMD")
        # Fins de ligne Windows, comme les autoruns Batocera
        return Autorun(lines=autorun.lines, eol="\r\n", trailing_eol=True).render()


class PackRequest(BaseModel):
    items: list[PackItem] = Field(min_length=1)
    rename_wine: bool = True
    delete_source: bool = False
    when: Literal["now", "night"] = "now"


def _renamed_folder(folders: dict[str, dict], folder_id: str) -> dict | None:
    """« Jeu.pc » déjà renommé en « Jeu.wine » par un empaquetage précédent."""
    system, _, name = folder_id.rpartition("/")
    stem = folder_stem(name)
    return next((f for f in folders.values()
                 if f["name"] == stem and (f["system"] or "") == system), None)


@router.post("/api/folders/pack")
def folders_pack(body: PackRequest):
    """Une tâche d'empaquetage par dossier ; les refus sont listés avec la raison."""
    folders = _folders()
    created, skipped = [], []
    for item in body.items:
        folder = folders.get(item.id) or _renamed_folder(folders, item.id)
        if folder is None:
            skipped.append({"id": item.id, "reason": "dossier introuvable (renommé ou supprimé ?)"})
            continue
        if folder["task"]:
            skipped.append({"id": item.id, "reason": "empaquetage déjà en attente ou en cours"})
            continue
        if folder["image_exists"]:
            skipped.append({"id": item.id, "reason": f"{folder['name']}.wsquashfs existe déjà"})
            continue
        if item.autorun is None and not folder["has_autorun"]:
            skipped.append({"id": item.id, "reason": "pas d'autorun : en générer un"})
            continue
        source = Path(folder["path"])
        params = {"source": str(source), "autorun": item.autorun, "encoding": item.encoding,
                  "rename_wine": body.rename_wine, "delete_source": body.delete_source}
        title = "empaquetage" + (" + .wine" if body.rename_wine else "") + \
            (" + suppression du dossier" if body.delete_source else "")
        try:
            task = deps.queue.submit("pack", folder["image_id"], pack_target(source), params, title,
                                night=body.when == "night")
        except TaskConflict as exc:
            skipped.append({"id": item.id, "reason": str(exc)})
            continue
        created.append(task.id)
    return {"tasks": created, "skipped": skipped}


@router.post("/api/folders/delete")
def folders_delete(body: MassIds):
    """Supprime des dossiers DÉJÀ empaquetés (l'image existe et se lit)."""
    folders = _folders()
    done, skipped = [], []
    for folder_id in body.ids:
        folder = folders.get(folder_id)
        if folder is None:
            skipped.append({"id": folder_id, "reason": "dossier inconnu"})
            continue
        image = pack_target(Path(folder["path"]))
        if not image.exists() or not scan._unsquashfs_ok(deps._settings, image):
            skipped.append({"id": folder_id,
                            "reason": "image absente ou illisible : dossier gardé"})
            continue
        if folder["task"]:
            skipped.append({"id": folder_id, "reason": "tâche en cours"})
            continue
        try:
            remove_tree(Path(folder["path"]))
        except OSError as exc:
            skipped.append({"id": folder_id, "reason": f"suppression incomplète : {exc}"})
            continue
        done.append(folder_id)
    return {"done": done, "skipped": skipped}
