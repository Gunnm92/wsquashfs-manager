"""Installation de jeux sur les machines (services/install.py, routes /api/install, /api/uninstall).

De faux agents v6 tournent dans des threads, en HTTPS (certificat auto-signé par machine,
test ignoré sans openssl). Ils gardent les jeux et les envois partiels en mémoire, et
journalisent chaque PUT (décalage, longueur). Blocs et pauses sont réduits pour les tests.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import save_saves_machines, saves_machines
from app.models import Task
from app.services import install
from app.services.rebuild import Job, RebuildCancelled, RebuildError
from app.services.tasks import TaskQueue
from app.services.trial import AgentRejected, AgentUnreachable, fingerprint_pem
from test_saves import SECRET, TOKEN, associate, auth_ok, make_cert, make_settings, machine_ids, pair_reply

BLOCK = 1 << 20
GIB = 1 << 30
NAME = "Jeu.wsquashfs"
OLD_MESSAGE = "agent trop ancien : wsquashfs-launcher --update sur cette machine, puis relancer son agent"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """Blocs de 1 Mio et pauses nulles : les reprises et envois multiples restent courts."""
    monkeypatch.setattr(install, "BLOCK_SIZE", BLOCK)
    monkeypatch.setattr(install, "RETRY_PAUSES", (0, 0, 0, 0, 0))


class GamesAgent(BaseHTTPRequestHandler):
    """Faux agent v6 : `st` (propre à chaque serveur) porte les jeux, les envois partiels et le journal."""

    protocol_version = "HTTP/1.0"
    st: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, payload=None, text=None):
        if text is not None:
            body, ctype = text.encode(), "text/plain; charset=utf-8"
        else:
            body, ctype = json.dumps(payload).encode(), "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not auth_ok(self.st, self.headers):
            self._send(401, {"detail": "jeton refuse"})
            return False
        return True

    def _old(self, path: str) -> bool:
        """Agent d'avant la v6 : routes /v1/games/* inconnues (réponse texte, sans « detail »)."""
        if self.st["old"] and path.startswith("/v1/games"):
            self._send(404, text="404 page not found\n")
            return True
        return False

    def _stored(self) -> int:
        st = self.st
        return sum(map(len, st["games"].values())) + sum(map(len, st["parts"].values()))

    def _listing(self) -> dict:
        st = self.st
        return {"dir": st["dir"], "set": st["dir_set"], "free": st["capacity"] - self._stored(),
                "games": [{"name": n, "size": len(d), "mtime": 1700000000}
                          for n, d in sorted(st["games"].items())],
                "parts": [{"name": n, "size": len(d)} for n, d in sorted(st["parts"].items())]}

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if not self._auth():
            return
        if url.path == "/v1/info":
            return self._send(200, {"version": "6", "host": self.st["host"], "user": "arcade", "os": "linux"})
        if self._old(url.path):
            return
        name = query.get("name", [""])[0]
        if url.path == "/v1/games/list":
            return self._send(200, self._listing())
        if url.path == "/v1/games/part":
            part = self.st["parts"].get(name, b"")
            installed = self.st["games"].get(name)
            return self._send(200, {"part_size": len(part), "installed": installed is not None,
                                    "size": len(installed) if installed is not None else 0})
        self._send(404, {"detail": "route inconnue"})

    def do_PUT(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)                  # toujours lu : la connexion reste propre
        if not self._auth() or self._old(url.path):
            return
        st = self.st
        if url.path != "/v1/games/file":
            return self._send(404, {"detail": "route inconnue"})
        name, offset = query.get("name", [""])[0], int(query.get("offset", ["0"])[0])
        if name in st["games"]:
            return self._send(409, {"detail": "déjà installé"})
        current = st["parts"].get(name, b"")
        if offset != len(current):
            return self._send(409, {"detail": "décalage", "part_size": len(current)})
        if length > st["capacity"] - self._stored():
            return self._send(507, {"detail": "espace insuffisant sur la machine"})
        st["puts"] += 1
        st["log"].append({"offset": offset, "length": length})
        if st["cut_at_put"] == st["puts"]:
            # Coupure au milieu de l'envoi : la moitié du bloc est gardée, pas de réponse
            st["cut_at_put"] = None
            st["parts"][name] = current + body[: length // 2]
            self.close_connection = True
            return
        st["parts"][name] = current + body
        self._send(200, {"part_size": len(st["parts"][name])})

    def do_POST(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if url.path == "/v1/pair":
            code, payload = pair_reply(self.st, self.headers)
            return self._send(code, payload, text="404 page not found\n" if payload is None else None)
        if not self._auth() or self._old(url.path):
            return
        st = self.st
        if url.path == "/v1/games/dir":
            wanted = body.get("dir", "")
            if wanted == "":
                st["dir"], st["dir_set"] = st["home"] + "/jeux", False
            elif wanted == st["home"] or wanted.startswith(st["home"] + "/"):
                st["dir"], st["dir_set"] = wanted, True
            else:
                return self._send(400, {"detail": "hors du HOME de la machine"})
            return self._send(200, {"dir": st["dir"], "set": st["dir_set"]})
        if url.path == "/v1/games/commit":
            name, size, sha = body.get("name", ""), int(body.get("size", -1)), body.get("sha256", "")
            if name not in st["parts"]:
                return self._send(404, {"detail": "pas d'envoi en cours"})
            part = st["parts"][name]
            if len(part) != size:
                return self._send(409, {"detail": "taille"})
            if st["bad_commits"] > 0 or hashlib.sha256(part).hexdigest() != sha:
                st["bad_commits"] = max(0, st["bad_commits"] - 1)
                st["rejected"] += 1
                del st["parts"][name]                   # la machine efface son envoi partiel
                return self._send(422, {"detail": "empreinte incorrecte"})
            st["games"][name] = st["parts"].pop(name)
            st["committed"].append(name)
            return self._send(200, {"name": name, "size": size})
        self._send(404, {"detail": "route inconnue"})

    def do_DELETE(self):
        url = urlsplit(self.path)
        name = parse_qs(url.query).get("name", [""])[0]
        if not self._auth() or self._old(url.path):
            return
        st = self.st
        if url.path != "/v1/games/file":
            return self._send(404, {"detail": "route inconnue"})
        if name in st["mounted"]:
            return self._send(409, {"detail": "jeu monté"})
        removed = name in st["games"] or name in st["parts"]
        st["games"].pop(name, None)
        st["parts"].pop(name, None)
        self._send(200, {"removed": removed})


@pytest.fixture
def games_agents(tmp_path):
    """Fabrique de faux agents v6 (un par machine), arrêtés à la fin du test."""
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    servers = []

    def make(host: str, *, games=None, parts=None, capacity=4 * GIB, home="/home/arcade",
             cut_at_put=None, bad_commits=0, old=False, mounted=()):
        cert, key = make_cert(tmp_path / host)
        st = {"host": host, "token": TOKEN, "home": home, "dir": home + "/jeux", "dir_set": False,
              "capacity": capacity, "games": dict(games or {}), "parts": dict(parts or {}),
              "mounted": set(mounted), "old": old, "puts": 0, "log": [], "cut_at_put": cut_at_put,
              "bad_commits": bad_commits, "rejected": 0, "committed": [],
              "secret": SECRET, "pair_open": True, "old_pair": False, "pairs": [], "bound": None}
        handler = type("H", (GamesAgent,), {"st": st})
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


def make_image(tmp: Path, size: int, name: str = NAME, seed: int = 1) -> Path:
    folder = tmp / "roms" / "arcade"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(random.Random(seed).randbytes(size))
    return path


def puts(agent: dict) -> list[int]:
    return [entry["offset"] for entry in agent["st"]["log"]]


def one_machine(tmp_path, agent: dict):
    settings = make_settings(tmp_path)
    associate(settings, agent)
    return settings, machine_ids(settings)[agent["host"]]


# ------------------------------------------------------------------ envoi

def test_install_sends_the_whole_image_and_the_machine_checks_it(games_agents, tmp_path):
    size = 3 * BLOCK + 1000
    steambox = games_agents("steambox")
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, size)
    result = install.install(settings, image, [mid])
    item = result["results"][0]
    assert item == {"machine": "steambox", "machine_id": mid, "ok": True,
                    "message": "installé sur steambox", "sent": size, "seconds": item["seconds"]}
    assert result["name"] == NAME and result["size"] == size
    assert steambox["st"]["games"][NAME] == image.read_bytes()
    assert steambox["st"]["rejected"] == 0 and steambox["st"]["committed"] == [NAME]
    assert puts(steambox) == [0, BLOCK, 2 * BLOCK, 3 * BLOCK]


def test_interrupted_upload_resumes_where_the_machine_stopped(games_agents, tmp_path):
    size = 3 * BLOCK + 1000
    steambox = games_agents("steambox", cut_at_put=2)      # coupure au 2e bloc, moitié gardée
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, size)
    item = install.install(settings, image, [mid])["results"][0]
    assert item["ok"] is True, item["message"]
    offsets = puts(steambox)
    assert offsets[:2] == [0, BLOCK]
    assert offsets[2] == BLOCK + BLOCK // 2                # reprise là où la machine en était
    assert steambox["st"]["games"][NAME] == image.read_bytes()
    assert steambox["st"]["rejected"] == 0                 # empreinte du préfixe recalculée juste
    assert item["sent"] == size - BLOCK // 2               # la moitié coupée n'est pas comptée


