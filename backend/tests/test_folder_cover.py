"""Illustration des dossiers à empaqueter : aperçu (SteamGridDB, nom alternatif), écriture dans
`.wsquashfs/cover`, refus pendant l'empaquetage, empaquetage qui garde cette illustration.
Aucun appel réseau : httpx.MockTransport ; aucun outil squashfs (faux binaires, monkeypatch)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import deps, main
from app.config import Settings
from app.models import Task
from app.services import art_choice, operations, sgdb
from app.services.artwork import COVER_DIR, ecrire_cover_dossier, folder_cover
from app.services.operations import run_pack
from app.services.rebuild import Job
from app.services.tasks import TaskQueue

PNG1 = b"\x89PNG\r\n\x1a\n" + b"grille-un" * 20
PNG2 = b"\x89PNG\r\n\x1a\n" + b"grille-deux" * 20
ID = "win/Jeu.pc"


def _mock(monkeypatch, calls: list[str]) -> None:
    """SteamGridDB : « Jeu » trouve le jeu 7, sa grille (id 1) ; les images viennent du CDN."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.steamgriddb.com":
            return httpx.Response(200, content=PNG1, headers={"content-type": "image/png"})
        path = request.url.path.split("/api/v2", 1)[-1]
        calls.append(path)
        if path.startswith("/search/autocomplete/"):
            return httpx.Response(200, json={"success": True, "data": [{"id": 7, "name": "Jeu"}]})
        if path.startswith("/icons/game/"):
            grille = {"id": 1, "score": 9, "url": "https://cdn.test/g1.png",
                      "thumb": "https://cdn.test/t1.png", "width": 600, "height": 900,
                      "author": {"name": "Bob"}}
            return httpx.Response(200, json={"success": True, "data": [grille]})
        return httpx.Response(404)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(handler))


@pytest.fixture
def pret(tmp_path, monkeypatch):
    """Dossier roms/win/Jeu.pc (sans autorun), réglages de test, file de tâches isolée."""
    win = tmp_path / "roms" / "win"
    folder = win / "Jeu.pc"
    (folder / "bin").mkdir(parents=True)
    (folder / "bin" / "Jeu.exe").write_bytes(b"MZ")
    folder.chmod(0o750)
    monkeypatch.setattr(deps._settings, "roms_dirs", [str(win)])
    monkeypatch.setattr(deps._settings, "state_dir", tmp_path / "state")
    monkeypatch.setattr(deps._settings, "sgdb_api_key", "cle-secrete")
    monkeypatch.setattr(deps._settings, "sgdb_authors", [])
    queue = TaskQueue(tmp_path / "state" / "tasks.json", deps.queue.handlers)
    monkeypatch.setattr(deps, "queue", queue)
    monkeypatch.setattr(sgdb, "_down_until", 0.0)
    art_choice.oublier()
    deps._analyses.clear()
    calls: list[str] = []
    _mock(monkeypatch, calls)
    return {"folder": folder, "queue": queue, "calls": calls, "state": tmp_path / "state",
            "client": TestClient(main.app, headers={"x-wsfs-token": ""})}


# ------------------------------------------------------------------ écriture


def test_ecrire_cover_cree_le_dossier_et_le_fichier(tmp_path):
    folder = tmp_path / "Jeu.pc"
    folder.mkdir()
    folder.chmod(0o750)
    cible = ecrire_cover_dossier(folder, PNG1)
    assert cible == folder / ".wsquashfs" / "cover"
    assert cible.read_bytes() == PNG1
    assert stat.S_IMODE((folder / ".wsquashfs").stat().st_mode) == 0o750
    assert stat.S_IMODE(cible.stat().st_mode) == 0o640           # permissions du dossier, sans exécution
    assert sorted(p.name for p in (folder / ".wsquashfs").iterdir()) == ["cover"]   # pas de reste


