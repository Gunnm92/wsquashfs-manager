"""Aperçu de l'illustration : les candidats proposés avant un remplacement, et le choix imposé à
la reconstruction.

Candidats d'une image, dans cet ordre :
- SteamGridDB (`sgdb:<id de la grille>`) : grilles portrait, auteurs préférés d'abord, puis note
  décroissante, 24 au plus (services/sgdb.py) ; liste gardée CANDIDATS_TTL secondes en mémoire ;
- jaquette d'EmulationStation (`es`), si elle existe ;
- icône de l'exécutable (`icon`), si elle existe.

Sécurité : une image n'est téléchargée que depuis une adresse de SA liste de candidats
(recalculée si elle a expiré) ; la requête ne fournit qu'une clé, jamais une adresse.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image

from ..config import Settings
from . import artwork as _art
from . import scan, sgdb
from .autorun import Autorun, decode_autorun

CANDIDATS_TTL = 300                 # s : liste SteamGridDB d'une image gardée ce délai
_CLE = re.compile(r"sgdb:\d+|es|icon")

_listes: dict[str, tuple[float, tuple[list[dict], dict | None, str | None, str | None]]] = {}
_garde = threading.Lock()


class CandidatError(Exception):
    """Candidat introuvable (`status` 404) ou indisponible (502)."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


def cle_valide(key: str) -> bool:
    """Forme d'une clé de candidat : `sgdb:<entier>`, `es` ou `icon`."""
    return bool(_CLE.fullmatch(key))


def oublier() -> None:
    """Actualiser : la liste SteamGridDB est recalculée au prochain aperçu."""
    with _garde:
        _listes.clear()


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except (OSError, ValueError):
        return None, None


