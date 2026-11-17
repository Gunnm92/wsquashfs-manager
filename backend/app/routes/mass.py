"""Actions en masse sur plusieurs images : autorun, fichiers, vérification, illustration,
sauvegardes, inscription des surcharges, validation des .old."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import deps
from ..services import art_choice, scan
from ..services.artwork import embedded_cover
from ..services.autorun import KEY_NAME_RE, apply_ops, decode_autorun, override_path
from ..services.operations import (
    commit_params,
    effective_autorun,
    plan_autorun,
    plan_files,
    write_override,
)
from ..services.rebuild import RebuildError, existing_parent, validate_backup

router = APIRouter()


class AutorunOp(BaseModel):
    op: Literal["set", "remove", "replace"]
    key: str | None = None
    value: str | None = None
    template: str | None = None

    @model_validator(mode="after")
    def _check(self) -> AutorunOp:
        if self.op in ("set", "remove") and not (self.key and KEY_NAME_RE.match(self.key)):
            raise ValueError(f"nom de clé invalide : {self.key!r}")
        if self.op == "set" and (self.value is None or "\n" in self.value or "\r" in self.value):
            raise ValueError("valeur absente ou sur plusieurs lignes")
        if self.op == "replace" and not (self.template or "").strip():
            raise ValueError("modèle vide")
        return self


class MassAutorun(BaseModel):
    ids: list[str] = Field(min_length=1)
    ops: list[AutorunOp] = Field(min_length=1)
    when: Literal["now", "night"] = "now"
    # image : reconstruction ; override : surcharge, effet immédiat
    target: Literal["image", "override"] = "image"

    def params(self) -> dict:
        return {"ops": [op.model_dump(exclude_none=True) for op in self.ops]}

    def title(self) -> str:
        parts = []
        for op in self.ops:
            if op.op == "set":
                parts.append(f"{op.key}={op.value}")
            elif op.op == "remove":
                parts.append(f"−{op.key}")
            else:
                parts.append("modèle")
        return ", ".join(parts)


@router.post("/api/mass/autorun/preview")
def mass_autorun_preview(body: MassAutorun):
    """Aperçu image par image, et espace disque nécessaire (chaque image
    reconstruite garde son .old jusqu'à validation)."""
    items: list[dict] = []
    needs: dict[str, dict] = {}
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
        except HTTPException:
            items.append({"id": image_id, "error": "image inconnue"})
            continue
        if body.target == "override":
            # Surcharge : comparée à l'autorun effectif, rien n'est reconstruit
            current = effective_autorun(deps._settings, info.path)
            wanted = apply_ops(current, body.params()["ops"], info.name, info.system)
            changed = current is None or wanted.render() != current.render()
            items.append({"id": image_id, "name": info.name,
                          "before": current.render() if current else None,
                          "after": wanted.render(), "changed": changed,
                          "issues": wanted.validate(set(scan._list_files(deps._settings, info.path))),
                          "blocked": None})
            continue
        try:
            plan = plan_autorun(deps._settings, info.path, info.name, info.system, body.params())
        except RebuildError as exc:
            items.append({"id": image_id, "name": info.name, "error": str(exc)})
            continue
        before = decode_autorun(plan.before)[0] if plan.before is not None else None
        blocked = deps._blocked_reason(info) if plan.changed else None
        items.append({"id": image_id, "name": info.name, "before": before,
                      "after": plan.after_text, "changed": plan.changed,
                      "issues": plan.issues, "blocked": blocked})
        if plan.changed and not blocked:
            fs = str(existing_parent(info.path.parent))
            entry = needs.setdefault(fs, {"path": fs, "needed": 0,
                                          "free": shutil.disk_usage(fs).free})
            entry["needed"] += info.size
    return {"items": items, "space": list(needs.values()),
            "ready": sum(1 for i in items if i.get("changed") and not i.get("blocked")
                         and not i.get("error"))}


@router.post("/api/mass/autorun")
def mass_autorun(body: MassAutorun):
    """Crée une tâche par image modifiée ; les autres sont listées avec la raison.
    En surcharge (target=override), écrit tout de suite les surcharges, sans tâche."""
    created, skipped = [], []
    params, title = body.params(), body.title()
    if body.target == "override":
        written = []
        for image_id in body.ids:
            try:
                info = deps._find_image(image_id)
            except HTTPException as exc:
                skipped.append({"id": image_id, "reason": exc.detail})
                continue
            current = effective_autorun(deps._settings, info.path)
            wanted = apply_ops(current, params["ops"], info.name, info.system)
            if current is not None and wanted.render() == current.render():
                skipped.append({"id": image_id, "reason": "aucun changement"})
                continue
            write_override(deps._settings, info.path, wanted)
            written.append(image_id)
        deps._refresh_library(force=False)
        return {"tasks": [], "overrides": written, "skipped": skipped}
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
            plan = plan_autorun(deps._settings, info.path, info.name, info.system, params,
                                validate=False)
        except (HTTPException, RebuildError) as exc:
            skipped.append({"id": image_id, "reason": getattr(exc, "detail", str(exc))})
            continue
        has_override = override_path(info.path).exists()
        if not plan.changed and not has_override:
            skipped.append({"id": image_id, "reason": "aucun changement"})
            continue
        task_params = {**params, "drop_override": True} if has_override else params
        try:
            created.append(deps._submit(info, "autorun", task_params, title,
                                   night=body.when == "night").id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


class MassIds(BaseModel):
    ids: list[str] = Field(min_length=1)
    when: Literal["now", "night"] = "now"


class FileCopy(BaseModel):
    dest: str = Field(min_length=1)       # chemin dans l'image ({exe_dir}, {name}…)
    source: str = Field(min_length=1)     # fichier sur le serveur

    @field_validator("source")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("chemin source absolu attendu (fichier sur le serveur)")
        return value


class RegValue(BaseModel):
    file: Literal["user.reg", "system.reg"] = "user.reg"
    # ex. Software\\Wine\\AppDefaults\\{exe_name}\\DllOverrides
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: str

    @field_validator("key", "name", "value")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("une seule ligne attendue")
        return value


class MassFiles(BaseModel):
    ids: list[str] = Field(min_length=1)
    copy_: list[FileCopy] = Field(default_factory=list, alias="copy")
    delete: list[str] = Field(default_factory=list)
    reg: list[RegValue] = Field(default_factory=list)
    when: Literal["now", "night"] = "now"

    @model_validator(mode="after")
    def _not_empty(self) -> MassFiles:
        if not (self.copy_ or self.delete or self.reg):
            raise ValueError("aucune opération")
        return self

    def params(self) -> dict:
        return {"copy": [c.model_dump() for c in self.copy_], "delete": self.delete,
                "reg": [r.model_dump() for r in self.reg]}

    def title(self) -> str:
        parts = [f"+{Path(c.dest).name}" for c in self.copy_]
        parts += [f"−{Path(d).name}" for d in self.delete]
        parts += [f"reg {r.name}={r.value}" for r in self.reg]
        return ", ".join(parts)


@router.post("/api/mass/files/preview")
def mass_files_preview(body: MassFiles):
    """Aperçu : fichiers ajoutés/remplacés/supprimés et valeurs de registre,
    image par image, avec les sauvegardes qui masqueraient un changement."""
    items: list[dict] = []
    needs: dict[str, dict] = {}
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
        except HTTPException:
            items.append({"id": image_id, "error": "image inconnue"})
            continue
        try:
            plan = plan_files(deps._settings, info.path, info.name, info.system, body.params())
        except RebuildError as exc:
            items.append({"id": image_id, "name": info.name, "error": str(exc)})
            continue
        rebuilds = not plan.changes.is_empty()
        blocked = deps._blocked_reason(info, rebuilds) if plan.changed else None
        items.append({"id": image_id, "name": info.name, "changed": plan.changed,
                      "actions": plan.actions, "warnings": plan.warnings, "blocked": blocked})
        if rebuilds and not blocked:
            fs = str(existing_parent(info.path.parent))
            entry = needs.setdefault(fs, {"path": fs, "needed": 0,
                                          "free": shutil.disk_usage(fs).free})
            entry["needed"] += info.size
    return {"items": items, "space": list(needs.values()),
            "ready": sum(1 for i in items if i.get("changed") and not i.get("blocked")
                         and not i.get("error"))}


@router.post("/api/mass/files")
def mass_files(body: MassFiles):
    created, skipped = [], []
    params, title = body.params(), body.title()
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
            plan = plan_files(deps._settings, info.path, info.name, info.system, params)
        except (HTTPException, RebuildError) as exc:
            skipped.append({"id": image_id, "reason": getattr(exc, "detail", str(exc))})
            continue
        if not plan.changed:
            skipped.append({"id": image_id, "reason": "aucun changement"})
            continue
        try:
            created.append(deps._submit(info, "files", params, title,
                                   rebuild=not plan.changes.is_empty(),
                                   night=body.when == "night").id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


@router.post("/api/mass/verify")
def mass_verify(body: MassIds):
    """Vérification d'intégrité (lecture seule), une tâche par image."""
    created, skipped = [], []
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
            created.append(deps._submit(info, "verify", {}, "vérification", rebuild=False,
                                   night=body.when == "night").id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


class MassCover(MassIds):
    # replace : remplace l'illustration intégrée par reconstruction (ancienne en .old)
    replace: bool = False
    # choices : {id d'image: clé du candidat de l'aperçu} (GET /api/image/art/candidates)
    choices: dict[str, str] = Field(default_factory=dict)

    @field_validator("choices")
    @classmethod
    def _cles(cls, value: dict[str, str]) -> dict[str, str]:
        if not all(art_choice.cle_valide(key) for key in value.values()):
            raise ValueError("clé de candidat invalide (sgdb:<id>, es ou icon)")
        return value


REFUS_REMPLACEMENT = "choisir l'illustration dans l'aperçu avant de remplacer"


@router.post("/api/mass/cover")
def mass_cover(body: MassCover):
    """Illustration ajoutée aux images qui n'en ont pas, sans reconstruction
    (mksquashfs en ajout), une tâche par image. Avec `replace`, l'illustration
    déjà intégrée est remplacée par reconstruction (ancienne image gardée en .old) ;
    ce remplacement exige un choix (`choices`) pour chaque image déjà illustrée : sans
    choix, l'image est refusée dans `skipped`. Un choix porte exactement le candidat choisi."""
    created, skipped = [], []
    title = "Remplacement de l'illustration" if body.replace else "illustration"
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
            choice = body.choices.get(image_id)
            if body.replace and choice is None and embedded_cover(deps._settings, info.path) is not None:
                skipped.append({"id": image_id, "reason": REFUS_REMPLACEMENT})
                continue
            params: dict = {"replace": body.replace}
            if choice is not None:
                params["choice"] = choice
            created.append(deps._submit(info, "cover", params, title,
                                   rebuild=body.replace, night=body.when == "night").id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


@router.post("/api/mass/saves/reset")
def mass_saves_reset(body: MassIds):
    done, skipped = [], []
    for image_id in body.ids:
        try:
            done.append({"id": image_id, "renamed": deps._reset_saves(image_id)})
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"done": done, "skipped": skipped}


@router.post("/api/mass/override/commit")
def mass_override_commit(body: MassIds):
    """« Inscrire dans l'image » : reconstruit chaque image avec son autorun
    effectif, puis supprime sa surcharge."""
    created, skipped = [], []
    for image_id in body.ids:
        try:
            info = deps._find_image(image_id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
            continue
        params = commit_params(deps._settings, info.path)
        if params is None:
            skipped.append({"id": image_id, "reason": "pas de surcharge"})
            continue
        try:
            created.append(deps._submit(info, "autorun", params, "surcharge inscrite dans l'image",
                                   night=body.when == "night").id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"tasks": created, "skipped": skipped}


@router.post("/api/mass/old/validate")
def mass_old_validate(body: MassIds):
    done, skipped = [], []
    for image_id in body.ids:
        try:
            deps._old_action(image_id, validate_backup)
            done.append(image_id)
        except HTTPException as exc:
            skipped.append({"id": image_id, "reason": exc.detail})
    return {"done": done, "skipped": skipped}
