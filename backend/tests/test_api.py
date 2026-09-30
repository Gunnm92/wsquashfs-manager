"""Tests de l'API : authentification et désignation des images par identifiant."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import ImageInfo


@pytest.fixture
def client(monkeypatch):
    images = [
        ImageInfo(id="arcade/jeu", path=Path("/roms/arcade/jeu.wsquashfs"), name="jeu",
                  system="arcade"),
        ImageInfo(id="windows/jeu", path=Path("/roms/windows/jeu.wsquashfs"), name="jeu",
                  system="windows"),
    ]
    monkeypatch.setattr(main.scan, "scan_all", lambda settings, force=False: images)
    return TestClient(main.app)


def test_bad_token_gives_401_not_500(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    r = client.get("/api/config", headers={"x-wsfs-token": "faux"})
    assert r.status_code == 401
    r = client.get("/api/config")
    assert r.status_code == 401


def test_good_token_passes(client, monkeypatch):
    monkeypatch.setattr(main._settings, "password", "sésame")
    token = client.post("/login", json={"password": "sésame"}).json()["token"]
    r = client.get("/api/config", headers={"x-wsfs-token": token.encode()})
    assert r.status_code == 200


def test_same_name_in_two_systems(client):
    r = client.get("/api/image", params={"id": "windows/jeu"})
    assert r.status_code == 200
    assert r.json()["system"] == "windows"
    assert client.get("/api/image", params={"id": "jeu"}).status_code == 404


def test_autorun_edit_rejects_bad_eol(client):
    r = client.post("/api/image/autorun", params={"id": "arcade/jeu"},
                    json={"lines": ["CMD=a.exe"], "eol": "\r"})
    assert r.status_code == 422
