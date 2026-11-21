"""Noms de jeux et dossiers des sauvegardes (SPEC « Manager de sauvegardes »).

Le manager de sauvegardes est réparti en modules :
- clients.py : registre des clients et déclaration du manager ;
- saves_store.py : la réserve du manager (archives, conflits, états) ;
- saves_sync.py : pull, push, suppression, décision, synchro automatique ;
- saves_status.py : l'état exposé (par client et par jeu) ;
- saves_actions.py : départage des conflits, envois automatiques, tâche « saves ».

Ce module ne contient que ce qui est commun : validité des noms de jeux et des
chemins de dossiers déclarés, préfixes des genres de sauvegardes.
"""

from __future__ import annotations

import re

from .errors import ManagerError

_CONTROL_OR_SEPARATOR = re.compile(r"[\x00-\x1f/\\]")
_GAME_SUFFIXES = (".wsquashfs", ".pc")     # nom d'image ou de dossier, jamais un nom de jeu
GAME_MAX = 200                             # caractères
DIR_MAX = 512                              # caractères


def valid_game(name: object) -> bool:
    """Nom de jeu sûr (règle commune avec le lanceur, contract/cases/names.yaml) : non vide, 200
    caractères au plus, ni « / », ni « \\ », ni caractère de contrôle, pas de « . » initial, pas de
    « .. », et pas de suffixe .wsquashfs ni .pc (casse ignorée)."""
    if not isinstance(name, str) or not name or len(name) > GAME_MAX:
        return False
    if _CONTROL_OR_SEPARATOR.search(name) or name.startswith(".") or ".." in name:
        return False
    return not name.lower().endswith(_GAME_SUFFIXES)


def clean_dir(value: object) -> str | None:
    """Chemin d'un dossier déclaré par un client (à partir de son HOME, ex. « .local/share/x »),
    relayé aux autres clients : rendu s'il est relatif, non vide, 512 caractères au plus, sans
    caractère de contrôle, sans espace au bord, sans « .. » (« / » ou « \\ » séparateurs), et pas
    seulement « . » (la racine HOME ne se déclare pas) ; None sinon. Le lanceur le revérifie à la
    réception (checkDeclaredRel), avec la même règle."""
    if not isinstance(value, str) or not value or len(value) > DIR_MAX:
        return None
    if value != value.strip() or value.startswith(("/", "~")) or any(ord(c) < 32 for c in value):
        return None
    parts = value.replace("\\", "/").split("/")
    if any(part == ".." for part in parts) or not any(part not in ("", ".") for part in parts):
        return None
    return value


def check_game(name: object) -> str:
    if not valid_game(name):
        raise ManagerError(f"nom de jeu invalide : {name!r}")
    return str(name)


def game_name(filename: str) -> str:
    """Nom de jeu = nom de fichier de l'image sans .wsquashfs ni .pc (casse ignorée),
    comme `image.stem` dans le calcul de la taille des sauvegardes (scan)."""
    name = filename
    for suffix in (".wsquashfs", ".pc"):
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
    return name


def dedupe(names) -> list[str]:
    return list(dict.fromkeys(names))


DIR_PREFIX = "dossier:"         # dossier déclaré sur les clients (agent v8)
SWITCH_PREFIX = "switch:"       # sauvegarde Eden d'un jeu Switch, par TitleID (agent v8)
