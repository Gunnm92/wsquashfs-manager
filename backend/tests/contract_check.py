"""Validation d'échanges HTTP contre contract/agent-api.yaml, le contrat commun de l'agent du
lanceur et du manager (OpenAPI 3.1, schémas JSON Schema 2020-12).

La copie du contrat est backend/contract/agent-api.yaml (`make contract` la recopie depuis le dépôt
du lanceur). Les fonctions rendent la liste des écarts trouvés (vide : conforme), pour que les
tests décident s'ils les affirment ou s'ils les accumulent (faux agents).
"""

from __future__ import annotations

import json
import threading
from functools import cache, lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract" / "agent-api.yaml"
RESOURCE = "urn:wsq:agent-api"


@lru_cache(maxsize=1)
def contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry() -> Registry:
    return Registry().with_resource(RESOURCE, Resource(contents=contract(), specification=DRAFT202012))


def pointer(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _lookup(ptr: str):
    node = contract()
    for seg in ptr.lstrip("/").split("/"):
        node = node[seg.replace("~1", "/").replace("~0", "~")]
    return node


def _resolve(node: dict, at: str) -> tuple[dict, str]:
    """Suit les $ref locaux (« #/components/... ») ; rend l'objet visé et son pointeur."""
    while "$ref" in node:
        at = node["$ref"].lstrip("#")
        node = _lookup(at)
    return node, at


@cache
def _validator(ptr: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"{RESOURCE}#{ptr}"}, registry=_registry())


def _errors(ptr: str, instance, what: str) -> list[str]:
    return [f"{what} : {'/'.join(map(str, e.absolute_path)) or '(racine)'} : {e.message}"
            for e in sorted(_validator(ptr).iter_errors(instance), key=lambda e: list(map(str, e.path)))]


def routes() -> list[tuple[str, str, dict]]:
    """(méthode, chemin, opération) de chaque route du contrat."""
    return [(method.upper(), path, op) for path, item in contract()["paths"].items()
            for method, op in item.items()]


def operation(method: str, path: str) -> dict:
    return contract()["paths"][path][method.lower()]


def schema_pointer(name: str) -> str:
    return f"/components/schemas/{name}"


def check_schema(name: str, instance) -> list[str]:
    """Écarts d'un document avec un schéma nommé de components/schemas."""
    return _errors(schema_pointer(name), instance, f"schéma {name}")


def check_response(method: str, target: str, status: int, headers, body: bytes, pair: bool = False) -> list[str]:
    """Écarts d'une réponse avec le contrat : code déclaré, type de contenu déclaré, en-têtes
    obligatoires, corps JSON conforme. `pair` : réponse du manager (x-pair-schema s'il existe).
    Un 404 ou 405 en texte brut (route inconnue du routeur Go) est admis."""
    method = method.upper()
    path = urlsplit(target).path
    what = f"{method} {path} -> {status}"
    try:
        op = operation(method, path)
    except KeyError:
        return [f"{what} : route absente du contrat"]
    ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if status in (404, 405) and ctype == "text/plain":
        return []
    responses = op["responses"]
    node = responses.get(str(status), responses.get("default"))
    if node is None:
        return [f"{what} : code absent du contrat"]
    at = f"/paths/{pointer(path)}/{method.lower()}/responses/{status}"
    resp, at = _resolve(node, at)
    problems = []
    for name, spec in (resp.get("headers") or {}).items():
        if spec.get("required") and not headers.get(name):
            problems.append(f"{what} : en-tête {name} absent")
    content = resp.get("content") or {}
    if not content:
        return problems
    media = content.get(ctype)
    if media is None:
        return [*problems, f"{what} : type de contenu {ctype!r} absent du contrat"]
    if ctype != "application/json":
        return problems
    key = "x-pair-schema" if pair and "x-pair-schema" in media else "schema"
    try:
        instance = json.loads(body)
    except ValueError:
        return [*problems, f"{what} : JSON illisible : {body[:200]!r}"]
    return [*problems, *_errors(f"{at}/content/{pointer(ctype)}/{key}", instance, what)]


def check_request(method: str, target: str, headers, body: bytes = b"") -> list[str]:
    """Écarts d'une requête avec le contrat : route connue, paramètres de requête déclarés (et les
    obligatoires présents), en-têtes déclarés conformes à leur schéma, corps JSON conforme."""
    method = method.upper()
    split = urlsplit(target)
    path = split.path
    what = f"requête {method} {path}"
    try:
        op = operation(method, path)
    except KeyError:
        return [f"{what} : route absente du contrat"]
    query = parse_qs(split.query, keep_blank_values=True)
    low = {k.lower(): v for k, v in headers.items()}
    problems: list[str] = []
    declared = set()
    for i, param in enumerate(op.get("parameters", [])):
        spec, at = _resolve(param, f"/paths/{pointer(path)}/{method.lower()}/parameters/{i}")
        name, where = spec["name"], spec["in"]
        if where == "query":
            declared.add(name)
            present = name in query
            value = query[name][0] if present else ""
        else:
            present = name.lower() in low
            value = low.get(name.lower(), "")
        if not present:
            if spec.get("required"):
                problems.append(f"{what} : {where} {name} obligatoire absent")
            continue
        problems += _errors(f"{at}/schema", value, f"{what} : {where} {name}")
    problems += [f"{what} : paramètre de requête {name!r} absent du contrat" for name in query
                 if name not in declared]
    rb = op.get("requestBody")
    if rb is None:
        return [*problems, f"{what} : corps envoyé, le contrat n'en décrit pas"] if body else problems
    if "application/json" in rb.get("content", {}):
        try:
            instance = json.loads(body)
        except ValueError:
            return [*problems, f"{what} : corps JSON attendu : {body[:100]!r}"]
        problems += _errors(f"/paths/{pointer(path)}/{method.lower()}/requestBody/content/application~1json/schema",
                            instance, f"{what} : corps")
    return problems


# ------------------------------------------------------------------ faux agents des tests

_lock = threading.Lock()
VIOLATIONS: list[str] = []


def record(problems: list[str]) -> None:
    """Garde les écarts d'un échange d'un faux agent (assertion à l'arrêt du faux agent)."""
    if problems:
        with _lock:
            VIOLATIONS.extend(problems)


def take_violations() -> list[str]:
    with _lock:
        found = list(dict.fromkeys(VIOLATIONS))
        VIOLATIONS.clear()
    return found


def sample_info(**override) -> dict:
    """Réponse valide de GET /v1/info d'un agent (version courante) pour les faux agents des tests."""
    info = {"version": "11", "host": "fake", "user": "arcade", "launcher": "/usr/bin/wsquashfs-launcher",
            "roms": ["/home/arcade/roms"], "session_process": "emulationstation", "os": "linux",
            "games_dir": "/home/arcade/Games", "games_dir_set": False, "manager": "", "manager_ui": "",
            "auto": False, "build": "", "playing": [], "controllers": []}
    info.update(override)
    return info

