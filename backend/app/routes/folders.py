"""Dossiers à empaqueter (*.pc, *.wine) : liste, analyse, empaquetage, suppression, illustration
du dossier (écrite dans `.wsquashfs/cover`, intégrée à l'empaquetage) ; base de connaissances
des propositions d'autorun."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from ..config import Settings
from ..deps import QueueDep, SettingsDep
from ..services import analyze as analyze_mod
from ..services import art_choice, scan, sgdb
from ..services.analyze import FolderAnalysis, add_local_game, analyze_folder, report
from ..services.artwork import ecrire_cover_dossier, folder_cover
from ..services.autorun import KEY_NAME_RE, Autorun, decode_autorun
from ..services.durable import write_atomic
from ..services.errors import ManagerError
from ..services.operations import (
    find_folders,
    folder_not_in_image,
    folder_stem,
    pack_target,
    remove_tree,
)
from ..services.tasks import TaskConflict, TaskQueue
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
    analyze_mod.analyses.clear()                    # les propositions en cache en tiennent compte
    return entry


def _folders(settings: Settings, queue: TaskQueue) -> dict[str, dict]:
    folders = {f["id"]: f for f in find_folders(settings)}
    for folder in folders.values():
        task = queue.active_for(folder["image_id"])
        folder["task"] = task.status.value if task else None
    return folders


def _find_folder(settings: Settings, queue: TaskQueue, folder_id: str) -> dict:
    folder = _folders(settings, queue).get(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"dossier inconnu : {folder_id}")
    return folder


@router.get("/api/folders")
def list_folders(settings: SettingsDep, queue: QueueDep):
    """Dossiers de jeux à empaqueter (*.pc, *.wine) des dossiers roms/."""
    return list(_folders(settings, queue).values())


def _analysis(folder: dict, refresh: bool) -> FolderAnalysis:
    path = Path(folder["path"])
    mtime = path.stat().st_mtime
    cached = analyze_mod.analyses.get(folder["path"])
    if cached and cached[0] == mtime and not refresh:
        return cached[1]
    fa = analyze_folder(path, folder["name"])
    if len(analyze_mod.analyses) >= analyze_mod.ANALYSES_MAX:
        analyze_mod.analyses.pop(next(iter(analyze_mod.analyses)))
    analyze_mod.analyses[folder["path"]] = (mtime, fa)
    return fa


@router.get("/api/folders/art")
def folder_art(queue: QueueDep, settings: SettingsDep, id: str, kind: Literal["cover", "icon"] = "icon"):
    """Illustration d'un dossier à empaqueter, comme pour une image :
    - `icon` (vue liste) : `<dossier>/.wsquashfs/cover`, l'illustration qui sera intégrée ;
    - `cover` (jaquettes) : l'affiche SteamGridDB en cache (nom et jeu mémorisés du dossier),
      sinon cette même illustration.
    Sans cache HTTP durable : le fichier peut être ajouté à tout moment (revalidé à chaque affichage)."""
    folder = _find_folder(settings, queue, id)
    art = sgdb.sgdb_artwork(settings, Path(folder["path"]), "cover", folder["id"]) if kind == "cover" else None
    art = art or folder_cover(Path(folder["path"]))
    if art is None:
        raise HTTPException(status_code=404, detail="aucune illustration")
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, no-cache"})


def _cible(folder: dict) -> art_choice.Cible:
    return art_choice.Cible(folder["id"], Path(folder["path"]), dossier=True)


@router.get("/api/folders/art/candidates")
def folder_art_candidates(queue: QueueDep, settings: SettingsDep, id: str, q: str | None = Query(None, max_length=sgdb.ALIAS_NOM_MAX),
                          game_id: int | None = None):
    """Aperçu de l'illustration d'un dossier à empaqueter (mêmes champs que pour une image).
    `q` et `game_id` : nom et jeu SteamGridDB, comme pour une image."""
    return art_choice.apercu(settings, _cible(_find_folder(settings, queue, id)), q, game_id)


@router.get("/api/folders/art/candidate")
def folder_art_candidate(queue: QueueDep, settings: SettingsDep, id: str, key: str, size: Literal["thumb", "full"] = "thumb"):
    """Image d'un candidat de l'aperçu d'un dossier (mêmes règles que pour une image)."""
    try:
        art = art_choice.candidat_fichier(settings, _cible(_find_folder(settings, queue, id)), key, size)
    except art_choice.CandidatError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})


