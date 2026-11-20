"""Concordance du manager avec contract/agent-api.yaml, le contrat commun de l'agent du lanceur
(Go) et du manager. Le même contrat est validé côté Go (internal/contracttest du lanceur).

Ce fichier vérifie :
- le contrat lui-même et sa copie (identique à celle du lanceur quand les deux dépôts sont là) ;
- les versions minimales du manager (constantes) contre les `x-since-agent` du contrat ;
- les routes que le manager sert aux lanceurs (machine_api) : la liste, et chaque réponse ;
- les requêtes que AgentHost envoie à un agent, et les réponses que les faux agents des tests
  rendent (leurs échanges sont contrôlés dans tests/contract_check.py, voir les fixtures).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from app.services import agent as agent_mod
from app.services import clients, machine_api, saves_store
from app.services.agent import AgentHost
from tests import contract_check as cc
from tests.test_agent import make_cert
from tests.test_machine_api import GAME, fp, gz, mapi, put_tar  # noqa: F401  (fixture mapi)
from tests.test_saves import make_settings

LAUNCHER_COPY = Path(__file__).resolve().parents[3] / "wsquashfs-launcher" / "contract" / "agent-api.yaml"


# ------------------------------------------------------------------ le contrat et sa copie

def test_contract_is_complete_and_well_formed():
    doc = cc.contract()
    assert doc["openapi"].startswith("3.1")
    assert doc["jsonSchemaDialect"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["info"]["version"].isdigit()
    listed = cc.routes()
    assert len(listed) == 24
    for method, path, op in listed:
        label = f"{method} {path}"
        assert isinstance(op["x-since-agent"], int) and 1 <= op["x-since-agent"] <= int(doc["info"]["version"]), label
        assert set(op["tags"]) <= {"agent", "pair"} and op["tags"], label
        assert set(op["x-served-by"]) <= {"agent", "manager"} and op["x-served-by"], label
        assert op["description"].strip() and "200" in op["responses"], label
        assert ("agent" in op["tags"]) == ("agent" in op["x-served-by"]), label


def test_every_schema_reference_resolves():
    """Chaque $ref du contrat vise un objet qui existe."""
    text = cc.CONTRACT_PATH.read_text(encoding="utf-8")
    for ref in set(re.findall(r'\$ref: "(#/[^"]+)"', text)):
        assert cc._lookup(ref.lstrip("#")) is not None, ref


def test_the_copy_is_the_launchers_contract():
    if not LAUNCHER_COPY.exists():
        pytest.skip("dépôt du lanceur absent : copie non comparée (make contract la recopie)")
    assert cc.CONTRACT_PATH.read_bytes() == LAUNCHER_COPY.read_bytes(), \
        "backend/contract/agent-api.yaml diffère de celui du lanceur : make contract"


def since(method: str, path: str) -> int:
    return cc.operation(method, path)["x-since-agent"]


def test_manager_minimum_versions_follow_the_contract():
    assert clients.MANAGER_PEER_VERSION == since("PUT", "/v1/saves/manager")
    assert agent_mod.PAIR_VERSION == since("POST", "/v1/pair")
    assert since("DELETE", "/v1/saves/tar") == 5     # AgentHost.saves_delete : « agent plus ancien » sur 404/405
    assert since("GET", "/v1/saves/dirs") == 8       # agent v8 : dossiers déclarés (clients.py, saves.DIR_PREFIX)
    assert since("PUT", "/v1/saves/dirs") == since("DELETE", "/v1/saves/dirs") == 8
    for method, path in (("GET", "/v1/games/list"), ("POST", "/v1/games/dir"), ("GET", "/v1/games/part"),
                         ("PUT", "/v1/games/file"), ("POST", "/v1/games/commit"), ("DELETE", "/v1/games/file")):
        assert since(method, path) == 6, f"{method} {path}"
    assert since("POST", "/v1/update") == 11 and since("POST", "/v1/events") == 11


def test_the_contract_version_is_the_highest_version_the_manager_knows():
    """info.version : la version de protocole actuelle, celle des événements et de la mise à jour (11)."""
    top = max(op["x-since-agent"] for _, _, op in cc.routes())
    assert int(cc.contract()["info"]["version"]) >= top


def test_info_fields_read_by_the_manager_exist_in_the_contract():
    """Champs de GET /v1/info lus par le manager (saves_status, clients, activity, trial)."""
    props = cc.contract()["components"]["schemas"]["Info"]["properties"]
    for field in ("version", "host", "user", "os", "auto", "manager", "manager_ui", "build", "playing",
                  "controllers", "session_process"):
        assert field in props, field


# ------------------------------------------------------------------ routes servies par le manager

def test_manager_serves_exactly_the_contract_routes_it_claims():
    served = {(method, route.path) for route in machine_api.build_router(lambda: None).routes
              if getattr(route, "methods", None) for method in route.methods if method != "HEAD"}
    claimed = {(method, path) for method, path, op in cc.routes() if "manager" in op["x-served-by"]}
    assert served == claimed


def check(response, pair: bool = True):
    request = response.request
    target = request.url.raw_path.decode()
    problems = cc.check_response(request.method, target, response.status_code, response.headers,
                                 response.content, pair=pair)
    assert not problems, problems
    return response


def test_manager_info_and_auth_errors_follow_the_contract(mapi):  # noqa: F811
    client, _, headers_a, _ = mapi
    assert check(client.get("/v1/info", headers=headers_a)).json()["version"] == "manager"
    check(client.get("/v1/info"))                                           # 401
    check(client.get("/v1/info", headers={"Authorization": "Bearer jeton-a"}))      # 400
    check(client.get("/v1/info", headers={"Authorization": "Bearer jeton-a", "X-Wsq-Machine": "pas valide !"}))


def test_manager_events_follow_the_contract(mapi):  # noqa: F811
    client, _, headers_a, _ = mapi
    start = {"event": "start", "game": "Crazy Taxi 3", "at": 1700000000,
             "controllers": [{"kind": "xbox", "name": "Pad", "bus": "usb"}]}
    exit_ = {"event": "exit", "game": "Crazy Taxi 3", "at": 1700003600, "started": 1700000000, "code": 0}
    for body in (start, exit_, {"event": "start", "game": "Sans manette"}):
        request_problems = cc.check_request("POST", "/v1/events", headers_a, json.dumps(body).encode())
        assert not request_problems, request_problems
        assert check(client.post("/v1/events", json=body, headers=headers_a)).json() == {"ok": True}
    check(client.post("/v1/events", json={"event": "pause", "game": "X"}, headers=headers_a))      # 422
    check(client.post("/v1/events", json={"event": "start"}, headers=headers_a))                    # 422
    check(client.post("/v1/events", json={"event": "start", "game": "../x"}, headers=headers_a))    # 400


def test_manager_saves_routes_follow_the_contract(mapi):  # noqa: F811
    client, settings, headers_a, headers_b = mapi
    check(client.get("/v1/saves/list", headers=headers_a))
    # Envois : premier, même contenu, nouvelle version, copie d'une autre machine, taille et date.
    for data, who, extra in ((gz("v1"), headers_a, {}), (gz("v1"), headers_a, {}), (gz("v2"), headers_a, {}),
                             (gz("v3"), headers_b, {"X-Wsq-Newest": "1", "X-Wsq-Size": "7"}),
                             (gz("v4"), headers_a, {"X-Wsq-Content": fp(gz("v4")), "X-Wsq-Newest": "9999999999"}),
                             (gz("v5"), headers_b, {"X-Wsq-Newest": "1"})):
        put = {**who, **extra, "X-Wsq-Fingerprint": fp(data)}
        request = cc.check_request("PUT", f"/v1/saves/tar?game={GAME}", put, data)
        assert not request, request
        check(put_tar(client, {**who, **extra}, data))
    check(put_tar(client, {**headers_a, "X-Wsq-Dir": ".local%2Fshare%2Fx"}, gz("z"), game="dossier:Zelda", profile="p"))
    listed = check(client.get("/v1/saves/list", headers=headers_a)).json()["games"]
    assert {g["name"] for g in listed} == {GAME, "dossier:Zelda"}
    got = check(client.get("/v1/saves/tar", params={"game": GAME}, headers=headers_a))
    assert got.headers["content-type"] == "application/gzip"
    check(client.get("/v1/saves/tar", params={"game": "Absent"}, headers=headers_a))                # 404
    check(client.get("/v1/saves/tar", params={"game": "../x"}, headers=headers_a))                  # 400
    current = saves_store.current_fingerprint(settings, GAME)
    check(client.post("/v1/saves/state", json={"game": GAME, "fingerprint": current}, headers=headers_b))
    check(client.post("/v1/saves/state", json={"game": GAME, "fingerprint": "autre"}, headers=headers_b))   # 409
    check(client.post("/v1/saves/state", json={"game": "../x", "fingerprint": "a"}, headers=headers_b))     # 400
    check(client.post("/v1/saves/state", json={"fingerprint": "a"}, headers=headers_b))                     # 422
    check(put_tar(client, headers_a, b"texte"))                                                      # 400
    check(put_tar(client, headers_a, gz("v1"), fingerprint="abc"))                                    # 400
    check(client.put("/v1/saves/tar", params={"game": GAME}, content=gz("v1"),
                     headers={"Authorization": "Bearer inconnu", "X-Wsq-Machine": "x"}))             # 401


def test_manager_put_over_the_limit_follows_the_contract(mapi, monkeypatch):  # noqa: F811
    client, _, headers_a, _ = mapi
    monkeypatch.setattr(machine_api, "MAX_ARCHIVE", 8)
    check(put_tar(client, headers_a, gz("trop long")))


# ------------------------------------------------------------------ requêtes de AgentHost

PNG_B64 = "iVBORw0KGgo="
FP = hashlib.sha256(b"x").hexdigest()

# Réponses canned d'un agent : chacune doit elle-même être dans le contrat (test_canned_replies).
INFO = {"version": "11", "host": "steambox", "user": "arcade", "launcher": "/usr/bin/wsquashfs-launcher",
        "roms": ["/home/arcade/roms"], "session_process": "emulationstation", "os": "steamos",
        "games_dir": "/home/arcade/Games", "games_dir_set": False, "manager": FP.upper(),
        "manager_ui": "http://10.0.0.2:8765", "auto": True, "build": "abc123def456",
        "playing": [{"game": "Jeu", "since": 1700000000}],
        "controllers": [{"kind": "dualsense", "name": "DualSense", "bus": "bluetooth"}]}
DIRS = {"dirs": [{"name": "zelda", "path": ".local/share/zelda"}], "eden": ""}
CANNED: dict[tuple[str, str], tuple[int, object, dict]] = {
    ("GET", "/v1/info"): (200, INFO, {}),
    ("GET", "/v1/doctor"): (200, {"code": 0, "sections": [{"title": "Système", "items": [
        {"status": "ok", "text": "x"}]}], "runners": [{"name": "GE-Proton", "path": "/opt/ge"}],
        "verdict": "✓ Prêt à lancer des jeux"}, {}),
    ("POST", "/v1/update"): (200, {"code": 0, "output": "ok", "before": "a", "after": "b", "restart": True}, {}),
    ("POST", "/v1/trial"): (200, {"report": {"code": 0, "elapsed": 60, "launcher_alive": True, "game_seen": True,
                                             "games": ["Jeu"], "windows": [], "left": {"procs": 0, "mounts": 0},
                                             "screenshot": True, "log": ""}, "screenshot": PNG_B64}, {}),
    ("POST", "/v1/maintenance"): (200, {"action": "clean", "code": 0, "output": "ok"}, {}),
    ("GET", "/v1/runner/list"): (200, {"path": "/opt/ge", "entries": {"files/bin/wine": 10, "proton": 0}}, {}),
    ("GET", "/v1/runner/tar"): (200, b"TARDATA", {"Content-Type": "application/x-tar"}),
    ("GET", "/v1/saves/list"): (200, {"games": [{"name": "Jeu", "fingerprint": FP, "content": FP, "newest": 1,
                                                  "size": 2, "state": ""}]}, {}),
    ("GET", "/v1/saves/tar"): (200, b"\x1f\x8bdata", {"Content-Type": "application/gzip",
                                                       "X-Wsq-Fingerprint": FP, "X-Wsq-Profile": "deck"}),
    ("PUT", "/v1/saves/tar"): (200, {"fingerprint": FP, "archived": ""}, {}),
    ("DELETE", "/v1/saves/tar"): (200, {"archived": "/b/x.tar.gz", "removed": 3}, {}),
    ("POST", "/v1/saves/state"): (200, {"fingerprint": FP}, {}),
    ("GET", "/v1/saves/dirs"): (200, DIRS, {}),
    ("PUT", "/v1/saves/dirs"): (200, DIRS, {}),
    ("DELETE", "/v1/saves/dirs"): (200, DIRS, {}),
    ("PUT", "/v1/saves/manager"): (200, {"manager": FP.upper(), "auto": False}, {}),
    ("GET", "/v1/games/list"): (200, {"dir": "/h/Games", "set": False, "free": 5, "games": [
        {"name": "Jeu.wsquashfs", "size": 1, "mtime": 2}], "parts": []}, {}),
    ("POST", "/v1/games/dir"): (200, {"dir": "/h/Games", "set": True}, {}),
    ("GET", "/v1/games/part"): (200, {"part_size": 0, "installed": False, "size": 0}, {}),
    ("PUT", "/v1/games/file"): (200, {"part_size": 3}, {}),
    ("POST", "/v1/games/commit"): (200, {"name": "Jeu.wsquashfs", "size": 3}, {}),
    ("DELETE", "/v1/games/file"): (200, {"removed": True}, {}),
}


def reply_of(canned: tuple[int, object, dict]) -> httpx.Response:
    status, payload, headers = canned
    if isinstance(payload, bytes):
        return httpx.Response(status, content=payload, headers=headers)
    return httpx.Response(status, json=payload, headers=headers)


@pytest.fixture
def pinned_host(tmp_path):
    """AgentHost dont le transport est un faux agent qui contrôle chaque requête reçue et rend la
    réponse canned de la route ; `problems` reçoit les écarts, `seen` les routes appelées."""
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    cert, _ = make_cert(tmp_path)
    problems: list[str] = []
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        target = request.url.raw_path.decode()
        problems.extend(cc.check_request(request.method, target, request.headers, body))
        key = (request.method, request.url.path)
        seen.append(key)
        reply = reply_of(CANNED[key])
        problems.extend(cc.check_response(request.method, target, reply.status_code, reply.headers, reply.content))
        return reply

    settings = make_settings(tmp_path)
    host = AgentHost(settings, 5, pem=cert.read_text(), url="https://agent.test", token="jeton", client_id="cafe" * 8)
    host.client = httpx.Client(base_url="https://agent.test", headers={"Authorization": "Bearer jeton"},
                               transport=httpx.MockTransport(handler))
    return host, problems, seen


def test_canned_replies_are_in_the_contract():
    for (method, path), (status, payload, headers) in CANNED.items():
        reply = reply_of((status, payload, headers))
        assert cc.check_response(method, path, status, reply.headers, reply.content) == [], (method, path)


def test_agent_host_requests_follow_the_contract(pinned_host, tmp_path):
    host, problems, seen = pinned_host
    archive = tmp_path / "save.tar.gz"
    archive.write_bytes(b"\x1f\x8bdata")
    out = tmp_path / "recu.tar.gz"
    host.info()
    host.doctor()
    host.update()
    assert host.trial("win/Jeu.wsquashfs", 60)[0]["game_seen"] is True
    host._call("POST", "/v1/maintenance", json={"action": "save", "rel": "win/Jeu.wsquashfs"})
    host._call("POST", "/v1/maintenance", json={"action": "sync-status"})
    assert host.listing("/opt/ge")["proton"] == 0
    chunks: list[bytes] = []
    host.fetch("/opt/ge", chunks.append)
    assert host.saves_list()[0]["name"] == "Jeu"
    host.saves_get("Jeu", out)
    host.saves_put("Jeu", archive, "deck")
    host.saves_put("dossier:Zelda", archive, None, dir_path=".local/share/zelda")
    host.saves_delete("Jeu")
    host.saves_ack("Jeu", FP)
    host.saves_manager_put("wsquashfs-manager (h)", "https://10.0.0.2:8765", "jeton-neuf_1", FP.upper())
    host.saves_manager_put("wsquashfs-manager (h)", "https://10.0.0.2:8765", "jeton-neuf_1", FP.upper(),
                           ui="http://10.0.0.2:8000")
    host.saves_dirs()
    host.saves_dir_put("zelda", ".local/share/zelda")
    host.saves_dir_delete("zelda")
    host.games_list()
    host.games_set_dir("/h/Games")
    host.games_set_dir("")
    host.games_part("Jeu.wsquashfs")
    host.games_put("Jeu.wsquashfs", 0, [b"abc"], 3, 3)
    host.games_commit("Jeu.wsquashfs", 3, FP)
    host.games_delete("Jeu.wsquashfs")
    assert not problems, problems
    # Chaque route de l'agent que le manager appelle l'a été (hors pair : voir test_agent_pair_request).
    called = set(seen)
    for method, path, op in cc.routes():
        if "agent" in op["x-served-by"] and (method, path) != ("POST", "/v1/pair"):
            assert (method, path) in called, f"{method} {path} jamais appelée par AgentHost"


def test_agent_host_sends_the_machine_header_everywhere(pinned_host):
    host, problems, _ = pinned_host
    host.info()
    host.games_list()
    assert not problems, problems
    assert host.client_id == "cafe" * 8


# ------------------------------------------------------------------ exemples de l'association

def test_pair_reply_example_is_in_the_contract():
    reply = {"token": "abc_DEF-123", "host": "steambox", "version": "11"}
    assert cc.check_schema("PairReply", reply) == []
    assert cc.check_schema("PairReply", {**reply, "version": "onze"}) != []
    assert cc.check_response("POST", "/v1/pair", 401, {"content-type": "application/json"},
                             json.dumps({"detail": "code d'association expiré ou déjà utilisé : relancer --pair"}).encode()) == []
    assert cc.check_response("POST", "/v1/pair", 404, {"content-type": "text/plain; charset=utf-8"},
                             b"404 page not found\n") == []


def test_the_contract_rejects_what_it_should():
    """Garde-fou des garde-fous : un écart est bien vu."""
    assert cc.check_response("GET", "/v1/info", 200, {"content-type": "application/json"},
                             b'{"version": "11"}') != []
    assert cc.check_response("GET", "/v1/info", 418, {"content-type": "application/json"}, b"{}") != []
    assert cc.check_request("GET", "/v1/saves/list", {}, b"") != []            # X-Wsq-Machine obligatoire
    assert cc.check_request("POST", "/v1/events", {"X-Wsq-Machine": "m"}, b'{"event": "pause", "game": "x"}') != []
    assert cc.check_response("GET", "/v1/saves/tar", 200, {"content-type": "application/gzip"}, b"") != []