def test_resume_gives_up_after_five_retries(games_agents, tmp_path, monkeypatch):
    steambox = games_agents("steambox")
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, 2 * BLOCK)

    def always_unreachable(self, *args, **kwargs):
        raise AgentUnreachable("agent injoignable (simulé)")

    monkeypatch.setattr(install.AgentHost, "games_put", always_unreachable)
    item = install.install(settings, image, [mid])["results"][0]
    assert item["ok"] is False and "reprises épuisées" in item["message"]
    assert NAME not in steambox["st"]["games"]


def test_part_longer_than_the_image_is_discarded(games_agents, tmp_path):
    size = 2 * BLOCK
    data = random.Random(1).randbytes(size)
    steambox = games_agents("steambox", parts={NAME: data + b"trop long"})
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, size)
    item = install.install(settings, image, [mid])["results"][0]
    assert item["ok"] is True, item["message"]
    assert puts(steambox)[0] == 0
    assert steambox["st"]["games"][NAME] == image.read_bytes()


def test_already_installed_with_the_same_size_sends_nothing(games_agents, tmp_path):
    size = BLOCK
    steambox = games_agents("steambox", games={NAME: random.Random(1).randbytes(size)})
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, size)
    item = install.install(settings, image, [mid])["results"][0]
    assert item["ok"] is True and item["message"] == "déjà installé sur steambox" and item["sent"] == 0
    assert puts(steambox) == []


