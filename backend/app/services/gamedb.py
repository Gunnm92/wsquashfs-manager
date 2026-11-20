"""Base de connaissances des jeux (`rules/games.yaml` livrée, `games.yaml` locale de
l'appdata prioritaire) : lecture, ajout depuis l'interface et recherche de l'entrée d'un jeu."""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .durable import write_atomic

if TYPE_CHECKING:
    from .analyze import FolderScan

GAMES_FILE = Path(__file__).resolve().parent.parent.parent / "rules" / "games.yaml"
_HASH_LIMIT = 300 * 2**20


# sur la base livrée avec l'application ; définie au démarrage par main.
LOCAL_GAMES_FILE: Path | None = None


def validate_game(game: dict, where: str = "") -> None:
    if not (game.get("exe") or game.get("sha256") or game.get("game")):
        raise ValueError(f"{where}exe, sha256 ou game requis")
    if not (game.get("keys") or game.get("requires") or game.get("prefer")):
        raise ValueError(f"{where}keys, requires ou prefer requis")


def _load_file(path: Path, source: str) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    games = list(data.get("games", []))
    for i, game in enumerate(games):
        validate_game(game, f"{path.name}, entrée {i} : ")
        game["_source"] = source
    return games


def load_games(path: Path | None = None) -> list[dict]:
    """Entrées de la base : locale d'abord (prioritaire), puis livrée."""
    if path is not None:                      # fichier explicite (tests)
        return _load_file(path, "livrée")
    local = _load_file(LOCAL_GAMES_FILE, "locale") if LOCAL_GAMES_FILE else []
    return local + _load_file(GAMES_FILE, "livrée")


def add_local_game(entry: dict) -> dict:
    """Ajoute une entrée à la base locale (« Ajouter à la base »)."""
    if LOCAL_GAMES_FILE is None:
        raise ValueError("base locale non configurée")
    entry = {k: v for k, v in entry.items() if v not in (None, "", [], {})}
    validate_game(entry)
    data: dict = {"games": []}
    if LOCAL_GAMES_FILE.exists():
        data = yaml.safe_load(LOCAL_GAMES_FILE.read_text()) or {"games": []}
    data.setdefault("games", []).append(entry)
    LOCAL_GAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(LOCAL_GAMES_FILE,
                 "# Base de connaissances locale (ajouts depuis wsquashfs-manager),\n"
                 "# prioritaire sur rules/games.yaml. Même format.\n"
                 + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return entry


def game_matches(game: dict, name: str) -> bool:
    pattern = game.get("game")
    return bool(pattern) and fnmatch.fnmatchcase(name.lower(), str(pattern).lower())


def missing_requirements(files: list[str], exe: str, game: dict) -> list[str]:
    """Fichiers exigés par la base, absents du dossier de l'exécutable (casse ignorée)."""
    directory = exe.rpartition("/")[0]
    present = {f.lower() for f in files}
    return [name for name in game.get("requires") or []
            if f"{directory}/{name}".lstrip("/").lower() not in present]


def match_game(games: list[dict], scan: FolderScan, exe: str,
               game_name: str | None = None) -> dict | None:
    """Entrée de la base pour ce jeu : empreinte de l'exécutable d'abord, puis
    nom de l'exécutable, puis nom du jeu."""
    name = exe.rsplit("/", 1)[-1].lower()
    by_name = [g for g in games if (g.get("exe") or "").lower() == name]
    with_hash = [g for g in games if g.get("sha256")]
    if with_hash:
        data = (scan.source.read_bytes(exe)
                if scan.sizes.get(exe, _HASH_LIMIT + 1) <= _HASH_LIMIT else None)
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        exact = next((g for g in with_hash if g["sha256"].lower() == digest), None)
        if exact:
            return exact
    if by_name:
        return by_name[0]
    if game_name:
        return next((g for g in games if game_matches(g, game_name)), None)
    return None
