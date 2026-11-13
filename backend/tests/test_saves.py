"""Réserve de sauvegardes : machines (association), réserve (rotation), pull, push,
push_from, états, tâche et API.

De faux wsquashfs-agent tournent dans des threads, en HTTPS avec un certificat
auto-signé par machine (test ignoré sans openssl). Ils gardent les sauvegardes
en mémoire et journalisent chaque requête (en-tête X-Wsq-Machine, corps).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, save_saves_machines, saves_client_id, saves_machines
from app.models import Task
from app.services import saves
from app.services.rebuild import Job, RebuildError
from app.services.tasks import TaskQueue
from app.services.trial import AgentHost, SavesBusy, fingerprint_pem

TOKEN = "jeton-saves"


def fp_of(data: bytes) -> str:
    return "F" + hashlib.sha256(data).hexdigest()[:12].upper()


def make_cert(tmp: Path) -> tuple[Path, Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=test",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


class SavesAgent(BaseHTTPRequestHandler):
    """Faux agent : `st` (propre à chaque serveur) porte l'état et le journal."""

    protocol_version = "HTTP/1.0"
    st: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, path: str, **extra):
        self.st["log"].append({"method": method, "path": path,
                               "machine": self.headers.get("X-Wsq-Machine"), **extra})

    def _auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.st['token']}":
            self._send(401, b'{"detail":"jeton refuse"}')
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        self._record("GET", url.path)
        if not self._auth():
            return
        if url.path == "/v1/info":
            return self._send(200, json.dumps({"version": "1", "host": self.st["host"], "user": "arcade"}).encode())
        if url.path == "/v1/saves/list":
            games = [{"name": name, "fingerprint": fp_of(g["data"]), "newest": g["newest"], "state": ""}
                     for name, g in sorted(self.st["games"].items())]
            return self._send(200, json.dumps({"games": games}).encode())
        if url.path == "/v1/saves/tar":
            game = parse_qs(url.query).get("game", [""])[0]
            if game in self.st["busy"]:
                return self._send(409, b'{"detail":"jeu en cours"}')
            if game not in self.st["games"]:
                return self._send(404, b'{"detail":"aucune sauvegarde"}')
            entry = self.st["games"][game]
            headers = {"X-Wsq-Fingerprint": fp_of(entry["data"])}
            if entry.get("profile"):
                headers["X-Wsq-Profile"] = entry["profile"]
            return self._send(200, entry["data"], "application/gzip", headers)
        self._send(404, b'{"detail":"route inconnue"}')

    def do_PUT(self):
        url = urlsplit(self.path)
        game = parse_qs(url.query).get("game", [""])[0]
        profile = self.headers.get("X-Wsq-Profile")
        self._record("PUT", url.path, game=game, profile=profile)
        if not self._auth():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if game in self.st["busy"]:
            return self._send(409, b'{"detail":"jeu en cours"}')
        if length > self.st.get("max", 1 << 30):
            return self._send(413, b'{"detail":"archive trop volumineuse"}')
        if not body.startswith(b"GZ"):
            return self._send(400, b'{"detail":"archive refusee"}')
        previous = self.st["games"].get(game)
        archived = previous is not None and fp_of(previous["data"]) != fp_of(body)
        self.st["games"][game] = {"data": body, "profile": profile, "newest": self.st.get("now", 1000)}
        self.st["archived"] += [game] if archived else []
        self._send(200, json.dumps({"fingerprint": fp_of(body), "archived": archived}).encode())

    def do_POST(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record("POST", url.path, body=body)
        if not self._auth():
            return
        if url.path == "/v1/saves/state":
            if body.get("game") in self.st["conflict"]:
                return self._send(409, b'{"detail":"machine modifiee entre-temps"}')
            return self._send(200, b"{}")
        self._send(404, b'{"detail":"route inconnue"}')


@pytest.fixture
def agents(tmp_path):
    """Fabrique de faux agents (un par machine) ; arrêtés à la fin du test."""
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    servers = []

    def make(host: str, games=None, busy=(), conflict=(), now=1000, token=TOKEN, cert_dir=None):
        cert, key = make_cert(tmp_path / (cert_dir or host))
        st = {"host": host, "token": token, "busy": set(busy), "conflict": set(conflict),
              "now": now, "archived": [], "log": [],
              "games": {name: {"data": data, "profile": profile, "newest": newest}
                        for name, (data, profile, newest) in (games or {}).items()}}
        handler = type("H", (SavesAgent,), {"st": st})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        pem = cert.read_text()
        return {"url": f"https://127.0.0.1:{server.server_address[1]}", "port": server.server_address[1],
                "pem": pem, "fp": fingerprint_pem(pem).replace(":", ""), "st": st, "host": host}

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def make_settings(tmp: Path, **kw) -> Settings:
    settings = Settings(state_dir=tmp / "state", runtime_config_file=tmp / "config.json", **kw)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


def associate(settings: Settings, agent: dict) -> dict:
    return saves.add_machine(settings, f"wsq://{TOKEN}@127.0.0.1:{agent['port']}#{agent['fp']}")


def machine_ids(settings: Settings) -> dict[str, str]:
    """Nom → identifiant des machines enregistrées."""
    return {m["name"]: m["id"] for m in saves.machine_rows(settings)}


def gz(text: str) -> bytes:
    return b"GZ" + text.encode()


def part_with(settings: Settings, game: str, data: bytes) -> Path:
    d = saves.reserve_dir(settings, game)
    d.mkdir(parents=True, exist_ok=True)
    part = d / "test.part"
    part.write_bytes(data)
    return part


# ------------------------------------------------------------------ noms et configuration

def test_game_names_are_derived_like_the_image_stem():
    assert saves.game_name("Jeu.wsquashfs") == "Jeu"
    assert saves.game_name("Jeu.PC.wsquashfs") == "Jeu"
    assert saves.game_name("Jeu.pc") == "Jeu"
    assert saves.game_name("Jeu Deux.wsquashfs") == "Jeu Deux"


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", ".cache", "", "a..b", "x\x00y", "x" * 201])
def test_invalid_game_names_are_refused(name):
    assert not saves.valid_game(name)
    with pytest.raises(RebuildError, match="nom de jeu invalide"):
        saves.check_game(name)


def test_client_id_is_created_once(tmp_path):
    settings = make_settings(tmp_path)
    first = saves_client_id(settings)
    assert len(first) == 32 and saves_client_id(settings) == first
    assert json.loads((tmp_path / "config.json").read_text())["saves_client_id"] == first


# ------------------------------------------------------------------ association

def test_association_names_the_machine_and_hides_secrets(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    added = associate(settings, steambox)
    assert added["name"] == "steambox" and added["replaced"] is False
    rows = saves.machine_rows(settings)
    assert [r["name"] for r in rows] == ["steambox"]
    text = json.dumps({"added": added, "rows": rows})
    assert TOKEN not in text and "BEGIN CERTIFICATE" not in text
    assert set(rows[0]) == {"id", "name", "url", "fingerprint", "kind", "removable"}


def test_same_fingerprint_or_same_url_replaces(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    first = associate(settings, steambox)
    again = associate(settings, steambox)
    assert again["replaced"] is True and again["id"] == first["id"]
    assert len(saves_machines(settings)) == 1
    # même adresse, autre certificat (agent réinstallé) : remplacement, identifiant gardé
    machines = saves_machines(settings)
    machines[0]["pem"] = agents("autre", cert_dir="autre")["pem"]
    save_saves_machines(settings, machines)
    third = associate(settings, steambox)
    assert third["replaced"] is True and third["id"] == first["id"] and len(saves_machines(settings)) == 1


def test_wrong_fingerprint_or_token_is_refused(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    with pytest.raises(RebuildError, match="ne correspond pas"):
        saves.add_machine(settings, f"wsq://{TOKEN}@127.0.0.1:{steambox['port']}#{'AB' * 32}")
    bad = agents("refusee", token="autre")
    with pytest.raises(RebuildError, match="jeton"):
        associate(settings, bad)
    assert saves_machines(settings) == []


def test_game_host_listed_first_unless_same_url(agents, tmp_path):
    host = agents("sb-agent")
    settings = make_settings(tmp_path, game_host="agent", agent_url=host["url"], agent_token=TOKEN)
    (settings.state_dir / "agent-cert.pem").write_text(host["pem"])
    rows = saves.machine_rows(settings)
    assert rows[0]["id"] == "game-host" and rows[0]["removable"] is False
    assert rows[0]["fingerprint"] == fingerprint_pem(host["pem"])
    with pytest.raises(ValueError, match="non supprimable"):
        saves.remove_machine(settings, "game-host")
    # la même adresse enregistrée dans la liste : elle remplace la ligne d'office
    associate(settings, host)
    rows = saves.machine_rows(settings)
    assert [r["id"] for r in rows] != ["game-host"] and len(rows) == 1


def test_remove_machine(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    added = associate(settings, steambox)
    saves.remove_machine(settings, added["id"])
    assert saves_machines(settings) == []
    with pytest.raises(KeyError):
        saves.remove_machine(settings, added["id"])


# ------------------------------------------------------------------ client (AgentHost)

def test_requests_carry_the_client_identifier(agents, tmp_path):
    steambox = agents("steambox", games={"Jeu": (gz("a"), "wine", 5)})
    settings = make_settings(tmp_path)
    host = AgentHost(settings, 10, pem=steambox["pem"], url=steambox["url"], token=TOKEN, client_id="cafe" * 8)
    assert host.saves_list()[0]["name"] == "Jeu"
    host.saves_ack("Jeu", "FX")
    assert [e["machine"] for e in steambox["st"]["log"]] == ["cafe" * 8, "cafe" * 8]


def test_busy_game_raises_savesbusy(agents, tmp_path):
    steambox = agents("steambox", games={"Jeu": (gz("a"), None, 5)}, busy={"Jeu"})
    host = AgentHost(make_settings(tmp_path), 10, pem=steambox["pem"], url=steambox["url"], token=TOKEN)
    with pytest.raises(SavesBusy, match="jeu en cours"):
        host.saves_get("Jeu", tmp_path / "x.part")


def test_saves_errors_are_clear(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    bad_token = AgentHost(settings, 10, pem=steambox["pem"], url=steambox["url"], token="faux")
    with pytest.raises(RebuildError, match="jeton refusé : refaire l'association"):
        bad_token.saves_list()
    other = agents("autre", cert_dir="autre-cert")
    wrong_cert = AgentHost(settings, 10, pem=other["pem"], url=steambox["url"], token=TOKEN)
    with pytest.raises(RebuildError, match="certificat de la machine différent"):
        wrong_cert.saves_list()
    closed = AgentHost(settings, 2, pem=steambox["pem"], url="https://127.0.0.1:1", token=TOKEN)
    with pytest.raises(RebuildError, match="injoignable"):
        closed.saves_list()


# ------------------------------------------------------------------ réserve

def test_store_writes_current_and_meta(tmp_path):
    settings = make_settings(tmp_path)
    part = part_with(settings, "Jeu", gz("v1"))
    meta = saves.store(settings, "Jeu", part, fingerprint="F1", newest=42, profile="wine-tkg",
                       source="steambox")
    d = saves.reserve_dir(settings, "Jeu")
    assert (d / "current.tar.gz").read_bytes() == gz("v1") and not part.exists()
    stored = json.loads((d / "meta.json").read_text())
    assert stored == meta and stored["fingerprint"] == "F1" and stored["newest"] == 42
    assert stored["source"] == "steambox" and stored["profile"] == "wine-tkg"
    assert stored["fetched_at"].endswith("+00:00")
    assert saves.reserve_games(settings) == ["Jeu"]


def test_rotation_keeps_the_three_most_recent_versions(tmp_path):
    settings = make_settings(tmp_path)
    versions = [gz(f"v{i}") for i in range(5)]
    for i, data in enumerate(versions):
        part = part_with(settings, "Jeu", data)
        saves.store(settings, "Jeu", part, fingerprint=fp_of(data), newest=i, profile=None, source="s")
    d = saves.reserve_dir(settings, "Jeu")
    archived = saves.archives(settings, "Jeu")
    assert len(archived) == 3
    assert [(d / f"{name}.tar.gz").read_bytes() for name in archived] == [versions[3], versions[2], versions[1]]
    assert all((d / f"{name}.json").is_file() for name in archived)
    assert (d / "current.tar.gz").read_bytes() == versions[4]
    assert json.loads((d / "meta.json").read_text())["fingerprint"] == fp_of(versions[4])


def test_same_version_is_not_archived(tmp_path):
    settings = make_settings(tmp_path)
    data = gz("same")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint="F", newest=1,
                profile=None, source="s")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint="F", newest=2,
                profile=None, source="s")
    assert saves.archives(settings, "Jeu") == []
    assert saves.reserve_meta(settings, "Jeu")["newest"] == 2


def test_store_refuses_invalid_game_name(tmp_path):
    settings = make_settings(tmp_path)
    part = tmp_path / "x.part"
    part.write_bytes(gz("x"))
    with pytest.raises(RebuildError, match="nom de jeu invalide"):
        saves.store(settings, "../evasion", part, fingerprint="F", newest=1, profile=None, source="s")
    assert not (settings.state_dir / "evasion").exists()


# ------------------------------------------------------------------ pull

def test_pull_stores_each_game_and_acks(agents, tmp_path):
    steambox = agents("steambox", games={"Alpha": (gz("a"), "wine-tkg", 300),
                                         "Beta": (gz("b"), None, 200)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    source = machine_ids(settings)["steambox"]
    result = saves.pull(settings, source, None)
    assert [r["game"] for r in result["results"]] == ["Alpha", "Beta"]
    assert all(r["ok"] for r in result["results"])
    assert saves.reserve_dir(settings, "Alpha").joinpath("current.tar.gz").read_bytes() == gz("a")
    assert saves.reserve_meta(settings, "Alpha")["newest"] == 300
    assert saves.reserve_meta(settings, "Alpha")["source"] == "steambox"
    acks = [e for e in steambox["st"]["log"] if e["path"] == "/v1/saves/state"]
    assert [a["body"] for a in acks] == [{"game": "Alpha", "fingerprint": fp_of(gz("a"))},
                                         {"game": "Beta", "fingerprint": fp_of(gz("b"))}]
    saves_calls = [e for e in steambox["st"]["log"] if e["path"].startswith("/v1/saves")]
    assert {e["machine"] for e in saves_calls} == {saves_client_id(settings)}


def test_pull_reports_busy_and_missing_games_per_game(agents, tmp_path):
    steambox = agents("steambox", games={"Alpha": (gz("a"), None, 1)}, busy={"Occupe"})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    source = machine_ids(settings)["steambox"]
    steambox["st"]["games"]["Occupe"] = {"data": gz("o"), "profile": None, "newest": 2}
    result = saves.pull(settings, source, ["Alpha", "Occupe", "Absent"])
    by_game = {r["game"]: r for r in result["results"]}
    assert by_game["Alpha"]["ok"] is True
    assert by_game["Occupe"]["ok"] is False and "jeu en cours" in by_game["Occupe"]["message"]
    assert by_game["Absent"]["ok"] is False and "aucune sauvegarde" in by_game["Absent"]["message"]
    assert saves.reserve_meta(settings, "Occupe") is None


# ------------------------------------------------------------------ push

def test_push_to_two_targets_one_busy(agents, tmp_path):
    a = agents("a-machine")
    b = agents("b-machine", busy={"Jeu"})
    settings = make_settings(tmp_path)
    associate(settings, a)
    associate(settings, b)
    ids = machine_ids(settings)
    data = gz("reserve")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=9,
                profile="proton", source="x")
    result = saves.push(settings, [ids["a-machine"], ids["b-machine"]], ["Jeu"])
    by_machine = {r["machine"]: r for r in result["results"]}
    assert by_machine["a-machine"]["ok"] is True and by_machine["a-machine"]["archived"] is False
    assert by_machine["b-machine"]["ok"] is False and "jeu en cours" in by_machine["b-machine"]["message"]
    put = [e for e in a["st"]["log"] if e["method"] == "PUT"][0]
    assert put["machine"] == saves_client_id(settings) and put["profile"] == "proton"
    assert a["st"]["games"]["Jeu"]["data"] == data
    assert [e["body"] for e in a["st"]["log"] if e["path"] == "/v1/saves/state"] == [
        {"game": "Jeu", "fingerprint": fp_of(data)}]


def test_push_archives_on_target_when_it_changed(agents, tmp_path):
    a = agents("a-machine", games={"Jeu": (gz("ancien"), None, 1)})
    settings = make_settings(tmp_path)
    associate(settings, a)
    data = gz("nouveau")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=9,
                profile=None, source="x")
    result = saves.push(settings, [machine_ids(settings)["a-machine"]], ["Jeu"])
    assert result["results"][0]["archived"] is True and a["st"]["archived"] == ["Jeu"]


def test_push_from_pulls_then_pushes_what_was_received(agents, tmp_path):
    source = agents("source", games={"G1": (gz("g1"), None, 500), "G2": (gz("g2"), None, 400)})
    a = agents("cible-a")
    b = agents("cible-b", busy={"G2"})
    settings = make_settings(tmp_path)
    associate(settings, source)
    associate(settings, a)
    associate(settings, b)
    ids = machine_ids(settings)
    result = saves.push_from(settings, ids["source"], [ids["cible-a"], ids["cible-b"]], None)
    assert [r["ok"] for r in result["pull"]["results"]] == [True, True]
    pushed = {(r["game"], r["machine"]): r["ok"] for r in result["push"]["results"]}
    assert pushed == {("G1", "cible-a"): True, ("G1", "cible-b"): True,
                      ("G2", "cible-a"): True, ("G2", "cible-b"): False}
    assert a["st"]["games"]["G2"]["data"] == gz("g2")


def test_push_from_sends_nothing_when_pull_failed(agents, tmp_path):
    source = agents("source", busy={"G1"}, games={"G1": (gz("g"), None, 1)})
    a = agents("cible-a")
    settings = make_settings(tmp_path)
    associate(settings, source)
    associate(settings, a)
    ids = machine_ids(settings)
    result = saves.push_from(settings, ids["source"], [ids["cible-a"]], ["G1"])
    assert result["push"] == {"results": []}
    assert not [e for e in a["st"]["log"] if e["method"] == "PUT"]


def test_failed_push_marks_the_task_failed_with_its_result(agents, tmp_path):
    a = agents("a-machine", busy={"Jeu"})
    settings = make_settings(tmp_path)
    associate(settings, a)
    data = gz("r")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=1,
                profile=None, source="x")
    task = Task(id="t1", kind="saves", params={"op": "push", "targets": [machine_ids(settings)["a-machine"]],
                                               "games": ["Jeu"]})
    with pytest.raises(RebuildError, match="1 échec"):
        saves.run_saves(settings, task, Job())
    assert task.params["result"]["results"][0]["ok"] is False


def test_push_auto_takes_each_game_from_its_most_recent_machine(agents, tmp_path):
    # A : la plus récente est m1 ; B : la plus récente est m2 (sources différentes)
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300), "B": (gz("b-old"), None, 100)})
    m2 = agents("m2", games={"B": (gz("b-new"), None, 900)})
    m3 = agents("m3", games={"A": (gz("a-old"), None, 10)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    associate(settings, m3)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m1"], ids["m2"], ids["m3"]], None)
    pushed = {(r["game"], r["machine"]): r["ok"] for r in result["push"]["results"] if r["machine"]}
    # A part de m1 vers m2 et m3 (pas vers elle-même) ; B part de m2 vers m1 et m3
    assert pushed == {("A", "m2"): True, ("A", "m3"): True, ("B", "m1"): True, ("B", "m3"): True}
    assert m2["st"]["games"]["A"]["data"] == gz("a-new")
    assert m3["st"]["games"]["A"]["data"] == gz("a-new")
    assert m1["st"]["games"]["B"]["data"] == gz("b-new")
    assert m3["st"]["games"]["B"]["data"] == gz("b-new")
    # Les sources ne reçoivent rien d'elles-mêmes
    assert not [e for e in m1["st"]["log"] if e["method"] == "PUT" and e["game"] == "A"]
    assert not [e for e in m2["st"]["log"] if e["method"] == "PUT" and e["game"] == "B"]


def test_push_auto_skips_identical_and_reserve_only_games(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300), "C": (gz("c"), None, 50)})
    m2 = agents("m2", games={"A": (gz("a-old"), None, 10), "C": (gz("c"), None, 40)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    data = gz("reserve")
    saves.store(settings, "D", part_with(settings, "D", data), fingerprint=fp_of(data), newest=7,
                profile=None, source="m1")
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m1"], ids["m2"]], None)
    by_game = {r["game"]: r for r in result["push"]["results"] if not r["machine"]}
    assert by_game["C"]["ok"] is True and by_game["C"]["message"] == "déjà identiques"
    assert by_game["D"]["ok"] is True and by_game["D"]["message"] == "aucune machine ne l'a : rien à pousser"
    # Aucun transfert pour C ni D : seul A part de m1 vers m2
    assert not [e for e in m1["st"]["log"] + m2["st"]["log"] if e["method"] == "PUT" and e["game"] in ("C", "D")]
    assert [(r["game"], r["machine"]) for r in result["push"]["results"] if r["machine"]] == [("A", "m2")]
    assert m2["st"]["games"]["A"]["data"] == gz("a-new")


def test_push_auto_reports_a_game_whose_only_target_is_its_source(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300)})
    m2 = agents("m2", games={"A": (gz("a-old"), None, 10)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m1"]], None)
    assert result["push"]["results"] == [{"game": "A", "machine": "", "machine_id": "", "ok": True,
                                          "message": "aucune cible joignable autre que la source",
                                          "archived": None}]
    assert not [e for e in m1["st"]["log"] + m2["st"]["log"] if e["method"] == "PUT"]

def test_push_auto_sends_to_an_absent_machine_and_skips_an_identical_one(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a"), None, 300)})
    m2 = agents("m2", games={"A": (gz("a"), None, 300)})     # même version que m1
    m3 = agents("m3")                                       # joignable, sans le jeu
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    associate(settings, m3)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m2"], ids["m3"]], ["A"])
    by_machine = {r["machine"]: r for r in result["push"]["results"]}
    assert by_machine["m2"]["message"] == "déjà identique" and by_machine["m2"]["ok"] is True
    assert by_machine["m3"]["ok"] is True and "envoyée" in by_machine["m3"]["message"]
    assert m3["st"]["games"]["A"]["data"] == gz("a")
    assert not [e for e in m2["st"]["log"] if e["method"] == "PUT"]

def test_push_auto_task_result_is_kept_and_reported(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300)})
    m2 = agents("m2", games={"A": (gz("a-old"), None, 10)}, busy={"A"})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    task = Task(id="t2", kind="saves", params={"op": "push_auto", "targets": [ids["m1"], ids["m2"]],
                                               "games": None})
    with pytest.raises(RebuildError, match="1 échec"):
        saves.run_saves(settings, task, Job())
    assert task.params["result"]["push"]["results"][0]["ok"] is False


# ------------------------------------------------------------------ états

def test_status_labels_and_one_listing_per_machine(agents, tmp_path):
    m1 = agents("m1", games={"Jeu": (gz("x"), None, 300), "Autre": (gz("q"), None, 5)})
    m2 = agents("m2", games={"Jeu": (gz("y"), None, 200)})
    m3 = agents("m3", games={"Jeu": (gz("z"), None, 250)})
    m4 = agents("m4", games={"Autre": (gz("q"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    associate(settings, m3)
    associate(settings, m4)
    # m1 : même version que m4 pour « Autre » ; m5 : adresse fermée
    save_saves_machines(settings, saves_machines(settings) + [
        {"id": "morte", "name": "morte", "url": "https://127.0.0.1:1", "token": TOKEN,
         "pem": m1["pem"]}])
    status = saves.status(settings)
    jeu = next(g for g in status["games"] if g["game"] == "Jeu")
    labels = {s["name"]: s["state"] for s in jeu["machines"]}
    # m1 a la version la plus récente (300) : c'est la source à proposer
    assert labels == {"m1": "plus récente", "m2": "plus ancienne", "m3": "plus ancienne",
                      "m4": "absente", "morte": "injoignable"}
    autre = next(g for g in status["games"] if g["game"] == "Autre")
    # m1 et m4 ont la même version, m2 et m3 (joignables) ne l'ont pas : elles sont « absente »
    assert {s["name"]: s["state"] for s in autre["machines"]} == {
        "m1": "plus récente", "m2": "absente", "m3": "absente", "m4": "plus récente", "morte": "injoignable"}
    assert all(len([e for e in m["st"]["log"] if e["path"] == "/v1/saves/list"]) == 1
               for m in (m1, m2, m3, m4))
    dead = next(s for s in jeu["machines"] if s["name"] == "morte")
    assert dead["message"]
    assert [m["name"] for m in status["machines"] if not m["ok"]] == ["morte"]


def test_status_reports_reserve_and_single_game(agents, tmp_path):
    m1 = agents("m1", games={"Jeu": (gz("x"), None, 300)})
    m2 = agents("m2")                       # joignable, sans le jeu
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    data = gz("reserve")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=7,
                profile=None, source="m1")
    status = saves.status(settings, ["Jeu"])
    assert [g["game"] for g in status["games"]] == ["Jeu"]
    assert status["games"][0]["reserve"]["fingerprint"] == fp_of(data)
    assert status["games"][0]["reserve"]["archives"] == 0
    # seule machine à avoir le jeu : « plus récente » ; la joignable sans le jeu : « absente »
    assert {s["name"]: s["state"] for s in status["games"][0]["machines"]} == {"m1": "plus récente", "m2": "absente"}


# ------------------------------------------------------------------ API

@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    queue = TaskQueue(tmp_path / "state" / "tasks.json", main.queue.handlers)
    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "queue", queue)
    image = SimpleNamespace(path=Path("/roms/arcade/Jeu.pc.wsquashfs"))

    def find_image(image_id):
        if image_id == "arcade/Jeu":
            return image
        raise main.HTTPException(status_code=404, detail=f"image inconnue : {image_id}")

    monkeypatch.setattr(main, "_find_image", find_image)
    return TestClient(main.app), settings, queue


def test_api_machines_codes_and_secrets(agents, api):
    client, settings, _ = api
    steambox = agents("steambox")
    code = f"wsq://{TOKEN}@127.0.0.1:{steambox['port']}#{steambox['fp']}"
    response = client.post("/api/saves/machines", json={"code": code})
    assert response.status_code == 200 and response.json()["name"] == "steambox"
    listed = client.get("/api/saves/machines").json()
    assert [m["name"] for m in listed["machines"]] == ["steambox"]
    assert TOKEN not in response.text and TOKEN not in client.get("/api/saves/machines").text
    assert "BEGIN CERTIFICATE" not in client.get("/api/saves/machines").text
    assert client.post("/api/saves/machines", json={"code": "n'importe quoi"}).status_code == 422
    machine_id = listed["machines"][0]["id"]
    assert client.delete(f"/api/saves/machines/{machine_id}").status_code == 200
    assert client.delete(f"/api/saves/machines/{machine_id}").status_code == 404


def test_api_game_host_cannot_be_deleted(agents, api):
    client, settings, _ = api
    host = agents("sb")
    settings.game_host, settings.agent_url, settings.agent_token = "agent", host["url"], TOKEN
    (settings.state_dir / "agent-cert.pem").write_text(host["pem"])
    assert client.get("/api/saves/machines").json()["machines"][0]["id"] == "game-host"
    assert client.delete("/api/saves/machines/game-host").status_code == 422


def test_api_status_for_one_image(agents, api):
    client, settings, _ = api
    m1 = agents("m1", games={"Jeu": (gz("x"), None, 1)})
    associate(settings, m1)
    data = client.get("/api/saves/status", params={"id": "arcade/Jeu"}).json()
    assert [g["game"] for g in data["games"]] == ["Jeu"]
    assert client.get("/api/saves/status", params={"id": "arcade/Absent"}).status_code == 404
    assert len(client.get("/api/saves/status").json()["games"]) == 1


def test_api_validation_codes(agents, api):
    client, settings, _ = api
    m1 = agents("m1")
    associate(settings, m1)
    mid = machine_ids(settings)["m1"]
    assert client.post("/api/saves/pull", json={"source": "inconnue", "all": True}).status_code == 404
    assert client.post("/api/saves/pull", json={"source": mid}).status_code == 422
    assert client.post("/api/saves/pull", json={"source": mid, "all": True, "ids": ["arcade/Jeu"]}).status_code == 422
    assert client.post("/api/saves/pull", json={"source": mid, "ids": ["arcade/Absent"]}).status_code == 404
    assert client.post("/api/saves/push", json={"targets": [mid, mid], "all": True}).status_code == 422
    assert client.post("/api/saves/push", json={"targets": ["inconnue"], "all": True}).status_code == 404
    assert client.post("/api/saves/push", json={"source": mid, "targets": [mid], "all": True}).status_code == 422
    assert client.post("/api/saves/push", json={"targets": [mid], "all": True}).status_code == 422   # réserve vide
    assert client.post("/api/saves/push", json={"targets": [mid], "ids": ["arcade/Jeu"]}).status_code == 422


def test_api_pull_and_push_queue_tasks(agents, api):
    client, settings, queue = api
    m1, m2 = agents("m1", games={"Jeu": (gz("x"), None, 1)}), agents("m2")
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    pulled = client.post("/api/saves/pull", json={"source": ids["m1"], "ids": ["arcade/Jeu"]})
    assert pulled.status_code == 200
    task = client.get(f"/api/tasks/{pulled.json()['task']}").json()
    assert task["kind"] == "saves" and task["params"]["op"] == "pull"
    assert task["params"]["games"] == ["Jeu"] and task["image"] is None
    data = gz("reserve")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=1,
                profile=None, source="m1")
    pushed = client.post("/api/saves/push", json={"targets": [ids["m2"]], "ids": ["arcade/Jeu"]})
    assert pushed.status_code == 200
    assert client.get(f"/api/tasks/{pushed.json()['task']}").json()["params"]["op"] == "push"
    assert queue.get(pushed.json()["task"]) is not None


def test_api_push_auto_queues_one_task_without_reserve(agents, api):
    client, settings, queue = api
    m1, m2 = agents("m1", games={"Jeu": (gz("x"), None, 5)}), agents("m2", games={"Jeu": (gz("y"), None, 1)})
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    # Pas de réserve : « auto » ne l'exige pas
    response = client.post("/api/saves/push", json={"source": "auto", "targets": [ids["m1"], ids["m2"]],
                                                    "ids": ["arcade/Jeu"]})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['task']}").json()
    assert task["params"]["op"] == "push_auto" and task["params"]["games"] == ["Jeu"]
    assert client.post("/api/saves/push", json={"source": "auto", "targets": [ids["m1"]],
                                                "ids": ["arcade/Absent"]}).status_code == 404


def test_pull_auto_takes_each_game_from_its_newest_machine(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300), "B": (gz("b"), None, 5)})
    m2 = agents("m2", games={"A": (gz("a-old"), None, 10), "B": (gz("b"), None, 4)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    result = saves.pull_auto(settings, ["A", "B", "C"])
    by_game = {r["game"]: r for r in result["results"]}
    assert by_game["A"]["ok"] is True and by_game["B"]["ok"] is True
    assert by_game["C"]["ok"] is False and "absent" in by_game["C"]["message"]
    # A vient de m1 (la plus récente), jamais de m2 ; B (identique) vient de la première machine
    assert saves.reserve_dir(settings, "A").joinpath(saves.CURRENT).read_bytes() == gz("a-new")
    assert [e["game"] for e in m2["st"]["log"] if e["method"] == "GET" and e["path"] == "/v1/saves/tar"] == []
    assert [e for e in m1["st"]["log"] if e["path"] == "/v1/saves/tar"] != []


def test_api_pull_auto_queues_a_pull_task(agents, api):
    client, settings, _ = api
    m1 = agents("m1", games={"Jeu": (gz("x"), None, 1)})
    associate(settings, m1)
    response = client.post("/api/saves/pull", json={"source": "auto", "ids": ["arcade/Jeu"]})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['task']}").json()
    assert task["params"]["op"] == "pull_auto" and task["params"]["games"] == ["Jeu"]
