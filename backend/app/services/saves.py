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

_GAME_RE = re.compile(r"^[^./\\\x00][^/\\\x00]{0,199}$")


def valid_game(name: object) -> bool:
    """Nom de jeu sûr pour un dossier : pas de « / », pas de « .. », pas de point initial."""
    return isinstance(name, str) and bool(_GAME_RE.match(name)) and ".." not in name


def clean_dir(value: object) -> str | None:
    """Chemin d'un dossier déclaré par un client (à partir de son HOME, ex. « .local/share/x »),
    relayé aux autres clients : rendu s'il est relatif, sans « .. » ni caractère de contrôle
    (512 caractères au plus), None sinon. Le lanceur le revérifie à la réception."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if value.startswith(("/", "~")) or any(ord(c) < 32 for c in value):
        return None
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
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
