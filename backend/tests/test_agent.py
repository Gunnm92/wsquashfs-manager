"""Agent réseau : association (empreinte), jeton, opérations, épinglage.

Un faux wsquashfs-agent tourne dans un thread, en HTTPS avec un certificat
auto-signé généré par openssl (test ignoré sans openssl).
"""

from __future__ import annotations

import base64
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from app.config import Settings
from app.services import trial
from app.services.rebuild import RebuildError

PNG = b"\x89PNG\r\n\x1a\nfake"
TOKEN = "jeton-de-test"
RUNNER_TREE = {"files/bin/wine": 10, "proton": 0}


def make_cert(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=test",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


class FakeAgent(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    seen: ClassVar[list[tuple[str, str]]] = []
    bodies: ClassVar[list[tuple[str, dict]]] = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, json.dumps({"detail": "jeton refusé"}).encode())
            return False
        return True

    def do_GET(self):
        FakeAgent.seen.append(("GET", self.path))
        if not self._ok():
            return
        if self.path.startswith("/v1/info"):
            return self._send(200, json.dumps({"version": "1", "host": "fake", "user": "arcade"}).encode())
        if self.path.startswith("/v1/doctor"):
            doctor = {"code": 0, "sections": [], "verdict": "✓ Prêt à lancer des jeux",
                      "runners": [{"name": "GE-Proton", "path": "/opt/ge"}]}
            return self._send(200, json.dumps(doctor).encode())
        if self.path.startswith("/v1/runner/list"):
            return self._send(200, json.dumps({"entries": RUNNER_TREE}).encode())
        if self.path.startswith("/v1/runner/tar"):
            return self._send(200, b"TARDATA", "application/x-tar")
        self._send(404, b'{"detail":"route inconnue"}')

    def do_POST(self):
        FakeAgent.seen.append(("POST", self.path))
        if not self._ok():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeAgent.bodies.append((self.path, body))
        if body.get("rel") == "win/Absent.wsquashfs":
            return self._send(404, b'{"detail":"image introuvable : win/Absent.wsquashfs"}')
        report = {"code": 0, "elapsed": 60, "launcher_alive": True, "game_seen": True, "games": [],
                  "windows": [{"id": "0x1", "name": "Jeu", "state": "_NET_WM_STATE_FULLSCREEN"}],
                  "left": {"procs": 0, "mounts": 0}, "screenshot": True, "log": ""}
        out = {"report": report, "screenshot": base64.b64encode(PNG).decode()}
        self._send(200, json.dumps(out).encode())


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    tmp = tmp_path_factory.mktemp("agent")
    cert, key = make_cert(tmp)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"https://127.0.0.1:{server.server_address[1]}", "pem": cert.read_text(), "tmp": tmp}
    server.shutdown()


def settings_for(tmp: Path, pem: str | None, url: str = "", token: str = TOKEN) -> Settings:
    s = Settings(state_dir=tmp / "state", game_host="agent", agent_url=url, agent_token=token)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    if pem:
        (s.state_dir / "agent-cert.pem").write_text(pem)
    return s


def test_fingerprint_matches_pair_format(agent):
    fp = trial.fingerprint_pem(agent["pem"])
    assert len(fp.split(":")) == 32 and fp == fp.upper()


def test_info_with_pinned_certificate(agent, tmp_path):
    host = trial.AgentHost(settings_for(tmp_path, agent["pem"], agent["url"]), 10)
    assert host.info()["host"] == "fake"


def test_wrong_token_is_reported_clearly(agent, tmp_path):
    host = trial.AgentHost(settings_for(tmp_path, agent["pem"], agent["url"], token="faux"), 10)
    with pytest.raises(RebuildError, match="jeton"):
        host.info()


def test_other_certificate_is_refused(agent, tmp_path):
    other = tmp_path / "autre"
    other.mkdir()
    cert, _ = make_cert(other)
    host = trial.AgentHost(settings_for(tmp_path, cert.read_text(), agent["url"]), 10)
    with pytest.raises(RebuildError, match="injoignable"):
        host.info()


