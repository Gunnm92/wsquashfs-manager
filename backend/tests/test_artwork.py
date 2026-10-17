"""Illustration d'un jeu : jaquette d'EmulationStation."""

from __future__ import annotations

from app.config import Settings
from app.services import artwork
from app.services.artwork import es_artwork

GAMELIST = """<?xml version="1.0"?>
<gameList>
  <game><path>./Autre.wsquashfs</path><thumbnail>./images/autre-thumb.png</thumbnail></game>
  <game>
    <path>./Mon Jeu.wsquashfs</path>
    <image>./images/Mon Jeu-image.png</image>
    <thumbnail>./images/Mon Jeu-thumb.jpg</thumbnail>
  </game>
</gameList>
"""


def _system(tmp_path, gamelist=None, images=()):
    d = tmp_path / "roms" / "windows"
    (d / "images").mkdir(parents=True)
    (d / "Mon Jeu.wsquashfs").write_bytes(b"")
    if gamelist is not None:
        (d / "gamelist.xml").write_text(gamelist)
    for name in images:
        (d / "images" / name).write_bytes(b"x")
    return d


def test_gamelist_prefers_thumbnail(tmp_path):
    d = _system(tmp_path, GAMELIST, ["Mon Jeu-image.png", "Mon Jeu-thumb.jpg"])
    art = es_artwork(d / "Mon Jeu.wsquashfs")
    assert art.path.name == "Mon Jeu-thumb.jpg" and art.media_type == "image/jpeg"
    assert art.kind == "es"


def test_gamelist_entry_with_missing_file_falls_back(tmp_path):
    d = _system(tmp_path, GAMELIST, ["Mon Jeu-image.png"])
    assert es_artwork(d / "Mon Jeu.wsquashfs").path.name == "Mon Jeu-image.png"


def test_conventional_name_without_gamelist(tmp_path):
    d = _system(tmp_path, None, ["Mon Jeu-marquee.png", "Mon Jeu-boxart.webp"])
    assert es_artwork(d / "Mon Jeu.wsquashfs").path.name == "Mon Jeu-boxart.webp"


def test_path_outside_system_dir_refused(tmp_path):
    (tmp_path / "secret.png").write_bytes(b"x")
    evil = GAMELIST.replace("./images/Mon Jeu-thumb.jpg", "../../secret.png").replace(
        "<image>./images/Mon Jeu-image.png</image>", "")
    d = _system(tmp_path, evil)
    assert es_artwork(d / "Mon Jeu.wsquashfs") is None


def test_broken_gamelist_ignored(tmp_path):
    d = _system(tmp_path, "<gameList><game>", ["Mon Jeu-thumb.png"])
    assert es_artwork(d / "Mon Jeu.wsquashfs").path.name == "Mon Jeu-thumb.png"


def test_icon_cache_follows_the_executable(tmp_path, monkeypatch):
    """Exécutable corrigé (par une surcharge : image inchangée) : nouvelle
    lecture, pas l'icône de l'ancien exécutable."""
    image = tmp_path / "jeu.wsquashfs"
    image.write_bytes(b"img")
    looked_up: list[str | None] = []
    monkeypatch.setattr(artwork, "_exe_path",
                        lambda image, cmd, dir_, unsquashfs: looked_up.append(cmd))
    settings = Settings(state_dir=tmp_path / "state")
    assert artwork.exe_icon(settings, image, "launcher.exe", "") is None
    assert artwork.exe_icon(settings, image, "launcher.exe", "") is None   # en cache
    assert artwork.exe_icon(settings, image, "game.exe", "Binaries") is None
    assert looked_up == ["launcher.exe", "game.exe"]
