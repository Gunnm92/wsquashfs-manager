"""Fiche d'une image : détail, fichiers, analyse et autorun, mise à jour de version,
anciennes versions (.old), sauvegardes de l'image, liens du prefix, runner, essai."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import library_index, task_submit
from ..config import WORK_DIR_NAME, Settings
from ..deps import ImageDep, QueueDep, SettingsDep
from ..models import ImageInfo
from ..services import analyze as analyze_mod
from ..services import scan
from ..services import trial as trial_svc
from ..services.analyze import analyze_image, report
from ..services.autorun import KEY_HELP, Autorun, decode_autorun, merge_override, override_path
from ..services.diskspace import existing_parent
from ..services.errors import ManagerError
from ..services.operations import autorun_sha, plan_autorun, write_override
from ..services.rebuild import restore_backup, validate_backup
from ..services.runners import plan_runner, suggest
from ..services.trial import game_host, host_name
from ..services.update import default_anchor, plan_update

router = APIRouter()


@router.get("/api/image")
def image_detail(info: ImageDep, queue: QueueDep):
    return library_index.with_task_state(queue, info)


@router.get("/api/image/files")
def image_files(info: ImageDep, settings: SettingsDep, q: str = "", limit: int = 2000):
    """Arborescence de l'image (lecture seule), avec recherche (casse ignorée)."""
    files = scan.list_files(settings, info.path)
    needle = q.lower()
    matches = [f for f in files if needle in f.lower()] if needle else files
    return {"total": len(files), "matches": len(matches), "files": matches[:limit]}


@router.get("/api/image/analyze")
def image_analyze(info: ImageDep, settings: SettingsDep, exe: str | None = None):
    """Proposition d'autorun pour une image existante (SPEC § 2.5), même
    moteur que pour les dossiers ; l'image n'est ni montée ni modifiée."""
    key = f"image:{info.path}"
    cached = analyze_mod.analyses.get(key)
    if cached and cached[0] == info.mtime:
        fa = cached[1]
    else:
        fa = analyze_image(info.path, info.name, settings.unsquashfs,
                           settings.tmp_dir or info.path.parent / WORK_DIR_NAME)
        if len(analyze_mod.analyses) >= analyze_mod.ANALYSES_MAX:
            analyze_mod.analyses.pop(next(iter(analyze_mod.analyses)))
        analyze_mod.analyses[key] = (info.mtime, fa)
    return {"id": info.id, **report(fa, exe)}


class UpdateRequest(BaseModel):
    source: str = Field(min_length=1)       # dossier du nouveau build ou du patch (serveur)
    anchor: str = ""                        # dossier de l'image où l'appliquer
    delete_missing: bool = False            # build complet : retirer ce qui n'y est plus
    version: str | None = None              # nouvelle GAME_VERSION (sinon proposée)
    clean_saves: bool = True                # mettre de côté les fichiers masquants
    when: Literal["now", "night"] = "now"

    @field_validator("source")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("chemin absolu attendu (dossier sur le serveur)")
        return value

    @field_validator("version")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\n" in value or "\r" in value:
            raise ValueError("version sur une seule ligne")
        return value.strip() or None


@router.get("/api/image/update")
def update_defaults(info: ImageDep, settings: SettingsDep):
    """Point d'ancrage proposé (DIR= de l'autorun) et version actuelle."""
    return {"anchor": default_anchor(settings, info.path), "version": info.version}


def _update_plan(settings: Settings, info: ImageInfo, body: UpdateRequest):
    try:
        return plan_update(settings, info.path, Path(body.source), body.anchor,
                           body.delete_missing, body.version)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/image/update/preview")
def update_preview(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: UpdateRequest):
    """Aperçu de la montée de version : ajoutés, remplacés, identiques,
    supprimés, fichiers masqués par les sauvegardes, version."""
    plan = _update_plan(settings, info, body)
    free = shutil.disk_usage(existing_parent(info.path.parent)).free
    return {**plan.summary(), "space": {"needed": info.size + plan.summary()["copy_size"],
                                        "free": free},
            "blocked": task_submit.blocked_reason(queue, info) if not plan.changes.is_empty() else None}


@router.post("/api/image/update")
def update_run(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: UpdateRequest):
    plan = _update_plan(settings, info, body)
    if plan.changes.is_empty():
        raise HTTPException(status_code=409, detail="rien à mettre à jour")
    params = {**body.model_dump(exclude={"when"}), "version": plan.version}
    title = f"montée de version{f' → {plan.version}' if plan.version else ''}"
    task = task_submit.submit(queue, info, "update", params, title, night=body.when == "night")
    return {"task": task.id, **plan.summary()}