def test_other_version_installed_is_a_failure(games_agents, tmp_path):
    steambox = games_agents("steambox", games={NAME: b"autre"})
    settings, mid = one_machine(tmp_path, steambox)
    item = install.install(settings, make_image(tmp_path, BLOCK), [mid])["results"][0]
    assert item["ok"] is False and "autre version" in item["message"]
    assert puts(steambox) == []


def test_insufficient_space_sends_nothing(games_agents, tmp_path):
    # Libre : 1 Gio + 1 Mio ; il faut le reste de l'image plus 1 Gio
    steambox = games_agents("steambox", capacity=GIB + BLOCK)
    settings, mid = one_machine(tmp_path, steambox)
    item = install.install(settings, make_image(tmp_path, 3 * BLOCK), [mid])["results"][0]
    assert item["ok"] is False
    assert item["message"].startswith("espace insuffisant sur steambox (libre : ")
    assert puts(steambox) == [] and steambox["st"]["parts"] == {}


def test_commit_refused_once_restarts_the_whole_upload(games_agents, tmp_path):
    size = 2 * BLOCK + 5
    steambox = games_agents("steambox", bad_commits=1)
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, size)
    item = install.install(settings, image, [mid])["results"][0]
    assert item["ok"] is True, item["message"]
    assert puts(steambox) == [0, BLOCK, 2 * BLOCK] * 2          # 2e envoi depuis 0
    assert steambox["st"]["rejected"] == 1
    assert steambox["st"]["games"][NAME] == image.read_bytes()


def test_commit_refused_twice_fails(games_agents, tmp_path):
    steambox = games_agents("steambox", bad_commits=2)
    settings, mid = one_machine(tmp_path, steambox)
    item = install.install(settings, make_image(tmp_path, BLOCK), [mid])["results"][0]
    assert item["ok"] is False and "empreinte refusée deux fois" in item["message"]
    assert NAME not in steambox["st"]["games"]


def test_old_agent_gets_a_clear_message(games_agents, tmp_path):
    old = games_agents("vieux", old=True)
    settings, mid = one_machine(tmp_path, old)
    item = install.install(settings, make_image(tmp_path, BLOCK), [mid])["results"][0]
    assert item["ok"] is False and item["message"] == OLD_MESSAGE


