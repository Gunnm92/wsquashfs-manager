"""Illustration intégrée (`.wsquashfs/cover`) et illustration des dossiers à empaqueter.

Aucun squashfs-tools réel : un faux `unsquashfs` (script shell) répond à `-cat <image> <membre>`
avec le contenu du fichier désigné par $FAKE_COVER (échec si absent) et note chaque appel dans
$FAKE_LOG."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.config import Settings
from app.models import ImageInfo
from app.services import artwork
from app.services.artwork import _sniff, embedded_cover, folder_cover
from app.services.tasks import TaskQueue

FAKE = """#!/bin/sh
# faux unsquashfs : -cat <image> <membre>
echo "$*" >> "$FAKE_LOG"
if [ "$1" = "-cat" ] && [ "$3" = ".wsquashfs/cover" ] && [ -f "$FAKE_COVER" ]; then
  cat "$FAKE_COVER"
  exit 0
fi
echo "unsquashfs: membre absent" >&2
exit 1
"""


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (6, 9), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (6, 9), (30, 200, 30)).save(out, "JPEG")
    return out.getvalue()


PNG = _png()
JPEG = _jpeg()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "unsquashfs"
    script.parent.mkdir()
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cover = tmp_path / "cover.bin"
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_COVER", str(cover))
    monkeypatch.setenv("FAKE_LOG", str(log))

    class Fake:
        path = script

        def set(self, data: bytes | None) -> None:
            if data is None:
                cover.unlink(missing_ok=True)
            else:
                cover.write_bytes(data)

        def calls(self) -> int:
            return len(log.read_text().splitlines()) if log.exists() else 0

    return Fake()


@pytest.fixture
def cfg(tmp_path, fake) -> Settings:
    return Settings(state_dir=tmp_path / "state", unsquashfs=str(fake.path),
                    runtime_config_file=tmp_path / "config.json", roms_dirs=[str(tmp_path / "roms" / "win")])


def _image(tmp_path: Path, name: str = "Jeu.wsquashfs") -> Path:
    path = tmp_path / "roms" / "win" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    return path


def _touch(path: Path, ns: int) -> None:
    """Change l'image (contenu et date) sans dépendre de la résolution de l'horloge."""
    path.write_bytes(path.read_bytes() + b"!")
    os.utime(path, ns=(ns, ns))


def _cache_files(cfg: Settings) -> list[str]:
    base = cfg.state_dir / "art" / "embedded"
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


# ------------------------------------------------------------------ octets

def test_sniff_reconnait_les_formats():
    assert _sniff(PNG) == (".png", "image/png")
    assert _sniff(JPEG) == (".jpg", "image/jpeg")
    assert _sniff(b"GIF89a....") == (".gif", "image/gif")
    assert _sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == (".webp", "image/webp")
    assert _sniff(b"quelque chose") == (".png", "image/png")


# ------------------------------------------------------------------ image

def test_embedded_cover_lue_et_mise_en_cache(cfg, fake, tmp_path):
    image = _image(tmp_path)
    fake.set(PNG)
    art = embedded_cover(cfg, image)
    assert art is not None and art.kind == "embedded" and art.media_type == "image/png"
    assert art.path.read_bytes() == PNG
    assert embedded_cover(cfg, image).path == art.path
    assert fake.calls() == 1                                  # la deuxième fois : cache
    assert len(_cache_files(cfg)) == 1


def test_absence_mise_en_cache(cfg, fake, tmp_path):
    image = _image(tmp_path)
    assert embedded_cover(cfg, image) is None
    assert embedded_cover(cfg, image) is None
    assert fake.calls() == 1
    names = _cache_files(cfg)
    assert len(names) == 1 and names[0].endswith(".none")


def test_image_modifiee_relue(cfg, fake, tmp_path):
    image = _image(tmp_path)
    fake.set(PNG)
    assert embedded_cover(cfg, image).media_type == "image/png"
    fake.set(JPEG)
    _touch(image, 2_000_000_000_000_000_000)
    art = embedded_cover(cfg, image)
    assert art.media_type == "image/jpeg" and art.path.read_bytes() == JPEG
    assert fake.calls() == 2
    assert len(_cache_files(cfg)) == 1                        # l'ancienne entrée est nettoyée


def test_absence_invalidee_quand_l_image_change(cfg, fake, tmp_path):
    image = _image(tmp_path)
    assert embedded_cover(cfg, image) is None
    fake.set(PNG)
    _touch(image, 2_000_000_000_000_000_000)
    assert embedded_cover(cfg, image) is not None


def test_outil_absent_rien_en_cache(cfg, tmp_path):
    image = _image(tmp_path)
    cfg.unsquashfs = str(tmp_path / "pas-la" / "unsquashfs")
    assert embedded_cover(cfg, image) is None
    assert _cache_files(cfg) == []                            # pas une absence : rien à garder


def test_image_inexistante(cfg, tmp_path):
    assert embedded_cover(cfg, tmp_path / "rien.wsquashfs") is None


def test_forget_purge_les_absences_seulement(cfg, fake, tmp_path):
    present = _image(tmp_path, "Oui.wsquashfs")
    absent = _image(tmp_path, "Non.wsquashfs")
    fake.set(PNG)
    embedded_cover(cfg, present)
    fake.set(None)
    embedded_cover(cfg, absent)
    artwork.forget_embedded_misses(cfg)
    names = _cache_files(cfg)
    assert len(names) == 1 and not names[0].endswith(".none")


# ------------------------------------------------------------------ dossier

def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "roms" / "win" / "Jeu.pc"
    folder.mkdir(parents=True)
    return folder


def test_folder_cover_presente(tmp_path):
    folder = _folder(tmp_path)
    (folder / ".wsquashfs").mkdir()
    (folder / ".wsquashfs" / "cover").write_bytes(JPEG)
    art = folder_cover(folder)
    assert art is not None and art.media_type == "image/jpeg" and art.path.read_bytes() == JPEG


def test_folder_cover_absente(tmp_path):
    folder = _folder(tmp_path)
    assert folder_cover(folder) is None
    (folder / ".wsquashfs").mkdir()
    assert folder_cover(folder) is None                       # dossier sans fichier


def test_folder_cover_lien_sortant_refuse(tmp_path):
    folder = _folder(tmp_path)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    (folder / ".wsquashfs").mkdir()
    (folder / ".wsquashfs" / "cover").symlink_to(secret)
    assert folder_cover(folder) is None


def test_folder_cover_lien_interne_accepte(tmp_path):
    folder = _folder(tmp_path)
    (folder / "art.png").write_bytes(PNG)
    (folder / ".wsquashfs").mkdir()
    (folder / ".wsquashfs" / "cover").symlink_to(folder / "art.png")
    assert folder_cover(folder) is not None


def test_folder_cover_dossier_lui_meme_lien(tmp_path):
    """.wsquashfs qui sort du dossier (lien de répertoire) : refusé."""
    folder = _folder(tmp_path)
    outside = tmp_path / "ailleurs"
    outside.mkdir()
    (outside / "cover").write_bytes(PNG)
    (folder / ".wsquashfs").symlink_to(outside, target_is_directory=True)
    assert folder_cover(folder) is None


# ------------------------------------------------------------------ routes

@pytest.fixture
def client(cfg, tmp_path, monkeypatch):
    image = _image(tmp_path)
    info = ImageInfo(id="win/jeu", path=image, name="Jeu", system="win", size=5)
    monkeypatch.setattr(main, "_settings", cfg)
    monkeypatch.setattr(main, "_find_image", lambda image_id: info)
    monkeypatch.setattr(main, "queue", TaskQueue(tmp_path / "state" / "tasks.json", main.queue.handlers))
    return TestClient(main.app)


def test_route_image_art_presente(client, fake):
    fake.set(PNG)
    reponse = client.get("/api/image/art", params={"id": "win/jeu"})
    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "image/png"
    assert reponse.headers["x-art-kind"] == "embedded"
    assert reponse.content == PNG


def test_route_image_art_kind_ignore(client, fake):
    fake.set(PNG)
    icone = client.get("/api/image/art", params={"id": "win/jeu", "kind": "icon"})
    jaquette = client.get("/api/image/art", params={"id": "win/jeu", "kind": "cover"})
    assert icone.content == jaquette.content == PNG


def test_route_image_art_absente(client, fake):
    fake.set(None)
    assert client.get("/api/image/art", params={"id": "win/jeu"}).status_code == 404
    assert client.get("/api/image/art", params={"id": "win/jeu"}).status_code == 404
    assert fake.calls() == 1                                  # absence en cache


def test_route_image_art_n_appelle_ni_sgdb_ni_es(client, fake, monkeypatch):
    def interdit(*args, **kwargs):
        raise AssertionError("l'affichage ne doit plus lire SteamGridDB ni ES")
    monkeypatch.setattr(artwork, "artwork", interdit)
    monkeypatch.setattr(artwork, "es_artwork", interdit)
    fake.set(None)
    assert client.get("/api/image/art", params={"id": "win/jeu"}).status_code == 404


def test_route_forget(client, fake):
    fake.set(None)
    client.get("/api/image/art", params={"id": "win/jeu"})
    assert client.post("/api/image/art/forget").json() == {"ok": True}
    client.get("/api/image/art", params={"id": "win/jeu"})
    assert fake.calls() == 2                                  # absence revalidée


def test_route_folder_art(client, cfg, tmp_path):
    folder = _folder(tmp_path)
    (folder / ".wsquashfs").mkdir()
    (folder / ".wsquashfs" / "cover").write_bytes(PNG)
    reponse = client.get("/api/folders/art", params={"id": "win/Jeu.pc"})
    assert reponse.status_code == 200 and reponse.content == PNG
    assert reponse.headers["x-art-kind"] == "embedded"
    assert client.get("/api/folders/art", params={"id": "win/inconnu.pc"}).status_code == 404


def test_route_folder_art_sans_cover(client, tmp_path):
    _folder(tmp_path)
    assert client.get("/api/folders/art", params={"id": "win/Jeu.pc"}).status_code == 404


def test_route_folder_art_lien_sortant(client, tmp_path):
    folder = _folder(tmp_path)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    (folder / ".wsquashfs").mkdir()
    (folder / ".wsquashfs" / "cover").symlink_to(secret)
    assert client.get("/api/folders/art", params={"id": "win/Jeu.pc"}).status_code == 404
