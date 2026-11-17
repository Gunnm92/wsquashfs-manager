"""Aperçu de l'illustration (choix manuel) : liste des candidats, cache, routes, règles de
remplacement en masse et choix imposé à la reconstruction. Aucun appel réseau réel :
httpx.MockTransport remplace le transport de SteamGridDB ; rien n'est monté (unsquashfs
absent ici : outils remplacés par monkeypatch)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import deps, main
from app.config import Settings
from app.models import ImageInfo, Task
from app.routes import library, mass
from app.services import art_choice, artwork, scan, sgdb
from app.services import rebuild as rebuild_mod
from app.services.operations import run_cover
from app.services.rebuild import COVER, Job, RebuildError
from app.services.tasks import TaskQueue


def _icone_ronde(couleur: tuple[int, int, int]) -> bytes:
    """Icône PNG ronde (disque opaque sur fond transparent) : passe le seuil de rondeur."""
    import io

    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, 63, 63), fill=(*couleur, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


PNG1 = _icone_ronde((200, 40, 40))
PNG2 = _icone_ronde((40, 40, 200))
ICON = b"\x89PNG\r\n\x1a\n" + b"icone" * 20
ID_IMAGE = "windows/Portal2"


# ------------------------------------------------------------------ faux SteamGridDB


class Faux:
    """API (`/icons/game/…`, recherche) et CDN : adresses `https://cdn.test/…`."""

    def __init__(self):
        self.grids: list[dict] = []
        self.cdn: dict[str, tuple[bytes, str]] = {}
        self.calls: list[str] = []
        self.downloads: list[str] = []
        self.status: int | None = None        # statut forcé pour l'API
        self.cdn_status: int | None = None    # statut forcé pour les téléchargements
        # résultats de l'autocomplétion par nom (défaut : « Portal », id 7) ; grilles par jeu
        self.jeux: dict[str, list[dict]] = {}
        self.grilles_par_jeu: dict[int, list[dict]] = {}

    def add_grid(self, id_: int, score: int, author: str, data: bytes) -> None:
        full, thumb = f"https://cdn.test/grid{id_}.png", f"https://cdn.test/thumb{id_}.png"
        self.cdn[full] = (data, "image/png")
        self.cdn[thumb] = (data, "image/png")
        self.grids.append({"id": id_, "score": score, "url": full, "thumb": thumb,
                           "width": 600, "height": 900, "author": {"name": author}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.steamgriddb.com":
            url = str(request.url)
            self.downloads.append(url)
            if self.cdn_status:
                return httpx.Response(self.cdn_status)
            data, mime = self.cdn[url]
            return httpx.Response(200, content=data, headers={"content-type": mime})
        path = request.url.path.split("/api/v2", 1)[-1]
        self.calls.append(path)
        if self.status:
            return httpx.Response(self.status, json={"success": False})
        if path.startswith("/search/autocomplete/"):
            nom = request.url.path.rsplit("/", 1)[-1]
            from urllib.parse import unquote
            nom = unquote(nom)
            data = self.jeux.get(nom, [{"id": 7, "name": "Portal"}])
            return httpx.Response(200, json={"success": True, "data": data})
        if path.startswith("/icons/game/"):
            jeu = int(path.rsplit("/", 1)[-1])
            data = self.grilles_par_jeu.get(jeu, self.grids)
            return httpx.Response(200, json={"success": True, "data": data})
        return httpx.Response(404)

    def count(self, prefix: str) -> int:
        return sum(1 for path in self.calls if path.startswith(prefix))


@pytest.fixture(autouse=True)
def etat_propre(monkeypatch):
    monkeypatch.setattr(sgdb, "_down_until", 0.0)
    sgdb._locks.clear()
    art_choice.oublier()
    monkeypatch.setattr(sgdb.time, "sleep", lambda seconds: None)
    # aucun appel réel : le transport par défaut refuse tout (les tests SteamGridDB le remplacent)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(503)))
    # par défaut : ni illustration intégrée, ni icône d'exécutable, ni autorun lisible
    monkeypatch.setattr(artwork, "embedded_cover", lambda settings, image: None)
    monkeypatch.setattr(artwork, "exe_icon", lambda settings, image, cmd, dir_: None)
    monkeypatch.setattr(art_choice, "_cmd_dir", lambda settings, image: (None, None))


@pytest.fixture
def api(monkeypatch) -> Faux:
    faux = Faux()
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(faux))
    return faux


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(state_dir=tmp_path / "state", runtime_config_file=tmp_path / "config.json",
                    sgdb_api_key="cle-secrete", sgdb_authors=[], sgdb_round_threshold=0.85)


@pytest.fixture
def image(tmp_path) -> Path:
    path = tmp_path / "roms" / "windows" / "Portal2.wsquashfs"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def _es(image: Path, data: bytes = b"es-jaquette") -> Path:
    """Jaquette d'EmulationStation : images/<jeu>-thumb.png (chemin cherché par es_artwork)."""
    target = image.parent / "images" / f"{image.stem}-thumb.png"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)
    return target


