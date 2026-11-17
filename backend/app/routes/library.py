"""Bibliothèque : liste des images, récupération des images abîmées (recovery) et
illustrations de l'image (aperçu des candidats compris)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import deps
from ..models import ImageInfo
from ..services import art_choice, scan, sgdb
from ..services.artwork import embedded_cover, es_artwork, forget_embedded_misses
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
def image_art(id: str, kind: Literal["cover", "icon"] = "icon"):
    """Illustration d'une image selon la vue :
    - `icon` (vue liste) : l'illustration intégrée à l'image (`.wsquashfs/cover`, l'icône) ;
    - `cover` (tuiles de la bibliothèque) : l'affiche SteamGridDB en cache dans l'appdata
      (téléchargée au premier affichage, absence mémorisée), sinon la jaquette
      d'EmulationStation, sinon l'illustration intégrée.
    404 si rien n'est trouvé."""
    info = deps._find_image(id)
    art = None
    if kind == "cover":
        art = (sgdb.sgdb_artwork(deps._settings, info.path, "cover", info.id)
               or es_artwork(info.path))
    if art is None:
        art = embedded_cover(deps._settings, info.path)
    if art is None:
        raise HTTPException(status_code=404, detail="aucune illustration")
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})


@router.get("/api/image/art/candidates")
def image_art_candidates(id: str, q: str | None = Query(None, max_length=sgdb.ALIAS_NOM_MAX),
                         game_id: int | None = None):
    """Aperçu du remplacement de l'illustration : candidats (SteamGridDB, jaquette
    d'EmulationStation, icône de l'exécutable) et illustration intégrée actuelle. Aucune
    modification d'illustration. `q` : chercher sous un autre nom (mémorisé pour ce jeu ;
    vide : revenir au nom de l'image) ; `game_id` : jeu SteamGridDB choisi parmi `games`.
    Voir services/art_choice.py."""
    info = deps._find_image(id)
    return art_choice.apercu(deps._settings, art_choice.Cible(info.id, info.path), q, game_id)


@router.delete("/api/image/art/alias")
def image_art_alias_reset(id: str):
    """Revenir au nom de l'image pour la recherche SteamGridDB (nom mémorisé effacé)."""
    info = deps._find_image(id)
    sgdb.oublier_alias(deps._settings, info.id)
    art_choice.oublier()
    return {"ok": True}


@router.get("/api/image/art/candidate")
def image_art_candidate(id: str, key: str, size: Literal["thumb", "full"] = "thumb"):
    """Image d'un candidat de l'aperçu. Clé inconnue pour cette image : 404 ; SteamGridDB
    injoignable ou clé refusée : 502. Seules les adresses de la liste de l'image sont suivies."""
    info = deps._find_image(id)
    try:
        art = art_choice.candidat_fichier(deps._settings, art_choice.Cible(info.id, info.path),
                                          key, size)
    except art_choice.CandidatError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return FileResponse(art.path, media_type=art.media_type,
                        headers={"X-Art-Kind": art.kind, "Cache-Control": "private, max-age=3600"})
