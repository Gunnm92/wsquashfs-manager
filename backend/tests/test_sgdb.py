"""SteamGridDB : icônes rondes, jaquettes, cache, délai de grâce, routes. Aucun appel réseau
réel : httpx.MockTransport remplace le transport (sgdb.TRANSPORT)."""

from __future__ import annotations

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import main
from app.config import Settings, save_sgdb
from app.services import artwork, sgdb

# ------------------------------------------------------------------ images de test


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def disque() -> bytes:
    """Disque plein sur fond transparent : parfaitement rond."""
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, 99, 99), fill=(200, 30, 30, 255))
    return _png(img)


def carre() -> bytes:
    """Carré plein : pas rond du tout."""
    return _png(Image.new("RGBA", (64, 64), (30, 30, 200, 255)))


# ------------------------------------------------------------------ faux SteamGridDB


class Api:
    """Réponses de l'API (`icons`, `grids`) et des CDN (`cdn`, url -> (octets, type MIME))."""

    def __init__(self):
        self.icons: list[dict] = []
        self.grids: list[dict] = []
        self.cdn: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, dict, str]] = []     # (chemin, paramètres, en-tête d'auth)
        self.downloads: list[str] = []
        self.rate_limit = 0            # nombre de 429 à renvoyer pour les icônes
        self.status: int | None = None  # statut forcé pour toute requête de l'API

    def add_icon(self, id_: int, score: float, author: str, data: bytes) -> None:
        url = f"https://cdn.test/icon{id_}.png"
        self.cdn[url] = (data, "image/png")
        self.icons.append({"id": id_, "score": score, "url": url, "thumb": url,
                           "author": {"name": author}})

    def add_grid(self, id_: int, score: float, author: str, data: bytes, mime: str) -> None:
        url = f"https://cdn.test/grid{id_}"
        self.cdn[url] = (data, mime)
        self.grids.append({"id": id_, "score": score, "url": url, "thumb": url,
                           "author": {"name": author}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.steamgriddb.com":
            url = str(request.url)
            self.downloads.append(url)
            data, mime = self.cdn[url]
            return httpx.Response(200, content=data, headers={"content-type": mime})
        path = request.url.path.split("/api/v2", 1)[-1]
        self.calls.append((path, dict(request.url.params), request.headers.get("authorization", "")))
        if self.status:
            return httpx.Response(self.status, json={"success": False})
        if path.startswith("/search/autocomplete/"):
            if path.endswith("/Inconnu"):
                return httpx.Response(404, json={"success": False})
            return httpx.Response(200, json={"success": True,
                                             "data": [{"id": 7, "name": "Portal"}]})
        if path.startswith("/icons/game/"):
            if self.rate_limit:
                self.rate_limit -= 1
                return httpx.Response(429, json={"success": False})
            return httpx.Response(200, json={"success": True, "data": self.icons})
        if path.startswith("/grids/game/"):
            return httpx.Response(200, json={"success": True, "data": self.grids})
        return httpx.Response(404)

    def count(self, prefix: str) -> int:
        return sum(1 for path, _, _ in self.calls if path.startswith(prefix))


@pytest.fixture(autouse=True)
def etat_propre(monkeypatch):
    monkeypatch.setattr(sgdb, "_down_until", 0.0)
    sgdb._locks.clear()
    monkeypatch.setattr(sgdb.time, "sleep", lambda seconds: None)


@pytest.fixture
def api(monkeypatch) -> Api:
    faux = Api()
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(faux))
    return faux


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(state_dir=tmp_path / "state", runtime_config_file=tmp_path / "config.json",
                    sgdb_api_key="cle-secrete", sgdb_authors=[], sgdb_round_threshold=0.85)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "roms" / "windows" / "Portal 2 (USA) [v1.2].wsquashfs"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def _base(cfg: Settings):
    return cfg.state_dir / "art" / "sgdb"


# ------------------------------------------------------------------ titre et rondeur


def test_nettoyer_titre_retire_parentheses_et_crochets():
    assert sgdb.nettoyer_titre("Portal 2 (USA) [v1.2]  ") == "Portal 2"
    assert sgdb.nettoyer_titre("Doom  (  Rev 1 )  Edition") == "Doom Edition"


def test_score_rond_disque_et_carre():
    assert sgdb.score_rond(Image.open(io.BytesIO(disque()))) > 0.9
    assert sgdb.score_rond(Image.open(io.BytesIO(carre()))) < 0.2


# ------------------------------------------------------------------ icônes


def test_icone_auteur_prefere_passe_avant_la_rondeur(cfg, api, image):
    api.add_icon(1, 9, "Bob", disque())
    api.add_icon(2, 5, "alice", carre())
    cfg.sgdb_authors = ["ALICE"]
    art = sgdb.sgdb_artwork(cfg, image, "icon")
    assert art is not None and art.kind == "sgdb" and art.media_type == "image/png"
    meta = json.loads((_base(cfg) / (art.path.stem + ".json")).read_text())
    assert meta["icon_id"] == 2 and meta["reason"] == "auteur préféré"
    assert Image.open(art.path).mode == "RGBA"


def test_icone_la_plus_ronde_si_pas_d_auteur_prefere(cfg, api, image):
    api.add_icon(1, 9, "Bob", carre())
    api.add_icon(2, 5, "Carl", disque())
    art = sgdb.sgdb_artwork(cfg, image, "icon")
    meta = json.loads((_base(cfg) / (art.path.stem + ".json")).read_text())
    assert meta["icon_id"] == 2 and meta["reason"].startswith("rondeur")


def test_aucune_icone_ronde_mise_en_absence(cfg, api, image):
    api.add_icon(1, 9, "Bob", carre())
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    none = list(_base(cfg).glob("*.none"))
    assert len(none) == 1
    assert "reason" in json.loads(none[0].read_text()) and "date" in json.loads(none[0].read_text())
    # l'absence récente évite une nouvelle demande
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    assert api.count("/icons/game/") == 1


def test_absence_revalidee_apres_sept_jours(cfg, api, image, monkeypatch):
    api.add_icon(1, 9, "Bob", carre())
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    none = next(_base(cfg).glob("*.none"))
    meta = json.loads(none.read_text())
    meta["date"] -= 8 * 86400
    none.write_text(json.dumps(meta))
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    assert api.count("/icons/game/") == 2


def test_429_puis_succes(cfg, api, image):
    api.add_icon(2, 5, "Carl", disque())
    api.rate_limit = 2
    art = sgdb.sgdb_artwork(cfg, image, "icon")
    assert art is not None
    assert api.count("/icons/game/") == 3


def test_429_persistant_ne_met_rien_en_cache(cfg, api, image):
    api.rate_limit = 10
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    assert not list(_base(cfg).glob("*.none"))
    assert api.count("/icons/game/") == 4              # 4 essais


def test_pas_de_cle_aucun_appel(cfg, api, image):
    cfg.sgdb_api_key = ""
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    assert sgdb.sgdb_artwork(cfg, image, "cover") is None
    assert api.calls == []


def test_cle_refusee_sans_absence_puis_delai_de_grace(cfg, api, image):
    api.status = 401
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None
    assert not list(_base(cfg).glob("*.none"))
    appels = len(api.calls)
    api.status = None
    assert sgdb.sgdb_artwork(cfg, image, "icon") is None     # délai de grâce : rien demandé
    assert len(api.calls) == appels


def test_erreur_reseau_delai_de_grace(cfg, image, monkeypatch):
    def panne(request):
        raise httpx.ConnectError("hors ligne", request=request)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(panne))
    assert sgdb.sgdb_artwork(cfg, image, "cover") is None
    assert sgdb._down_until > 0
    assert not list(_base(cfg).glob("*.none"))


