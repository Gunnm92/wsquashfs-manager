"""API des machines (services/machine_api.py) : jeton par machine, en-têtes, liste avec états,
envoi et archivage de la version précédente, états par machine, et écoute HTTPS sur
machine_port (la seule partie qui démarre un serveur, testée avec un port libre)."""

from __future__ import annotations

import hashlib
import shutil
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import deps, machines, main
from app.config import save_saves_machines, saves_machines
from app.services import clients, machine_api, saves_store
from app.services.errors import ManagerError
from tests.test_saves import make_settings

GAME = "Jeu"


def gz(text: str) -> bytes:
    return b"\x1f\x8b" + text.encode()


def fp(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def add_manager_machine(settings, machine_id: str, name: str, token: str) -> None:
    """Machine déclarée au manager : seul l'empreinte de son jeton est enregistrée."""
    machines = saves_machines(settings)
    machines.append({"id": machine_id, "name": name, "url": f"https://{name}:8765", "token": "",
                     "pem": "", "manager_token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    save_saves_machines(settings, machines)


@pytest.fixture
def mapi(tmp_path, monkeypatch):
    """Client de machine_app (sans TLS) : deux machines déclarées, jetons A et B."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(deps, "_settings", settings)
    add_manager_machine(settings, "aaaa0001", "steambox", "jeton-a")
    add_manager_machine(settings, "bbbb0002", "aibox", "jeton-b")
    headers_a = {"Authorization": "Bearer jeton-a", "X-Wsq-Machine": "steambox-sync"}
    headers_b = {"Authorization": "Bearer jeton-b", "X-Wsq-Machine": "aibox-sync"}
    return TestClient(machines.machine_app), settings, headers_a, headers_b


def put_tar(client, headers, data: bytes, game: str = GAME, fingerprint: str | None = None,
            profile: str | None = None):
    head = {"Content-Type": "application/gzip", "X-Wsq-Fingerprint": fingerprint or fp(data), **headers}
    if profile:
        head["X-Wsq-Profile"] = profile
    return client.put("/v1/saves/tar", params={"game": game}, content=data, headers=head)


def test_info_needs_a_known_token(mapi):
    client, _, headers_a, _ = mapi
    assert client.get("/v1/info", headers=headers_a).json()["version"] == "manager"
    refused = client.get("/v1/info", headers={"Authorization": "Bearer inconnu", "X-Wsq-Machine": "x"})
    assert refused.status_code == 401 and refused.json() == {"detail": "jeton refusé"}
    assert client.get("/v1/info").status_code == 401


def test_token_is_checked_before_the_machine_header(mapi):
    client, _, _headers_a, _ = mapi
    assert client.get("/v1/info", headers={"Authorization": "Bearer jeton-a"}).status_code == 400
    bad = {"Authorization": "Bearer jeton-a", "X-Wsq-Machine": "pas valide !"}
    assert client.get("/v1/info", headers=bad).status_code == 400
    assert client.get("/v1/info", headers={"Authorization": "Bearer jeton-x"}).status_code == 401


def test_the_interface_is_not_served(mapi):
    client, _, headers_a, _ = mapi
    assert client.get("/", headers=headers_a).status_code == 404
    assert client.get("/api/config", headers=headers_a).status_code == 404
    assert client.get("/docs").status_code == 404


def test_list_gives_the_state_of_the_calling_machine(mapi):
    client, settings, headers_a, headers_b = mapi
    data = gz("v1")
    saves_store.store(settings, GAME, part_of(settings, data), fingerprint=fp(data), newest=42, size=7,
                profile="prefixe", source="steambox")
    saves_store.record_state(settings, GAME, "aaaa0001", fp(data))
    listed = client.get("/v1/saves/list", headers=headers_a).json()["games"]
    assert listed == [{"name": GAME, "fingerprint": fp(data), "newest": 42, "size": 7, "content": "",
                       "state": fp(data)}]
    assert client.get("/v1/saves/list", headers=headers_b).json()["games"][0]["state"] == ""


def part_of(settings, data: bytes):
    folder = saves_store.reserve_dir(settings, GAME)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / "test.part"
    part.write_bytes(data)
    return part


def test_put_stores_the_archive_and_archives_the_previous_version(mapi):
    client, settings, headers_a, _ = mapi
    first = put_tar(client, headers_a, gz("v1"), profile="proton")
    assert first.status_code == 200 and first.json() == {"fingerprint": fp(gz("v1")), "archived": ""}
    assert saves_store.reserve_meta(settings, GAME)["profile"] == "proton"
    second = put_tar(client, headers_a, gz("v2"))
    assert second.status_code == 200 and second.json()["archived"].endswith(".tar.gz")
    meta = saves_store.reserve_meta(settings, GAME)
    assert meta["fingerprint"] == fp(gz("v2")) and meta["size"] == len(gz("v2"))
    assert meta["source"] == "steambox" and meta["profile"] is None
    assert saves_store.archives(settings, GAME)[0] == second.json()["archived"][:-len(".tar.gz")]


def test_put_keeps_three_archived_versions(mapi):
    client, settings, headers_a, _ = mapi
    for index in range(5):
        assert put_tar(client, headers_a, gz(f"v{index}")).status_code == 200
    assert len(saves_store.archives(settings, GAME)) == saves_store.KEEP_ARCHIVES == 3


def test_same_version_is_not_archived(mapi):
    client, settings, headers_a, _ = mapi
    put_tar(client, headers_a, gz("v1"))
    again = put_tar(client, headers_a, gz("v1"))
    assert again.json()["archived"] == "" and saves_store.archives(settings, GAME) == []


def test_put_without_a_valid_fingerprint_is_refused(mapi):
    client, settings, headers_a, _ = mapi
    missing = client.put("/v1/saves/tar", params={"game": GAME}, content=gz("v1"), headers=headers_a)
    assert missing.status_code == 400
    short = put_tar(client, headers_a, gz("v1"), fingerprint="abc")
    assert short.status_code == 400
    assert saves_store.reserve_games(settings) == []


def test_put_of_a_body_that_is_not_gzip_leaves_nothing(mapi):
    client, settings, headers_a, _ = mapi
    assert put_tar(client, headers_a, b"texte").status_code == 400
    assert put_tar(client, headers_a, b"").status_code == 400
    assert saves_store.reserve_games(settings) == []
    folder = saves_store.reserve_dir(settings, GAME)
    assert not folder.exists() or list(folder.iterdir()) == []


def test_put_over_the_size_limit_is_refused(mapi, monkeypatch):
    client, settings, headers_a, _ = mapi
    monkeypatch.setattr(machine_api, "MAX_ARCHIVE", 8)
    assert put_tar(client, headers_a, gz("trop long")).status_code == 413
    # Envoi sans longueur déclarée (chunked) : la limite compte pendant la lecture
    chunked = client.put("/v1/saves/tar", params={"game": GAME}, headers={**headers_a, "X-Wsq-Fingerprint": fp(gz("x"))},
                         content=iter([gz("trop long"), b"encore"]))
    assert chunked.status_code == 413
    assert saves_store.reserve_games(settings) == []
    folder = saves_store.reserve_dir(settings, GAME)
    assert not folder.exists() or list(folder.iterdir()) == []


def test_put_with_a_bad_game_name_is_refused(mapi):
    client, _, headers_a, _ = mapi
    assert put_tar(client, headers_a, gz("v1"), game="../evasion").status_code == 400


def test_get_returns_the_current_version_with_its_headers(mapi):
    client, _settings, headers_a, _ = mapi
    put_tar(client, headers_a, gz("v1"), profile="proton-ge")
    got = client.get("/v1/saves/tar", params={"game": GAME}, headers=headers_a)
    assert got.status_code == 200 and got.content == gz("v1")
    assert got.headers["content-type"] == "application/gzip"
    assert got.headers["x-wsq-fingerprint"] == fp(gz("v1"))
    assert got.headers["x-wsq-profile"] == "proton-ge"


def test_get_errors(mapi):
    client, _, headers_a, _ = mapi
    assert client.get("/v1/saves/tar", params={"game": GAME}, headers=headers_a).status_code == 404
    assert client.get("/v1/saves/tar", params={"game": "../x"}, headers=headers_a).status_code == 400
    assert client.get("/v1/saves/tar", headers=headers_a).status_code == 400


def test_state_is_recorded_when_the_fingerprint_is_the_current_one(mapi):
    client, _settings, headers_a, headers_b = mapi
    put_tar(client, headers_a, gz("v1"))
    ok = client.post("/v1/saves/state", json={"game": GAME, "fingerprint": fp(gz("v1"))}, headers=headers_b)
    assert ok.status_code == 200 and ok.json() == {"fingerprint": fp(gz("v1"))}
    listed = client.get("/v1/saves/list", headers=headers_b).json()["games"][0]
    assert listed["state"] == fp(gz("v1"))


def test_state_is_refused_when_the_manager_has_another_version(mapi):
    client, _, headers_a, _ = mapi
    put_tar(client, headers_a, gz("v1"))
    other = client.post("/v1/saves/state", json={"game": GAME, "fingerprint": fp(gz("autre"))}, headers=headers_a)
    assert other.status_code == 409
    absent = client.post("/v1/saves/state", json={"game": "Absent", "fingerprint": fp(gz("v1"))}, headers=headers_a)
    assert absent.status_code == 404 and absent.json() == {"detail": "aucune sauvegarde pour ce jeu"}


def test_states_are_per_machine_and_survive_a_new_version(mapi):
    """Le envoi de A ne change pas l'état de B, et une version nouvelle garde les états."""
    client, _settings, headers_a, headers_b = mapi
    put_tar(client, headers_a, gz("v1"))
    client.post("/v1/saves/state", json={"game": GAME, "fingerprint": fp(gz("v1"))}, headers=headers_b)
    put_tar(client, headers_a, gz("v2"))
    states = {m: client.get("/v1/saves/list", headers=h).json()["games"][0]["state"]
              for m, h in (("a", headers_a), ("b", headers_b))}
    assert states == {"a": fp(gz("v2")), "b": fp(gz("v1"))}


def test_put_needs_a_known_token(mapi):
    client, settings, _, _ = mapi
    response = put_tar(client, {"Authorization": "Bearer inconnu", "X-Wsq-Machine": "x"}, gz("v1"))
    assert response.status_code == 401
    assert saves_store.reserve_games(settings) == []


def test_manager_certificate_has_the_pairing_format(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    settings = make_settings(tmp_path)
    shown = clients.manager_fingerprint(settings)
    parts = shown.split(":")
    assert len(parts) == 32 and all(len(p) == 2 and p == p.upper() for p in parts)
    cert, key = clients.machine_tls(settings, create=False)
    assert cert.is_file() and key.is_file() and (key.stat().st_mode & 0o077) == 0
    assert clients.manager_fingerprint(settings) == shown            # créé une seule fois


def test_no_certificate_without_openssl(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    with pytest.raises(ManagerError):
        clients.machine_tls(settings)
    assert clients.manager_fingerprint(settings, create=False) == ""


def test_server_is_off_when_the_port_is_zero(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    assert settings.machine_port == 0
    assert machines._start_machine_server(settings) is None


def test_machine_server_serves_only_the_machine_api(tmp_path, monkeypatch):
    """Écoute HTTPS réelle sur un port libre : l'API des machines répond avec le certificat
    épinglé ; l'interface n'est pas servie sur ce port."""
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = make_settings(tmp_path, machine_port=port, machine_host="127.0.0.1")
    monkeypatch.setattr(deps, "_settings", settings)
    add_manager_machine(settings, "aaaa0001", "steambox", "jeton-a")
    server = machines._start_machine_server(settings)
    assert server is not None
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        pem = clients.machine_tls(settings, create=False)[0].read_text()
        from app.services.agent import pinned_context
        url = f"https://127.0.0.1:{port}"
        with httpx.Client(base_url=url, verify=pinned_context(pem)) as client:
            ok = client.get("/v1/info", headers={"Authorization": "Bearer jeton-a", "X-Wsq-Machine": "steambox-sync"})
            assert ok.status_code == 200 and ok.json()["version"] == "manager"
            assert client.get("/", headers={"Authorization": "Bearer jeton-a"}).status_code == 404
            assert client.get("/v1/info").status_code == 401
    finally:
        server.should_exit = True
    assert machine_api.MAX_ARCHIVE == 2 << 30


def test_machine_url_setting_is_checked_and_shown(mapi):
    """Réglages : adresse du manager pour les machines, validée ; l'empreinte n'est affichée
    qu'avec un certificat existant (pas créé par la lecture de la configuration)."""
    _, settings, _, _ = mapi
    client = TestClient(main.app, headers={"x-wsfs-token": ""})
    config = client.get("/api/config").json()
    assert config["machine_url"] == "" and config["machine_port"] == 0 and config["machine_fingerprint"] == ""
    for bad in ("http://192.168.1.10:8790", "https://192.168.1.10", "https://192.168.1.10:99999"):
        assert client.put("/api/config/machine_url", json={"url": bad}).status_code == 422
    saved = client.put("/api/config/machine_url", json={"url": "https://192.168.1.10:8790/"})
    assert saved.status_code == 200 and saved.json()["machine_url"] == "https://192.168.1.10:8790"
    assert settings.machine_url == "https://192.168.1.10:8790"
    assert client.put("/api/config/machine_url", json={"url": ""}).json()["machine_url"] == ""
    assert not (settings.state_dir / "tls").exists()


def test_put_keeps_the_size_and_date_of_the_save_files(mapi):
    """Taille des fichiers et date du plus récent envoyées par le lanceur (X-Wsq-Size,
    X-Wsq-Newest) : gardées telles quelles, comme la liste d'un agent (elles départagent un
    conflit de --sync) ; sans en-tête : taille de l'archive."""
    client, _settings, headers_a, _ = mapi
    data = gz("partie")
    assert put_tar(client, {**headers_a, "X-Wsq-Size": "123456", "X-Wsq-Newest": "1700000000"}, data).status_code == 200
    listed = client.get("/v1/saves/list", headers=headers_a).json()["games"][0]
    assert (listed["size"], listed["newest"]) == (123456, 1700000000)
    other = gz("autre partie")
    assert put_tar(client, {**headers_a, "X-Wsq-Size": "abc"}, other).status_code == 200
    assert client.get("/v1/saves/list", headers=headers_a).json()["games"][0]["size"] == len(other)


def test_put_refuses_an_unsafe_declared_dir(mapi):
    """Chemin de dossier relayé aux autres machines : relatif à HOME, sans « .. »."""
    client, _settings, headers_a, _ = mapi
    name = "dossier:Zelda"
    for bad in ("%2Fetc%2Fcron.d", "..%2F..%2F.ssh", "a%2F..%2F..%2Fb", "~%2F.bashrc", "a%0Ab"):
        assert put_tar(client, {**headers_a, "X-Wsq-Dir": bad}, gz("z"), game=name).status_code == 400, bad


def test_clean_dir():
    from app.services.saves import clean_dir
    assert clean_dir(".local/share/x") == ".local/share/x"
    assert clean_dir("a..b/c") == "a..b/c"
    for bad in ("", "/etc", "../x", "a/../../b", "~/x", "a\nb", "x" * 513, None, 3):
        assert clean_dir(bad) is None, bad


def test_put_keeps_the_path_of_a_declared_dir(mapi):
    """Dossier déclaré envoyé par une machine : son chemin (X-Wsq-Dir, encodé) est gardé et rendu
    par la liste, pour qu'une autre machine le déclare à la réception."""
    client, _settings, headers_a, _ = mapi
    name = "dossier:Zelda Ocarina Of Time"
    data = gz("zelda")
    assert put_tar(client, {**headers_a, "X-Wsq-Dir": ".local%2Fshare%2Fhookshot%2Fsave"}, data, game=name).status_code == 200
    listed = client.get("/v1/saves/list", headers=headers_a).json()["games"]
    assert listed[0]["dir"] == ".local/share/hookshot/save"
