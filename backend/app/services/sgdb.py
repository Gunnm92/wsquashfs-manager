"""Illustrations de SteamGridDB (API v2) : icônes rondes (`kind=icon`) et jaquettes 600×900
(`kind=cover`).

Le jeu est cherché d'après le nom de l'image (titre nettoyé). Puis l'image est choisie :
1. d'abord un auteur préféré (réglage `sgdb_authors`, dans l'ordre de la liste) ;
2. sinon, pour une icône, la plus « ronde » si elle atteint le seuil ; pour une jaquette,
   la mieux notée.

Cache dans `<state_dir>/art/sgdb` :
- `<clé>.<ext>` + `<clé>.json` : l'illustration retenue (sans limite de durée) ;
- `<clé>.none` : aucune illustration, revalidée après MISS_TTL ;
- `game-<sha1 du titre>.json` : id du jeu trouvé (ou introuvable), pour ne pas refaire la
  recherche pour l'autre `kind`.
Erreur réseau ou clé refusée : rien n'est mis en cache, et plus aucun appel pendant GRACE
secondes (une vignette ne bloque pas les suivantes).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

from ..config import Settings
from . import artwork as _art  # cycle : artwork importe ce module ; Artwork est lu à l'exécution

API_URL = "https://www.steamgriddb.com/api/v2"
TIMEOUT = 20                      # s
ESSAIS = 4                        # 429 : réessai avec attente 2**essai s
MISS_TTL = 7 * 86400              # s : absence revalidée après ce délai
GRACE = 300                       # s : délai sans appel après une erreur réseau ou une clé refusée
RONDE_MAX = 20                    # icônes PNG dont la rondeur est mesurée, au plus
# Formats portrait des jaquettes (600×900 en premier) ; le mime des icônes est filtré côté serveur
ICON_PARAMS = {"mimes": "image/png", "types": "static", "nsfw": "false", "humor": "false",
               "epilepsy": "false"}
COVER_PARAMS = {"dimensions": "600x900,660x930,342x482", "types": "static", "nsfw": "false",
                "humor": "false", "epilepsy": "false"}
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
_SUFFIX = {".png": ".png", ".jpg": ".jpg", ".jpeg": ".jpg", ".webp": ".webp"}

# Bouchon des tests (httpx.MockTransport) : None en production
TRANSPORT: httpx.BaseTransport | None = None

log = logging.getLogger(__name__)
_down_until = 0.0
_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class SgdbError(Exception):
    """Échec passager (réseau, réponse inattendue, clé refusée) : rien n'est mis en cache."""


class SgdbKeyError(SgdbError):
    """Clé refusée (401 ou 403)."""


# ------------------------------------------------------------------ titre et rondeur

def nettoyer_titre(nom: str) -> str:
    """Nom de jeu sans les parenthèses et crochets (« (USA) », « [v1.2] »), espaces normalisés."""
    return " ".join(re.sub(r"\s*[\(\[].*?[\)\]]", "", nom).split())


def score_rond(img: Image.Image) -> float:
    """Rondeur d'une image, de 0 à 1 : image réduite à 64×64 ; part opaque du disque
    (distance au centre < 0,85 r) × part transparente des coins (> 1,05 r)."""
    pixels = img.convert("RGBA").resize((64, 64)).getchannel("A").tobytes()
    centre, rayon = 32.0, 32.0
    interieur = exterieur = opaques = transparents = 0
    for y in range(64):
        for x in range(64):
            distance = math.hypot(x + 0.5 - centre, y + 0.5 - centre)
            alpha = pixels[y * 64 + x]
            if distance < 0.85 * rayon:
                interieur += 1
                opaques += alpha > 200
            elif distance > 1.05 * rayon:
                exterieur += 1
                transparents += alpha < 40
    if not interieur or not exterieur:
        return 0.0
    return (opaques / interieur) * (transparents / exterieur)


# ------------------------------------------------------------------ choix

def _score(item: dict) -> float:
    try:
        return float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _auteur(item: dict) -> str:
    author = item.get("author")
    return str(author.get("name") or "").lower() if isinstance(author, dict) else ""


def _trier(items: list[dict]) -> list[dict]:
    return sorted((i for i in items if isinstance(i, dict) and i.get("url")), key=lambda i: -_score(i))


def _choisir_auteur(items: list[dict], authors: list[str]) -> dict | None:
    """Premier auteur préféré (ordre de la liste, sans casse) ayant une image ; la mieux notée."""
    rang: dict[str, int] = {}
    for n, name in enumerate(authors):
        rang.setdefault(name.lower(), n)
    candidats = [i for i in items if _auteur(i) in rang]
    if not candidats:
        return None
    # items est trié par note : min() garde la mieux notée parmi les égalités de rang
    return min(candidats, key=lambda i: rang[_auteur(i)])