def test_ecrire_cover_remplace_l_ancienne(tmp_path):
    folder = tmp_path / "Jeu.pc"
    folder.mkdir()
    ecrire_cover_dossier(folder, PNG1)
    ecrire_cover_dossier(folder, PNG2)
    assert (folder / ".wsquashfs" / "cover").read_bytes() == PNG2
    assert sorted(p.name for p in (folder / ".wsquashfs").iterdir()) == ["cover"]


def test_ecrire_cover_refuse_un_dossier_absent_ou_un_fichier(tmp_path):
    with pytest.raises(OSError):
        ecrire_cover_dossier(tmp_path / "absent.pc", PNG1)
    folder = tmp_path / "Jeu.pc"
    folder.mkdir()
    (folder / ".wsquashfs").write_bytes(b"pas un dossier")
    with pytest.raises(OSError, match="ordinaire"):
        ecrire_cover_dossier(folder, PNG1)
    assert (folder / ".wsquashfs").read_bytes() == b"pas un dossier"


def test_cover_du_dossier_lue_par_folder_cover(tmp_path):
    folder = tmp_path / "Jeu.pc"
    folder.mkdir()
    assert folder_cover(folder) is None
    ecrire_cover_dossier(folder, PNG1)
    art = folder_cover(folder)
    assert art is not None and art.path.read_bytes() == PNG1 and art.media_type == "image/png"


# ------------------------------------------------------------------ aperçu


def test_apercu_dossier_titre_sans_suffixe_et_adresses(pret):
    client = pret["client"]
    data = client.get("/api/folders/art/candidates", params={"id": ID}).json()
    assert data["query"] == "Jeu" and data["default_query"] == "Jeu"
    assert data["current"] == {"present": False, "sha1": None, "url": None}
    assert data["game"] == {"id": 7, "name": "Jeu"}
    cand = data["candidates"][0]
    assert cand["key"] == "sgdb:1" and cand["thumb"].startswith("/api/folders/art/candidate?")
    assert "size=full" in cand["full"]
    assert "/search/autocomplete/Jeu" in pret["calls"]
    assert not any(c["key"] in ("es", "icon") for c in data["candidates"])   # pas de locaux


def test_apercu_dossier_montre_la_cover_actuelle(pret):
    ecrire_cover_dossier(pret["folder"], PNG2)
    data = pret["client"].get("/api/folders/art/candidates", params={"id": ID}).json()
    assert data["current"]["present"] is True
    assert data["current"]["url"].startswith("/api/folders/art?id=win%2FJeu.pc")


def test_candidat_dossier_et_liste_blanche(pret):
    client = pret["client"]
    client.get("/api/folders/art/candidates", params={"id": ID})
    ok = client.get("/api/folders/art/candidate", params={"id": ID, "key": "sgdb:1", "size": "full"})
    assert ok.status_code == 200 and ok.content == PNG1
    assert client.get("/api/folders/art/candidate", params={"id": ID, "key": "sgdb:99"}).status_code == 404
    assert client.get("/api/folders/art/candidate", params={"id": ID, "key": "es"}).status_code == 404
    assert client.get("/api/folders/art/candidate", params={"id": ID, "key": "https://evil.test/x"}).status_code == 404
    assert client.get("/api/folders/art/candidate", params={"id": "win/Nope.pc", "key": "sgdb:1"}).status_code == 404


def test_nom_alternatif_pour_un_dossier(pret):
    client = pret["client"]
    data = client.get("/api/folders/art/candidates", params={"id": ID, "q": "Autre Nom"}).json()
    assert data["query"] == "Autre Nom" and data["alias"]["query"] == "Autre Nom"
    assert data["alias"]["game_id"] == 7
    fichier = pret["state"] / "art" / "sgdb" / "aliases.json"
    assert "win/Jeu.pc" in json.loads(fichier.read_text())
    assert client.delete("/api/folders/art/alias", params={"id": ID}).json() == {"ok": True}
    assert client.get("/api/folders/art/candidates", params={"id": ID}).json()["alias"] is None


# ------------------------------------------------------------------ écriture par la route


