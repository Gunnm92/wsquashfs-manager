"""Illustration Steam (Steam ROM Manager) lue sur les machines : cache, revalidation, repli."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.services import artwork, trial
from app.services.artwork import artwork as artwork_for
from app.services.rebuild import RebuildError

PNG = b"\x89PNG\r\n\x1a\nfake"
GAMELIST = """<?xml version="1.0"?>
<gameList><game><path>./Mon Jeu.wsquashfs</path><thumbnail>./images/mon-thumb.jpg</thumbnail></game></gameList>
"""


class Machines:
    """Fausses machines : `replies[id]` est appelé avec l'ETag envoyé ; il renvoie le résultat
    de games_art ou lève une exception (machine injoignable)."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.calls: list[tuple[str, str, str, str]] = []

    def open(self, settings, machine_id, timeout=3600):
        machines = self

        class Host:
            client = type("C", (), {"close": lambda self: None})()

            def games_art(self, name, kind, etag="", timeout=10):
                machines.calls.append((machine_id, name, kind, etag))
                reply = machines.replies[machine_id]
                if isinstance(reply, BaseException):
                    raise reply
                return reply(etag) if callable(reply) else reply

        return Host()


@pytest.fixture(autouse=True)
def clean_down():
    artwork._steam_down.clear()
    yield
    artwork._steam_down.clear()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Réglages, une image dans un dossier d'ES, et les machines associées : `hosts`
    (liste d'identifiants, d'abord la machine d'essai)."""
    d = tmp_path / "roms" / "windows"
    (d / "images").mkdir(parents=True)
    image = d / "Mon Jeu.wsquashfs"
    image.write_bytes(b"")
    (d / "gamelist.xml").write_text(GAMELIST)
    (d / "images" / "mon-thumb.jpg").write_bytes(b"jpg")
    settings = Settings(state_dir=tmp_path / "state", runtime_config_file=tmp_path / "config.json",
                        machine_port=0)
    state = {"hosts": []}
    monkeypatch.setattr(artwork, "game_host_machine", lambda s: state["hosts"][0] if state["hosts"] else "")
    monkeypatch.setattr(artwork, "machine_rows",
                        lambda s: [{"id": h} for h in state["hosts"]])

    def use(replies: dict) -> Machines:
        state["hosts"] = list(replies)
        machines = Machines(replies)
        monkeypatch.setattr(artwork, "open_machine", machines.open)
        return machines

    return settings, image, use


def expire(settings):
    """Simule un cache plus vieux que STEAM_TTL (sans attendre)."""
    for meta in (settings.state_dir / "art" / "steam").glob("*.json"):
        data = json.loads(meta.read_text())
        data["checked"] = 0
        meta.write_text(json.dumps(data))


def test_steam_art_found_and_cached(setup):
    settings, image, use = setup
    machines = use({"pc": ("image/png", PNG, '"e1"')})
    art = artwork.steam_artwork(settings, image, "cover")
    assert art.kind == "steam" and art.media_type == "image/png"
    assert art.path.read_bytes() == PNG
    assert art.path.parent == settings.state_dir / "art" / "steam"
    assert machines.calls == [("pc", "Mon Jeu.wsquashfs", "cover", "")]
    # Moins de 600 s : servi sans interroger la machine
    again = artwork.steam_artwork(settings, image, "cover")
    assert again.path == art.path and len(machines.calls) == 1


def test_steam_cache_is_per_kind(setup):
    settings, image, use = setup
    machines = use({"pc": ("image/png", PNG, '"e1"')})
    artwork.steam_artwork(settings, image, "cover")
    artwork.steam_artwork(settings, image, "icon")
    assert [c[2] for c in machines.calls] == ["cover", "icon"]


def test_404_falls_back_to_es_and_is_cached(setup):
    settings, image, use = setup
    machines = use({"pc": None})
    art = artwork_for(settings, image, None, None)
    assert art.kind == "es"                      # repli sur la jaquette d'ES
    assert list((settings.state_dir / "art" / "steam").glob("*.none"))
    artwork_for(settings, image, None, None)
    assert len(machines.calls) == 1              # absence mise en cache


def test_304_keeps_the_cached_file(setup):
    settings, image, use = setup
    machines = use({"pc": lambda etag: ("not-modified", None, etag) if etag == '"e1"'
                    else ("image/png", PNG, '"e1"')})
    first = artwork.steam_artwork(settings, image, "cover")
    expire(settings)
    second = artwork.steam_artwork(settings, image, "cover")
    assert machines.calls[1][3] == '"e1"'        # If-None-Match envoyé
    assert second.kind == "steam" and second.path == first.path
    assert second.path.read_bytes() == PNG
    meta = json.loads(next((settings.state_dir / "art" / "steam").glob("*.json")).read_text())
    assert meta["checked"] > 0 and meta["etag"] == '"e1"'


def test_unreachable_machine_serves_the_cache(setup):
    settings, image, use = setup
    use({"pc": ("image/png", PNG, '"e1"')})
    first = artwork.steam_artwork(settings, image, "cover")
    expire(settings)
    machines = use({"pc": RebuildError("machine injoignable")})
    assert artwork.steam_artwork(settings, image, "cover").path == first.path
    assert machines.calls[0][0] == "pc"


def test_unreachable_without_cache_falls_back(setup):
    settings, image, use = setup
    use({"pc": RebuildError("machine injoignable")})
    assert artwork_for(settings, image, None, None).kind == "es"
    assert not list((settings.state_dir / "art" / "steam").glob("*.none"))


def test_unreachable_machine_is_not_asked_again_at_once(setup):
    settings, image, use = setup
    machines = use({"pc": RebuildError("machine injoignable")})
    artwork.steam_artwork(settings, image, "cover")
    artwork.steam_artwork(settings, image, "cover")
    assert len(machines.calls) == 1


def test_next_machine_answers_when_the_first_is_absent(setup):
    settings, image, use = setup
    use({"essai": None, "autre": ("image/jpeg", b"jpeg", '"j1"')})
    art = artwork.steam_artwork(settings, image, "cover")
    assert art.kind == "steam" and art.media_type == "image/jpeg"


def test_no_machine_means_no_request(setup, monkeypatch):
    settings, image, _ = setup
    monkeypatch.setattr(artwork, "game_host_machine", lambda s: "")
    monkeypatch.setattr(artwork, "machine_rows", lambda s: [])

    def forbidden(*args, **kwargs):
        raise AssertionError("aucune machine : pas d'appel")

    monkeypatch.setattr(artwork, "open_machine", forbidden)
    assert artwork.steam_artwork(settings, image, "cover") is None
    assert artwork_for(settings, image, None, None).kind == "es"


def test_forget_revalidates_at_next_call(setup):
    settings, image, use = setup
    machines = use({"pc": lambda etag: ("not-modified", None, etag) if etag else ("image/png", PNG, '"e1"')})
    artwork.steam_artwork(settings, image, "cover")
    artwork.forget_steam_cache(settings)
    art = artwork.steam_artwork(settings, image, "cover")
    assert len(machines.calls) == 2 and machines.calls[1][3] == '"e1"'
    assert art.kind == "steam"


def test_forget_clears_absence_too(setup):
    settings, image, use = setup
    use({"pc": None})
    artwork.steam_artwork(settings, image, "cover")
    artwork.forget_steam_cache(settings)
    use({"pc": ("image/png", PNG, '"e1"')})     # la machine a maintenant une illustration
    art = artwork.steam_artwork(settings, image, "cover")
    assert art.kind == "steam"


def test_games_art_status_codes(tmp_path):
    """AgentHost.games_art : 404 → None, 304 → not-modified, 200 → (type, données, ETag)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params["name"] == "absent.wsquashfs":
            return httpx.Response(404, json={"detail": "aucune illustration"})
        if request.headers.get("if-none-match") == '"e1"':
            return httpx.Response(304)
        if request.url.params["name"] == "boom.wsquashfs":
            raise httpx.ConnectError("refusé")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png", "etag": '"e1"'})

    host = trial.AgentHost.__new__(trial.AgentHost)
    host.settings = Settings(state_dir=tmp_path)
    host.url = "https://machine.test"
    host.timeout = 5
    host._client_id = "cafe" * 8
    host.client = httpx.Client(base_url=host.url, transport=httpx.MockTransport(handler))

    assert host.games_art("absent.wsquashfs", "cover") is None
    assert host.games_art("Mon Jeu.wsquashfs", "icon", etag='"e1"') == ("not-modified", None, '"e1"')
    assert host.games_art("Mon Jeu.wsquashfs", "cover") == ("image/png", PNG, '"e1"')
    assert seen[-1].url.path == "/v1/games/art" and seen[-1].url.params["kind"] == "cover"
    assert seen[-1].headers["x-wsq-machine"] == "cafe" * 8
    with pytest.raises(RebuildError):
        host.games_art("boom.wsquashfs", "cover")
