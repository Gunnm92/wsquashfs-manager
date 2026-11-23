"""Réglages communs du lanceur, par machine et par plateforme.

Ordre du lanceur, du plus faible au plus fort : défauts du lanceur, machine (fichier
`~/.config/wsquashfs/defaults.autorun` puis variables WSQUASHFS_* qui l'emportent), dossier
de plateforme (`<roms>/<système>/_defaults.autorun`), image, surcharge du jeu.

- Machine : lue et écrite par l'agent du client (services/agent.py, routes /v1/config*).
- Plateforme : fichiers `_defaults.autorun` et `_optiscaler.ini` écrits par le manager, à côté
  des images ; un jeu installé sur un client ne les voit pas.

Modèle OptiScaler : `optiscaler.ini` installé sur le client est le modèle complet ; le fichier
partiel (`[Section]` + `Clé=valeur` changées) n'en garde que ce qui diffère. Le lanceur l'applique
une fois, quand il copie l'OptiScaler.ini dans un jeu qui n'en a pas.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

from ..config import Settings
from . import scan
from .agent import ConfigConflict
from .autorun import KEY_HELP, KEY_NAME_RE, Autorun, decode_autorun
from .durable import fsync_dir, write_atomic

DEFAULTS_FILE = "_defaults.autorun"
OPTI_FILE = "_optiscaler.ini"
MAX_BYTES = 64 * 1024

# Clés montrées dans le formulaire, par groupe (aide et valeurs : services/autorun.KEY_HELP)
GROUPS: list[tuple[str, list[str]]] = [
    ("Rendu et GPU", ["DXVK", "VKD3D", "D7VK", "NVAPI", "VKREFLEX", "FSR", "FSR4", "OPTISCALER"]),
    ("Synchronisation", ["ESYNC", "FSYNC"]),
    ("Affichage", ["VIRTUAL_DESKTOP"]),
    ("Manettes", ["HIDRAW"]),
    ("Runner", ["WINE", "PROTON", "RUNNER", "ARCH", "LANG"]),
    ("Environnement", ["ENV"]),
]
# Clés propres à une image (chemins, commandes) : jamais dans les réglages communs
EXCLUDED = ["CMD", "DIR", "GAME_VERSION", "SAVEDIR", "SAVEFILES", "PRE", "ATTACH", "ATTACH_PROCESS"]
# Aide de repli quand KEY_HELP ne connaît pas la clé
# Préréglages : remplissent le formulaire sans rien enregistrer. `ini` : (section, clé, valeur),
# gardé seulement si la clé existe dans le modèle OptiScaler de la machine.
PRESETS: list[dict] = [
    {"id": "amd", "label": "AMD", "values": {"FSR4": "1"},
     "ini": [("Upscalers", "Dx12Upscaler", "fsr31"), ("FSR", "Fsr4Update", "true")]},
    {"id": "nvidia", "label": "NVIDIA", "values": {"NVAPI": "1"},
     "ini": [("Upscalers", "Dx12Upscaler", "dlss")]},
    {"id": "framegen", "label": "Génération d'images", "values": {},
     "ini": [("FrameGen", "FGInput", "fsrfg"), ("FrameGen", "FGOutput", "fsrfg")]},
]

WARN_OPTISCALER = "Le modèle OptiScaler ne s'applique qu'aux jeux qui n'ont pas encore leur OptiScaler.ini."
WARN_PLATFORM = ("Couche plateforme lue à côté de l'image : un jeu installé sur un client ne la voit pas.")

_SECTION_RE = re.compile(r"^\[([^\[\]=;]+)\]$")
_INI_KEY_RE = re.compile(r"^[^\[\]=;\s][^\[\]=;]*$")


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ clés et préréglages

def keys_payload() -> dict:
    """Groupes de clés (aide, valeurs proposées), clés exclues et préréglages (sans modèle)."""
    groups = []
    for title, keys in GROUPS:
        items = []
        for key in keys:
            help_text, choices = KEY_HELP.get(key, ("", []))
            items.append({"key": key, "help": help_text, "choices": list(choices)})
        groups.append({"title": title, "keys": items})
    return {"groups": groups, "excluded": list(EXCLUDED), "presets": presets_for(None)}


def presets_for(model: dict[str, dict[str, str]] | None) -> list[dict]:
    """Préréglages utilisables avec ce modèle OptiScaler : une clé ini n'est proposée que si elle
    existe dans le modèle (aucun modèle : pas de clé ini du tout)."""
    out = []
    for preset in PRESETS:
        ini = [{"section": s, "key": k, "value": v} for s, k, v in preset["ini"]
               if model is not None and k in model.get(s, {})]
        out.append({"id": preset["id"], "label": preset["label"], "values": dict(preset["values"]), "ini": ini})
    return out


# ------------------------------------------------------------------ lanceur (autorun)

def check_defaults(text: str) -> None:
    """Validation d'un defaults.autorun, comme l'agent : 64 Kio au plus, pas de NUL, lignes
    vides, commentaires (REM, #) ou CLÉ=valeur. ValueError avec un message français."""
    if "\0" in text:
        raise ValueError("caractère NUL dans le fichier")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("fichier trop grand (64 Kio au plus)")
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if not line.strip() or Autorun.is_comment(line):
            continue
        key, sep, _ = line.partition("=")
        if "\r" in line or not sep or not KEY_NAME_RE.match(key):
            raise ValueError(f"ligne {number} : CLÉ=valeur attendu (ex. DXVK=1)")


def defaults_values(text: str) -> dict[str, str]:
    """Première valeur de chaque clé du fichier (comme le lanceur)."""
    parsed = Autorun.parse(text)
    return {key: parsed.get(key) or "" for key, _, _ in parsed.items()}


def apply_defaults_values(current: str, values: dict[str, str]) -> str:
    """Modifie les clés données, les autres lignes (commentaires, clés hors formulaire) restent.
    Valeur vide : clé retirée."""
    out = Autorun.parse(current)
    for key, value in values.items():
        if not KEY_NAME_RE.match(key):
            raise ValueError(f"clé invalide : {key}")
        value = value.strip()
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"valeur de {key} sur plusieurs lignes")
        if value:
            out.set(key, value)
        else:
            out.remove(key)
    return out.render()