def _cible(image: Path) -> art_choice.Cible:
    return art_choice.Cible(ID_IMAGE, image)


def _apercu(cfg: Settings, image: Path, **kwargs) -> dict:
    return art_choice.apercu(cfg, _cible(image), **kwargs)


def _cles(reponse: dict) -> list[str]:
    return [c["key"] for c in reponse["candidates"]]


# ------------------------------------------------------------------ liste des candidats


def test_liste_auteurs_preferes_puis_note(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    api.add_grid(2, 5, "alice", PNG2)
    api.add_grid(3, 7, "Carl", PNG1)
    cfg.sgdb_authors = ["Alice", "Bob"]
    reponse = _apercu(cfg, image)
    assert _cles(reponse) == ["sgdb:2", "sgdb:1", "sgdb:3"]
    assert reponse["preferred"] == "sgdb:2"              # ce que le choix automatique prendrait
    assert reponse["game"] == {"id": 7, "name": "Portal"}
    assert reponse["sgdb_configured"] is True and "sgdb_error" not in reponse
    premier = reponse["candidates"][0]
    assert premier["author"] == "alice" and premier["score"] == 5
    assert premier["width"] == 600 and premier["height"] == 900
    assert premier["source"] == "sgdb" and premier["same_as_current"] is False
    assert premier["thumb"].startswith("/api/image/art/candidate?") and "size=thumb" in premier["thumb"]
    assert "size=full" in premier["full"] and "key=sgdb%3A2" in premier["thumb"]


def test_sans_auteur_prefere_la_mieux_notee_est_preferee(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    api.add_grid(2, 5, "alice", PNG2)
    api.add_grid(3, 7, "Carl", PNG1)
    reponse = _apercu(cfg, image)
    assert _cles(reponse) == ["sgdb:1", "sgdb:3", "sgdb:2"]
    assert reponse["preferred"] == "sgdb:1"


def test_icone_et_es_ajoutees_apres_steamgriddb(cfg, api, image, monkeypatch):
    api.add_grid(1, 9, "Bob", PNG1)
    _es(image)
    icone = image.parent / "icone.png"
    icone.write_bytes(ICON)
    monkeypatch.setattr(artwork, "exe_icon",
                        lambda settings, img, cmd, dir_: artwork.Artwork(icone, "image/png", "icon"))
    reponse = _apercu(cfg, image)
    assert _cles(reponse) == ["sgdb:1", "icon", "es"]     # ordre du choix automatique d'une icône
    es = reponse["candidates"][2]
    assert es["label"] == "Jaquette EmulationStation" and es["author"] is None and es["score"] is None
    assert reponse["candidates"][1]["label"] == "Icône de l'exécutable"
    assert reponse["preferred"] == "sgdb:1"


def test_sans_steamgriddb_icone_puis_es_et_preferee_locale(cfg, api, image, monkeypatch):
    cfg.sgdb_api_key = ""
    _es(image)
    icone = image.parent / "icone.png"
    icone.write_bytes(ICON)
    monkeypatch.setattr(artwork, "exe_icon",
                        lambda settings, img, cmd, dir_: artwork.Artwork(icone, "image/png", "icon"))
    reponse = _apercu(cfg, image)
    assert reponse["sgdb_configured"] is False and reponse["game"] is None
    assert _cles(reponse) == ["icon", "es"] and reponse["preferred"] == "icon"
    assert "sgdb_error" not in reponse
    assert api.calls == []                               # rien demandé au site


def test_sans_cle_et_sans_locale_rien_a_proposer(cfg, api, image):
    cfg.sgdb_api_key = ""
    reponse = _apercu(cfg, image)
    assert reponse["candidates"] == [] and reponse["preferred"] is None
    assert api.calls == []


def test_erreur_reseau_donne_sgdb_error_sans_candidats_sgdb(cfg, image, monkeypatch):
    def panne(request):
        raise httpx.ConnectError("hors ligne", request=request)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(panne))
    _es(image)
    reponse = _apercu(cfg, image)
    assert reponse["sgdb_error"].startswith("SteamGridDB injoignable")
    assert _cles(reponse) == ["es"] and reponse["preferred"] == "es"
    assert reponse["sgdb_configured"] is True


def test_cle_refusee_donne_sgdb_error(cfg, api, image):
    api.status = 401
    reponse = _apercu(cfg, image)
    assert reponse["sgdb_error"] == "clé SteamGridDB refusée"
    assert reponse["candidates"] == []


def test_liste_gardee_cinq_minutes(cfg, api, image, monkeypatch):
    api.add_grid(1, 9, "Bob", PNG1)
    horloge = [1000.0]
    monkeypatch.setattr(art_choice, "time", SimpleNamespace(time=lambda: horloge[0]))
    _apercu(cfg, image)
    _apercu(cfg, image)
    assert api.count("/icons/game/") == 1                # deuxième ouverture : sans appel
    horloge[0] += art_choice.CANDIDATS_TTL + 1
    _apercu(cfg, image)
    assert api.count("/icons/game/") == 2
    art_choice.oublier()                                 # « Actualiser »
    _apercu(cfg, image)
    assert api.count("/icons/game/") == 3


def test_same_as_current(cfg, api, image, monkeypatch):
    """Octets identiques à l'illustration intégrée : SteamGridDB seulement si son plein format
    est déjà en cache ; une jaquette ES comparée à son sha1."""
    api.add_grid(1, 9, "Bob", PNG1)
    integree = image.parent / "integree.png"
    integree.write_bytes(PNG1)
    monkeypatch.setattr(artwork, "embedded_cover",
                        lambda settings, img: artwork.Artwork(integree, "image/png", "embedded"))
    _es(image, PNG1)
    reponse = _apercu(cfg, image)
    assert reponse["current"]["present"] is True
    assert reponse["current"]["url"] == "/api/image/art?id=windows%2FPortal2"
    assert len(reponse["current"]["sha1"]) == 40
    par_cle = {c["key"]: c["same_as_current"] for c in reponse["candidates"]}
    assert par_cle == {"sgdb:1": False, "es": True}      # plein format non téléchargé : faux

    art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "full")   # mis en cache
    par_cle = {c["key"]: c["same_as_current"] for c in _apercu(cfg, image)["candidates"]}
    assert par_cle["sgdb:1"] is True


