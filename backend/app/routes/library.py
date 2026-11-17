"""Bibliothèque : liste des images, récupération des images abîmées (recovery) et
illustrations de l'image (aperçu des candidats compris)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import deps
from ..models import ImageInfo
from ..services import art_choice, scan
from ..services.artwork import embedded_cover, forget_embedded_misses
from ..services.durable import restore
from ..services.sgdb import forget_sgdb_misses

router = APIRouter()


@router.get("/api/library", response_model=list[ImageInfo])
def library(refresh: bool = False):
    """Ludothèque : toutes les images des dossiers roms/, avec cache."""
    return [deps._with_task_state(info) for info in deps._refresh_library(force=refresh)]


@router.get("/api/recovery")
def recovery():
    """Images absentes ou illisibles dont l'ancienne version (.old) est saine."""
    return [d for d in scan.find_damaged(deps._settings) if not deps.queue.active_for(d["id"])]


class RecoveryRestore(BaseModel):
    old: str


@router.post("/api/recovery/restore")
def recovery_restore(body: RecoveryRestore):
    """Remet l'ancienne version d'une image abîmée (seulement une image
    signalée par /api/recovery)."""
    damaged = {d["old"]: d for d in recovery()}
    if body.old not in damaged:
        raise HTTPException(status_code=404, detail="rien à remettre pour ce fichier")
    image = Path(damaged[body.old]["image"])
    if image.exists() and scan.image_in_use(image):
        raise HTTPException(status_code=409, detail="jeu en cours")
    restore(Path(body.old), image)
    deps._refresh_library(force=False)
    return {"restored": str(image)}


@router.post("/api/image/art/forget")
def image_art_forget():
    """Actualiser : les absences (SteamGridDB, illustration intégrée) sont revalidées au
    prochain affichage."""
    forget_sgdb_misses(deps._settings)
    forget_embedded_misses(deps._settings)
    art_choice.oublier()
    return {"ok": True}


@router.get("/api/image/art")
def image_art(id: str, kind: Literal["cover", "icon"] = "cover"):
    """Illustration intégrée à l'image (`.wsquashfs/cover`) : la seule affichée. `kind` est
    accepté pour compatibilité et ignoré (une seule image pour icône et jaquette). 404 si
    l'image n'en a pas."""
    info = deps._find_image(id)
    art = embedded_cover(deps._settings, info.path)
    if art is None:
        raise HTTPException(status_code=404, detail="aucune illustration")
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})


@router.get("/api/image/art/candidates")
def image_art_candidates(id: str):
    """Aperçu du remplacement de l'illustration : candidats (SteamGridDB, jaquette
    d'EmulationStation, icône de l'exécutable) et illustration intégrée actuelle. Aucune
    modification. Voir services/art_choice.py."""
    info = deps._find_image(id)
    return art_choice.apercu(deps._settings, info.id, info.path)


@router.get("/api/image/art/candidate")
def image_art_candidate(id: str, key: str, size: Literal["thumb", "full"] = "thumb"):
    """Image d'un candidat de l'aperçu. Clé inconnue pour cette image : 404 ; SteamGridDB
    injoignable ou clé refusée : 502. Seules les adresses de la liste de l'image sont suivies."""
    info = deps._find_image(id)
    try:
        art = art_choice.candidat_fichier(deps._settings, info.path, key, size)
    except art_choice.CandidatError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})