def test_cache_reutilise_sans_appel(cfg, api, image):
    api.add_icon(2, 5, "Carl", disque())
    first = sgdb.sgdb_artwork(cfg, image, "icon")
    appels = len(api.calls)
    again = sgdb.sgdb_artwork(cfg, image, "icon")
    assert again is not None and again.path == first.path
    assert len(api.calls) == appels


def test_artwork_icon_n_appelle_que_sgdb(cfg, api, image, monkeypatch):
    api.add_icon(2, 5, "Carl", disque())

    def interdit(*args, **kwargs):
        raise AssertionError("source suivante appelée alors que SteamGridDB répond")
    monkeypatch.setattr(artwork, "es_artwork", interdit)
    monkeypatch.setattr(artwork, "exe_icon", interdit)
    art = artwork.artwork(cfg, image, None, None, "icon")
    assert art.kind == "sgdb"


# ------------------------------------------------------------------ jaquettes


def test_jaquette_auteur_prefere_puis_meilleure_note(cfg, api, image):
    api.add_grid(1, 9, "Bob", b"png-bob", "image/png")
    api.add_grid(2, 5, "alice", b"jpg-alice", "image/jpeg")
    cfg.sgdb_authors = ["Alice"]
    art = sgdb.sgdb_artwork(cfg, image, "cover")
    assert art.path.suffix == ".jpg" and art.media_type == "image/jpeg"
    assert art.path.read_bytes() == b"jpg-alice"
    assert api.calls[-1][1]["dimensions"].startswith("600x900")

    cfg.sgdb_authors = []
    art = sgdb.sgdb_artwork(cfg, image, "cover")          # autre clé : sans auteur, la mieux notée
    assert art.path.read_bytes() == b"png-bob"


def test_recherche_une_seule_fois_pour_icone_et_jaquette(cfg, api, image):
    api.add_icon(2, 5, "Carl", disque())
    api.add_grid(1, 9, "Bob", b"png-bob", "image/png")
    assert sgdb.sgdb_artwork(cfg, image, "icon") is not None
    assert sgdb.sgdb_artwork(cfg, image, "cover") is not None
    assert api.count("/search/") == 1


def test_artwork_cover_source_sgdb(cfg, api, image, monkeypatch):
    api.add_grid(1, 9, "Bob", b"png-bob", "image/png")
    monkeypatch.setattr(artwork, "es_artwork", lambda image: None)
    monkeypatch.setattr(artwork, "exe_icon", lambda *a: None)
    art = artwork.artwork(cfg, image, None, None, "cover")
    assert art.kind == "sgdb"