def test_post_cover_ecrit_le_candidat(pret):
    client = pret["client"]
    client.get("/api/folders/art/candidates", params={"id": ID})
    reponse = client.post("/api/folders/cover", json={"id": ID, "key": "sgdb:1"})
    assert reponse.status_code == 200 and reponse.json() == {"ok": True, "cover": ".wsquashfs/cover"}
    assert (pret["folder"] / ".wsquashfs" / "cover").read_bytes() == PNG1
    assert client.get("/api/folders/art/candidates", params={"id": ID}).json()["current"]["present"]


def test_post_cover_refuse_pendant_l_empaquetage(pret):
    from app.services.operations import pack_target
    client = pret["client"]
    pret["queue"].submit("pack", "win/Jeu", pack_target(pret["folder"]), {}, "empaquetage")
    reponse = client.post("/api/folders/cover", json={"id": ID, "key": "sgdb:1"})
    assert reponse.status_code == 409 and "en file" in reponse.json()["detail"]
    assert not (pret["folder"] / ".wsquashfs").exists()


def test_post_cover_refuse_une_fois_l_image_creee(pret):
    (pret["folder"].parent / "Jeu.wsquashfs").write_bytes(b"")
    reponse = pret["client"].post("/api/folders/cover", json={"id": ID, "key": "sgdb:1"})
    assert reponse.status_code == 409 and "image déjà créée" in reponse.json()["detail"]
    assert not (pret["folder"] / ".wsquashfs").exists()


def test_post_cover_cle_et_dossier_invalides(pret):
    client = pret["client"]
    assert client.post("/api/folders/cover", json={"id": ID, "key": "https://evil.test/x"}).status_code == 422
    assert client.post("/api/folders/cover", json={"id": ID, "key": "sgdb:99"}).status_code == 404
    assert client.post("/api/folders/cover", json={"id": "win/Nope.pc", "key": "sgdb:1"}).status_code == 404
    assert not (pret["folder"] / ".wsquashfs").exists()


def test_post_cover_sgdb_en_panne_donne_502(pret, monkeypatch):
    def panne(request):
        raise httpx.ConnectError("hors ligne", request=request)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(panne))
    reponse = pret["client"].post("/api/folders/cover", json={"id": ID, "key": "sgdb:1"})
    assert reponse.status_code == 502 and "SteamGridDB" in reponse.json()["detail"]
    assert not (pret["folder"] / ".wsquashfs").exists()


# ------------------------------------------------------------------ empaquetage


def _faux_mksquashfs(monkeypatch, capture: dict):
    """Pas de squashfs-tools ici : mksquashfs et la vérification sont remplacés ; on relève les
    sources passées à mksquashfs et les fichiers attendus dans l'image."""
    def run_progress(cmd, job, label, start, end):
        capture["cmd"] = cmd
        part = Path(next(a for a in cmd if str(a).endswith(".part")))
        part.write_bytes(b"squashfs")
    def verify(settings, part, expected, changes):
        capture["expected"] = set(expected)
    monkeypatch.setattr(operations, "run_progress", run_progress)
    monkeypatch.setattr(operations, "verify", verify)


def test_empaquetage_garde_la_cover_du_dossier(tmp_path, monkeypatch):
    folder = tmp_path / "roms" / "win" / "Jeu.pc"
    (folder / "bin").mkdir(parents=True)
    (folder / "bin" / "Jeu.exe").write_bytes(b"MZ")
    ecrire_cover_dossier(folder, PNG1)
    capture: dict = {}
    _faux_mksquashfs(monkeypatch, capture)
    settings = Settings(mksquashfs=sys.executable, tmp_dir=tmp_path / "tmp")
    task = Task(id="p", kind="pack", image="win/Jeu", image_path=str(folder.with_name("Jeu.wsquashfs")),
                params={"source": str(folder), "autorun": "DIR=bin\r\nCMD=Jeu.exe\r\n",
                        "rename_wine": False, "delete_source": False})
    run_pack(settings, task, Job())
    # mksquashfs reçoit le dossier entier (dont .wsquashfs) : la cover est dans l'image
    sources = capture["cmd"][1:capture["cmd"].index(str(folder.with_name("Jeu.wsquashfs.part")))]
    assert str(folder / COVER_DIR) in sources
    assert {".wsquashfs", ".wsquashfs/cover"} <= capture["expected"]
    assert (folder / COVER_DIR / "cover").read_bytes() == PNG1      # le dossier n'est pas touché