def test_one_failing_machine_does_not_stop_the_others(games_agents, tmp_path):
    old = games_agents("vieux", old=True)
    good = games_agents("steambox")
    settings = make_settings(tmp_path)
    associate(settings, old)
    associate(settings, good)
    ids = machine_ids(settings)
    result = install.install(settings, make_image(tmp_path, BLOCK), [ids["vieux"], ids["steambox"]])
    assert [r["ok"] for r in result["results"]] == [False, True]
    assert good["st"]["games"][NAME]


def test_only_wsquashfs_images_install(tmp_path):
    with pytest.raises(RebuildError, match="seules les images .wsquashfs"):
        install.install(make_settings(tmp_path), make_image(tmp_path, BLOCK, name="Jeu.pc"), [])


def test_cancel_stops_between_blocks(games_agents, tmp_path):
    steambox = games_agents("steambox")
    settings, mid = one_machine(tmp_path, steambox)
    image = make_image(tmp_path, 3 * BLOCK)
    holder = {}

    def cancel_after_first_block(phase, fraction):
        holder["job"].cancel()

    holder["job"] = Job(on_progress=cancel_after_first_block)
    with pytest.raises(RebuildCancelled):
        install.install(settings, image, [mid], holder["job"])
    assert puts(steambox) == [0]                             # un seul bloc envoyé, rien validé
    assert len(steambox["st"]["parts"][NAME]) == BLOCK and steambox["st"]["games"] == {}


# ------------------------------------------------------------------ désinstallation

def test_uninstall_removes_then_reports_not_installed(games_agents, tmp_path):
    steambox = games_agents("steambox", games={NAME: b"jeu"})
    settings, mid = one_machine(tmp_path, steambox)
    first = install.uninstall(settings, NAME, [mid])
    assert first["results"] == [{"machine": "steambox", "machine_id": mid, "ok": True,
                                 "message": "désinstallé de steambox"}]
    assert steambox["st"]["games"] == {}
    second = install.uninstall(settings, NAME, [mid])["results"][0]
    assert second["ok"] is True and second["message"] == "pas installé sur steambox"


def test_mounted_game_fails_on_that_machine_only(games_agents, tmp_path):
    busy = games_agents("busy", games={NAME: b"jeu"}, mounted={NAME})
    free = games_agents("libre", games={NAME: b"jeu"})
    settings = make_settings(tmp_path)
    associate(settings, busy)
    associate(settings, free)
    ids = machine_ids(settings)
    results = install.uninstall(settings, NAME, [ids["busy"], ids["libre"]])["results"]
    assert [r["ok"] for r in results] == [False, True]
    assert results[0]["message"] == "jeu monté ou en cours : suppression refusée"
    assert NAME in busy["st"]["games"] and free["st"]["games"] == {}


def test_uninstall_refuses_a_bad_name(tmp_path):
    with pytest.raises(RebuildError, match="nom d'image invalide"):
        install.uninstall(make_settings(tmp_path), "../Jeu.wsquashfs", [])


# ------------------------------------------------------------------ dossier, états, tâche

def test_set_dir_relays_the_agent_refusal(games_agents, tmp_path):
    steambox = games_agents("steambox")
    settings, mid = one_machine(tmp_path, steambox)
    with pytest.raises(AgentRejected, match="hors du HOME"):
        install.set_dir(settings, mid, "/etc")
    assert install.set_dir(settings, mid, "/home/arcade/roms") == {"dir": "/home/arcade/roms", "set": True}
    assert install.set_dir(settings, mid, "") == {"dir": "/home/arcade/jeux", "set": False}


def test_status_lists_games_parts_and_unreachable_machines(games_agents, tmp_path):
    steambox = games_agents("steambox", games={NAME: b"jeu"}, parts={"Autre.wsquashfs": b"abc"})
    down = games_agents("eteinte")
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    associate(settings, down)
    machines = saves_machines(settings)
    for machine in machines:
        if machine["name"] == "eteinte":
            machine["url"] = "https://127.0.0.1:1"
    save_saves_machines(settings, machines)
    data = install.status(settings)
    by_name = {m["name"]: m for m in data["machines"]}
    up = by_name["steambox"]
    assert set(up) == {"id", "name", "ok", "message", "dir", "set", "free", "games", "parts", "rate"}
    assert up["ok"] is True and up["message"] == "" and up["set"] is False
    assert up["dir"] == "/home/arcade/jeux" and up["games"] == [NAME]
    assert up["parts"] == [{"name": "Autre.wsquashfs", "size": 3}]
    assert up["free"] == 4 * GIB - 3 - 3
    assert by_name["eteinte"]["ok"] is False and "injoignable" in by_name["eteinte"]["message"]
    assert by_name["eteinte"]["games"] == [] and by_name["eteinte"]["parts"] == []


