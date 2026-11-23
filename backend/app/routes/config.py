"""Réglages communs du lanceur (page Configuration) : par machine (agent du client) et par
plateforme (dossiers de système de la ludothèque). Logique dans services/machine_config.py.

Chaque écriture accepte `?preview=1` : renvoie {before, after, diff} sans rien écrire.
Erreurs : 404 inconnu, 409 fichier changé, 422 invalide, 502 agent injoignable."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import SettingsDep
from ..services import clients as clients_svc
from ..services import machine_config as mc
from ..services.agent import AgentHost, AgentUnreachable, ConfigConflict
from ..services.errors import ManagerError

router = APIRouter()


class ConfigBody(BaseModel):
    """Fichier d'autorun : texte brut, ou valeurs du formulaire (clés du groupe) à appliquer."""
    text: str | None = None
    values: dict[str, str] | None = None
    base_sha: str | None = None         # sha lu à l'ouverture (None : pas de fichier)


class OptiBody(BaseModel):
    """Modèle partiel OptiScaler : texte brut, ou valeurs { section: { clé: valeur } } du formulaire."""
    text: str | None = None
    values: dict[str, dict[str, str]] | None = None
    base_sha: str | None = None


@contextmanager
def _erreurs() -> Iterator[None]:
    """Traduit les erreurs des services en codes HTTP."""
    try:
        yield
    except ConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ManagerError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _choix(text: str | None, values: object) -> None:
    if (text is None) == (values is None):
        raise HTTPException(status_code=422, detail="texte ou valeurs (un seul des deux)")


def _machine_row(settings, machine_id: str) -> dict:
    try:
        return clients_svc.find_machine(settings, machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"client inconnu : {machine_id}") from None


def _host(settings, machine_id: str) -> AgentHost:
    _machine_row(settings, machine_id)
    with _erreurs():
        return clients_svc.open_machine(settings, machine_id, timeout=30)


def _folder(settings, folder_id: str) -> dict:
    for folder in mc.platform_folders(settings):
        if folder["id"] == folder_id:
            return folder
    raise HTTPException(status_code=404, detail=f"dossier inconnu : {folder_id}")


def _reference_template(settings, machine_id: str | None = None) -> str:
    """Modèle OptiScaler de référence pour une plateforme (qui n'a pas d'agent) : celui de la
    première machine qui l'a installé. Une machine injoignable est ignorée."""
    ids = [machine_id] if machine_id else [row["id"] for row in clients_svc.machine_rows(settings)]
    for mid in ids:
        try:
            template = clients_svc.open_machine(settings, mid, timeout=8).config(timeout=8)["optiscaler"]["template"]
        except (ManagerError, KeyError, TypeError):
            continue
        if template:
            return template
    return ""


def _defaults_block(path: str, text: str, sha: str | None) -> dict:
    return {"path": path, "text": text, "sha": sha, "values": mc.defaults_values(text)}


def _preview(before: str, after: str) -> dict:
    return {"before": before, "after": after, "diff": mc.diff_text(before, after)}


# ------------------------------------------------------------------ clés, préréglages

@router.get("/api/config/keys")
def config_keys():
    """Groupes de clés du formulaire (aide, valeurs proposées), clés exclues, préréglages."""
    return mc.keys_payload()


# ------------------------------------------------------------------ machines

@router.get("/api/config/machines")
def config_machines(settings: SettingsDep):
    """Clients associés avec le GPU détecté et la version d'OptiScaler (un client injoignable
    garde sa ligne, avec `error`)."""
    rows = clients_svc.machine_rows(settings)

    def probe(row: dict) -> dict:
        out = {"id": row["id"], "name": row["name"], "kind": row["kind"], "gpu": None,
               "optiscaler": None, "error": ""}
        try:
            cfg = clients_svc.open_machine(settings, row["id"], timeout=8).config(timeout=8)
        except ManagerError as exc:
            out["error"] = str(exc)
            return out
        opti = cfg.get("optiscaler") or {}
        out["gpu"] = cfg.get("gpu")
        out["optiscaler"] = {"installed": bool(opti.get("installed")), "version": str(opti.get("version") or "")}
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        machines = list(pool.map(probe, rows))
    return {"machines": machines}


@router.get("/api/config/machines/{machine_id}")
def config_machine(settings: SettingsDep, machine_id: str):
    """Réglages d'une machine : défauts (fichier), modèle OptiScaler et son modèle partiel,
    variables WSQUASHFS_* de l'agent qui l'emportent sur le fichier."""
    row = _machine_row(settings, machine_id)
    with _erreurs():
        cfg = _host(settings, machine_id).config()
    opti = cfg.get("optiscaler") or {}
    overrides = opti.get("overrides") or {}
    template = opti.get("template") or ""
    sections = mc.parse_optiscaler_schema(template)
    env = cfg.get("env") or {}
    return {
        "machine": {"id": row["id"], "name": row["name"], "kind": row["kind"]},
        "gpu": cfg.get("gpu"),
        "defaults": _defaults_block(str((cfg.get("defaults") or {}).get("path") or ""),
                                    (cfg.get("defaults") or {}).get("text") or "",
                                    (cfg.get("defaults") or {}).get("sha")),
        "optiscaler": {"installed": bool(opti.get("installed")), "version": str(opti.get("version") or ""),
                       **mc.optiscaler_view(template, overrides.get("text") or "", overrides.get("sha"),
                                            str(overrides.get("path") or ""))},
        "presets": mc.presets_for(mc.model_values(sections)),
        "env": [{"key": key, "var": f"WSQUASHFS_{key}"} for _, keys in mc.GROUPS for key in keys
                if f"WSQUASHFS_{key}" in env],
        "warnings": {"lanceur": [], "optiscaler": [mc.WARN_OPTISCALER]},
    }


