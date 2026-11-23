"""Réglages communs du lanceur : modèle OptiScaler, fichiers partiels, couche plateforme, routes.

Aucune ludothèque ni client réels : dossiers temporaires et faux agent en mémoire."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from app import deps, main
from app.services import clients as clients_svc
from app.services import machine_config as mc
from app.services import scan
from app.services.agent import AgentHost, ConfigConflict

# Extrait du modèle OptiScaler.ini (forme réelle des commentaires)
TEMPLATE = """\
[Upscalers]
; xess, fsr21, fsr22, fsr31 (also for FSR4), dlss - Default (auto) is xess
Dx12Upscaler=auto
; true or false - Default (auto) is false
Dx11Upscaler=false

[FrameGen]
; nofg, dlssg, nukems, fsrfg, upscaler, fsrfg30 - Default (auto) is nofg
FGInput=nofg
FGOutput=nofg

[FSR]
; true or false - Default (auto) is false
Fsr4Update=false

[Spoofing]
; Spoofing Dxgi : texte libre
Dxgi=auto
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ schéma

def test_schema_reads_sections_help_and_choices():
    sections = {s["name"]: s for s in mc.parse_optiscaler_schema(TEMPLATE)}
    assert list(sections) == ["Upscalers", "FrameGen", "FSR", "Spoofing"]
    dx12 = sections["Upscalers"]["keys"][0]
    assert dx12["key"] == "Dx12Upscaler" and dx12["default"] == "auto"
    assert dx12["choices"] == ["xess", "fsr21", "fsr22", "fsr31", "dlss", "auto"]
    assert "xess, fsr21" in dx12["help"]
    assert sections["Upscalers"]["keys"][1]["choices"] == ["true", "false", "auto"]
    assert sections["FrameGen"]["keys"][0]["choices"] == [
        "nofg", "dlssg", "nukems", "fsrfg", "upscaler", "fsrfg30", "auto"]
    assert sections["FSR"]["keys"][0]["key"] == "Fsr4Update"
    # Aide en texte libre : pas de liste de choix
    assert sections["Spoofing"]["keys"][0]["choices"] is None


def test_schema_help_stops_at_blank_line():
    text = "[S]\n; ancienne aide\n\n; aide\nCle=1\n"
    assert mc.parse_optiscaler_schema(text)[0]["keys"][0]["help"] == "aide"


def test_model_values_maps_section_key_to_default():
    model = mc.model_values(mc.parse_optiscaler_schema(TEMPLATE))
    assert model["FSR"] == {"Fsr4Update": "false"}
    assert model["Upscalers"]["Dx12Upscaler"] == "auto"


# ------------------------------------------------------------------ fichier partiel

def test_partial_keeps_only_what_differs_in_model_order():
    model = mc.model_values(mc.parse_optiscaler_schema(TEMPLATE))
    values = {
        "FSR": {"Fsr4Update": "true"},                        # changé
        "Upscalers": {"Dx12Upscaler": "fsr31", "Dx11Upscaler": "false"},   # un seul changé
        "FrameGen": {"FGInput": "nofg", "FGOutput": "nofg"},  # rien : section absente
        "Autre": {"Clé": "1"},                                # hors modèle : gardée
    }
    text = mc.render_partial_ini(values, model)
    assert text == "[Upscalers]\nDx12Upscaler=fsr31\n\n[FSR]\nFsr4Update=true\n\n[Autre]\nClé=1\n"


def test_partial_empty_when_nothing_differs():
    model = mc.model_values(mc.parse_optiscaler_schema(TEMPLATE))
    assert mc.render_partial_ini({"FSR": {"Fsr4Update": "false"}}, model) == ""


def test_partial_parse_roundtrip_and_errors():
    parsed = mc.parse_partial_ini("; note\n[Upscalers]\nDx12Upscaler=fsr31\n")
    assert parsed == {"Upscalers": {"Dx12Upscaler": "fsr31"}}
    with pytest.raises(ValueError, match="hors d'une section"):
        mc.parse_partial_ini("Dx12Upscaler=fsr31\n")
    with pytest.raises(ValueError, match="en double"):
        mc.parse_partial_ini("[A]\nk=1\nk=2\n")
    with pytest.raises(ValueError, match="attendu"):
        mc.parse_partial_ini("[A]\nsans égal\n")
    with pytest.raises(ValueError, match="NUL"):
        mc.parse_partial_ini("[A]\x00\n")


def test_merged_partial_applies_form_values_and_drops_model_defaults():
    model = mc.model_values(mc.parse_optiscaler_schema(TEMPLATE))
    current = "[Upscalers]\nDx12Upscaler=fsr31\n"
    # Le formulaire remet la valeur du modèle : la surcharge disparaît
    text = mc.merged_partial(current, {"Upscalers": {"Dx12Upscaler": "auto"}, "FSR": {"Fsr4Update": "true"}}, model)
    assert text == "[FSR]\nFsr4Update=true\n"
    with pytest.raises(ValueError):
        mc.merged_partial("", {"A": {"k": "a=b"}}, model)