def test_run_install_keeps_the_result_and_fails_partially(games_agents, tmp_path):
    steambox = games_agents("steambox")
    busy = games_agents("busy", games={NAME: b"autre version"}, mounted={NAME})
    settings = make_settings(tmp_path)
    associate(settings, steambox)
    associate(settings, busy)
    ids = machine_ids(settings)
    image = make_image(tmp_path, BLOCK)
    task = Task(id="t1", kind="install", image="arcade/Jeu", image_path=str(image),
                params={"op": "install", "machines": [ids["steambox"], ids["busy"]]})
    with pytest.raises(RebuildError, match="1 échec"):
        install.run_install(settings, task, Job())
    results = task.params["result"]["results"]
    assert [r["ok"] for r in results] == [True, False]         # busy : autre version déjà installée
    assert results[1]["message"].startswith("une autre version")
    task = Task(id="t2", kind="install", params={"op": "uninstall", "name": NAME,
                                                 "machines": [ids["busy"]]})
    with pytest.raises(RebuildError, match="1 échec"):
        install.run_install(settings, task, Job())
    assert task.params["result"]["results"][0]["ok"] is False
    assert install.run_install(settings, Task(id="t3", kind="install", params={
        "op": "uninstall", "name": NAME, "machines": [ids["steambox"]]}), Job()) == "1 opération(s) réussie(s) de 1 machine(s)"


# ------------------------------------------------------------------ API

@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.mode = "connecte"                  # API des machines : hors mode autonome
    queue = TaskQueue(tmp_path / "state" / "tasks.json", main.queue.handlers)
    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "queue", queue)
    images = {"arcade/Jeu": SimpleNamespace(id="arcade/Jeu", path=Path("/roms/arcade/Jeu.wsquashfs")),
              "arcade/Autre": SimpleNamespace(id="arcade/Autre", path=Path("/roms/arcade/Autre.wsquashfs")),
              "arcade/Pc": SimpleNamespace(id="arcade/Pc", path=Path("/roms/arcade/Pc.pc"))}

    def find_image(image_id):
        if image_id in images:
            return images[image_id]
        raise main.HTTPException(status_code=404, detail=f"image inconnue : {image_id}")

    monkeypatch.setattr(main, "_find_image", find_image)
    return TestClient(main.app), settings, queue


def test_api_install_queues_one_task_per_image(games_agents, api):
    client, settings, _ = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    response = client.post("/api/install", json={"ids": ["arcade/Jeu"], "machines": [mid]})
    assert response.status_code == 200
    task = client.get(f"/api/tasks/{response.json()['tasks'][0]}").json()
    assert task["kind"] == "install" and task["image"] == "arcade/Jeu"
    assert task["image_path"] == "/roms/arcade/Jeu.wsquashfs"
    assert task["params"] == {"op": "install", "machines": [mid]}


def test_api_install_validation(games_agents, api):
    client, settings, _ = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    post = lambda body: client.post("/api/install", json=body).status_code  # noqa: E731
    assert post({"ids": [], "machines": [mid]}) == 422
    assert post({"ids": ["arcade/Jeu"], "machines": []}) == 422
    assert post({"ids": ["arcade/Jeu", "arcade/Jeu"], "machines": [mid]}) == 422
    assert post({"ids": ["arcade/Jeu"], "machines": [mid, mid]}) == 422
    assert post({"ids": ["arcade/Pc"], "machines": [mid]}) == 422         # pas un .wsquashfs
    assert post({"ids": ["arcade/Absent"], "machines": [mid]}) == 404
    assert post({"ids": ["arcade/Jeu"], "machines": ["inconnue"]}) == 404


def test_api_install_conflicts_with_an_active_task_on_the_image(games_agents, api):
    client, settings, queue = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    queue.submit("autorun", "arcade/Jeu", Path("/roms/arcade/Jeu.wsquashfs"), {}, "autorun")
    response = client.post("/api/install", json={"ids": ["arcade/Jeu"], "machines": [mid]})
    assert response.status_code == 409
    assert [t.kind for t in queue.list()] == ["autorun"]              # rien soumis