@router.put("/api/config/machines/{machine_id}/defaults")
def config_machine_defaults(settings: SettingsDep, machine_id: str, body: ConfigBody, preview: bool = False):
    """Écrit defaults.autorun du client (texte vide : fichier supprimé)."""
    _machine_row(settings, machine_id)
    _choix(body.text, body.values)
    with _erreurs():
        host = _host(settings, machine_id)
        block = host.config()["defaults"]
        mc.check_sha(block.get("sha"), body.base_sha)
        after = mc.compute_defaults(block.get("text") or "", body.text, body.values)
        if preview:
            return _preview(block.get("text") or "", after)
        return {"sha": host.config_defaults_put(after, body.base_sha).get("sha")}


@router.put("/api/config/machines/{machine_id}/optiscaler")
def config_machine_optiscaler(settings: SettingsDep, machine_id: str, body: OptiBody, preview: bool = False):
    """Écrit le modèle partiel OptiScaler du client (texte vide : fichier supprimé)."""
    _machine_row(settings, machine_id)
    _choix(body.text, body.values)
    with _erreurs():
        host = _host(settings, machine_id)
        opti = host.config().get("optiscaler") or {}
        overrides = opti.get("overrides") or {}
        mc.check_sha(overrides.get("sha"), body.base_sha)
        model = mc.model_values(mc.parse_optiscaler_schema(opti.get("template") or ""))
        current = overrides.get("text") or ""
        after = mc.compute_optiscaler(current, body.text, body.values, model)
        if preview:
            return _preview(current, after)
        return {"sha": host.config_optiscaler_put(after, body.base_sha).get("sha")}


# ------------------------------------------------------------------ plateformes (dossiers)

@router.get("/api/config/folders")
def config_folders(settings: SettingsDep):
    """Dossiers de système qui contiennent des images, avec leurs fichiers de réglages présents."""
    out = []
    for folder in mc.platform_folders(settings):
        base = Path(folder["path"])
        out.append({**folder, "defaults": (base / mc.DEFAULTS_FILE).is_file(),
                    "optiscaler": (base / mc.OPTI_FILE).is_file()})
    return {"folders": out}


@router.get("/api/config/folders/{folder_id}")
def config_folder(settings: SettingsDep, folder_id: str):
    """Réglages d'une plateforme ; le modèle OptiScaler vient d'une machine qui l'a installé."""
    folder = _folder(settings, folder_id)
    base = Path(folder["path"])
    with _erreurs():
        defaults_text, defaults_sha = mc.read_layer(base / mc.DEFAULTS_FILE)
        opti_text, opti_sha = mc.read_layer(base / mc.OPTI_FILE)
        template = _reference_template(settings)
    model = mc.model_values(mc.parse_optiscaler_schema(template))
    return {
        "folder": folder,
        "defaults": _defaults_block(str(base / mc.DEFAULTS_FILE), defaults_text, defaults_sha),
        "optiscaler": {"reference": bool(template),
                       **mc.optiscaler_view(template, opti_text, opti_sha, str(base / mc.OPTI_FILE))},
        "presets": mc.presets_for(model),
        "warnings": {"lanceur": [mc.WARN_PLATFORM], "optiscaler": [mc.WARN_PLATFORM, mc.WARN_OPTISCALER]},
    }


@router.put("/api/config/folders/{folder_id}/defaults")
def config_folder_defaults(settings: SettingsDep, folder_id: str, body: ConfigBody, preview: bool = False):
    """Écrit _defaults.autorun dans le dossier de plateforme (texte vide : fichier supprimé)."""
    path = Path(_folder(settings, folder_id)["path"]) / mc.DEFAULTS_FILE
    _choix(body.text, body.values)
    with _erreurs():
        current, sha = mc.read_layer(path)
        mc.check_sha(sha, body.base_sha)
        after = mc.compute_defaults(current, body.text, body.values)
        if preview:
            return _preview(current, after)
        return {"sha": mc.write_layer(path, after)}


@router.put("/api/config/folders/{folder_id}/optiscaler")
def config_folder_optiscaler(settings: SettingsDep, folder_id: str, body: OptiBody, preview: bool = False):
    """Écrit _optiscaler.ini dans le dossier de plateforme (texte vide : fichier supprimé)."""
    path = Path(_folder(settings, folder_id)["path"]) / mc.OPTI_FILE
    _choix(body.text, body.values)
    with _erreurs():
        current, sha = mc.read_layer(path)
        mc.check_sha(sha, body.base_sha)
        model = mc.model_values(mc.parse_optiscaler_schema(_reference_template(settings)))
        after = mc.compute_optiscaler(current, body.text, body.values, model)
        if preview:
            return _preview(current, after)
        return {"sha": mc.write_layer(path, after)}