def compute_defaults(current: str, text: str | None, values: dict[str, str] | None) -> str:
    """Texte final d'un defaults.autorun : texte brut ou valeurs du formulaire appliquées au
    fichier actuel. ValueError si invalide."""
    if text is not None:
        check_defaults(text)
        return text
    result = apply_defaults_values(current, values or {})
    check_defaults(result)
    return result


# ------------------------------------------------------------------ OptiScaler

def parse_optiscaler_schema(template: str) -> list[dict]:
    """Sections du modèle complet : [{name, keys:[{key, default, help, choices}]}].

    `help` : les lignes de commentaire `;` qui précèdent la clé (sans ligne vide entre les deux).
    `choices` : valeurs possibles lues dans l'aide (« xess, fsr31 … - Default (auto) is xess »,
    « true or false - Default … »), avec « auto » ; None si l'aide ne les décrit pas clairement."""
    sections: list[dict] = []
    by_name: dict[str, dict] = {}
    section: dict | None = None
    comment: list[str] = []
    for raw in template.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            comment = []
            continue
        if line.startswith(";"):
            comment.append(line.lstrip(";").strip())
            continue
        if match := _SECTION_RE.match(line):
            name = match.group(1).strip()
            section = by_name.get(name)
            if section is None:
                section = {"name": name, "keys": []}
                by_name[name] = section
                sections.append(section)
            comment = []
            continue
        key, sep, value = line.partition("=")
        if not sep or section is None or not key.strip():
            comment = []
            continue
        key = key.strip()
        if not any(item["key"] == key for item in section["keys"]):
            help_text = " ".join(part for part in comment if part)
            section["keys"].append({"key": key, "default": value.strip(), "help": help_text,
                                    "choices": _choices(help_text)})
        comment = []
    return sections


def _choices(help_text: str) -> list[str] | None:
    """Valeurs possibles d'après « … - Default (auto) is … » ; None si une partie n'est pas une valeur."""
    match = re.search(r"^(.*?)\s+-\s+Default\b", help_text)
    if not match:
        return None
    body = re.sub(r"\([^)]*\)", "", match.group(1))
    tokens = [t.strip() for t in re.split(r",|\bor\b", body)]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2 or not all(re.fullmatch(r"[\w.+-]+", t) for t in tokens):
        return None
    choices = list(dict.fromkeys(tokens))
    if "auto" not in choices:
        choices.append("auto")
    return choices


def model_values(sections: list[dict]) -> dict[str, dict[str, str]]:
    """Modèle complet → {section: {clé: valeur du modèle}}."""
    return {s["name"]: {k["key"]: k["default"] for k in s["keys"]} for s in sections}


def parse_partial_ini(text: str) -> dict[str, dict[str, str]]:
    """Fichier partiel → {section: {clé: valeur}}, dans l'ordre du fichier. ValueError si invalide
    (64 Kio, NUL, clé hors section, clé en double, ligne qui n'est ni [Section] ni Clé=valeur)."""
    if "\0" in text:
        raise ValueError("caractère NUL dans le fichier")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("fichier trop grand (64 Kio au plus)")
    out: dict[str, dict[str, str]] = {}
    section: str | None = None
    for number, raw in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if match := _SECTION_RE.match(line):
            section = match.group(1).strip()
            out.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _INI_KEY_RE.match(key):
            raise ValueError(f"ligne {number} : [Section] ou Clé=valeur attendu")
        if section is None:
            raise ValueError(f"ligne {number} : clé {key} hors d'une section [Section]")
        if key in out[section]:
            raise ValueError(f"ligne {number} : clé {key} en double dans [{section}]")
        out[section][key] = value.strip()
    return out