def _ext_url(url: str) -> str | None:
    """Extension d'une adresse d'image (None si elle n'en a pas de connue)."""
    return _SUFFIX.get(Path(url.split("?", 1)[0]).suffix.lower())


def choisir_icone(items: list[dict], authors: list[str], seuil: float,
                  telecharger: Callable[[str], tuple[bytes, str]]) -> tuple[dict, str] | None:
    """Icône retenue et sa raison, ou None. Auteur préféré d'abord ; sinon la PNG la plus
    ronde, si elle atteint `seuil` (la vignette est téléchargée pour la mesurer)."""
    trie = _trier(items)
    if (par_auteur := _choisir_auteur(trie, authors)) is not None:
        return par_auteur, "auteur préféré"
    meilleure: tuple[float, dict] | None = None
    for item in trie[:RONDE_MAX]:           # icônes PNG (filtrées par l'API)
        try:
            img = Image.open(io.BytesIO(telecharger(item.get("thumb") or item["url"])[0]))
            rondeur = score_rond(img)
        except (SgdbError, OSError, ValueError):
            continue
        if meilleure is None or rondeur > meilleure[0]:
            meilleure = (rondeur, item)
    if meilleure is None or meilleure[0] < seuil:
        return None
    return meilleure[1], f"rondeur {meilleure[0]:.2f}"


def choisir_jaquette(items: list[dict], authors: list[str]) -> tuple[dict, str] | None:
    """Jaquette retenue et sa raison, ou None : auteur préféré, sinon la mieux notée."""
    trie = _trier(items)
    if (par_auteur := _choisir_auteur(trie, authors)) is not None:
        return par_auteur, "auteur préféré"
    if trie:
        return trie[0], "meilleure note"
    return None


# ------------------------------------------------------------------ réseau

def _client(api_key: str) -> httpx.Client:
    return httpx.Client(base_url=API_URL, headers={"Authorization": f"Bearer {api_key}"},
                        timeout=TIMEOUT, transport=TRANSPORT)