# ------------------------------------------------------------------ autorun (défauts)

def test_defaults_validation_matches_agent_rules():
    mc.check_defaults("DXVK=1\r\n# commentaire\nREM autre\n\nENV=A=1\n")
    for bad, message in [("pas une clé\n", "CLÉ=valeur"), ("A\x00=1\n", "NUL"),
                         ("1A=2\n", "CLÉ=valeur"), ("x" * (64 * 1024 + 1), "64 Kio")]:
        with pytest.raises(ValueError, match=message):
            mc.check_defaults(bad)


def test_defaults_values_keep_comments_order_and_unlisted_keys():
    current = "# réglages de salon\r\nDXVK=1\r\nCMD=jeu.exe\r\nVIRTUAL_DESKTOP=1280x720\r\n"
    out = mc.apply_defaults_values(current, {"DXVK": "0", "VIRTUAL_DESKTOP": "", "FSYNC": "1"})
    assert out == "# réglages de salon\r\nDXVK=0\r\nCMD=jeu.exe\r\nFSYNC=1\r\n"
    assert mc.defaults_values(out) == {"DXVK": "0", "CMD": "jeu.exe", "FSYNC": "1"}


def test_defaults_values_refuse_bad_key():
    with pytest.raises(ValueError, match="clé invalide"):
        mc.apply_defaults_values("", {"A B": "1"})


def test_presets_keep_ini_only_when_the_model_has_the_key():
    model = mc.model_values(mc.parse_optiscaler_schema(TEMPLATE))
    amd = next(p for p in mc.presets_for(model) if p["id"] == "amd")
    assert amd["values"] == {"FSR4": "1"}
    assert {(i["section"], i["key"]) for i in amd["ini"]} == {("Upscalers", "Dx12Upscaler"), ("FSR", "Fsr4Update")}
    # Sans modèle (machine sans OptiScaler) : aucune clé ini proposée
    assert all(p["ini"] == [] for p in mc.presets_for(None))


def test_keys_payload_groups_exclude_per_image_keys():
    payload = mc.keys_payload()
    keys = [k["key"] for g in payload["groups"] for k in g["keys"]]
    assert "OPTISCALER" in keys and "FSR4" in keys and "ENV" in keys
    assert not set(keys) & set(payload["excluded"])
    assert {"CMD", "DIR", "GAME_VERSION", "SAVEDIR", "SAVEFILES", "PRE", "ATTACH", "ATTACH_PROCESS"} == set(payload["excluded"])


# ------------------------------------------------------------------ plateformes

def test_platform_folders_only_systems_with_images(tmp_path, monkeypatch):
    roms = tmp_path / "roms"
    (roms / "win").mkdir(parents=True)
    (roms / "win" / "jeu.wsquashfs").write_bytes(b"x")
    (roms / "win" / "_defaults.autorun").write_text("DXVK=1\n")
    (roms / "win" / "_optiscaler.ini").write_text("[FSR]\nFsr4Update=true\n")
    (roms / "vide").mkdir()
    (roms / "vide" / "_defaults.autorun").write_text("DXVK=1\n")
    monkeypatch.setattr(deps.settings, "roms_dirs", [str(roms)])
    folders = mc.platform_folders(deps.settings)
    assert [f["id"] for f in folders] == ["win"]
    assert folders[0]["path"] == str(roms / "win")
    # Les fichiers de réglages ne sont jamais pris pour des images
    assert scan._find_images([str(roms)], 3) == [(roms / "win" / "jeu.wsquashfs", "win")]


def test_layer_write_checks_sha_and_deletes_when_empty(tmp_path):
    path = tmp_path / "_defaults.autorun"
    assert mc.read_layer(path) == ("", None)
    assert mc.write_layer(path, "DXVK=1\n") == sha("DXVK=1\n")
    text, current = mc.read_layer(path)
    assert text == "DXVK=1\n" and current == sha("DXVK=1\n")
    mc.check_sha(current, current)
    with pytest.raises(ConfigConflict):
        mc.check_sha(sha("autre"), current)
    assert mc.write_layer(path, "  \n") is None and not path.exists()


def test_diff_is_empty_when_nothing_changes():
    assert mc.diff_text("A=1\n", "A=1\n") == ""
    assert "-A=1" in mc.diff_text("A=1\n", "A=2\n") and "+A=2" in mc.diff_text("A=1\n", "A=2\n")


# ------------------------------------------------------------------ routes plateforme