def test_not_associated_without_certificate(tmp_path):
    with pytest.raises(RebuildError, match="non associé"):
        trial.AgentHost(settings_for(tmp_path, None, "https://127.0.0.1:1"), 10)


def test_doctor_and_runner_operations(agent, tmp_path):
    host = trial.AgentHost(settings_for(tmp_path, agent["pem"], agent["url"]), 10)
    assert host.doctor()["runners"][0]["name"] == "GE-Proton"
    assert host.listing("/opt/ge") == RUNNER_TREE
    chunks = []
    assert host.fetch("/opt/ge", chunks.append) == (0, b"")
    assert b"".join(chunks) == b"TARDATA"


def test_trial_through_agent_returns_report_and_screenshot(agent, tmp_path):
    settings = settings_for(tmp_path, agent["pem"], agent["url"])
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"hsqs")
    host = trial.AgentHost(settings, 10)
    assert host.path_of(image) == "win/Jeu.wsquashfs"
    data, shot = host.trial("win/Jeu.wsquashfs", 60)
    assert shot == PNG and data["game_seen"] is True and data["windows"][0]["name"] == "Jeu"
    with pytest.raises(RebuildError, match="image introuvable"):
        host.trial("win/Absent.wsquashfs", 60)


def test_game_host_agent_selected_by_settings(agent, tmp_path):
    settings = settings_for(tmp_path, agent["pem"], agent["url"])
    host = trial.game_host(settings)
    assert isinstance(host, trial.AgentHost)
    assert trial.host_name(settings) == f"l'agent ({agent['url']})"


def test_maintenance_refuses_unlisted_actions(agent, tmp_path):
    settings = settings_for(tmp_path, agent["pem"], agent["url"])
    with pytest.raises(RebuildError, match="non autorisée"):
        trial.maintenance(settings, "clear")          # --clear n'est pas exposé
    with pytest.raises(RebuildError, match="non autorisée"):
        trial.maintenance(settings, "pack")
    with pytest.raises(RebuildError, match="non autorisée"):
        trial.maintenance(settings, "backup")         # retiré du lanceur (--saves le remplace)
    with pytest.raises(RebuildError, match="ne prend pas de jeu"):
        trial.maintenance(settings, "clean", tmp_path / "Jeu.wsquashfs")


def test_maintenance_saves_all_or_one_game(agent, tmp_path):
    settings = settings_for(tmp_path, agent["pem"], agent["url"])
    image = tmp_path / "roms" / "win" / "Jeu.wsquashfs"
    FakeAgent.bodies.clear()
    trial.maintenance(settings, "saves")
    trial.maintenance(settings, "saves", image)
    assert FakeAgent.bodies == [("/v1/maintenance", {"action": "saves"}),
                                ("/v1/maintenance", {"action": "saves", "rel": "win/Jeu.wsquashfs"})]


def test_host_status_connected_and_refused(agent, tmp_path):
    ok = trial.host_status(settings_for(tmp_path, agent["pem"], agent["url"]))
    assert ok["state"] == "ok" and "fake" in ok["detail"]
    ko = trial.host_status(settings_for(tmp_path, agent["pem"], agent["url"], token="faux"))
    assert ko["state"] == "ko" and "jeton" in ko["detail"]
    off = trial.host_status(Settings(state_dir=tmp_path / "s", game_host="off"))
    assert off["state"] == "off"


def test_pair_code_is_parsed():
    fp = "AB" * 32
    pair = trial.parse_pair_code(f"wsq://jeton-ABC_12@192.168.1.20:8790#{fp}")
    assert pair == {"url": "https://192.168.1.20:8790", "token": "jeton-ABC_12", "fingerprint": fp}


@pytest.mark.parametrize("code", ["", "https://192.168.1.20:8790", "wsq://jeton@hote:8790",
                                  "wsq://@hote:8790#" + "AB" * 32, "wsq://j@hote:8790#AA"])
def test_bad_pair_code_is_refused(code):
    with pytest.raises(ValueError):
        trial.parse_pair_code(code)
