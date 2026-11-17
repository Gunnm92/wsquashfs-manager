"""Aperçu de l'illustration : les candidats proposés avant un remplacement, et le choix imposé à
la reconstruction. Vaut pour une image (ligne de la ludothèque) et pour un dossier à empaqueter.

Candidats d'une cible, dans cet ordre :
- SteamGridDB (`sgdb:<id de la grille>`) : grilles portrait, auteurs préférés d'abord, puis note
  décroissante, 24 au plus (services/sgdb.py) ; liste gardée CANDIDATS_TTL secondes en mémoire ;
- jaquette d'EmulationStation (`es`), si elle existe ; icône de l'exécutable (`icon`), si elle
  existe. Locaux : images seulement (un dossier n'a ni gamelist ni exécutable analysé ici).

Nom de recherche : le titre de la cible, sauf nom alternatif mémorisé (sgdb.alias_de) : le nom
et le jeu choisis remplacent le titre pour l'aperçu ET pour le choix automatique.

Sécurité : une image n'est téléchargée que depuis une adresse de SA liste de candidats
(recalculée si elle a expiré) ; la requête ne fournit qu'une clé, jamais une adresse.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image

from ..config import Settings
from . import artwork as _art
from . import scan, sgdb
from .autorun import Autorun, decode_autorun
from .operations import folder_stem

CANDIDATS_TTL = 300                 # s : liste SteamGridDB d'une cible gardée ce délai
_CLE = re.compile(r"sgdb:\d+|es|icon")

_listes: dict[str, tuple[float, tuple]] = {}
_garde = threading.Lock()


class CandidatError(Exception):
    """Candidat introuvable (`status` 404) ou indisponible (502)."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Cible:
    """Ce que l'aperçu illustre : une image (`cle` = son id, « système/nom ») ou un dossier à
    empaqueter (`cle` = son id, « système/Nom.pc »)."""
    cle: str
    path: Path
    dossier: bool = False

    def defaut(self) -> str:
        """Titre de recherche par défaut : le nom sans suffixe (.pc, .wine pour un dossier)."""
        nom = folder_stem(self.path.name) if self.dossier else self.path.stem
        return sgdb.nettoyer_titre(nom)

    def url_actuelle(self) -> str:
        base = "/api/folders/art" if self.dossier else "/api/image/art"
        return f"{base}?{urlencode({'id': self.cle})}"

    def url_candidat(self, key: str, size: str) -> str:
        base = "/api/folders/art/candidate" if self.dossier else "/api/image/art/candidate"
        return f"{base}?{urlencode({'id': self.cle, 'key': key, 'size': size})}"


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

def _sgdb(settings: Settings, cible: Cible, titre: str, gid: int | None) -> tuple:
    """(jeu, jeux trouvés, grilles, clé préférée, erreur) ; les grilles gardent leurs adresses
    (interne). Liste gardée par nom et jeu demandés."""
    if not settings.sgdb_api_key or not titre:
        return None, [], [], None, None
    now = time.time()
    cache_key = f"{cible.path}|{titre.lower()}|{gid}"
    with _garde:
        cached = _listes.get(cache_key)
    if cached is not None and now - cached[0] < CANDIDATS_TTL:
        return cached[1]
    try:
        jeu, jeux, grilles, retenue = sgdb.grilles_candidates(settings, titre, gid)
        pref = None if retenue is None or _entier(retenue.get("id")) is None else f"sgdb:{retenue['id']}"
        result = (jeu, jeux, grilles, pref, None)
    except sgdb.SgdbError as exc:
        result = (None, [], [], None, str(exc))
    with _garde:
        _listes[cache_key] = (now, result)
    return result


def _entrees_sgdb(settings: Settings, cible: Cible, titre: str, gid: int | None) -> tuple:
    """(entrées, jeu, jeux, clé préférée, erreur) : entrées de l'aperçu pour SteamGridDB."""
    jeu, jeux, grilles, preferee, erreur = _sgdb(settings, cible, titre, gid)
    entrees = []
    for grille in grilles:
        grid_id = _entier(grille.get("id"))
        if grid_id is None or not isinstance(grille.get("url"), str):
            continue
        auteur = grille.get("author") if isinstance(grille.get("author"), dict) else {}
        nom = str(auteur.get("name") or "") or None
        try:
            score = int(float(grille.get("score")))
        except (TypeError, ValueError):
            score = None
        entrees.append({
            "key": f"sgdb:{grid_id}", "source": "sgdb",
            "label": "SteamGridDB" + (f", par {nom}" if nom else ""),
            "author": nom, "score": score, "width": _entier(grille.get("width")),
            "height": _entier(grille.get("height")), "grid_id": grid_id,
            "urls": {"thumb": str(grille.get("thumb") or grille["url"]), "full": grille["url"]},
        })
    return entrees, jeu, jeux, preferee, erreur


# ------------------------------------------------------------------ locaux (ES, icône)