@pytest.fixture
def platform(tmp_path, monkeypatch):
    roms = tmp_path / "roms"
    (roms / "win").mkdir(parents=True)
    (roms / "win" / "jeu.wsquashfs").write_bytes(b"x")
    monkeypatch.setattr(deps.settings, "roms_dirs", [str(roms)])
    monkeypatch.setattr(deps.settings, "password", "")
    return TestClient(main.app, headers={"x-wsfs-token": ""}), roms / "win"


def test_platform_defaults_written_from_form_and_preview_writes_nothing(platform):
    client, folder = platform
    body = {"values": {"DXVK": "0", "NVAPI": "1"}, "base_sha": None}
    preview = client.put("/api/config/folders/win/defaults?preview=1", json=body).json()
    assert preview["before"] == "" and "NVAPI=1" in preview["after"] and "+DXVK=0" in preview["diff"]
    assert not (folder / "_defaults.autorun").exists()

    done = client.put("/api/config/folders/win/defaults", json=body)
    assert done.status_code == 200
    written = (folder / "_defaults.autorun").read_bytes().decode()     # CRLF, comme un autorun Windows
    assert written == preview["after"]
    assert done.json() == {"sha": sha(written)}

    detail = client.get("/api/config/folders/win").json()
    assert detail["defaults"]["values"] == {"DXVK": "0", "NVAPI": "1"}
    assert detail["defaults"]["sha"] == sha(written)
    assert detail["folder"]["id"] == "win"


def test_platform_conflict_when_file_changed_since_read(platform):
    client, folder = platform
    (folder / "_defaults.autorun").write_text("DXVK=1\n")
    stale = client.put("/api/config/folders/win/defaults", json={"values": {"DXVK": "0"}, "base_sha": sha("ancien\n")})
    assert stale.status_code == 409
    assert (folder / "_defaults.autorun").read_text() == "DXVK=1\n"


def test_platform_refuses_invalid_text_and_missing_body(platform):
    client, folder = platform
    bad = client.put("/api/config/folders/win/defaults", json={"text": "pas une clé\n", "base_sha": None})
    assert bad.status_code == 422 and "CLÉ=valeur" in bad.json()["detail"]
    both = client.put("/api/config/folders/win/defaults", json={"text": "A=1\n", "values": {}, "base_sha": None})
    assert both.status_code == 422
    assert not (folder / "_defaults.autorun").exists()


def test_unknown_platform_is_404_and_free_paths_are_never_used(platform):
    client, _ = platform
    assert client.get("/api/config/folders/../../etc").status_code == 404
    assert client.get("/api/config/folders/inconnu").status_code == 404
    assert client.put("/api/config/folders/inconnu/optiscaler", json={"text": "", "base_sha": None}).status_code == 404


def test_platform_optiscaler_partial_and_empty_deletes(platform, monkeypatch):
    client, folder = platform
    monkeypatch.setattr(clients_svc, "machine_rows", lambda settings: [])     # pas de modèle de référence
    values = {"FSR": {"Fsr4Update": "true"}}
    done = client.put("/api/config/folders/win/optiscaler", json={"values": values, "base_sha": None})
    assert done.status_code == 200
    assert (folder / "_optiscaler.ini").read_text() == "[FSR]\nFsr4Update=true\n"
    detail = client.get("/api/config/folders/win").json()
    assert detail["optiscaler"]["reference"] is False
    assert detail["optiscaler"]["sections"][0]["keys"][0]["override"] is True
    removed = client.put("/api/config/folders/win/optiscaler",
                         json={"text": "", "base_sha": done.json()["sha"]})
    assert removed.status_code == 200 and not (folder / "_optiscaler.ini").exists()


# ------------------------------------------------------------------ routes machine (faux agent)

class FakeHost:
    """Agent en mémoire : mêmes réponses que /v1/config et ses PUT (sha, conflit)."""

    def __init__(self):
        self.files: dict[str, str | None] = {}
        self.template = TEMPLATE
        self.env: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def config(self, timeout: float = 30) -> dict:
        d, o = self.files["defaults"], self.files["optiscaler"]
        return {
            "gpu": {"nvidia": False, "amd": True},
            "defaults": {"path": "~/.config/wsquashfs/defaults.autorun", "text": d or "",
                         "sha": sha(d) if d else None},
            "optiscaler": {"installed": True, "version": "0.7.9", "template": self.template,
                           "overrides": {"path": "~/.config/wsquashfs/optiscaler.ini", "text": o or "",
                                         "sha": sha(o) if o else None}},
            "env": dict(self.env),
        }

    def _put(self, key: str, text: str, base_sha: str | None) -> dict:
        current = self.files[key]
        if (sha(current) if current else None) != base_sha:
            raise ConfigConflict("fichier modifié entre-temps : recharger")
        self.writes.append((key, text))
        self.files[key] = text or None
        return {"sha": sha(text) if text else None}

    def config_defaults_put(self, text: str, base_sha: str | None) -> dict:
        return self._put("defaults", text, base_sha)

    def config_optiscaler_put(self, text: str, base_sha: str | None) -> dict:
        return self._put("optiscaler", text, base_sha)