@router.get("/api/autorun/keys")
def autorun_keys():
    return [{"key": key, "help": help_, "values": values}
            for key, (help_, values) in KEY_HELP.items()]


@router.get("/api/image/autorun")
def autorun_raw(info: ImageDep, settings: SettingsDep):
    """Lecture brute de l'autorun (sans monter : unsquashfs -cat)."""
    data = scan.read_autorun_bytes(settings, info.path)
    if data is None:
        override = scan.read_override(info.path)
        return {"found": False, "text": "", "lines": [], "eol": "\r\n", "trailing_eol": True,
                "encoding": "utf-8", "sha": None, "items": [], "unknown_keys": [],
                "effective": override.render() if override else "",
                "override": {"text": override.render(),
                             "keys": sorted({k for k, _, _ in override.items()})}
                if override is not None else None}
    text, encoding = decode_autorun(data)
    autorun = Autorun.parse(text)
    override = scan.read_override(info.path)
    effective = merge_override(autorun, override) if override is not None else autorun
    return {
        "found": True,
        "text": autorun.render(),
        # Autorun appliqué par le lanceur (image + surcharge <jeu>.wsquashfs.autorun)
        "effective": effective.render(),
        "override": {"text": override.render(), "keys": sorted({k for k, _, _ in override.items()})}
        if override is not None else None,
        "lines": autorun.lines,
        "eol": autorun.eol,
        "trailing_eol": autorun.trailing_eol,
        "encoding": encoding,
        "sha": autorun_sha(data),
        "items": [
            {"key": key, "value": value, "line": line} for key, value, line in autorun.items()
        ],
        "unknown_keys": autorun.unknown_keys(),
    }


class AutorunEdit(BaseModel):
    lines: list[str]
    eol: Literal["\r\n", "\n"] = "\r\n"
    trailing_eol: bool = True
    encoding: Literal["utf-8", "latin-1"] = "utf-8"
    base_sha: str | None = None     # empreinte lue à l'ouverture (édition concurrente)
    when: Literal["now", "night"] = "now"

    @field_validator("lines")
    @classmethod
    def _no_newline(cls, lines: list[str]) -> list[str]:
        if any("\n" in line or "\r" in line for line in lines):
            raise ValueError("une ligne ne peut pas contenir de saut de ligne")
        return lines

    def params(self) -> dict:
        text = Autorun(lines=self.lines, eol=self.eol, trailing_eol=self.trailing_eol).render()
        return {"text": text, "encoding": self.encoding, "base_sha": self.base_sha}


def _plan(settings: Settings, info: ImageInfo, params: dict, validate: bool = True):
    try:
        return plan_autorun(settings, info.path, info.name, info.system, params, validate)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/image/autorun/preview")
def autorun_preview(info: ImageDep, settings: SettingsDep, body: AutorunEdit):
    """Valide le contenu proposé sans écrire ni créer de tâche."""
    plan = _plan(settings, info, body.params())
    return {"text": plan.after_text, "changed": plan.changed, "issues": plan.issues}


@router.post("/api/image/autorun")
def autorun_edit(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: AutorunEdit):
    """Planifie la reconstruction de l'image avec le nouvel autorun."""
    params = body.params()
    has_override = override_path(info.path).exists()
    if has_override:
        params["drop_override"] = True      # l'autorun effectif est inscrit dans l'image
    plan = _plan(settings, info, params)
    if not plan.changed and not has_override:
        raise HTTPException(status_code=409, detail="aucun changement")
    if not plan.changed:
        # Surcharge sans effet : la supprimer suffit, pas de reconstruction
        override_path(info.path).unlink(missing_ok=True)
        return {"task": None, "issues": plan.issues, "override_removed": True}
    task = task_submit.submit(queue, info, "autorun", params, "autorun édité", night=body.when == "night")
    return {"task": task.id, "issues": plan.issues}


@router.put("/api/image/override")
def override_write(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: AutorunEdit):
    """« Appliquer tout de suite » : écrit dans <jeu>.wsquashfs.autorun la
    différence avec l'autorun de l'image, lue par le lanceur au prochain
    lancement — sans reconstruire l'image."""
    wanted = Autorun(lines=body.lines, eol=body.eol, trailing_eol=body.trailing_eol)
    keys = write_override(settings, info.path, wanted)
    return {"keys": keys, "image": library_index.fresh_image(settings, queue, info.id),
            "issues": wanted.validate(set(scan.list_files(settings, info.path)))}