def test_sans_illustration_integree(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    reponse = _apercu(cfg, image)
    assert reponse["current"] == {"present": False, "sha1": None, "url": None}
    assert reponse["candidates"][0]["same_as_current"] is False


# ------------------------------------------------------------------ fichiers des candidats


def test_candidat_sgdb_thumb_mis_en_cache(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    art = art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "thumb")
    assert art.kind == "sgdb" and art.media_type == "image/png"
    assert art.path == cfg.state_dir / "art" / "sgdb" / "candidates-icons" / "1-thumb.png"
    assert art.path.read_bytes() == PNG1
    art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "thumb")
    assert api.downloads == ["https://cdn.test/thumb1.png"]   # une seule fois


def test_candidat_format_inconnu_refuse(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    api.cdn["https://cdn.test/grid1.png"] = (b"<html>pas une image</html>", "text/html")
    with pytest.raises(art_choice.CandidatError) as exc:
        art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "full")
    assert exc.value.status == 502 and "format" in str(exc.value)
    assert not list((cfg.state_dir / "art" / "sgdb" / "candidates").glob("1-full.*"))


def test_candidat_es_et_icone(cfg, api, image, monkeypatch):
    es = _es(image, PNG2)
    icone = image.parent / "icone.png"
    icone.write_bytes(ICON)
    monkeypatch.setattr(artwork, "exe_icon",
                        lambda settings, img, cmd, dir_: artwork.Artwork(icone, "image/png", "icon"))
    assert art_choice.candidat_fichier(cfg, _cible(image), "es", "full").path == es.resolve()
    assert art_choice.candidat_fichier(cfg, _cible(image), "icon", "thumb").path == icone
    assert api.calls == []