def test_api_install_is_all_or_nothing_across_images(games_agents, api):
    client, settings, queue = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    queue.submit("autorun", "arcade/Autre", Path("/roms/arcade/Autre.wsquashfs"), {}, "autorun")
    response = client.post("/api/install", json={"ids": ["arcade/Jeu", "arcade/Autre"], "machines": [mid]})
    assert response.status_code == 409                                # Autre est occupée
    assert [t.kind for t in queue.list()] == ["autorun"]              # Jeu n'a pas été soumise
    response = client.post("/api/install", json={"ids": ["arcade/Pc", "arcade/Jeu"], "machines": [mid]})
    assert response.status_code == 422                                # .pc refusé avant tout
    assert [t.kind for t in queue.list()] == ["autorun"]


def test_api_uninstall_queues_tasks_and_validates_names(games_agents, api):
    client, settings, queue = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    response = client.post("/api/uninstall", json={"names": [NAME], "machines": [mid]})
    assert response.status_code == 200 and len(response.json()["tasks"]) == 1
    task = client.get(f"/api/tasks/{response.json()['tasks'][0]}").json()
    assert task["kind"] == "install" and task["image"] is None
    assert task["params"] == {"op": "uninstall", "name": NAME, "machines": [mid]}
    assert client.post("/api/uninstall", json={"names": ["../x.wsquashfs"], "machines": [mid]}).status_code == 422
    assert client.post("/api/uninstall", json={"names": ["Jeu.pc"], "machines": [mid]}).status_code == 422
    assert client.post("/api/uninstall", json={"names": [NAME, NAME], "machines": [mid]}).status_code == 422
    assert client.post("/api/uninstall", json={"names": [NAME], "machines": ["inconnue"]}).status_code == 404


def test_api_set_dir_relays_400_and_404(games_agents, api):
    client, settings, _ = api
    steambox = games_agents("steambox")
    associate(settings, steambox)
    mid = machine_ids(settings)["steambox"]
    refused = client.post(f"/api/install/machines/{mid}/dir", json={"dir": "/etc"})
    assert refused.status_code == 400 and "hors du HOME" in refused.json()["detail"]
    accepted = client.post(f"/api/install/machines/{mid}/dir", json={"dir": "/home/arcade/roms"})
    assert accepted.status_code == 200 and accepted.json() == {"dir": "/home/arcade/roms", "set": True}
    assert client.post("/api/install/machines/inconnue/dir", json={"dir": ""}).status_code == 404


def test_api_status_shape(games_agents, api):
    client, settings, _ = api
    steambox = games_agents("steambox", games={NAME: b"jeu"})
    associate(settings, steambox)
    data = client.get("/api/install/status").json()
    assert set(data) == {"machines"}
    machine = data["machines"][0]
    assert set(machine) == {"id", "name", "ok", "message", "dir", "set", "free", "games", "parts", "rate"}
    assert machine["games"] == [NAME] and machine["ok"] is True



def test_duration_text_rounds_for_humans():
    assert install._duration_text(20) == "moins d'1 min"
    assert install._duration_text(11 * 60 + 10) == "~11 min"
    assert install._duration_text(80 * 60) == "~1 h 20"


def test_progress_shows_remaining_time_and_rate_is_remembered(games_agents, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "MIN_MEASURED", 1)
    size = 3 * BLOCK + 1000
    deck = games_agents("steamdeck")
    settings, mid = one_machine(tmp_path, deck)
    phases = []
    job = Job(on_progress=lambda phase, value: phases.append(phase))
    install.install(settings, make_image(tmp_path, size), [mid], job)
    assert phases and all(" · reste " in p and "Mio/s" in p for p in phases)
    assert phases[-1].endswith("reste validation")
    # Débit retenu pour cette machine, renvoyé par le statut (estimation du dialogue)
    rate = install.measured_rates(settings)[mid]
    assert rate > 0
    assert install.status(settings)["machines"][0]["rate"] == rate


def test_short_or_failed_transfer_does_not_change_the_rate(games_agents, tmp_path):
    deck = games_agents("steamdeck")
    settings, mid = one_machine(tmp_path, deck)
    install.install(settings, make_image(tmp_path, 1000), [mid])      # sous MIN_MEASURED
    assert install.measured_rates(settings) == {}
    assert install.status(settings)["machines"][0]["rate"] == 0