@router.delete("/api/image/override")
def override_delete(info: ImageDep, settings: SettingsDep, queue: QueueDep):
    override_path(info.path).unlink(missing_ok=True)
    return library_index.fresh_image(settings, queue, info.id)


@router.post("/api/image/old/validate")
def old_validate(settings: SettingsDep, queue: QueueDep, id: str):
    """La nouvelle image convient : l'ancienne (.old) est supprimée."""
    return task_submit.old_action(settings, queue, id, validate_backup)


@router.post("/api/image/old/restore")
def old_restore(settings: SettingsDep, queue: QueueDep, id: str):
    """Revenir à l'ancienne image : la reconstruction est abandonnée."""
    return task_submit.old_action(settings, queue, id, restore_backup)


@router.post("/api/image/saves/reset")
def saves_reset(settings: SettingsDep, queue: QueueDep, id: str):
    """Sauvegardes renommées en <jeu>.avant-<date> (jamais supprimées)."""
    renamed = task_submit.reset_saves(settings, queue, id)
    return {"renamed": renamed, "image": library_index.fresh_image(settings, queue, id)}


class TrialRequest(BaseModel):
    duration: int = Field(default=60, ge=10, le=600)
    when: Literal["now", "night"] = "now"


@router.get("/api/image/links")
def image_links_report(info: ImageDep, settings: SettingsDep):
    """Liens du prefix vers un runner hors de l'image ou absent, et
    réparations proposées avec les runners installés sur le client d'essai."""
    steambox_error = None
    if settings.mode == "autonome":
        runners = []            # pas de machine en autonome : constat seul, sans proposition
        steambox_error = task_submit.MODE_AUTONOME_DETAIL
    else:
        try:
            runners = trial_svc.doctor(settings).get("runners", [])
        except ManagerError as exc:
            runners = []        # client d'essai désactivé ou injoignable : constat sans proposition
            steambox_error = str(exc)
    try:
        return {**suggest(settings, info.path, runners), "steambox_error": steambox_error}
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class RunnerRequest(BaseModel):
    source: str = Field(min_length=2, pattern=r"^/")    # dossier du runner sur le client d'essai
    sub: str = ""
    dest: str
    set_key: bool = False
    when: Literal["now", "night"] = "now"


@router.post("/api/image/runner")
def image_runner(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: RunnerRequest):
    """Joint un runner du client d'essai à l'image (ou complète un runner intégré),
    liens du prefix réécrits ; seulement depuis un runner installé (--doctor)."""
    try:
        installed = {r["path"].rstrip("/") for r in trial_svc.doctor(settings).get("runners", [])}
    except ManagerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if body.source.rstrip("/") not in installed:
        raise HTTPException(status_code=422, detail=f"runner inconnu de {host_name(settings)} (--doctor)")
    try:
        plan = plan_runner(settings, info.path, body.source, body.sub, body.dest, body.set_key)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    title = (f"runner : {plan.dest_prefix}" + (f", {plan.key}={plan.dest}" if plan.autorun else ""))
    task = task_submit.submit(queue, info, "runner", {"source": plan.source, "sub": plan.sub, "dest": plan.dest,
                                    "set_key": body.set_key}, title,
                   night=body.when == "night")
    return {"task": task.id, "plan": plan.to_dict()}


class RelinkRequest(BaseModel):
    when: Literal["now", "night"] = "now"


@router.post("/api/image/relink")
def image_relink(info: ImageDep, queue: QueueDep, body: RelinkRequest):
    """Réécrit en relatif les liens du prefix vers le runner déjà joint à l'image
    (reconstruction, original gardé en .old)."""
    task = task_submit.submit(queue, info, "relink", {}, "liens réécrits en relatif", night=body.when == "night")
    return {"task": task.id}


@router.post("/api/image/trial")
def image_trial(info: ImageDep, settings: SettingsDep, queue: QueueDep, body: TrialRequest):
    """Lancement d'essai chronométré sur le client d'essai (le jeu s'affiche
    sur l'écran de la session pendant la durée choisie, puis est arrêté)."""
    try:
        target = game_host(settings).path_of(info.path)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = task_submit.submit(queue, info, "trial", {"duration": body.duration, "target": target},
                   f"essai {body.duration} s", rebuild=False, night=body.when == "night")
    return {"task": task.id, "target": target}
