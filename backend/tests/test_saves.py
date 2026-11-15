"""Manager de sauvegardes : machines (association, icônes), manager (rotation), pull, push,
push_from, suppression (machines, manager), états (taille, suggestion), tâche et API.

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
from app.services.trial import OLD_AGENT_MESSAGE, AgentHost, AgentProbeError, SavesBusy, fingerprint_pem

TOKEN = "jeton-saves"                 # jeton durable de l'agent (rendu par /v1/pair, jamais dans un code)
SECRET = "secret-du-code"             # secret d'un code d'association : usage unique


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


def pair_reply(st: dict, headers) -> tuple[int, dict | None]:
    """Faux agent version 7, POST /v1/pair : le secret du code s'échange une seule fois
    contre le jeton durable, lié à l'identifiant X-Wsq-Machine de l'association."""
    st["pairs"].append({"auth": headers.get("Authorization"), "machine": headers.get("X-Wsq-Machine")})
    if st["old_pair"]:
        return 404, None                                   # agent version 6 : pas de route
    if headers.get("Authorization") != f"Bearer {st['secret']}" or not st["pair_open"]:
        return 401, {"detail": "code d'association expiré ou déjà utilisé : relancer --pair"}
    if not headers.get("X-Wsq-Machine"):
        return 400, {"detail": "identifiant de machine absent"}
    st["pair_open"] = False
    st["bound"] = headers.get("X-Wsq-Machine")
    return 200, {"token": st["token"], "host": st["host"], "version": "7"}


def auth_ok(st: dict, headers) -> bool:
    """Jeton durable, et identifiant lié à l'association (agent version 7)."""
    if headers.get("Authorization") != f"Bearer {st['token']}":
        return False
    return st["bound"] is None or headers.get("X-Wsq-Machine") == st["bound"]


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
        if not auth_ok(self.st, self.headers):
            self._send(401, b'{"detail":"jeton refuse"}')
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        self._record("GET", url.path)
        if not self._auth():
            return
        if url.path == "/v1/info":
            if self.st["info_fail"]:
                return self._send(500, b'{"detail":"info indisponible"}')
            return self._send(200, json.dumps({"version": "4", "host": self.st["host"], "user": "arcade",
                                               "os": self.st["os"]}).encode())
        if url.path == "/v1/saves/list":
            if self.st.get("no_saves_routes"):         # agent v2 : route inconnue du routeur Go
                return self._send(404, b"404 page not found\n", ctype="text/plain; charset=utf-8")
            games = [{"name": name, "fingerprint": "" if g.get("gone") else fp_of(g["data"]),
                      "newest": g["newest"], "size": g["size"], "state": "",
                      **({"label": self.st["labels"][name]} if name in self.st["labels"] else {})}
                     for name, g in sorted(self.st["games"].items())]
            return self._send(200, json.dumps({"games": games}).encode())
        if url.path == "/v1/saves/dirs":
            if self.st["no_dirs"]:                      # agent v7 : route inconnue du routeur Go
                return self._send(404, b"404 page not found\n", ctype="text/plain; charset=utf-8")
            return self._send(200, json.dumps({"dirs": self.st["dirs"], "eden": ""}).encode())
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
        if url.path == "/v1/saves/dirs":
            self._record("PUT", url.path)
            if not self._auth():
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if not str(body.get("path", "")).startswith("/home/"):
                return self._send(400, b'{"detail":"le dossier doit etre un sous-dossier de HOME"}')
            self.st["dirs"] = [d for d in self.st["dirs"] if d["name"] != body["name"]] + [
                {"name": body["name"], "path": body["path"]}]
            return self._send(200, json.dumps({"dirs": self.st["dirs"], "eden": ""}).encode())
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
        self.st["games"][game] = {"data": body, "profile": profile, "newest": self.st.get("now", 1000),
                                  "size": len(body)}
        self.st["archived"] += [game] if archived else []
        self._send(200, json.dumps({"fingerprint": fp_of(body), "archived": archived}).encode())

    def do_DELETE(self):
        url = urlsplit(self.path)
        game = parse_qs(url.query).get("game", [""])[0]
        self._record("DELETE", url.path, game=game)
        if not self._auth():
            return
        if url.path == "/v1/saves/dirs":
            name = parse_qs(url.query).get("name", [""])[0]
            if all(d["name"] != name for d in self.st["dirs"]):
                return self._send(404, b'{"detail":"dossier inconnu"}')
            self.st["dirs"] = [d for d in self.st["dirs"] if d["name"] != name]
            return self._send(200, json.dumps({"dirs": self.st["dirs"], "eden": ""}).encode())
        if url.path != "/v1/saves/tar":
            return self._send(404, b'{"detail":"route inconnue"}')
        if self.st["old_status"]:                       # agent plus ancien : route absente
            return self._send(self.st["old_status"], b'{"detail":"non trouve"}')
        if game in self.st["busy"]:
            return self._send(409, b'{"detail":"jeu en cours"}')
        if game not in self.st["games"]:
            return self._send(200, json.dumps({"archived": "", "removed": 0}).encode())
        del self.st["games"][game]
        self.st["deleted"].append(game)
        archived = f"/saves/archive/{game}"
        self._send(200, json.dumps({"archived": archived, "removed": 1}).encode())

    def do_POST(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record("POST", url.path, body=body)
        if url.path == "/v1/pair":
            code, payload = pair_reply(self.st, self.headers)
            if payload is None:
                return self._send(code, b"404 page not found\n", "text/plain; charset=utf-8")
            return self._send(code, json.dumps(payload).encode())
        if not self._auth():
            return
        if url.path == "/v1/saves/state":
            if body.get("game") in self.st["conflict"]:
                return self._send(409, b'{"detail":"machine modifiee entre-temps"}')
            return self._send(200, b"{}")
        self._send(404, b'{"detail":"route inconnue"}')


def fake_game(spec: tuple) -> dict:
    """Jeu d'un faux agent : (données, profil, newest) ou (données, profil, newest, taille).
    Sans taille, la taille est celle des données."""
    data, profile, newest, *rest = spec
    return {"data": data, "profile": profile, "newest": newest,
            "size": rest[0] if rest else len(data)}


@pytest.fixture
def agents(tmp_path):
    """Fabrique de faux agents (un par machine) ; arrêtés à la fin du test.
    `os_name` : système renvoyé par /v1/info ; `info_fail` : /v1/info en erreur ;
    `old_status` : code de réponse de DELETE /v1/saves/tar (agent plus ancien : 404 ou 405) ;
    `secret` : secret du code d'association ; `old_pair` : agent sans /v1/pair (version 6)."""
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    servers = []

    def make(host: str, games=None, busy=(), conflict=(), now=1000, token=TOKEN, cert_dir=None,
             os_name="linux", info_fail=False, old_status=None, no_saves_routes=False,
             secret=SECRET, old_pair=False, labels=None, dirs=None, no_dirs=False):
        cert, key = make_cert(tmp_path / (cert_dir or host))
        st = {"host": host, "token": token, "busy": set(busy), "conflict": set(conflict),
              "now": now, "archived": [], "log": [], "os": os_name, "info_fail": info_fail,
              "old_status": old_status, "deleted": [], "no_saves_routes": no_saves_routes,
              "secret": secret, "pair_open": True, "old_pair": old_pair, "pairs": [], "bound": None,
              "labels": dict(labels or {}), "dirs": list(dirs or []), "no_dirs": no_dirs,
              "games": {name: fake_game(spec) for name, spec in (games or {}).items()}}
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
    """Association avec un code neuf (`wsquashfs-launcher --pair` relancé sur la machine)."""
    agent["st"]["pair_open"] = True
    return saves.add_machine(settings, f"wsq://{SECRET}@127.0.0.1:{agent['port']}#{agent['fp']}")


def refused(settings: Settings, code: str) -> AgentProbeError:
    """Association refusée : l'exception (avec son code HTTP et son message)."""
    with pytest.raises(AgentProbeError) as info:
        saves.add_machine(settings, code)
    return info.value


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
    assert set(rows[0]) == {"id", "name", "url", "fingerprint", "kind", "removable", "icon"}
    assert rows[0]["icon"] is None                       # pas d'icône choisie : détection


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


def test_wrong_fingerprint_or_secret_is_refused(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    with pytest.raises(RebuildError, match="ne correspond pas"):
        saves.add_machine(settings, f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{'AB' * 32}")
    bad = agents("refusee", secret="autre-code")
    assert refused(settings, f"wsq://{SECRET}@127.0.0.1:{bad['port']}#{bad['fp']}").status == 422
    assert saves_machines(settings) == []


def test_wrong_fingerprint_is_refused_before_the_secret_is_sent(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    with pytest.raises(RebuildError, match="ne correspond pas"):
        saves.add_machine(settings, f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{'AB' * 32}")
    assert steambox["st"]["pairs"] == [] and steambox["st"]["log"] == []      # rien n'est parti
    assert saves_machines(settings) == []


def test_association_stores_the_durable_token_never_the_secret(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    machine = saves_machines(settings)[0]
    assert machine["token"] == TOKEN and SECRET not in json.dumps(machine)
    assert SECRET not in (tmp_path / "config.json").read_text()
    assert steambox["st"]["pairs"] == [{"auth": f"Bearer {SECRET}", "machine": saves_client_id(settings)}]


def test_used_code_is_refused_and_nothing_changes(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    before = saves_machines(settings)
    error = refused(settings, f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{steambox['fp']}")
    assert error.status == 422 and "expiré ou déjà utilisé" in str(error) and "--pair" in str(error)
    assert saves_machines(settings) == before                  # rien d'enregistré à la place


def test_old_agent_gets_the_update_message(agents, tmp_path):
    old = agents("vieux", old_pair=True)
    settings = make_settings(tmp_path)
    error = refused(settings, f"wsq://{SECRET}@127.0.0.1:{old['port']}#{old['fp']}")
    assert error.status == 422 and str(error) == OLD_AGENT_MESSAGE
    assert saves_machines(settings) == []


def test_unreachable_agent_at_association(tmp_path):
    settings = make_settings(tmp_path)
    error = refused(settings, f"wsq://{SECRET}@127.0.0.1:1#{'AB' * 32}")
    assert error.status == 502 and saves_machines(settings) == []


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
    meta = saves.store(settings, "Jeu", part, fingerprint="F1", newest=42, size=1234,
                       profile="wine-tkg", source="steambox")
    d = saves.reserve_dir(settings, "Jeu")
    assert (d / "current.tar.gz").read_bytes() == gz("v1") and not part.exists()
    stored = json.loads((d / "meta.json").read_text())
    assert stored == meta and stored["fingerprint"] == "F1" and stored["newest"] == 42
    assert stored["size"] == 1234
    assert stored["source"] == "steambox" and stored["profile"] == "wine-tkg"
    assert stored["fetched_at"].endswith("+00:00")
    assert saves.reserve_games(settings) == ["Jeu"]


def test_rotation_keeps_the_three_most_recent_versions(tmp_path):
    settings = make_settings(tmp_path)
    versions = [gz(f"v{i}") for i in range(5)]
    for i, data in enumerate(versions):
        part = part_with(settings, "Jeu", data)
        saves.store(settings, "Jeu", part, fingerprint=fp_of(data), newest=i, size=len(data), profile=None,
                    source="s")
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
                size=1, profile=None, source="s")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint="F", newest=2,
                size=1, profile=None, source="s")
    assert saves.archives(settings, "Jeu") == []
    assert saves.reserve_meta(settings, "Jeu")["newest"] == 2


def test_store_refuses_invalid_game_name(tmp_path):
    settings = make_settings(tmp_path)
    part = tmp_path / "x.part"
    part.write_bytes(gz("x"))
    with pytest.raises(RebuildError, match="nom de jeu invalide"):
        saves.store(settings, "../evasion", part, fingerprint="F", newest=1, size=1, profile=None, source="s")
    assert not (settings.state_dir / "evasion").exists()


# ------------------------------------------------------------------ pull

def test_pull_stores_each_game_and_acks(agents, tmp_path):
    steambox = agents("steambox", games={"Alpha": (gz("a"), "wine-tkg", 300, 4096),
                                         "Beta": (gz("b"), None, 200)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    source = machine_ids(settings)["steambox"]
    result = saves.pull(settings, source, None)
    assert [r["game"] for r in result["results"]] == ["Alpha", "Beta"]
    assert all(r["ok"] for r in result["results"])
    assert saves.reserve_dir(settings, "Alpha").joinpath("current.tar.gz").read_bytes() == gz("a")
    assert saves.reserve_meta(settings, "Alpha")["newest"] == 300
    assert saves.reserve_meta(settings, "Alpha")["size"] == 4096          # taille de la liste, gardée
    assert saves.reserve_meta(settings, "Beta")["size"] == len(gz("b"))   # à défaut, celle des données
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
    steambox["st"]["games"]["Occupe"] = fake_game((gz("o"), None, 2))
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
                size=len(data), profile="proton", source="x")
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
                size=len(data), profile=None, source="x")
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
                size=len(data), profile=None, source="x")
    task = Task(id="t1", kind="saves", params={"op": "push", "targets": [machine_ids(settings)["a-machine"]],
                                               "games": ["Jeu"]})
    with pytest.raises(RebuildError, match="1 échec"):
        saves.run_saves(settings, task, Job())
    assert task.params["result"]["results"][0]["ok"] is False


def test_push_auto_takes_each_game_from_its_most_recent_machine(agents, tmp_path):
    # A : la plus récente est m1 ; B : la plus récente est m2 (sources différentes)
    # Tailles égales : la plus récente l'emporte (la taille est testée à part)
    m1 = agents("m1", games={"A": (gz("a-new"), None, 300, 50), "B": (gz("b-old"), None, 100, 50)})
    m2 = agents("m2", games={"B": (gz("b-new"), None, 900, 50)})
    m3 = agents("m3", games={"A": (gz("a-old"), None, 10, 50)})
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    associate(settings, m3)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m1"], ids["m2"], ids["m3"]], None)
    pushed = {(r["game"], r["machine"]): r["ok"] for r in result["push"]["results"]
              if r["machine"] and r["message"].startswith("envoyée")}
    # A part de m1 vers m3 ; B part de m2 vers m1. Les machines sans le jeu (m2 pour A,
    # m3 pour B) ne reçoivent rien : absent n'est pas une différence
    assert pushed == {("A", "m3"): True, ("B", "m1"): True}
    skipped = {(r["game"], r["machine"]) for r in result["push"]["results"] if r["message"].startswith("absente")}
    assert skipped == {("A", "m2"), ("B", "m3")}
    assert m3["st"]["games"]["A"]["data"] == gz("a-new")
    assert m1["st"]["games"]["B"]["data"] == gz("b-new")
    assert "A" not in m2["st"]["games"] and "B" not in m3["st"]["games"]
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
                size=len(data), profile=None, source="m1")
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

def test_push_auto_skips_an_absent_machine_and_an_identical_one(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a"), None, 300)})
    m2 = agents("m2", games={"A": (gz("a"), None, 300)})     # même version que m1
    m3 = agents("m3")                                       # joignable, sans le jeu
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    associate(settings, m3)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["m2"], ids["m3"]], ["A"])
    # m1 et m2 identiques, m3 sans le jeu : rien à synchroniser
    assert [(r["game"], r["ok"], r["message"]) for r in result["push"]["results"]] == [("A", True, "déjà identiques")]
    assert "A" not in m3["st"]["games"]
    assert not [e for e in m2["st"]["log"] + m3["st"]["log"] if e["method"] == "PUT"]


def test_push_from_an_explicit_source_installs_on_an_absent_machine(agents, tmp_path):
    m1 = agents("m1", games={"A": (gz("a"), None, 300)})
    m2 = agents("m2")                                       # joignable, sans le jeu
    settings = make_settings(tmp_path)
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    result = saves.push_from(settings, ids["m1"], [ids["m2"]], ["A"])
    assert [r["ok"] for r in result["push"]["results"]] == [True]
    assert m2["st"]["games"]["A"]["data"] == gz("a")

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
    # Tailles égales (3 octets) : m1 a la date la plus récente (300), c'est la source suggérée
    assert labels == {"m1": "plus avancée", "m2": "en retard", "m3": "en retard",
                      "m4": "absente", "morte": "injoignable"}
    assert (jeu["check"], jeu["check_by"]) == (False, "")
    autre = next(g for g in status["games"] if g["game"] == "Autre")
    # m1 et m4 ont la même version : « identique », même si m2 et m3 ne l'ont pas
    assert {s["name"]: s["state"] for s in autre["machines"]} == {
        "m1": "identique", "m2": "absente", "m3": "absente", "m4": "identique", "morte": "injoignable"}
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
                size=777, profile=None, source="m1")
    status = saves.status(settings, ["Jeu"])
    assert [g["game"] for g in status["games"]] == ["Jeu"]
    assert status["games"][0]["reserve"]["fingerprint"] == fp_of(data)
    assert status["games"][0]["reserve"]["archives"] == 0
    assert status["games"][0]["reserve"]["size"] == 777
    # seule machine à avoir le jeu : « présente » (rien à comparer) ; sans le jeu : « absente »
    assert {s["name"]: s["state"] for s in status["games"][0]["machines"]} == {"m1": "présente", "m2": "absente"}
    assert status["games"][0]["check"] is False


# ------------------------------------------------------------------ suppression

def delete_machine_ids(settings, *names):
    ids = machine_ids(settings)
    return [ids[name] for name in names]


def test_delete_on_a_machine_archives_and_removes_it(agents, tmp_path):
    steambox = agents("steambox", games={"Jeu": (gz("a"), None, 5), "Autre": (gz("b"), None, 6)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    result = saves.delete(settings, delete_machine_ids(settings, "steambox"), ["Jeu"], manager=False)
    assert result["results"] == [{"game": "Jeu", "machine": "steambox", "machine_id": machine_ids(settings)["steambox"],
                                  "ok": True, "message": "supprimée sur steambox (archivée)", "archived": True}]
    assert steambox["st"]["deleted"] == ["Jeu"] and "Jeu" not in steambox["st"]["games"]
    assert "Autre" in steambox["st"]["games"]                       # les autres jeux ne bougent pas
    log = [e for e in steambox["st"]["log"] if e["method"] == "DELETE"]
    assert log == [{"method": "DELETE", "path": "/v1/saves/tar", "machine": saves_client_id(settings), "game": "Jeu"}]
    # le jeu n'est plus listé sur la machine : absent, pas présent
    jeu = saves.status(settings, ["Jeu"])["games"][0]
    assert {s["name"]: s["state"] for s in jeu["machines"]} == {"steambox": "absente"}


def test_delete_on_a_machine_without_the_game_is_a_noop(agents, tmp_path):
    steambox = agents("steambox")
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    result = saves.delete(settings, delete_machine_ids(settings, "steambox"), ["Jeu"], manager=False)
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["message"] == "rien à supprimer sur steambox"
    assert result["results"][0]["archived"] is None


def test_delete_on_the_manager_keeps_the_current_version_as_dated(tmp_path):
    settings = make_settings(tmp_path)
    data = gz("courante")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", gz("ancienne")), fingerprint=fp_of(gz("ancienne")),
                newest=1, size=8, profile="wine", source="x")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=2,
                size=len(data), profile="wine", source="x")
    d = saves.reserve_dir(settings, "Jeu")
    result = saves.delete(settings, [], ["Jeu"], manager=True)
    entry = result["results"][0]
    assert (entry["game"], entry["machine"], entry["machine_id"], entry["ok"]) == ("Jeu", "Manager", "manager", True)
    assert entry["message"] == "supprimée du manager (version archivée)" and entry["archived"] is True
    assert not (d / "current.tar.gz").exists() and not (d / "meta.json").exists()
    assert saves.reserve_games(settings) == []
    archived = saves.archives(settings, "Jeu")
    assert len(archived) == 2
    # la version datée garde l'archive et les informations de la version retirée
    assert (d / f"{archived[0]}.tar.gz").read_bytes() == data
    assert json.loads((d / f"{archived[0]}.json").read_text())["fingerprint"] == fp_of(data)


def test_manager_removal_rotates_to_three_dated_versions(tmp_path):
    settings = make_settings(tmp_path)
    versions = [gz(f"v{i}") for i in range(4)]
    for i, data in enumerate(versions):
        saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=i,
                    size=len(data), profile=None, source="s")
    assert len(saves.archives(settings, "Jeu")) == 3
    saves.remove_from_manager(settings, "Jeu")
    d = saves.reserve_dir(settings, "Jeu")
    archived = saves.archives(settings, "Jeu")
    assert len(archived) == 3
    assert [(d / f"{name}.tar.gz").read_bytes() for name in archived] == [versions[3], versions[2], versions[1]]


def test_delete_on_the_manager_of_an_absent_game_is_ok(tmp_path):
    settings = make_settings(tmp_path)
    result = saves.delete(settings, [], ["Absent"], manager=True)
    assert result["results"] == [{"game": "Absent", "machine": "Manager", "machine_id": "manager", "ok": True,
                                  "message": "pas sur le manager", "archived": None}]


def test_delete_on_a_machine_and_the_manager(agents, tmp_path):
    steambox = agents("steambox", games={"Jeu": (gz("a"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    data = gz("reserve")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=9,
                size=len(data), profile=None, source="steambox")
    result = saves.delete(settings, delete_machine_ids(settings, "steambox"), ["Jeu"], manager=True)
    by_machine = {r["machine"]: r for r in result["results"]}
    assert set(by_machine) == {"steambox", "Manager"}
    assert all(r["ok"] for r in result["results"])
    assert "Jeu" not in steambox["st"]["games"]
    assert saves.reserve_games(settings) == [] and len(saves.archives(settings, "Jeu")) == 1


def test_delete_busy_game_fails_on_that_machine_only(agents, tmp_path):
    busy = agents("occupee", games={"Jeu": (gz("a"), None, 5)}, busy={"Jeu"})
    free = agents("libre", games={"Jeu": (gz("b"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, busy)
    associate(settings, free)
    ids = delete_machine_ids(settings, "occupee", "libre")
    result = saves.delete(settings, ids, ["Jeu"], manager=False)
    by_machine = {r["machine"]: r for r in result["results"]}
    assert by_machine["occupee"]["ok"] is False
    assert by_machine["occupee"]["message"] == "jeu en cours sur occupee : non supprimée"
    assert by_machine["libre"]["ok"] is True
    assert "Jeu" in busy["st"]["games"] and "Jeu" not in free["st"]["games"]


@pytest.mark.parametrize("status", [404, 405])
def test_old_agent_gets_a_clear_message(agents, tmp_path, status):
    old = agents("vieux", games={"Jeu": (gz("a"), None, 5)}, old_status=status)
    settings = make_settings(tmp_path)
    associate(settings, old)
    result = saves.delete(settings, delete_machine_ids(settings, "vieux"), ["Jeu"], manager=False)
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["message"] == "agent trop ancien : wsquashfs-launcher --update sur cette machine"
    assert "Jeu" in old["st"]["games"]


def test_delete_unreachable_machine_fails_for_each_game(agents, tmp_path):
    steambox = agents("steambox", games={"A": (gz("a"), None, 5), "B": (gz("b"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    save_saves_machines(settings, saves_machines(settings) + [
        {"id": "morte", "name": "morte", "url": "https://127.0.0.1:1", "token": TOKEN, "pem": steambox["pem"]}])
    result = saves.delete(settings, ["morte"], ["A", "B"], manager=False)
    assert [r["ok"] for r in result["results"]] == [False, False]
    assert all("injoignable" in r["message"] for r in result["results"])


def test_delete_refuses_invalid_game_name(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(RebuildError, match="nom de jeu invalide"):
        saves.delete(settings, [], ["../x"], manager=True)


def test_delete_task_reports_partial_failure(agents, tmp_path):
    busy = agents("occupee", games={"Jeu": (gz("a"), None, 5)}, busy={"Jeu"})
    settings = make_settings(tmp_path)
    associate(settings, busy)
    task = Task(id="t9", kind="saves", params={"op": "delete", "machines": delete_machine_ids(settings, "occupee"),
                                               "games": ["Jeu"], "manager": False})
    with pytest.raises(RebuildError, match="1 échec"):
        saves.run_saves(settings, task, Job())
    assert task.params["result"]["results"][0]["ok"] is False


def test_delete_task_succeeds_on_manager_only(tmp_path):
    settings = make_settings(tmp_path)
    data = gz("r")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=1,
                size=len(data), profile=None, source="x")
    task = Task(id="t10", kind="saves", params={"op": "delete", "machines": [], "games": ["Jeu"], "manager": True})
    message = saves.run_saves(settings, task, Job())
    assert message == "1 opération(s) réussie(s) sur le manager"
    assert saves.reserve_games(settings) == []


def test_fingerprint_less_entry_counts_as_absent(agents, tmp_path):
    steambox = agents("steambox", games={"Jeu": (gz("a"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    steambox["st"]["games"]["Jeu"]["gone"] = True            # listé, sans empreinte
    jeu = saves.status(settings, ["Jeu"])["games"][0]
    assert {s["name"]: s["state"] for s in jeu["machines"]} == {"steambox": "absente"}
    # une copie complète ne prend pas le jeu supprimé
    result = saves.pull(settings, machine_ids(settings)["steambox"], None)
    assert result["results"] == []


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
    code = f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{steambox['fp']}"
    response = client.post("/api/saves/machines", json={"code": code})
    assert response.status_code == 200 and response.json()["name"] == "steambox"
    listed = client.get("/api/saves/machines").json()
    assert [m["name"] for m in listed["machines"]] == ["steambox"]
    for text in (response.text, client.get("/api/saves/machines").text):
        assert TOKEN not in text and SECRET not in text and "BEGIN CERTIFICATE" not in text
    assert client.post("/api/saves/machines", json={"code": "n'importe quoi"}).status_code == 422
    machine_id = listed["machines"][0]["id"]
    assert client.delete(f"/api/saves/machines/{machine_id}").status_code == 200
    assert client.delete(f"/api/saves/machines/{machine_id}").status_code == 404


def test_api_used_code_gives_422_with_the_message(agents, api):
    client, settings, _ = api
    steambox = agents("steambox")
    code = f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{steambox['fp']}"
    assert client.post("/api/saves/machines", json={"code": code}).status_code == 200
    again = client.post("/api/saves/machines", json={"code": code})
    assert again.status_code == 422 and "expiré ou déjà utilisé" in again.json()["detail"]
    assert len(client.get("/api/saves/machines").json()["machines"]) == 1


def test_api_old_agent_is_422_not_401(agents, api):
    client, settings, _ = api
    old = agents("vieux", old_pair=True)
    response = client.post("/api/saves/machines", json={"code": f"wsq://{SECRET}@127.0.0.1:{old['port']}#{old['fp']}"})
    assert response.status_code == 422 and response.json()["detail"] == OLD_AGENT_MESSAGE


def test_api_game_host_is_chosen_by_machine_id_from_a_code(agents, api):
    """Réglages : le code s'associe (Sauvegardes), puis la machine est choisie par son identifiant ;
    aucun jeton ne sort de l'API, et le mode agent sans machine associée est refusé."""
    client, settings, _ = api
    steambox = agents("steambox")
    assert client.put("/api/config/game_host", json={"game_host": "agent"}).status_code == 422
    added = client.post("/api/saves/machines",
                        json={"code": f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{steambox['fp']}"}).json()
    config = client.put("/api/config/game_host/machine", json={"id": added["id"]})
    assert config.status_code == 200 and config.json()["game_host"] == "agent"
    assert settings.agent_token == TOKEN and TOKEN not in config.text
    assert "agent_token" not in config.json() and "agent_token_set" not in config.json()
    assert client.get("/api/config").status_code == 200 and TOKEN not in client.get("/api/config").text
    # Le mode agent se garde une fois associé ; l'ancienne sonde n'existe plus
    assert client.put("/api/config/game_host", json={"game_host": "local"}).status_code == 200
    assert client.put("/api/config/game_host", json={"game_host": "agent"}).status_code == 200
    assert client.post("/api/config/agent/probe", json={"code": "wsq://x@h:1#AB"}).status_code == 404


def test_api_requests_carry_the_pairing_identifier(agents, api):
    client, settings, _ = api
    steambox = agents("steambox", games={"Jeu": (gz("a"), "wine", 5)})
    client.post("/api/saves/machines",
                json={"code": f"wsq://{SECRET}@127.0.0.1:{steambox['port']}#{steambox['fp']}"})
    assert client.get("/api/saves/status").status_code == 200
    machines = {entry["machine"] for entry in steambox["st"]["log"]}
    assert machines == {saves_client_id(settings)}           # association et suite : même identifiant


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
    assert client.post("/api/saves/pull", json={"source": mid, "all": True, "games": ["Jeu"]}).status_code == 422
    assert client.post("/api/saves/pull", json={"source": mid, "games": ["../x"]}).status_code == 422
    assert client.post("/api/saves/pull", json={"source": mid, "games": [".cache"]}).status_code == 422
    assert client.post("/api/saves/push", json={"targets": [mid, mid], "all": True}).status_code == 422
    assert client.post("/api/saves/push", json={"targets": ["inconnue"], "all": True}).status_code == 404
    assert client.post("/api/saves/push", json={"source": mid, "targets": [mid], "all": True}).status_code == 422
    assert client.post("/api/saves/push", json={"targets": [mid], "all": True}).status_code == 422   # réserve vide
    assert client.post("/api/saves/push", json={"targets": [mid], "games": ["Jeu"]}).status_code == 422


def test_api_pull_and_push_queue_tasks(agents, api):
    client, settings, queue = api
    m1, m2 = agents("m1", games={"Jeu": (gz("x"), None, 1)}), agents("m2")
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    pulled = client.post("/api/saves/pull", json={"source": ids["m1"], "games": ["Jeu"]})
    assert pulled.status_code == 200
    task = client.get(f"/api/tasks/{pulled.json()['task']}").json()
    assert task["kind"] == "saves" and task["params"]["op"] == "pull"
    assert task["params"]["games"] == ["Jeu"] and task["image"] is None
    data = gz("reserve")
    saves.store(settings, "Jeu", part_with(settings, "Jeu", data), fingerprint=fp_of(data), newest=1,
                size=len(data), profile=None, source="m1")
    pushed = client.post("/api/saves/push", json={"targets": [ids["m2"]], "games": ["Jeu"]})
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
                                                    "games": ["Jeu"]})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['task']}").json()
    assert task["params"]["op"] == "push_auto" and task["params"]["games"] == ["Jeu"]
    # Un jeu sans image dans la bibliothèque (installé sur une seule machine) est accepté
    assert client.post("/api/saves/push", json={"source": "auto", "targets": [ids["m1"]],
                                                "games": ["Seulement sur aibox"]}).status_code == 200


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
    response = client.post("/api/saves/pull", json={"source": "auto", "games": ["Jeu"]})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['task']}").json()
    assert task["params"]["op"] == "pull_auto" and task["params"]["games"] == ["Jeu"]


# ------------------------------------------------------------------ suggestion : taille, puis date

def test_best_orders_by_size_then_date():
    states = [{"state": "présente", "name": "a", "size": 10, "newest": 900},
              {"state": "plus avancée", "name": "b", "size": 20, "newest": 1},
              {"state": "absente", "name": "c", "size": 99, "newest": 99},
              {"state": "injoignable", "name": "d", "size": 99, "newest": 99}]
    assert saves.best(states)["name"] == "b"
    assert saves.best([s for s in states if s["state"] in ("absente", "injoignable")]) is None


def test_suggestion_prefers_the_heavier_even_when_older(agents, tmp_path):
    heavy = agents("heavy", games={"Jeu": (gz("lourd"), None, 100, 900)})
    recent = agents("recent", games={"Jeu": (gz("leger"), None, 300, 500)})
    settings = make_settings(tmp_path)
    associate(settings, heavy)
    associate(settings, recent)
    jeu = saves.status(settings, ["Jeu"])["games"][0]
    assert {s["name"]: s["state"] for s in jeu["machines"]} == {"heavy": "plus avancée", "recent": "en retard"}
    assert {s["name"]: s["size"] for s in jeu["machines"]} == {"heavy": 900, "recent": 500}
    # la suggérée (heavy) n'a pas la date la plus récente : à vérifier, « recent » la donne
    assert (jeu["check"], jeu["check_by"]) == (True, "recent")


def test_equal_size_falls_back_to_the_date(agents, tmp_path):
    old = agents("old", games={"Jeu": (gz("a"), None, 100, 500)})
    new = agents("new", games={"Jeu": (gz("b"), None, 300, 500)})
    settings = make_settings(tmp_path)
    associate(settings, old)
    associate(settings, new)
    jeu = saves.status(settings, ["Jeu"])["games"][0]
    assert {s["name"]: s["state"] for s in jeu["machines"]} == {"old": "en retard", "new": "plus avancée"}
    assert (jeu["check"], jeu["check_by"]) == (False, "")


def test_identical_machines_are_labelled_identical(agents, tmp_path):
    a = agents("a", games={"Jeu": (gz("same"), None, 100, 500)})
    b = agents("b", games={"Jeu": (gz("same"), None, 100, 500)})
    settings = make_settings(tmp_path)
    associate(settings, a)
    associate(settings, b)
    jeu = saves.status(settings, ["Jeu"])["games"][0]
    assert {s["name"]: s["state"] for s in jeu["machines"]} == {"a": "identique", "b": "identique"}
    assert (jeu["check"], jeu["check_by"]) == (False, "")


def test_push_auto_takes_the_heaviest_machine(agents, tmp_path):
    heavy = agents("heavy", games={"Jeu": (gz("lourd"), None, 100, 900)})
    recent = agents("recent", games={"Jeu": (gz("leger"), None, 300, 500)})
    target = agents("cible", games={"Jeu": (gz("vieux"), None, 50, 100)})
    settings = make_settings(tmp_path)
    for m in (heavy, recent, target):
        associate(settings, m)
    ids = machine_ids(settings)
    result = saves.push_auto(settings, [ids["cible"]], None)
    pushed = [(r["game"], r["machine"], r["ok"]) for r in result["push"]["results"] if r["machine"]]
    assert pushed == [("Jeu", "cible", True)]
    assert target["st"]["games"]["Jeu"]["data"] == gz("lourd")
    assert not [e for e in heavy["st"]["log"] + recent["st"]["log"] if e["method"] == "PUT"]


# ------------------------------------------------------------------ système et icônes

def test_machines_carry_os_and_detected_icon(agents, tmp_path):
    steamos = agents("sb", os_name="steamos")
    unknown = agents("truc", os_name="fedora")
    settings = make_settings(tmp_path)
    associate(settings, steamos)
    associate(settings, unknown)
    machines = {m["name"]: m for m in saves.status(settings)["machines"]}
    assert (machines["sb"]["os"], machines["sb"]["icon"], machines["sb"]["icon_set"]) == ("steamos", "steamos", False)
    # système inconnu des icônes : icône « linux », le système reste affiché
    assert (machines["truc"]["os"], machines["truc"]["icon"], machines["truc"]["icon_set"]) == ("fedora", "linux", False)


def test_failed_info_keeps_the_machine_reachable(agents, tmp_path):
    m = agents("m", games={"Jeu": (gz("x"), None, 5)})
    settings = make_settings(tmp_path)
    associate(settings, m)
    m["st"]["info_fail"] = True                 # /v1/info en erreur, /v1/saves/list répond
    status = saves.status(settings)
    machine = status["machines"][0]
    assert machine["ok"] is True and (machine["os"], machine["icon"]) == ("", "linux")
    assert status["games"][0]["machines"][0]["state"] == "présente"


def test_chosen_icon_wins_over_detection_and_can_be_reset(agents, tmp_path):
    sb = agents("sb", os_name="steamos")
    settings = make_settings(tmp_path)
    associate(settings, sb)
    mid = machine_ids(settings)["sb"]
    assert saves.set_icon(settings, mid, "batocera") == {"id": mid, "icon": "batocera", "icon_set": True}
    row = saves.status(settings)["machines"][0]
    assert (row["os"], row["icon"], row["icon_set"]) == ("steamos", "batocera", True)
    assert saves.machine_rows(settings)[0]["icon"] == "batocera"
    saves.set_icon(settings, mid, None)
    row = saves.status(settings)["machines"][0]
    assert (row["icon"], row["icon_set"]) == ("steamos", False)
    # l'icône choisie survit à une nouvelle association de la même machine
    saves.set_icon(settings, mid, "linux")
    associate(settings, sb)
    assert saves.machine_rows(settings)[0]["icon"] == "linux"


def test_game_host_icon_is_read_back_from_config(tmp_path, monkeypatch):
    from app.config import get_settings

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"saves_game_host_icon": "steamos"}))
    monkeypatch.setenv("WSQUASHFS_MGR_RUNTIME_CONFIG_FILE", str(cfg))
    assert get_settings().saves_game_host_icon == "steamos"
    cfg.write_text(json.dumps({"saves_game_host_icon": "xbox"}))     # valeur inconnue : ignorée
    assert get_settings().saves_game_host_icon is None


def test_api_patch_icon(agents, api):
    client, settings, _ = api
    sb = agents("sb", os_name="steamos")
    associate(settings, sb)
    mid = machine_ids(settings)["sb"]
    response = client.patch(f"/api/saves/machines/{mid}", json={"icon": "batocera"})
    assert response.status_code == 200 and response.json() == {"id": mid, "icon": "batocera", "icon_set": True}
    assert client.get("/api/saves/machines").json()["machines"][0]["icon"] == "batocera"
    response = client.patch(f"/api/saves/machines/{mid}", json={"icon": None})
    assert response.status_code == 200 and response.json()["icon_set"] is False
    assert client.get("/api/saves/status").json()["machines"][0]["icon"] == "steamos"
    assert client.patch(f"/api/saves/machines/{mid}", json={"icon": "windows"}).status_code == 400
    assert client.patch("/api/saves/machines/inconnue", json={"icon": "linux"}).status_code == 404
    assert client.patch("/api/saves/machines/inconnue", json={"icon": None}).status_code == 404


def test_api_patch_icon_of_the_game_host(agents, api):
    client, settings, _ = api
    host = agents("sb", os_name="steamos")
    settings.game_host, settings.agent_url, settings.agent_token = "agent", host["url"], TOKEN
    (settings.state_dir / "agent-cert.pem").write_text(host["pem"])
    config = settings.runtime_config_file
    assert client.patch("/api/saves/machines/game-host", json={"icon": "batocera"}).status_code == 200
    assert json.loads(config.read_text())["saves_game_host_icon"] == "batocera"
    assert settings.saves_game_host_icon == "batocera"
    row = client.get("/api/saves/status").json()["machines"][0]
    assert (row["id"], row["icon"], row["icon_set"]) == ("game-host", "batocera", True)
    assert client.patch("/api/saves/machines/game-host", json={"icon": None}).status_code == 200
    assert json.loads(config.read_text())["saves_game_host_icon"] is None
    assert client.patch("/api/saves/machines/game-host", json={"icon": "xbox"}).status_code == 400
    assert settings.saves_game_host_icon is None


def test_api_delete_validation_codes(agents, api):
    client, settings, _ = api
    m1 = agents("m1")
    associate(settings, m1)
    mid = machine_ids(settings)["m1"]
    post = lambda body: client.post("/api/saves/delete", json=body)  # noqa: E731
    assert post({"games": [], "machines": [mid]}).status_code == 422              # jeu obligatoire
    assert post({"games": ["../x"], "machines": [mid]}).status_code == 422
    assert post({"games": ["Jeu", "Jeu"], "machines": [mid]}).status_code == 422   # jeu en double
    assert post({"games": ["Jeu"], "machines": [mid, mid]}).status_code == 422     # machine en double
    assert post({"games": ["Jeu"], "machines": []}).status_code == 422             # ni machine ni manager
    assert post({"games": ["Jeu"]}).status_code == 422
    assert post({"games": ["Jeu"], "machines": ["inconnue"]}).status_code == 404
    assert post({"games": ["Jeu"], "machines": [mid], "manager": "pas un booléen"}).status_code == 422


def test_api_delete_queues_one_task(agents, api):
    client, settings, queue = api
    m1, m2 = agents("m1", games={"Jeu": (gz("x"), None, 1)}), agents("m2")
    associate(settings, m1)
    associate(settings, m2)
    ids = machine_ids(settings)
    response = client.post("/api/saves/delete", json={"games": ["Jeu", "Autre"], "machines": [ids["m1"]],
                                                      "manager": True})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['task']}").json()
    assert task["kind"] == "saves" and task["image"] is None
    assert task["params"] == {"op": "delete", "machines": [ids["m1"]], "games": ["Jeu", "Autre"], "manager": True}
    assert "1 machine" in task["title"] and "manager" in task["title"]
    manager_only = client.post("/api/saves/delete", json={"games": ["Jeu"], "machines": [], "manager": True})
    assert manager_only.status_code == 200
    assert client.get(f"/api/tasks/{manager_only.json()['task']}").json()["params"]["machines"] == []
    assert queue.get(response.json()["task"]) is not None



def test_status_names_an_agent_too_old_for_saves(agents, tmp_path):
    # Agent d'avant la synchro (v2) : /v1/saves/list répond le 404 texte du routeur Go
    old = agents("steamdeck", no_saves_routes=True)
    settings = make_settings(tmp_path)
    associate(settings, old)
    machine = saves.status(settings)["machines"][0]
    assert machine["ok"] is False
    assert "agent trop ancien" in machine["message"] and "--update" in machine["message"]


# ------------------------------------------------------------------ machine de jeu choisie parmi les associées

def test_associated_machine_becomes_the_game_host(agents, api):
    client, settings, _ = api
    aibox = agents("aibox")
    associate(settings, aibox)
    mid = machine_ids(settings)["aibox"]
    config = client.put("/api/config/game_host/machine", json={"id": mid}).json()
    assert config["game_host"] == "agent" and config["agent_url"] == aibox["url"]
    assert config["game_host_machine"] == mid
    # Même certificat épinglé que l'association : la machine de jeu répond sans nouveau code
    assert saves.agent_cert_path(settings).read_text().strip() == aibox["pem"].strip()
    assert saves.AgentHost(settings, 15).info()["host"] == "aibox"
    # Pas de doublon « machine de jeu » dans la liste : même adresse que la machine associée
    assert [r["id"] for r in saves.machine_rows(settings)] == [mid]
    assert client.put("/api/config/game_host/machine", json={"id": "inconnue"}).status_code == 404
    assert client.put("/api/config/game_host/machine", json={"id": saves.GAME_HOST_ID}).status_code == 404


def test_forgetting_the_game_host_machine_leaves_no_ghost(agents, api):
    """Oublier la machine choisie comme machine de jeu : plus de « machine de jeu (adresse) »
    injoignable dans la liste ; la machine de jeu est désactivée, l'onglet Images revient à
    la bibliothèque. Une autre machine oubliée ne touche pas à la machine de jeu."""
    client, settings, _ = api
    aibox, deck = agents("aibox"), agents("deck")
    associate(settings, aibox)
    associate(settings, deck)
    ids = machine_ids(settings)
    client.put("/api/config/game_host/machine", json={"id": ids["aibox"]})
    assert client.delete(f"/api/saves/machines/{ids['deck']}").status_code == 200
    config = client.get("/api/config").json()
    assert config["game_host"] == "agent" and config["game_host_machine"] == ids["aibox"]
    assert client.delete(f"/api/saves/machines/{ids['aibox']}").status_code == 200
    assert client.get("/api/saves/machines").json()["machines"] == []
    config = client.get("/api/config").json()
    assert config["game_host"] == "off" and config["images_source"] == ""
    assert settings.agent_url == "" and settings.agent_token == ""


# ------------------------------------------------------------------ dossiers déclarés et Switch (agent v8)

def test_status_names_folders_and_switch_games(agents, api):
    """Un dossier déclaré et un jeu Switch arrivent comme des jeux, avec leur nom lisible (celui de
    la machine, sinon déduit) et leur nature ; un jeu Windows garde son nom."""
    client, settings, _ = api
    aibox = agents("aibox", games={"dossier:hookshot": (b"GZh", "", 900), "switch:0100F2C0115B6000": (b"GZz", "", 900),
                                   "Hollow Knight": (b"GZk", "deck", 900)},
                   labels={"dossier:hookshot": "hookshot"})
    deck = agents("deck", games={"switch:0100F2C0115B6000": (b"GZz2", "", 800)},
                  labels={"switch:0100F2C0115B6000": "Switch · Zelda TOTK"})
    associate(settings, aibox)
    associate(settings, deck)
    games = {g["game"]: g for g in client.get("/api/saves/status").json()["games"]}
    assert (games["dossier:hookshot"]["label"], games["dossier:hookshot"]["kind"]) == ("hookshot", "dossier")
    assert (games["switch:0100F2C0115B6000"]["label"], games["switch:0100F2C0115B6000"]["kind"]) == (
        "Switch · Zelda TOTK", "switch")
    assert (games["Hollow Knight"]["label"], games["Hollow Knight"]["kind"]) == ("Hollow Knight", "jeu")
    # Sans nom donné par une machine : déduit du nom
    assert saves.label_of("switch:0100000000010000", [None, {}]) == "Switch · 0100000000010000"
    assert saves.label_of("dossier:retroarch", []) == "retroarch"


def test_api_machine_dirs_are_declared_through_the_agent(agents, api):
    """Le manager déclare, liste et retire les dossiers d'une machine par son agent ; le refus de
    l'agent (chemin hors HOME) remonte en 422 avec son message ; machine inconnue : 404."""
    client, settings, _ = api
    aibox = agents("aibox")
    associate(settings, aibox)
    mid = machine_ids(settings)["aibox"]
    assert client.get(f"/api/saves/machines/{mid}/dirs").json() == {"dirs": [], "eden": ""}
    reply = client.put(f"/api/saves/machines/{mid}/dirs",
                       json={"name": "hookshot", "path": "/home/arcade/.local/share/hookshot/save"})
    assert reply.status_code == 200
    assert reply.json()["dirs"] == [{"name": "hookshot", "path": "/home/arcade/.local/share/hookshot/save"}]
    refused = client.put(f"/api/saves/machines/{mid}/dirs", json={"name": "etc", "path": "/etc"})
    assert refused.status_code == 422 and "HOME" in refused.json()["detail"]
    assert client.delete(f"/api/saves/machines/{mid}/dirs", params={"name": "hookshot"}).json()["dirs"] == []
    assert client.delete(f"/api/saves/machines/{mid}/dirs", params={"name": "hookshot"}).status_code == 422
    assert client.get("/api/saves/machines/inconnue/dirs").status_code == 404


def test_api_machine_dirs_on_an_older_agent(agents, api):
    """Agent v7 (route inconnue) : mise à jour demandée."""
    client, settings, _ = api
    old = agents("old", no_dirs=True)
    associate(settings, old)
    reply = client.get(f"/api/saves/machines/{machine_ids(settings)['old']}/dirs")
    assert reply.status_code == 422 and "--update" in reply.json()["detail"]


# ------------------------------------------------------------------ source de l'onglet Images

def test_game_host_machine_becomes_the_images_source(agents, api, monkeypatch):
    client, settings, _ = api
    aibox = agents("aibox")
    associate(settings, aibox)
    mid = machine_ids(settings)["aibox"]
    assert client.get("/api/source/games").json() == {"source": None, "games": []}
    config = client.put("/api/config/game_host/machine", json={"id": mid}).json()
    assert config["images_source"] == mid and config["images_source_name"] == "aibox"
    # Retour à la bibliothèque du manager, puis machine inconnue refusée
    assert client.put("/api/config/images_source", json={"id": ""}).json()["images_source"] == ""
    assert client.put("/api/config/images_source", json={"id": "inconnue"}).status_code == 404
    assert client.put("/api/config/images_source", json={"id": mid}).json()["images_source"] == mid


def test_source_games_matches_the_library_by_file_name(api, monkeypatch, tmp_path):
    client, settings, _ = api
    save_saves_machines(settings, [{"id": "m1", "name": "aibox", "url": "https://127.0.0.1:1", "token": TOKEN, "pem": ""}])
    main.save_images_source(settings, "m1")

    class Host:
        def games_list(self, timeout=15):
            return {"dir": "/home/default/Games/non-steam", "free": 123,
                    "games": [{"name": "Jeu.pc.wsquashfs", "size": 10, "mtime": 5},
                              {"name": "Songs of Silence.wsquashfs", "size": 20, "mtime": 6}]}

    monkeypatch.setattr(saves, "open_machine", lambda *a, **k: Host())
    monkeypatch.setattr(main, "_refresh_library", lambda force=False: [SimpleNamespace(id="arcade/Jeu", path=Path("/roms/arcade/Jeu.pc.wsquashfs"))])
    data = client.get("/api/source/games").json()
    assert data["source"]["name"] == "aibox" and data["source"]["ok"] and data["source"]["free"] == 123
    assert data["games"] == [{"name": "Jeu.pc.wsquashfs", "size": 10, "mtime": 5, "image": "arcade/Jeu"},
                             {"name": "Songs of Silence.wsquashfs", "size": 20, "mtime": 6, "image": None}]


def test_unreachable_source_is_reported(api, monkeypatch):
    client, settings, _ = api
    save_saves_machines(settings, [{"id": "m1", "name": "deck", "url": "https://127.0.0.1:1", "token": TOKEN, "pem": ""}])
    main.save_images_source(settings, "m1")

    def down(*a, **k):
        raise saves.RebuildError("machine injoignable")

    monkeypatch.setattr(saves, "open_machine", down)
    data = client.get("/api/source/games").json()
    assert data["source"]["ok"] is False and "injoignable" in data["source"]["message"] and data["games"] == []