def render_partial_ini(values: dict[str, dict[str, str]],
                       model: dict[str, dict[str, str]] | None = None) -> str:
    """Fichier partiel : seulement ce qui diffère du modèle, sections dans l'ordre du modèle.
    Rien à garder : texte vide (fichier supprimé)."""
    model = model or {}
    blocks = []
    for section in [*model, *(s for s in values if s not in model)]:
        lines = [f"{key}={value}" for key, value in values.get(section, {}).items()
                 if model.get(section, {}).get(key) != value]
        if lines:
            blocks.append("\n".join([f"[{section}]", *lines]))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def merged_partial(current: str, values: dict[str, dict[str, str]],
                   model: dict[str, dict[str, str]] | None) -> str:
    """Fichier partiel actuel, corrigé par les valeurs du formulaire (toutes les clés affichées),
    puis réduit à ce qui diffère du modèle. ValueError si un nom ou une valeur est impossible."""
    merged = parse_partial_ini(current) if current.strip() else {}
    for section, keys in values.items():
        if not _INI_KEY_RE.match(section):
            raise ValueError(f"nom de section impossible : {section}")
        for key, value in keys.items():
            if not _INI_KEY_RE.match(key) or any(c in value for c in "=\n\r\0"):
                raise ValueError(f"valeur impossible pour [{section}] {key}")
            merged.setdefault(section, {})[key] = value.strip()
    text = render_partial_ini(merged, model)
    if text:
        parse_partial_ini(text)         # relecture : ce qui sera écrit est un fichier valide
    return text


def compute_optiscaler(current: str, text: str | None, values: dict | None,
                       model: dict[str, dict[str, str]] | None) -> str:
    """Texte final du modèle partiel : texte brut validé, ou valeurs du formulaire fusionnées."""
    if text is not None:
        if text.strip():
            parse_partial_ini(text)
        return text
    return merged_partial(current, values or {}, model)


def optiscaler_view(template: str, text: str, sha: str | None, path: str) -> dict:
    """Modèle + modèle partiel pour la fiche : sections avec la valeur effective de chaque clé
    (surcharge ou modèle), `override` si la surcharge la change ; les clés de la surcharge absentes
    du modèle sont ajoutées (`extra`)."""
    sections = parse_optiscaler_schema(template)
    error = ""
    try:
        partial = parse_partial_ini(text) if text.strip() else {}
    except ValueError as exc:
        partial, error = {}, str(exc)
    for section in sections:
        overrides = partial.get(section["name"], {})
        for item in section["keys"]:
            item["override"] = item["key"] in overrides
            item["value"] = overrides.get(item["key"], item["default"])
    known = {(s["name"], k["key"]) for s in sections for k in s["keys"]}
    for name, keys in partial.items():
        for key, value in keys.items():
            if (name, key) in known:
                continue
            target = next((s for s in sections if s["name"] == name), None)
            if target is None:
                target = {"name": name, "keys": []}
                sections.append(target)
            target["keys"].append({"key": key, "default": "", "help": "", "choices": None,
                                   "override": True, "value": value, "extra": True})
    return {"sections": sections, "text": text, "sha": sha, "path": path, "error": error}


# ------------------------------------------------------------------ dossiers de plateforme

def platform_folders(settings: Settings) -> list[dict]:
    """Dossiers de système (roms/<système>/) qui contiennent au moins une image :
    [{id, system, path}], id = nom du système. Même déduction du système que la bibliothèque."""
    found: dict[str, Path] = {}
    for directory in settings.roms_dirs:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for image, system in scan._find_images([directory], settings.scan_max_depth):
            if not system or system in found:
                continue
            rel = image.relative_to(root)
            found[system] = root / system if len(rel.parts) > 1 else root
    return [{"id": system, "system": system, "path": str(path)} for system, path in sorted(found.items())]


def read_layer(path: Path) -> tuple[str, str | None]:
    """(texte, sha) d'un fichier de plateforme ; ("", None) s'il n'existe pas."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "", None
    return decode_autorun(data)[0], sha_of(data)


def check_sha(current_sha: str | None, base_sha: str | None) -> None:
    if current_sha != base_sha:
        raise ConfigConflict("fichier modifié entre-temps : recharger")


def write_layer(path: Path, text: str) -> str | None:
    """Écrit (ou supprime si le texte est vide) un fichier de plateforme → sha du nouveau contenu."""
    if not text.strip():
        path.unlink(missing_ok=True)
        fsync_dir(path.parent)
        return None
    write_atomic(path, text)
    return sha_of(text.encode("utf-8"))


def diff_text(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        "avant", "après"))