def test_candidat_inconnu_ou_cle_invalide_404(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    for cle in ("sgdb:999", "es", "bidule", "https://evil.test/x", "../../etc/passwd"):
        with pytest.raises(art_choice.CandidatError) as exc:
            art_choice.candidat_fichier(cfg, _cible(image), cle, "full")
        assert exc.value.status == 404, cle
    assert set(api.downloads) <= {"https://cdn.test/thumb1.png"}     # seule la mesure de rondeur


def test_candidat_sgdb_panne_donne_502(cfg, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    api.cdn_status = 500
    with pytest.raises(art_choice.CandidatError) as exc:
        art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "thumb")
    assert exc.value.status == 502
    api.cdn_status = None
    api.status = 403
    art_choice.oublier()
    with pytest.raises(art_choice.CandidatError) as exc:
        art_choice.candidat_fichier(cfg, _cible(image), "sgdb:1", "thumb")
    assert exc.value.status == 502 and "SteamGridDB" in str(exc.value)


# ------------------------------------------------------------------ routes


@pytest.fixture
def client(cfg, image, monkeypatch):
    monkeypatch.setattr(deps, "_settings", cfg)
    info = ImageInfo(id=ID_IMAGE, path=image, name="Portal2", system="windows", size=1)
    monkeypatch.setattr(deps, "_find_image", lambda image_id: info)
    return TestClient(main.app, headers={"x-wsfs-token": ""})


def test_route_candidates_forme_de_la_reponse(client, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    reponse = client.get("/api/image/art/candidates", params={"id": ID_IMAGE})
    assert reponse.status_code == 200
    data = reponse.json()
    assert set(data) == {"current", "sgdb_configured", "game", "games", "query", "default_query",
                         "alias", "candidates", "preferred"}
    assert data["candidates"][0]["thumb"].startswith("/api/image/art/candidate?")
    assert "cle-secrete" not in reponse.text


def test_route_candidate_thumb_servi_et_mis_en_cache(client, api, cfg, image):
    api.add_grid(1, 9, "Bob", PNG1)
    premiere = client.get("/api/image/art/candidate",
                          params={"id": ID_IMAGE, "key": "sgdb:1", "size": "thumb"})
    assert premiere.status_code == 200 and premiere.content == PNG1
    assert premiere.headers["content-type"] == "image/png"
    seconde = client.get("/api/image/art/candidate", params={"id": ID_IMAGE, "key": "sgdb:1"})
    assert seconde.content == PNG1                       # size par défaut : thumb
    assert api.downloads == ["https://cdn.test/thumb1.png"]
    assert (cfg.state_dir / "art" / "sgdb" / "candidates-icons" / "1-thumb.png").is_file()


def test_route_candidate_cle_inconnue_404(client, api):
    api.add_grid(1, 9, "Bob", PNG1)
    assert client.get("/api/image/art/candidate",
                      params={"id": ID_IMAGE, "key": "sgdb:999"}).status_code == 404
    assert client.get("/api/image/art/candidate",
                      params={"id": ID_IMAGE, "key": "es"}).status_code == 404
    assert set(api.downloads) <= {"https://cdn.test/thumb1.png"}     # seule la mesure de rondeur


def test_route_url_hors_liste_impossible(client, api):
    """La requête ne fournit jamais d'adresse : seule celle de la liste est suivie."""
    api.add_grid(1, 9, "Bob", PNG1)
    reponse = client.get("/api/image/art/candidate", params={
        "id": ID_IMAGE, "key": "sgdb:1", "size": "full", "url": "https://evil.test/x"})
    assert reponse.status_code == 200 and reponse.content == PNG1
    assert api.downloads == ["https://cdn.test/thumb1.png", "https://cdn.test/grid1.png"]  # mesure, puis choix
    assert client.get("/api/image/art/candidate", params={
        "id": ID_IMAGE, "key": "https://evil.test/x"}).status_code == 404
    assert client.get("/api/image/art/candidate", params={
        "id": ID_IMAGE, "key": "sgdb:1", "size": "gros"}).status_code == 422


def test_route_candidate_es_et_erreur_sgdb(client, api, image):
    _es(image, PNG2)
    reponse = client.get("/api/image/art/candidate", params={"id": ID_IMAGE, "key": "es"})
    assert reponse.status_code == 200 and reponse.content == PNG2
    assert reponse.headers["x-art-kind"] == "es"
    api.status = 401
    art_choice.oublier()
    assert client.get("/api/image/art/candidate",
                      params={"id": ID_IMAGE, "key": "sgdb:1"}).status_code == 502


def test_route_actualiser_vide_la_liste(client, api, image):
    api.add_grid(1, 9, "Bob", PNG1)
    client.get("/api/image/art/candidates", params={"id": ID_IMAGE})
    assert art_choice._listes
    assert client.post("/api/image/art/forget").json() == {"ok": True}
    assert art_choice._listes == {}


# ------------------------------------------------------------------ remplacement en masse


@pytest.fixture
def masse(tmp_path, monkeypatch):
    """Deux images, file de tâches réelle ; `illustrees` : images ayant déjà une illustration."""
    queue = TaskQueue(tmp_path / "state" / "tasks.json", deps.queue.handlers)
    monkeypatch.setattr(deps, "queue", queue)
    images = {}
    for nom in ("a", "b"):
        path = tmp_path / "roms" / "win" / f"{nom}.wsquashfs"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        images[f"win/{nom}"] = ImageInfo(id=f"win/{nom}", path=path, name=nom, system="win",
                                         size=3)
    illustrees: set[str] = set()

    def trouver(image_id):
        if image_id not in images:
            raise HTTPException(status_code=404, detail=f"image inconnue : {image_id}")
        return images[image_id]

    monkeypatch.setattr(deps, "_find_image", trouver)
    # tâches de la file jamais réelles : arborescence illisible (aucun outil ici), rien au réseau
    monkeypatch.setattr(scan, "_list_files_uncached", lambda settings, img: None)
    # embedded_cover est importée dans les deux routeurs qui l'appellent
    for routeur in (library, mass):
        monkeypatch.setattr(routeur, "embedded_cover", lambda settings, path: (
            artwork.Artwork(path, "image/png", "embedded") if path.stem in illustrees else None))
    client = TestClient(main.app, headers={"x-wsfs-token": ""})
    return SimpleNamespace(client=client, queue=queue, illustrees=illustrees)


REFUS = "choisir l'illustration dans l'aperçu avant de remplacer"


def test_choix_porte_dans_params_de_la_tache(masse):
    masse.illustrees.add("a")
    body = {"ids": ["win/a"], "replace": True, "choices": {"win/a": "sgdb:1"}}
    reponse = masse.client.post("/api/mass/cover", json=body).json()
    assert reponse["skipped"] == [] and len(reponse["tasks"]) == 1
    tache = masse.queue.get(reponse["tasks"][0])
    assert tache.kind == "cover" and tache.params == {"replace": True, "choice": "sgdb:1"}


def test_remplacement_sans_choix_refuse_si_illustree(masse):
    masse.illustrees.add("a")
    reponse = masse.client.post("/api/mass/cover",
                                json={"ids": ["win/a", "win/b"], "replace": True}).json()
    assert reponse["skipped"] == [{"id": "win/a", "reason": REFUS}]
    assert [masse.queue.get(t).image for t in reponse["tasks"]] == ["win/b"]   # sans illustration
    assert masse.queue.get(reponse["tasks"][0]).params == {"replace": True}


def test_ajout_sans_choix_inchange(masse):
    masse.illustrees.add("a")
    reponse = masse.client.post("/api/mass/cover", json={"ids": ["win/a"]}).json()
    assert reponse["skipped"] == [] and len(reponse["tasks"]) == 1
    assert masse.queue.get(reponse["tasks"][0]).params == {"replace": False}


def test_choix_de_cle_invalide_422(masse):
    reponse = masse.client.post("/api/mass/cover", json={
        "ids": ["win/a"], "replace": True, "choices": {"win/a": "https://evil.test/x"}})
    assert reponse.status_code == 422


# ------------------------------------------------------------------ reconstruction avec choix


def _run_setup(monkeypatch, tmp_path: Path, *, present: bool, current: bytes | None):
    """Image factice, arborescence et reconstruction monkeypatchées. Pas de repli possible : la
    sélection automatique (artwork) lève si elle est appelée."""
    from app.services import artwork as art_mod
    image = tmp_path / "roms" / "win" / "Portal2.wsquashfs"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"squashfs")
    files = ["Game/game.exe", "autorun.cmd"] + ([".wsquashfs", COVER] if present else [])
    monkeypatch.setattr(scan, "_list_files_uncached", lambda settings, img: files)
    monkeypatch.setattr(scan, "image_in_use", lambda img: False)
    monkeypatch.setattr(scan, "_unsquashfs_cat", lambda settings, img, member: current)

    def interdit(*args, **kwargs):
        raise AssertionError("sélection automatique appelée alors qu'un choix est imposé")
    monkeypatch.setattr(art_mod, "artwork", interdit)
    calls = []

    def fake_rebuild(settings, img, changes, job=None, in_use=None):
        calls.append(changes)
        return rebuild_mod.RebuildResult(mode="extract", size_before=1, size_after=1,
                                         backup=img.with_name(img.name + ".old"), duration=0.0)
    monkeypatch.setattr(rebuild_mod, "rebuild", fake_rebuild)
    return image, calls


def _cover_task(image: Path, params: dict) -> Task:
    return Task(id="c", kind="cover", image="win/Portal2", image_path=str(image), params=params)


def test_run_cover_utilise_exactement_le_choix(monkeypatch, tmp_path, cfg, api):
    api.add_grid(1, 9, "Bob", PNG1)
    api.add_grid(2, 8, "Carl", PNG2)
    image, calls = _run_setup(monkeypatch, tmp_path, present=True, current=b"ancienne")
    message = run_cover(cfg, _cover_task(image, {"replace": True, "choice": "sgdb:2"}), Job())
    assert message.startswith("Illustration remplacée (SteamGridDB, jaquette)")
    assert len(calls) == 1
    fichier = calls[0].copy[COVER]
    assert fichier.read_bytes() == PNG2                  # pas la mieux notée (sgdb:1)
    assert fichier == cfg.state_dir / "art" / "sgdb" / "candidates-icons" / "2-full.png"
    assert calls[0].write == {} and calls[0].delete == []


def test_run_cover_choix_identique_a_l_integree(monkeypatch, tmp_path, cfg, api):
    api.add_grid(1, 9, "Bob", PNG1)
    image, calls = _run_setup(monkeypatch, tmp_path, present=True, current=PNG1)
    message = run_cover(cfg, _cover_task(image, {"replace": True, "choice": "sgdb:1"}), Job())
    assert message == "Illustration déjà à jour : rien à faire"
    assert calls == []


def test_run_cover_choix_introuvable_echoue_sans_repli(monkeypatch, tmp_path, cfg, api):
    api.add_grid(1, 9, "Bob", PNG1)
    image, calls = _run_setup(monkeypatch, tmp_path, present=True, current=b"ancienne")
    with pytest.raises(RebuildError, match="aucune autre source"):
        run_cover(cfg, _cover_task(image, {"replace": True, "choice": "sgdb:999"}), Job())
    assert calls == []


def test_run_cover_choix_indisponible_echoue_sans_repli(monkeypatch, tmp_path, cfg, api):
    api.add_grid(1, 9, "Bob", PNG1)
    api.cdn_status = 500
    image, calls = _run_setup(monkeypatch, tmp_path, present=True, current=b"ancienne")
    with pytest.raises(RebuildError, match="indisponible"):
        run_cover(cfg, _cover_task(image, {"replace": True, "choice": "sgdb:1"}), Job())
    assert calls == []


def test_run_cover_choix_es_absent_echoue(monkeypatch, tmp_path, cfg, api):
    image, calls = _run_setup(monkeypatch, tmp_path, present=True, current=b"ancienne")
    with pytest.raises(RebuildError, match="indisponible"):
        run_cover(cfg, _cover_task(image, {"replace": True, "choice": "es"}), Job())
    assert calls == []


# ------------------------------------------------------------------ noms alternatifs


@pytest.fixture
def roms(cfg, image) -> Settings:
    """Ludothèque réelle de la forme roms/<système>/<image> : l'id de l'image est windows/Portal2."""
    cfg.roms_dirs = [str(image.parents[1])]
    return cfg


def _jeux_portal(api):
    """« Portal 2 » trouve les jeux 42 (grille 5) et 43 (sans grille) ; « Portal » (7) : grille 1."""
    api.jeux["Portal 2"] = [{"id": 42, "name": "Portal 2"}, {"id": 43, "name": "Portal 2 (dev)"}]
    api.add_grid(5, 9, "Bob", PNG2)
    api.grilles_par_jeu[42] = [api.grids.pop()]
    api.grilles_par_jeu[43] = []
    api.add_grid(1, 8, "Carl", PNG1)
    api.grilles_par_jeu[7] = [api.grids.pop()]


def test_q_cherche_sous_un_autre_nom(roms, api, image):
    _jeux_portal(api)
    reponse = _apercu(roms, image, q="Portal 2")
    assert reponse["query"] == "Portal 2" and reponse["default_query"] == "Portal2"
    assert reponse["game"] == {"id": 42, "name": "Portal 2"}
    assert [j["id"] for j in reponse["games"]] == [42, 43]
    assert _cles(reponse) == ["sgdb:5"] and reponse["preferred"] == "sgdb:5"
    assert reponse["alias"] == {"query": "Portal 2", "game_id": 42, "game_name": "Portal 2"}
    assert "/icons/game/42" in api.calls and "/icons/game/7" not in api.calls


def test_nom_memorise_dans_le_fichier_du_jeu(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    fichier = roms.state_dir / "art" / "sgdb" / "aliases.json"
    assert json.loads(fichier.read_text()) == {"windows/Portal2": {
        "query": "Portal 2", "game_id": 42, "game_name": "Portal 2",
        "date": json.loads(fichier.read_text())["windows/Portal2"]["date"]}}


def test_nom_memorise_sert_aux_apercus_suivants(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    appels = len(api.calls)
    art_choice.oublier()
    reponse = _apercu(roms, image)                       # sans q : le nom mémorisé
    assert reponse["query"] == "Portal 2" and reponse["game"]["id"] == 42
    assert _cles(reponse) == ["sgdb:5"]
    assert "/icons/game/42" in api.calls[appels:] and "/icons/game/7" not in api.calls[appels:]


def test_game_id_force_le_jeu_et_la_memorisation(roms, api, image):
    _jeux_portal(api)
    reponse = _apercu(roms, image, q="Portal 2", game_id=43)
    assert reponse["game"]["id"] == 43 and reponse["candidates"] == []
    assert reponse["alias"]["game_id"] == 43
    assert "/icons/game/43" in api.calls and "/icons/game/42" not in api.calls
    # game_id seul : le nom mémorisé est repris, le jeu est celui demandé
    art_choice.oublier()
    assert _apercu(roms, image, game_id=42)["game"]["id"] == 42


def test_q_vide_revient_au_nom_par_defaut(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    reponse = _apercu(roms, image, q="")
    assert reponse["query"] == "Portal2" and reponse["alias"] is None
    assert reponse["game"] == {"id": 7, "name": "Portal"}
    assert _cles(reponse) == ["sgdb:1"]
    fichier = roms.state_dir / "art" / "sgdb" / "aliases.json"
    assert "windows/Portal2" not in json.loads(fichier.read_text())


def test_nom_introuvable_n_est_pas_memorise(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    api.jeux["Rien du tout"] = []
    reponse = _apercu(roms, image, q="Rien du tout")
    assert reponse["game"] is None and reponse["games"] == [] and reponse["candidates"] == []
    assert reponse["query"] == "Rien du tout"
    assert reponse["alias"]["query"] == "Portal 2"       # l'ancien choix reste en place
    assert "sgdb_error" not in reponse


def test_sans_nom_memorise_rien_n_est_ecrit(roms, api, image):
    _jeux_portal(api)
    api.jeux["Rien"] = []
    _apercu(roms, image, q="Rien")
    assert not (roms.state_dir / "art" / "sgdb" / "aliases.json").exists()


def test_le_nom_memorise_s_applique_au_choix_automatique(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    art = sgdb.sgdb_artwork(roms, image, "icon")
    assert art is not None and art.path.read_bytes() == PNG2     # icône du jeu 42
    assert "/icons/game/42" in api.calls
    assert "/icons/game/7" not in api.calls


def test_choix_automatique_et_apercu_restent_d_accord(roms, api, image):
    _jeux_portal(api)
    reponse = _apercu(roms, image, q="Portal 2")
    art = sgdb.sgdb_artwork(roms, image, "icon")         # l'illustration intégrée est l'icône
    assert reponse["preferred"] == "sgdb:5" and art is not None and art.path.read_bytes() == PNG2


def test_candidat_suit_le_nom_memorise_et_liste_blanche(roms, api, image):
    _jeux_portal(api)
    _apercu(roms, image, q="Portal 2")
    art = art_choice.candidat_fichier(roms, _cible(image), "sgdb:5", "thumb")
    assert art.path.read_bytes() == PNG2
    with pytest.raises(art_choice.CandidatError) as exc:  # grille de l'ancien jeu : hors liste
        art_choice.candidat_fichier(roms, _cible(image), "sgdb:1", "thumb")
    assert exc.value.status == 404


def test_cache_des_candidats_depend_du_nom(roms, api, image):
    _jeux_portal(api)
    avant = _cles(_apercu(roms, image))
    apres = _cles(_apercu(roms, image, q="Portal 2"))
    assert avant == ["sgdb:1"] and apres == ["sgdb:5"]
    assert len(art_choice._listes) == 2


def test_route_q_game_id_et_suppression_du_nom(client, api, image, roms):
    _jeux_portal(api)
    data = client.get("/api/image/art/candidates", params={"id": ID_IMAGE, "q": "Portal 2"}).json()
    assert data["game"]["id"] == 42 and data["alias"]["query"] == "Portal 2"
    assert client.get("/api/image/art/candidates",
                      params={"id": ID_IMAGE, "q": "Portal 2", "game_id": 43}).json()["game"]["id"] == 43
    assert client.delete("/api/image/art/alias", params={"id": ID_IMAGE}).json() == {"ok": True}
    data = client.get("/api/image/art/candidates", params={"id": ID_IMAGE}).json()
    assert data["alias"] is None and data["query"] == "Portal2"
    assert client.get("/api/image/art/candidates", params={"id": ID_IMAGE, "game_id": "x"}).status_code == 422
    assert client.get("/api/image/art/candidates",
                      params={"id": ID_IMAGE, "q": "x" * 201}).status_code == 422


def test_icone_automatique_exe_avant_jaquette_es(cfg, image, monkeypatch):
    """Sans icône SteamGridDB, l'illustration intégrée (une icône) prend l'icône de l'exécutable
    avant la jaquette d'EmulationStation ; une affiche (`cover`) garde l'ordre inverse."""
    cfg.sgdb_api_key = ""
    es = _es(image)
    icone = image.parent / "icone.png"
    icone.write_bytes(ICON)
    monkeypatch.setattr(artwork, "exe_icon",
                        lambda settings, img, cmd, dir_: artwork.Artwork(icone, "image/png", "icon"))
    assert artwork.artwork(cfg, image, "jeu.exe", None).path == icone
    assert artwork.artwork(cfg, image, "jeu.exe", None, kind="cover").path == es.resolve()
