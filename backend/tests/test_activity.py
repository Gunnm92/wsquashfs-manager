"""Activité des clients (services/activity.py) : événements de partie reçus par machine_api, état de
référence relu par GET /v1/info, plantages, historique, et routes de l'interface (activité, mise à jour)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import deps, main
from app.services import activity, clients
from app.services.agent import AgentUnreachable
from tests.test_machine_api import mapi  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def clean_activity():
    activity.reset()
    yield
    activity.reset()


def test_start_then_exit_tracks_the_game(mapi):  # noqa: F811
    client, settings, headers_a, _ = mapi
    start = client.post("/v1/events", headers=headers_a, json={
        "event": "start", "game": "Crazy Taxi 3", "at": 1000,
        "controllers": [{"kind": "dualsense", "name": "DualSense", "bus": "bluetooth"}, {"kind": "inconnu"}]})
    assert start.status_code == 200
    snap = activity.snapshot()
    assert snap["live"]["aaaa0001"] == [{"game": "Crazy Taxi 3", "since": 1000}]
    assert snap["controllers"]["aaaa0001"] == [{"kind": "dualsense", "name": "DualSense", "bus": "bluetooth"}]
    seq = snap["seq"]
    client.post("/v1/events", headers=headers_a, json={"event": "exit", "game": "Crazy Taxi 3", "at": 3700,
                                                         "started": 1000, "code": 0})
    snap = activity.snapshot(seq)
    assert snap["live"]["aaaa0001"] == []
    [ev] = snap["events"]
    assert (ev["name"], ev["event"], ev["duration"], ev["code"], ev["crash"]) == ("steambox", "exit", 2700, 0, False)
    history = (settings.state_dir / "activity.jsonl").read_text().splitlines()
    assert json.loads(history[-1])["game"] == "Crazy Taxi 3"


def test_quick_error_exit_is_a_crash(mapi):  # noqa: F811
    client, _, headers_a, _ = mapi
    client.post("/v1/events", headers=headers_a, json={"event": "exit", "game": "Jeu", "at": 120, "started": 100, "code": 1})
    client.post("/v1/events", headers=headers_a, json={"event": "exit", "game": "Jeu", "at": 1000, "started": 100, "code": 1})
    first, second = activity.snapshot()["events"]
    assert first["crash"] is True and second["crash"] is False      # 20 s : plantage ; 15 min : non


def test_events_need_a_token_and_a_valid_body(mapi):  # noqa: F811
    client, _, headers_a, _ = mapi
    assert client.post("/v1/events", json={"event": "start", "game": "Jeu"}).status_code == 401
    assert client.post("/v1/events", headers=headers_a, json={"event": "autre", "game": "Jeu"}).status_code == 400
    assert client.post("/v1/events", headers=headers_a, json={"event": "start", "game": "../x"}).status_code == 400
    assert activity.snapshot()["events"] == []


def test_info_is_the_reference_state():
    activity.from_info("m1", {"version": "10"})                      # agent ancien : rien
    assert "m1" not in activity.snapshot()["live"]
    activity.from_info("m1", {"playing": [{"game": "A", "since": 5}, {"game": ""}],
                              "controllers": [{"kind": "xbox", "name": "Pad", "bus": "usb"}]})
    snap = activity.snapshot()
    assert snap["live"]["m1"] == [{"game": "A", "since": 5}]
    assert snap["controllers"]["m1"] == [{"kind": "xbox", "name": "Pad", "bus": "usb"}]
    activity.from_info("m1", {"playing": [], "controllers": []})    # la partie s'est terminée entre-temps
    assert activity.snapshot()["live"]["m1"] == []
    activity.forget("m1")
    assert "m1" not in activity.snapshot()["controllers"]


def test_history_is_trimmed(tmp_path, monkeypatch):
    from tests.test_saves import make_settings
    settings = make_settings(tmp_path)
    monkeypatch.setattr(activity, "HISTORY_MAX", 5)
    monkeypatch.setattr(activity, "HISTORY_KEEP", 3)
    for i in range(7):
        activity.record(settings, {"id": "m", "name": "m"}, {"event": "exit", "game": f"J{i}", "at": 10 + i, "started": 1, "code": 0})
    lines = (settings.state_dir / "activity.jsonl").read_text().splitlines()
    assert len(lines) <= 5 and json.loads(lines[-1])["game"] == "J6"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from tests.test_saves import make_settings
    settings = make_settings(tmp_path)
    settings.mode = "connecte"                                   # routes des clients : mode connecté
    monkeypatch.setattr(deps, "_settings", settings)
    return TestClient(main.app, headers={"x-wsfs-token": ""})     # en-tête de l'interface


def test_activity_route_returns_events_after_since(ui):
    activity.record(deps._settings, {"id": "m", "name": "SteamBox"}, {"event": "start", "game": "A", "at": 1})
    all_ = ui.get("/api/clients/activity").json()
    assert all_["seq"] == 1 and len(all_["events"]) == 1 and all_["live"]["m"][0]["game"] == "A"
    assert ui.get("/api/clients/activity", params={"since": 1}).json()["events"] == []


def test_update_route(ui, monkeypatch):
    class Host:
        def update(self):
            return {"code": 0, "output": "ok", "before": "a", "after": "b", "restart": True}

    monkeypatch.setattr(clients, "open_machine", lambda settings, machine_id, timeout: Host())
    assert ui.post("/api/saves/machines/m1/update").json()["restart"] is True

    class Down:
        def update(self):
            raise AgentUnreachable("agent injoignable")

    monkeypatch.setattr(clients, "open_machine", lambda settings, machine_id, timeout: Down())
    assert ui.post("/api/saves/machines/m1/update").status_code == 502