def test_empaquetage_sans_autorun_prend_le_dossier_entier(tmp_path, monkeypatch):
    folder = tmp_path / "roms" / "win" / "Jeu.pc"
    (folder / "autorun.cmd").parent.mkdir(parents=True)
    (folder / "autorun.cmd").write_bytes(b"CMD=Jeu.exe\r\n")
    ecrire_cover_dossier(folder, PNG1)
    capture: dict = {}
    _faux_mksquashfs(monkeypatch, capture)
    settings = Settings(mksquashfs=sys.executable, tmp_dir=tmp_path / "tmp")
    task = Task(id="p", kind="pack", image="win/Jeu", image_path=str(folder.with_name("Jeu.wsquashfs")),
                params={"source": str(folder), "autorun": None, "rename_wine": False,
                        "delete_source": False})
    run_pack(settings, task, Job())
    assert str(folder) in capture["cmd"] and ".wsquashfs/cover" in capture["expected"]


# ------------------------------------------------------------------ nom alternatif et choix automatique (image)


def test_cle_image_et_systeme_d_apres_roms(tmp_path):
    from app.services import scan
    roms = tmp_path / "roms"
    image = roms / "win" / "Jeu.wsquashfs"
    assert scan.system_of([str(roms)], image) == "win"            # roms/ : sous-dossier = système
    assert scan.system_of([str(roms / "win")], image) == "win"    # roms/<système> : le dossier choisi
    assert scan.system_of([str(tmp_path / "autre")], image) is None
    assert sgdb.cle_image(Settings(roms_dirs=[str(roms)]), image) == "win/Jeu"


def test_apercu_reste_ouvert_pendant_l_empaquetage(pret):
    """Pendant l'empaquetage, l'aperçu reste accessible ; seule l'écriture est refusée."""
    from app.services.operations import pack_target
    pret["queue"].submit("pack", "win/Jeu", pack_target(pret["folder"]), {}, "empaquetage")
    assert pret["client"].get("/api/folders/art/candidates", params={"id": ID}).status_code == 200


# ------------------------------------------------------------------ jaquettes


def test_art_cover_prend_l_affiche_steamgriddb_puis_l_illustration(pret, monkeypatch):
    """Vue jaquettes : `kind=cover` donne l'affiche SteamGridDB du dossier ; sans affiche,
    l'illustration du dossier ; `icon` (vue liste) reste l'illustration du dossier."""
    client = pret["client"]
    assert client.get("/api/folders/art", params={"id": ID, "kind": "cover"}).status_code == 404
    ecrire_cover_dossier(pret["folder"], PNG2)
    assert client.get("/api/folders/art", params={"id": ID, "kind": "cover"}).content == PNG2

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.steamgriddb.com":
            return httpx.Response(200, content=PNG1, headers={"content-type": "image/png"})
        path = request.url.path.split("/api/v2", 1)[-1]
        if path.startswith("/search/autocomplete/"):
            return httpx.Response(200, json={"success": True, "data": [{"id": 7, "name": "Jeu"}]})
        if path.startswith("/grids/game/"):
            grille = {"id": 1, "score": 9, "url": "https://cdn.test/g1.png", "thumb": "https://cdn.test/t1.png",
                      "width": 600, "height": 900, "author": {"name": "Bob"}}
            return httpx.Response(200, json={"success": True, "data": [grille]})
        return httpx.Response(404)
    monkeypatch.setattr(sgdb, "TRANSPORT", httpx.MockTransport(handler))
    sgdb.forget_sgdb_misses(deps._settings)
    assert client.get("/api/folders/art", params={"id": ID, "kind": "cover"}).content == PNG1
    assert client.get("/api/folders/art", params={"id": ID}).content == PNG2