def _get(client: httpx.Client, path: str, params: dict | None = None) -> list | None:
    """Données d'une réponse de l'API (liste), None si aucune (404, succès faux).
    429 : réessai ; 401/403 : SgdbKeyError ; autre échec : SgdbError."""
    for essai in range(ESSAIS):
        try:
            response = client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SgdbError(f"SteamGridDB injoignable : {exc}") from exc
        if response.status_code == 429:
            if essai + 1 < ESSAIS:
                time.sleep(2 ** essai)
                continue
            raise SgdbError("SteamGridDB : trop de requêtes (429)")
        if response.status_code in (401, 403):
            raise SgdbKeyError("clé SteamGridDB refusée")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SgdbError(f"SteamGridDB : HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SgdbError("SteamGridDB : réponse illisible") from exc
        if not isinstance(body, dict) or body.get("success") is False:
            return None
        data = body.get("data")
        return data if isinstance(data, list) else None
    return None


def _telecharger(url: str) -> tuple[bytes, str]:
    """(contenu, type MIME annoncé) d'une adresse (CDN) : sans la clé d'API."""
    try:
        with httpx.Client(timeout=TIMEOUT, transport=TRANSPORT, follow_redirects=True) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise SgdbError(f"téléchargement impossible : {exc}") from exc
    if response.status_code >= 400 or not response.content:
        raise SgdbError(f"téléchargement : HTTP {response.status_code}")
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return response.content, media


def normaliser_cle(texte: str) -> str:
    """Clé API telle que collée : la ligne entière « Key: … » de la page de SteamGridDB est
    acceptée (les 32 caractères hexadécimaux sont repris) ; sinon le texte nettoyé."""
    texte = texte.strip()
    trouve = re.search(r"\b[0-9a-fA-F]{32}\b", texte)
    return trouve.group(0).lower() if trouve else texte


def tester_cle(settings: Settings, cle: str | None = None) -> tuple[bool, str]:
    """Essai d'une clé (recherche de « Portal ») : celle saisie si `cle` est fourni, sinon la clé
    enregistrée ; (ok, message). Ne met rien en cache et n'enregistre rien."""
    cle = normaliser_cle(cle) if cle else settings.sgdb_api_key
    if not cle:
        return False, "aucune clé saisie ni enregistrée"
    try:
        with _client(cle) as client:
            data = _get(client, f"/search/autocomplete/{quote('Portal', safe='')}") or []
    except SgdbKeyError:
        return False, "clé refusée par SteamGridDB"
    except SgdbError as exc:
        return False, str(exc)
    if not data:
        return False, "clé acceptée, mais la recherche de « Portal » est vide"
    return True, f"clé valide (« Portal » : {data[0].get('name', '?')})"


# ------------------------------------------------------------------ cache

def _dossier(settings: Settings) -> Path:
    return settings.state_dir / "art" / "sgdb"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _lire_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ecrire(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(path.name + ".part")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def _retirer(base: Path, key: str) -> None:
    for old in base.glob(f"{key}.*"):
        old.unlink(missing_ok=True)


def _lire_art(base: Path, key: str) -> _art.Artwork | None:
    meta = _lire_json(base / f"{key}.json")
    if not meta or not isinstance(meta.get("file"), str):
        return None
    path = base / meta["file"]
    if not path.is_file():
        return None
    return _art.Artwork(path, str(meta.get("media_type", "image/png")), "sgdb")


def _absence_recente(base: Path, key: str) -> bool:
    meta = _lire_json(base / f"{key}.none")
    if meta is None:
        return False
    try:
        date = float(meta.get("date", 0))
    except (TypeError, ValueError):
        return False
    return time.time() - date < MISS_TTL


def _absence(base: Path, key: str, raison: str) -> None:
    _retirer(base, key)
    _ecrire(base / f"{key}.none", json.dumps({"reason": raison, "date": time.time()}))


def _verrou(key: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _grace() -> None:
    global _down_until
    _down_until = time.time() + GRACE


def _jeu(client: httpx.Client, base: Path, titre: str) -> tuple[int | None, str]:
    """(id, nom) du jeu sur SteamGridDB, ou (None, "") ; cache par titre."""
    path = base / f"game-{_sha(titre.lower())}.json"
    meta = _lire_json(path)
    if meta is not None:
        if meta.get("game_id"):
            return int(meta["game_id"]), str(meta.get("game_name", ""))
        if time.time() - float(meta.get("date", 0)) < MISS_TTL:
            return None, ""
    found = _get(client, f"/search/autocomplete/{quote(titre, safe='')}") or []
    first = found[0] if found and isinstance(found[0], dict) and "id" in found[0] else None
    game_id = int(first["id"]) if first else None
    name = str(first.get("name", "")) if first else ""
    _ecrire(path, json.dumps({"game_id": game_id, "game_name": name, "date": time.time()}))
    return game_id, name


def _cache_ou_pause(base: Path, key: str) -> tuple[bool, _art.Artwork | None]:
    """(vrai si la réponse est connue sans appel : illustration, ou None) ; faux : à demander."""
    art = _lire_art(base, key)
    if art is not None:
        return True, art
    if _absence_recente(base, key) or time.time() < _down_until:
        return True, None
    return False, None


def _recuperer(client: httpx.Client, base: Path, key: str, kind: str, titre: str,
               authors: list[str], seuil: float) -> _art.Artwork | None:
    game_id, game_name = _jeu(client, base, titre)
    if game_id is None:
        return _absence(base, key, "jeu introuvable sur SteamGridDB")
    if kind == "icon":
        items = _get(client, f"/icons/game/{game_id}", ICON_PARAMS) or []
        choix = choisir_icone(items, authors, seuil, _telecharger)
        if choix is None:
            return _absence(base, key, "aucune icône ronde ni d'auteur préféré")
        item, raison = choix
        try:
            img = Image.open(io.BytesIO(_telecharger(item["url"])[0])).convert("RGBA")
        except (OSError, ValueError):
            return _absence(base, key, "icône illisible")
        out = io.BytesIO()
        img.save(out, "PNG")
        data, ext, media, id_name = out.getvalue(), ".png", "image/png", "icon_id"
    else:
        items = _get(client, f"/grids/game/{game_id}", COVER_PARAMS) or []
        choix = choisir_jaquette(items, authors)
        if choix is None:
            return _absence(base, key, "aucune jaquette")
        item, raison = choix
        data, announced = _telecharger(item["url"])
        ext = _EXT.get(announced) or _ext_url(str(item["url"]))
        if ext is None:
            return _absence(base, key, "format de jaquette non géré")
        media, id_name = _MEDIA[ext], "grid_id"
    auteur = item.get("author") if isinstance(item.get("author"), dict) else {}
    meta = {"file": f"{key}{ext}", "media_type": media, "game_id": game_id,
            "game_name": game_name, id_name: item.get("id"),
            "author": str(auteur.get("name") or ""), "reason": raison, "fetched": time.time()}
    _retirer(base, key)
    _ecrire(base / f"{key}{ext}", data)
    _ecrire(base / f"{key}.json", json.dumps(meta))
    return _lire_art(base, key)


def sgdb_artwork(settings: Settings, image: Path, kind: str) -> _art.Artwork | None:
    """Icône (`kind` « icon ») ou jaquette (« cover ») SteamGridDB de l'image, None si aucune
    (pas de clé, absence récente, erreur passagère, ou rien de retenu)."""
    if not settings.sgdb_api_key or kind not in ("icon", "cover"):
        return None
    titre = nettoyer_titre(image.stem)
    if not titre:
        return None
    base = _dossier(settings)
    authors = list(settings.sgdb_authors)
    seuil = settings.sgdb_round_threshold
    key = _sha("|".join([kind, titre.lower(), ",".join(a.lower() for a in authors), f"{seuil:.2f}"]))

    base.mkdir(parents=True, exist_ok=True)
    fait, art = _cache_ou_pause(base, key)
    if fait:
        return art
    with _verrou(f"sgdb:{key}"):
        fait, art = _cache_ou_pause(base, key)
        if fait:
            return art
        try:
            with _client(settings.sgdb_api_key) as client:
                return _recuperer(client, base, key, kind, titre, authors, seuil)
        except SgdbError as exc:
            _grace()
            log.warning("SteamGridDB (%s) : %s", titre, exc)
            return None


def forget_sgdb_misses(settings: Settings) -> None:
    """Actualiser : absences et jeux introuvables revalidés au prochain affichage ; les
    illustrations trouvées sont gardées. Le délai de grâce est levé."""
    global _down_until
    _down_until = 0.0
    base = _dossier(settings)
    if not base.is_dir():
        return
    for none in base.glob("*.none"):
        none.unlink(missing_ok=True)
    for game in base.glob("game-*.json"):
        meta = _lire_json(game)
        if meta is not None and not meta.get("game_id"):
            game.unlink(missing_ok=True)


# ------------------------------------------------------------------ aperçu (choix manuel)

CANDIDATS_MAX = 24                # grilles proposées dans l'aperçu, au plus
MEDIA_PAR_EXTENSION = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
                       ".gif": "image/gif"}


def _ordonner(items: list[dict], authors: list[str]) -> list[dict]:
    """Grilles avec une adresse : auteurs préférés dans l'ordre de la liste, puis note décroissante."""
    rang: dict[str, int] = {}
    for n, name in enumerate(authors):
        rang.setdefault(name.lower(), n)
    return sorted(_trier(items), key=lambda i: rang.get(_auteur(i), len(rang)))


def grilles_candidates(settings: Settings, image: Path) -> tuple[dict | None, list[dict], dict | None]:
    """Pour l'aperçu : (jeu {"id", "name"} ou None, grilles triées (CANDIDATS_MAX au plus), grille
    que le choix automatique prendrait ou None). Sans clé ou jeu introuvable : rien, sans appel
    au-delà de la recherche. Lève SgdbError (réseau, clé refusée)."""
    titre = nettoyer_titre(image.stem)
    if not settings.sgdb_api_key or not titre:
        return None, [], None
    base = _dossier(settings)
    base.mkdir(parents=True, exist_ok=True)
    authors = list(settings.sgdb_authors)
    with _client(settings.sgdb_api_key) as client:
        game_id, game_name = _jeu(client, base, titre)
        if game_id is None:
            return None, [], None
        items = _get(client, f"/grids/game/{game_id}", COVER_PARAMS) or []
    retenue = choisir_jaquette(items, authors)
    return ({"id": game_id, "name": game_name}, _ordonner(items, authors)[:CANDIDATS_MAX],
            retenue[0] if retenue else None)


def format_image(data: bytes) -> tuple[str, str] | None:
    """(extension, type MIME) d'après les octets ; None si ce n'est pas une image gérée."""
    for magic, ext, media in ((b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
                              (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
                              (b"GIF87a", ".gif", "image/gif"), (b"GIF89a", ".gif", "image/gif")):
        if data.startswith(magic):
            return ext, media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def grille_en_cache(settings: Settings, grid_id: int, size: str) -> _art.Artwork | None:
    """Image d'une grille déjà téléchargée (`candidates/<id>-<size>.<ext>`), sans appel."""
    base = _dossier(settings) / "candidates"
    for path in base.glob(f"{int(grid_id)}-{size}.*"):
        if path.suffix in MEDIA_PAR_EXTENSION and path.is_file():
            return _art.Artwork(path, MEDIA_PAR_EXTENSION[path.suffix], "sgdb")
    return None


def fichier_grille(settings: Settings, grid_id: int, url: str, size: str) -> _art.Artwork:
    """Image d'une grille (`size` « thumb » ou « full »), téléchargée depuis `url` (adresse
    tirée de la liste des candidats de l'image, jamais de la requête) puis mise en cache.
    Lève SgdbError."""
    if (cached := grille_en_cache(settings, grid_id, size)) is not None:
        return cached
    data, _announced = _telecharger(url)
    fmt = format_image(data)
    if fmt is None:
        raise SgdbError("format d'image non géré")
    ext, media = fmt
    base = _dossier(settings) / "candidates"
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"{int(grid_id)}-{size}{ext}"
    _ecrire(out, data)
    return _art.Artwork(out, media, "sgdb")