@router.delete("/api/folders/art/alias")
def folder_art_alias_reset(queue: QueueDep, settings: SettingsDep, id: str):
    """Revenir au nom du dossier pour la recherche SteamGridDB (nom mémorisé effacé)."""
    folder = _find_folder(settings, queue, id)
    sgdb.oublier_alias(settings, folder["id"])
    art_choice.oublier()
    return {"ok": True}


class FolderCover(BaseModel):
    id: str
    key: str                            # clé d'un candidat de l'aperçu (sgdb:<id>)

    @field_validator("key")
    @classmethod
    def _cle(cls, value: str) -> str:
        if not art_choice.cle_valide(value):
            raise ValueError("clé de candidat invalide")
        return value


@router.post("/api/folders/cover")
def folder_cover_set(queue: QueueDep, settings: SettingsDep, body: FolderCover):
    """Écrit le candidat choisi dans `<dossier>/.wsquashfs/cover` : l'illustration est intégrée
    à l'empaquetage. Aucune reconstruction. Refusé pendant l'empaquetage du dossier, et une fois
    l'image créée (son illustration se change alors depuis la fiche de l'image)."""
    folder = _find_folder(settings, queue, body.id)
    if folder["task"]:
        raise HTTPException(status_code=409, detail="empaquetage en file ou en cours : illustration refusée")
    if folder["image_exists"]:
        raise HTTPException(status_code=409, detail="image déjà créée : son illustration se change depuis la fiche de l'image")
    try:
        art = art_choice.candidat_fichier(settings, _cible(folder), body.key, "full")
    except art_choice.CandidatError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    try:
        target = ecrire_cover_dossier(Path(folder["path"]), art.path.read_bytes())
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"écriture impossible : {exc}") from exc
    return {"ok": True, "cover": str(target.relative_to(Path(folder["path"])))}


@router.get("/api/folders/analyze")
def folder_analyze(settings: SettingsDep, queue: QueueDep, id: str, exe: str | None = None, refresh: bool = False):
    """Analyse et proposition d'autorun justifiée (SPEC § 2.5) ; `exe`
    choisit un autre candidat."""
    folder = _find_folder(settings, queue, id)
    result = report(_analysis(folder, refresh), exe)
    return {**folder, **result}


def _autorun_file(folder: Path) -> Path | None:
    """`autorun.cmd` du dossier, quelle que soit sa casse."""
    try:
        return next((p for p in folder.iterdir() if p.name.lower() == "autorun.cmd" and p.is_file()), None)
    except OSError:
        return None


@router.get("/api/folders/autorun")
def folder_autorun(settings: SettingsDep, queue: QueueDep, id: str):
    """`autorun.cmd` du dossier tel qu'il est sur le disque (panneau du dossier)."""
    folder = _find_folder(settings, queue, id)
    path = _autorun_file(Path(folder["path"]))
    if path is None:
        return {"found": False, "text": "", "eol": "\r\n", "encoding": "utf-8", "sha": None}
    data = path.read_bytes()
    text, encoding = decode_autorun(data)
    parsed = Autorun.parse(text)
    return {"found": True, "text": text.replace("\r\n", "\n"), "eol": parsed.eol, "encoding": encoding,
            "sha": hashlib.sha256(data).hexdigest()}


class FolderAutorun(BaseModel):
    text: str
    eol: Literal["\r\n", "\n"] = "\r\n"
    encoding: Literal["utf-8", "latin-1"] = "utf-8"
    base_sha: str | None = None          # autorun lu à l'ouverture (None : il n'y en avait pas)
    check: bool = False                  # contrôle seulement, rien n'est écrit