def _entier(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ SteamGridDB

def _sgdb(settings: Settings, image: Path) -> tuple[dict | None, list[dict], str | None, str | None]:
    """(jeu, grilles, clé préférée, erreur) ; les grilles gardent leurs adresses (interne)."""
    if not settings.sgdb_api_key:
        return None, [], None, None
    now = time.time()
    cache_key = str(image)
    with _garde:
        cached = _listes.get(cache_key)
    if cached is not None and now - cached[0] < CANDIDATS_TTL:
        return cached[1]
    try:
        game, grilles, retenue = sgdb.grilles_candidates(settings, image)
        gid = _entier(retenue.get("id")) if retenue else None
        result = (game, grilles, None if gid is None else f"sgdb:{gid}", None)
    except sgdb.SgdbError as exc:
        result = (None, [], None, str(exc))
    with _garde:
        _listes[cache_key] = (now, result)
    return result


def _entrees_sgdb(settings: Settings, image: Path) -> tuple[list[dict], dict | None, str | None,
                                                            str | None]:
    game, grilles, preferee, erreur = _sgdb(settings, image)
    entrees = []
    for grille in grilles:
        gid = _entier(grille.get("id"))
        if gid is None or not isinstance(grille.get("url"), str):
            continue
        auteur = grille.get("author") if isinstance(grille.get("author"), dict) else {}
        nom = str(auteur.get("name") or "") or None
        try:
            score = int(float(grille.get("score")))
        except (TypeError, ValueError):
            score = None
        entrees.append({
            "key": f"sgdb:{gid}", "source": "sgdb",
            "label": "SteamGridDB" + (f", par {nom}" if nom else ""),
            "author": nom, "score": score, "width": _entier(grille.get("width")),
            "height": _entier(grille.get("height")), "grid_id": gid,
            "urls": {"thumb": str(grille.get("thumb") or grille["url"]), "full": grille["url"]},
        })
    return entrees, game, preferee, erreur


# ------------------------------------------------------------------ locaux (ES, icône)

def _cmd_dir(settings: Settings, image: Path) -> tuple[str | None, str | None]:
    data = scan.read_autorun_bytes(settings, image)
    a = Autorun.parse(decode_autorun(data)[0]) if data is not None else None
    return (a.get("CMD") if a else None), (a.get("DIR") if a else None)


def _entrees_locales(settings: Settings, image: Path) -> list[dict]:
    entrees = []
    es = _art.es_artwork(image)
    if es is not None:
        w, h = _dimensions(es.path)
        entrees.append({"key": "es", "source": "es", "label": "Jaquette EmulationStation",
                        "author": None, "score": None, "width": w, "height": h,
                        "path": es.path, "media": es.media_type})
    cmd, dir_ = _cmd_dir(settings, image)
    icon = _art.exe_icon(settings, image, cmd, dir_)
    if icon is not None:
        w, h = _dimensions(icon.path)
        entrees.append({"key": "icon", "source": "icon", "label": "Icône de l'exécutable",
                        "author": None, "score": None, "width": w, "height": h,
                        "path": icon.path, "media": icon.media_type})
    return entrees


# ------------------------------------------------------------------ aperçu

def _actuelle(settings: Settings, image_id: str, image: Path) -> dict:
    art = _art.embedded_cover(settings, image)
    if art is None:
        return {"present": False, "sha1": None, "url": None}
    return {"present": True, "sha1": _sha1(art.path),
            "url": f"/api/image/art?{urlencode({'id': image_id})}"}


def _meme_que(settings: Settings, entree: dict, sha: str | None) -> bool:
    """Octets du candidat identiques à l'illustration intégrée (SteamGridDB : seulement si son
    plein format est déjà en cache)."""
    if sha is None:
        return False
    if entree["source"] == "sgdb":
        art = sgdb.grille_en_cache(settings, entree["grid_id"], "full")
    else:
        art = _art.Artwork(entree["path"], entree["media"], entree["source"])
    return art is not None and art.path.is_file() and _sha1(art.path) == sha


def _adresse(image_id: str, key: str, size: str) -> str:
    return f"/api/image/art/candidate?{urlencode({'id': image_id, 'key': key, 'size': size})}"


def apercu(settings: Settings, image_id: str, image: Path) -> dict:
    """Réponse de GET /api/image/art/candidates (voir la documentation de l'API dans le README)."""
    actuelle = _actuelle(settings, image_id, image)
    sgdb_entrees, game, preferee, erreur = _entrees_sgdb(settings, image)
    locales = _entrees_locales(settings, image)
    entrees = sgdb_entrees + locales
    candidates = []
    for entree in entrees:
        candidates.append({
            "key": entree["key"], "source": entree["source"], "label": entree["label"],
            "author": entree["author"], "score": entree["score"], "width": entree["width"],
            "height": entree["height"],
            "thumb": _adresse(image_id, entree["key"], "thumb"),
            "full": _adresse(image_id, entree["key"], "full"),
            "same_as_current": _meme_que(settings, entree, actuelle["sha1"]),
        })
    if preferee is None and locales:
        preferee = locales[0]["key"]
    reponse = {"current": actuelle, "sgdb_configured": bool(settings.sgdb_api_key),
               "game": game, "candidates": candidates, "preferred": preferee}
    if erreur is not None:
        reponse["sgdb_error"] = erreur
    return reponse


def candidat_fichier(settings: Settings, image: Path, key: str, size: str) -> _art.Artwork:
    """Fichier d'un candidat de l'image (`size` « thumb » ou « full » pour SteamGridDB). Lève
    CandidatError : 404 si la clé n'est pas dans la liste de l'image, 502 si SteamGridDB est
    injoignable ou refuse la clé ou le téléchargement."""
    if not cle_valide(key):
        raise CandidatError("clé de candidat invalide", 404)
    if key.startswith("sgdb:"):
        entrees, _game, _preferee, erreur = _entrees_sgdb(settings, image)
        entree = next((e for e in entrees if e["key"] == key), None)
        if entree is None:
            if erreur is not None:
                raise CandidatError(f"SteamGridDB : {erreur}", 502)
            raise CandidatError("candidat inconnu pour cette image", 404)
        try:
            return sgdb.fichier_grille(settings, entree["grid_id"], entree["urls"][size], size)
        except sgdb.SgdbError as exc:
            raise CandidatError(f"SteamGridDB : {exc}", 502) from exc
    entree = next((e for e in _entrees_locales(settings, image) if e["key"] == key), None)
    if entree is None or not Path(entree["path"]).is_file():
        raise CandidatError("candidat inconnu pour cette image", 404)
    return _art.Artwork(Path(entree["path"]), entree["media"], entree["source"])