def _cmd_dir(settings: Settings, image: Path) -> tuple[str | None, str | None]:
    data = scan.read_autorun_bytes(settings, image)
    a = Autorun.parse(decode_autorun(data)[0]) if data is not None else None
    return (a.get("CMD") if a else None), (a.get("DIR") if a else None)


def _entrees_locales(settings: Settings, cible: Cible) -> list[dict]:
    if cible.dossier:
        return []
    image = cible.path
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

def _actuelle(settings: Settings, cible: Cible) -> dict:
    art = _art.folder_cover(cible.path) if cible.dossier else _art.embedded_cover(settings, cible.path)
    if art is None:
        return {"present": False, "sha1": None, "url": None}
    return {"present": True, "sha1": _sha1(art.path), "url": cible.url_actuelle()}


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


def _requete(settings: Settings, cible: Cible, q: str | None, game_id: int | None) -> tuple[str, int | None]:
    """(nom, id du jeu forcé) de la recherche. `q` : nom saisi (None : celui mémorisé ou le
    titre) ; `game_id` : jeu choisi (None : celui mémorisé, sinon le premier trouvé)."""
    titre, memorise = sgdb.requete_de(settings, cible.cle, cible.defaut())
    if q is not None:
        titre, memorise = q.strip(), None
    gid = game_id if game_id is not None else (memorise[0] if memorise else None)
    return titre, gid


def apercu(settings: Settings, cible: Cible, q: str | None = None,
           game_id: int | None = None) -> dict:
    """Réponse de GET …/art/candidates (voir la documentation de l'API dans le README).
    `q` vide (« Revenir au nom de l'image ») oublie le nom mémorisé. Un nom saisi (`q`) ou un
    jeu choisi (`game_id`) est mémorisé s'il trouve un jeu, pour l'aperçu et le choix automatique."""
    defaut = cible.defaut()
    if q is not None and not q.strip():
        sgdb.oublier_alias(settings, cible.cle)
        q, game_id = None, None
    titre, gid = _requete(settings, cible, q, game_id)
    if settings.sgdb_api_key:
        sgdb_entrees, jeu, jeux, preferee, erreur = _entrees_sgdb(settings, cible, titre, gid)
    else:
        sgdb_entrees, jeu, jeux, preferee, erreur = [], None, [], None, None
    if (q is not None or game_id is not None) and jeu is not None and erreur is None:
        sgdb.memoriser_alias(settings, cible.cle, titre, jeu)
    actuelle = _actuelle(settings, cible)
    locales = _entrees_locales(settings, cible)
    entrees = sgdb_entrees + locales
    candidates = []
    for entree in entrees:
        candidates.append({
            "key": entree["key"], "source": entree["source"], "label": entree["label"],
            "author": entree["author"], "score": entree["score"], "width": entree["width"],
            "height": entree["height"],
            "thumb": cible.url_candidat(entree["key"], "thumb"),
            "full": cible.url_candidat(entree["key"], "full"),
            "same_as_current": _meme_que(settings, entree, actuelle["sha1"]),
        })
    if preferee is None and locales:
        preferee = locales[0]["key"]
    alias = sgdb.alias_de(settings, cible.cle)
    reponse = {"current": actuelle, "sgdb_configured": bool(settings.sgdb_api_key),
               "game": jeu, "games": jeux, "query": titre, "default_query": defaut,
               "alias": {"query": alias["query"], "game_id": alias["game_id"],
                         "game_name": alias["game_name"]} if alias else None,
               "candidates": candidates, "preferred": preferee}
    if erreur is not None:
        reponse["sgdb_error"] = erreur
    return reponse


def candidat_fichier(settings: Settings, cible: Cible, key: str, size: str) -> _art.Artwork:
    """Fichier d'un candidat de la cible (`size` « thumb » ou « full » pour SteamGridDB). Lève
    CandidatError : 404 si la clé n'est pas dans la liste de la cible, 502 si SteamGridDB est
    injoignable ou refuse la clé ou le téléchargement. Le nom et le jeu mémorisés s'appliquent,
    comme à l'aperçu."""
    if not cle_valide(key):
        raise CandidatError("clé de candidat invalide", 404)
    if key.startswith("sgdb:"):
        titre, gid = _requete(settings, cible, None, None)
        entrees, _jeu, _jeux, _pref, erreur = _entrees_sgdb(settings, cible, titre, gid)
        entree = next((e for e in entrees if e["key"] == key), None)
        if entree is None:
            if erreur is not None:
                raise CandidatError(f"SteamGridDB : {erreur}", 502)
            raise CandidatError("candidat inconnu pour cette image", 404)
        try:
            return sgdb.fichier_grille(settings, entree["grid_id"], entree["urls"][size], size)
        except sgdb.SgdbError as exc:
            raise CandidatError(f"SteamGridDB : {exc}", 502) from exc
    entree = next((e for e in _entrees_locales(settings, cible) if e["key"] == key), None)
    if entree is None or not Path(entree["path"]).is_file():
        raise CandidatError("candidat inconnu pour cette image", 404)
    return _art.Artwork(Path(entree["path"]), entree["media"], entree["source"])