@pytest.fixture
def machine(monkeypatch):
    fake = FakeHost()
    fake.files = {"defaults": "DXVK=1\n", "optiscaler": None}
    fake.env = {"WSQUASHFS_FSR4": "0"}
    rows = [{"id": "salon", "name": "Salon", "kind": "saved", "url": "https://x", "fingerprint": "", "removable": True, "icon": None}]
    monkeypatch.setattr(clients_svc, "machine_rows", lambda settings: rows)
    monkeypatch.setattr(clients_svc, "open_machine", lambda settings, machine_id, timeout=3600: fake)
    monkeypatch.setattr(deps.settings, "password", "")
    return fake, TestClient(main.app, headers={"x-wsfs-token": ""})


def test_machine_detail_shows_defaults_schema_and_env_warning(machine):
    _, client = machine
    body = client.get("/api/config/machines/salon").json()
    assert body["machine"]["name"] == "Salon" and body["gpu"]["amd"] is True
    assert body["defaults"]["values"] == {"DXVK": "1"}
    assert body["optiscaler"]["installed"] is True
    keys = {s["name"]: s["keys"] for s in body["optiscaler"]["sections"]}
    assert keys["Upscalers"][0]["choices"][-1] == "auto"
    assert body["env"] == [{"key": "FSR4", "var": "WSQUASHFS_FSR4"}]
    amd = next(p for p in body["presets"] if p["id"] == "amd")
    assert {"section": "FSR", "key": "Fsr4Update", "value": "true"} in amd["ini"]
    assert client.get("/api/config/machines/inconnu").status_code == 404


def test_machine_defaults_put_from_form_keeps_unlisted_lines(machine):
    fake, client = machine
    fake.files["defaults"] = "# salon\nDXVK=1\nCMD=jeu.exe\n"
    base = sha(fake.files["defaults"])
    preview = client.put("/api/config/machines/salon/defaults?preview=1",
                         json={"values": {"DXVK": "0", "NVAPI": "1"}, "base_sha": base}).json()
    assert preview["after"] == "# salon\nDXVK=0\nCMD=jeu.exe\nNVAPI=1\n"
    assert fake.writes == []                                              # aperçu : rien écrit
    done = client.put("/api/config/machines/salon/defaults",
                      json={"values": {"DXVK": "0", "NVAPI": "1"}, "base_sha": base})
    assert done.status_code == 200 and done.json()["sha"] == sha(fake.files["defaults"])
    assert "CMD=jeu.exe" in fake.files["defaults"] and fake.writes[-1][0] == "defaults"


def test_machine_put_conflict_is_409_and_nothing_written(machine):
    fake, client = machine
    response = client.put("/api/config/machines/salon/defaults", json={"text": "A=1\n", "base_sha": sha("autre\n")})
    assert response.status_code == 409
    assert fake.writes == []


def test_machine_optiscaler_put_writes_only_changes(machine):
    fake, client = machine
    values = {"Upscalers": {"Dx12Upscaler": "fsr31"}, "FSR": {"Fsr4Update": "false"}}
    done = client.put("/api/config/machines/salon/optiscaler", json={"values": values, "base_sha": None})
    assert done.status_code == 200
    assert fake.files["optiscaler"] == "[Upscalers]\nDx12Upscaler=fsr31\n"


def test_machine_optiscaler_invalid_text_is_422(machine):
    fake, client = machine
    response = client.put("/api/config/machines/salon/optiscaler", json={"text": "Dx12Upscaler=fsr31\n", "base_sha": None})
    assert response.status_code == 422 and "section" in response.json()["detail"]
    assert fake.writes == []


def test_machines_list_tolerates_unreachable_agent(machine, monkeypatch):
    from app.services.agent import AgentUnreachable

    def down(settings, machine_id, timeout=3600):
        raise AgentUnreachable("agent injoignable (test)")

    _, client = machine
    monkeypatch.setattr(clients_svc, "open_machine", down)
    row = client.get("/api/config/machines").json()["machines"][0]
    assert row["id"] == "salon" and row["optiscaler"] is None and "injoignable" in row["error"]


def test_old_agent_answers_404_and_gets_the_update_message():
    response = httpx.Response(404, text="404 page not found")
    error = AgentHost._config_error(object.__new__(AgentHost), response)
    assert str(error) == "lanceur trop ancien sur ce client : le mettre à jour"