def test_jeu_introuvable_mis_en_absence(cfg, api, image, monkeypatch):
    monkeypatch.setattr(sgdb, "nettoyer_titre", lambda nom: "Inconnu")
    assert sgdb.sgdb_artwork(cfg, image, "cover") is None
    assert len(list(_base(cfg).glob("*.none"))) == 1
    assert sgdb.sgdb_artwork(cfg, image, "cover") is None
    assert api.count("/search/") == 1


def test_purge_efface_les_absences_garde_les_trouvailles(cfg, api, image):
    api.add_icon(2, 5, "Carl", disque())
    api.add_grid(1, 9, "Bob", b"png-bob", "image/png")
    sgdb.sgdb_artwork(cfg, image, "icon")
    other = image.with_name("Autre.wsquashfs")
    api.icons.clear()
    sgdb.sgdb_artwork(cfg, image, "cover")
    sgdb.sgdb_artwork(cfg, other.with_name("Inconnu.wsquashfs"), "cover")  # rien à trouver
    sgdb.forget_sgdb_misses(cfg)
    assert not list(_base(cfg).glob("*.none"))
    assert sgdb.sgdb_artwork(cfg, image, "icon") is not None   # trouvaille gardée


# ------------------------------------------------------------------ réglages et routes


def test_save_sgdb_validation(cfg):
    with pytest.raises(ValueError):
        save_sgdb(cfg, None, [], 1.5)
    with pytest.raises(ValueError):
        save_sgdb(cfg, None, [f"A{n}" for n in range(21)], 0.5)
    save_sgdb(cfg, None, ["Bob", "  bob ", "", "Alice"], 0.5)
    assert cfg.sgdb_authors == ["Bob", "Alice"] and cfg.sgdb_api_key == "cle-secrete"


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(main, "_settings", cfg)
    return TestClient(main.app)


def test_route_put_sgdb_ne_renvoie_pas_la_cle(client, cfg):
    reponse = client.put("/api/config/sgdb", json={"authors": ["Bob"], "threshold": 0.9})
    assert reponse.status_code == 200
    data = reponse.json()
    assert data["sgdb_configured"] is True
    assert data["sgdb_authors"] == ["Bob"] and data["sgdb_round_threshold"] == 0.9
    assert "cle-secrete" not in reponse.text
    assert cfg.sgdb_api_key == "cle-secrete"                 # clé absente : inchangée

    nouvelle = client.put("/api/config/sgdb", json={"api_key": "autre", "authors": [], "threshold": 0.85})
    assert "autre" not in nouvelle.text and nouvelle.json()["sgdb_configured"] is True
    assert json.loads(cfg.runtime_config_file.read_text())["sgdb_api_key"] == "autre"

    effacee = client.put("/api/config/sgdb", json={"api_key": "", "authors": [], "threshold": 0.85})
    assert effacee.json()["sgdb_configured"] is False


def test_route_put_sgdb_seuil_invalide(client):
    assert client.put("/api/config/sgdb", json={"authors": [], "threshold": 2}).status_code == 422


def test_route_test_sgdb(client, api, cfg):
    reponse = client.post("/api/config/sgdb/test")
    assert reponse.status_code == 200 and reponse.json()["ok"] is True
    assert api.calls[0][2] == "Bearer cle-secrete"
    assert not _base(cfg).exists()                          # rien mis en cache


def test_route_test_sgdb_cle_refusee(client, api):
    api.status = 403
    assert client.post("/api/config/sgdb/test").json()["ok"] is False


def test_route_purge_art(client):
    assert client.post("/api/image/art/forget").json() == {"ok": True}


def test_cle_collee_avec_son_libelle_est_normalisee():
    """La ligne « Key: … » copiée depuis SteamGridDB donne la clé seule."""
    from app.services.sgdb import normaliser_cle

    cle = "0123456789abcdef0123456789ABCDEF"
    assert normaliser_cle(f"Key: {cle}\n") == cle.lower()
    assert normaliser_cle(f"  {cle}  ") == cle.lower()
    assert normaliser_cle("pas une clé") == "pas une clé"


def test_tester_la_cle_saisie_sans_l_enregistrer(tmp_path, monkeypatch):
    """« Tester » essaie la clé saisie, même si aucune n'est enregistrée, sans l'enregistrer."""
    from app.config import Settings
    from app.services import sgdb

    vues = []

    class Client:
        def __init__(self, cle):
            vues.append(cle)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sgdb, "_client", Client)
    monkeypatch.setattr(sgdb, "_get", lambda client, chemin, params=None: [{"name": "Portal"}])
    settings = Settings(state_dir=tmp_path / "s")
    assert sgdb.tester_cle(settings) == (False, "aucune clé saisie ni enregistrée")
    ok, message = sgdb.tester_cle(settings, "Key: 0123456789abcdef0123456789abcdef")
    assert ok and "Portal" in message
    assert vues == ["0123456789abcdef0123456789abcdef"] and settings.sgdb_api_key == ""
