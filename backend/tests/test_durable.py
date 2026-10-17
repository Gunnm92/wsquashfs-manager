"""Écritures durables, et remise d'une image abîmée depuis son .old."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import durable
from app.services.durable import publish_new, restore, swap_keep_old, write_atomic
from app.services.tasks import TaskQueue


def _no_link(*args, **kwargs):
    raise OSError(errno.EPERM, "pas de lien physique ici")


# ------------------------------------------------------------------ écriture atomique

def test_write_atomic_replaces_and_leaves_nothing(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("ancien")
    write_atomic(target, "nouveau", mode=0o600)
    assert target.read_text() == "nouveau"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    write_atomic(target, b"cache", durable=False)
    assert target.read_bytes() == b"cache"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_write_atomic_failure_keeps_old_content(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_text("ancien")
    monkeypatch.setattr(durable.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("plein")))
    with pytest.raises(OSError):
        write_atomic(target, "nouveau")
    assert target.read_text() == "ancien"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


# ------------------------------------------------------------------ nouvelle image

def test_publish_new(tmp_path):
    part, target = tmp_path / "jeu.wsquashfs.part", tmp_path / "jeu.wsquashfs"
    part.write_bytes(b"image")
    publish_new(part, target)
    assert target.read_bytes() == b"image" and not part.exists()


@pytest.mark.parametrize("link", [True, False])
def test_publish_new_never_overwrites(tmp_path, monkeypatch, link):
    if not link:
        monkeypatch.setattr(durable.os, "link", _no_link)
    part, target = tmp_path / "jeu.wsquashfs.part", tmp_path / "jeu.wsquashfs"
    part.write_bytes(b"nouvelle")
    target.write_bytes(b"existante")
    with pytest.raises(FileExistsError):
        publish_new(part, target)
    assert target.read_bytes() == b"existante" and part.read_bytes() == b"nouvelle"


def test_publish_new_without_hard_links(tmp_path, monkeypatch):
    monkeypatch.setattr(durable.os, "link", _no_link)
    part, target = tmp_path / "jeu.wsquashfs.part", tmp_path / "jeu.wsquashfs"
    part.write_bytes(b"image")
    publish_new(part, target)
    assert target.read_bytes() == b"image" and not part.exists()


# ------------------------------------------------------------------ échange

@pytest.fixture
def files(tmp_path):
    image, part, backup = (tmp_path / n for n in ("jeu.wsquashfs", "jeu.wsquashfs.part",
                                                  "jeu.wsquashfs.old"))
    image.write_bytes(b"originale")
    part.write_bytes(b"reconstruite")
    return image, part, backup


def test_swap_keeps_original_and_image_name_never_missing(files, monkeypatch):
    image, part, backup = files
    inode = image.stat().st_ino
    real_replace = os.replace

    def replace(src, dst):
        assert image.exists(), "le nom de l'image a disparu pendant l'échange"
        return real_replace(src, dst)

    monkeypatch.setattr(durable.os, "replace", replace)
    swap_keep_old(image, part, backup)
    assert image.read_bytes() == b"reconstruite" and not part.exists()
    assert backup.read_bytes() == b"originale" and backup.stat().st_ino == inode


def test_swap_without_hard_links(files, monkeypatch):
    image, part, backup = files
    monkeypatch.setattr(durable.os, "link", _no_link)
    swap_keep_old(image, part, backup)
    assert image.read_bytes() == b"reconstruite" and backup.read_bytes() == b"originale"


@pytest.mark.parametrize("link", [True, False])
def test_swap_failure_restores_starting_state(files, monkeypatch, link):
    image, part, backup = files
    if not link:
        monkeypatch.setattr(durable.os, "link", _no_link)
    real_replace = os.replace

    def replace(src, dst):
        if Path(src) == part:
            raise OSError(errno.EIO, "erreur disque")
        return real_replace(src, dst)

    monkeypatch.setattr(durable.os, "replace", replace)
    monkeypatch.setattr(durable.os, "rename", lambda s, d: real_replace(s, d))
    with pytest.raises(OSError):
        swap_keep_old(image, part, backup)
    assert image.read_bytes() == b"originale" and not backup.exists()
    assert part.read_bytes() == b"reconstruite"


def test_swap_refuses_existing_backup(files):
    image, part, backup = files
    backup.write_bytes(b"autre")
    with pytest.raises(FileExistsError):
        swap_keep_old(image, part, backup)
    assert image.read_bytes() == b"originale" and backup.read_bytes() == b"autre"


def test_restore(files):
    image, _, backup = files
    backup.write_bytes(b"ancienne")
    restore(backup, image)
    assert image.read_bytes() == b"ancienne" and not backup.exists()


# ------------------------------------------------------------------ images abîmées

needs_tools = pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
                                 reason="squashfs-tools absent")


def _squash(src_root: Path, out: Path) -> None:
    src = src_root / out.name
    src.mkdir(parents=True)
    (src / "autorun.cmd").write_bytes(b"CMD=jeu.exe\r\n")
    subprocess.run(["mksquashfs", str(src), str(out), "-noappend", "-quiet"], check=True,
                   capture_output=True)


@needs_tools
def test_damaged_images_found_and_restored(tmp_path, monkeypatch):
    roms = tmp_path / "roms" / "windows"
    roms.mkdir(parents=True)
    src = tmp_path / "src"
    _squash(src, roms / "absente.wsquashfs.old")                 # image disparue
    _squash(src, roms / "abimee.wsquashfs.old")                  # image corrompue
    (roms / "abimee.wsquashfs").write_bytes(b"\0" * 4096)
    _squash(src, roms / "saine.wsquashfs")                       # rien à signaler
    _squash(src, roms / "saine.wsquashfs.old")
    (roms / "perdue.wsquashfs.old").write_bytes(b"illisible")    # rien de sain à remettre
    monkeypatch.setattr(main._settings, "roms_dirs", [str(tmp_path / "roms")])
    monkeypatch.setattr(main, "queue", TaskQueue(tmp_path / "state" / "tasks.json",
                                                 main.queue.handlers))
    monkeypatch.setattr(main.scan, "image_in_use", lambda image: False)
    monkeypatch.setattr(main, "_refresh_library", lambda force: [])
    client = TestClient(main.app)

    found = {d["id"]: d["problem"] for d in client.get("/api/recovery").json()}
    assert found == {"windows/absente": "image absente", "windows/abimee": "image illisible"}

    old = str(roms / "abimee.wsquashfs.old")
    assert client.post("/api/recovery/restore", json={"old": old}).status_code == 200
    assert not (roms / "abimee.wsquashfs.old").exists()
    assert subprocess.run(["unsquashfs", "-s", str(roms / "abimee.wsquashfs")],
                          capture_output=True, check=False).returncode == 0
    assert [d["id"] for d in client.get("/api/recovery").json()] == ["windows/absente"]
    # seul un fichier signalé peut être remis
    r = client.post("/api/recovery/restore", json={"old": str(roms / "saine.wsquashfs.old")})
    assert r.status_code == 404