@router.put("/api/folders/autorun")
def folder_autorun_write(settings: SettingsDep, queue: QueueDep, id: str, body: FolderAutorun):
    """Enregistre `autorun.cmd` dans le dossier, avant empaquetage (l'empaquetage le reprend
    comme autorun existant). Refusé pendant l'empaquetage du dossier, ou si le fichier a
    changé depuis sa lecture."""
    folder = _find_folder(settings, queue, id)
    root = Path(folder["path"])
    autorun = Autorun.parse(body.text.replace("\r\n", "\n"))
    autorun.eol, autorun.trailing_eol = body.eol, True
    issues = autorun.validate(set(_analysis(folder, False).scan.files))
    if body.check:
        return {"written": False, "issues": issues}
    if not autorun.get("CMD"):
        raise HTTPException(status_code=422, detail="autorun sans CMD")
    if folder["task"]:
        raise HTTPException(status_code=409, detail="empaquetage en file ou en cours : autorun non modifiable")
    try:
        data = autorun.encode(body.encoding)
    except UnicodeEncodeError:
        data = autorun.encode("utf-8")
    current = _autorun_file(root)
    old = current.read_bytes() if current else None
    if (hashlib.sha256(old).hexdigest() if old is not None else None) != body.base_sha:
        raise HTTPException(status_code=409, detail="autorun.cmd modifié entre-temps : recharger le dossier")
    if old == data:
        return {"written": False, "issues": issues}
    try:
        mode = current.stat().st_mode & 0o777 if current else 0o644
        write_atomic(current or root / "autorun.cmd", data, mode=mode)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"écriture impossible : {exc}") from exc
    return {"written": True, "issues": issues}


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
def folders_pack(settings: SettingsDep, queue: QueueDep, body: PackRequest):
    """Une tâche d'empaquetage par dossier ; les refus sont listés avec la raison."""
    folders = _folders(settings, queue)
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
            task = queue.submit("pack", folder["image_id"], pack_target(source), params, title,
                                night=body.when == "night")
        except TaskConflict as exc:
            skipped.append({"id": item.id, "reason": str(exc)})
            continue
        created.append(task.id)
    return {"tasks": created, "skipped": skipped}


@router.post("/api/folders/delete")
def folders_delete(queue: QueueDep, settings: SettingsDep, body: MassIds):
    """Supprime des dossiers DÉJÀ empaquetés (l'image existe et se lit)."""
    folders = _folders(settings, queue)
    done, skipped = [], []
    for folder_id in body.ids:
        folder = folders.get(folder_id)
        if folder is None:
            skipped.append({"id": folder_id, "reason": "dossier inconnu"})
            continue
        image = pack_target(Path(folder["path"]))
        if not image.exists() or not scan.unsquashfs_ok(settings, image):
            skipped.append({"id": folder_id,
                            "reason": "image absente ou illisible : dossier gardé"})
            continue
        if folder["task"]:
            skipped.append({"id": folder_id, "reason": "tâche en cours"})
            continue
        # L'image doit contenir tout le dossier (mêmes fichiers, mêmes tailles) : une image d'une
        # autre version, ou un dossier modifié depuis l'empaquetage, garde le dossier
        try:
            differ = folder_not_in_image(settings, Path(folder["path"]), image)
        except ManagerError as exc:
            skipped.append({"id": folder_id, "reason": f"{exc} : dossier gardé"})
            continue
        if differ:
            skipped.append({"id": folder_id,
                            "reason": f"{len(differ)} fichier(s) absent(s) de l'image ou d'une autre "
                                      f"taille (ex. {differ[0]}) : dossier gardé"})
            continue
        try:
            remove_tree(Path(folder["path"]))
        except OSError as exc:
            skipped.append({"id": folder_id, "reason": f"suppression incomplète : {exc}"})
            continue
        done.append(folder_id)
    return {"done": done, "skipped": skipped}
