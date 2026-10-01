"""Configuration du manager.

Valeurs par défaut alignées sur les conventions de wsquashfs-launcher
(WSQUASHFS_SAVES_DIR, etc.).
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WSQUASHFS_MGR_", env_file=".env")

    # Dossiers de jeux à scanner : liste de chemins vers des dossiers roms/
    # (variable d'env : liste séparée par des virgules)
    roms_dirs: Annotated[list[str], NoDecode] = []

    @field_validator("roms_dirs", mode="before")
    @classmethod
    def _split_roms_dirs(cls, value):
        if isinstance(value, str):
            return [path.strip() for path in value.split(",") if path.strip()]
        return value

    # Sauvegardes (même convention que wsquashfs-launcher)
    saves_dir: Path = Path.home() / ".local/share/wsquashfs/saves"

    # Dossier temporaire (couches overlay, extraction de repli). Le .part est
    # construit À CÔTÉ de l'image, pour un échange atomique (même disque).
    tmp_dir: Path = Path("/tmp/wsquashfs-manager")
    # Dossiers choisis depuis l'interface (n'écrase pas .env)
    runtime_config_file: Path = Path.home() / ".config/wsquashfs-manager/config.json"
    # État de la file de tâches (survit à un redémarrage, SPEC § 4)
    state_dir: Path = Path.home() / ".local/state/wsquashfs-manager"

    # File de tâches : 1 par défaut, c'est surtout du disque
    task_concurrency: int = 1
    task_priority_nice: int = 5        # nice -n
    task_priority_ionice: int = 3      # classe ionice (3 = idle)

    # Reconstruction : overlay FUSE si disponible, sinon extraction complète
    rebuild_mode: Literal["auto", "overlay", "extract"] = "auto"

    # Rétention des .old (None = suppression manuelle uniquement)
    old_retention_days: int | None = None

    # Profondeur du scan sous chaque dossier roms/ : roms/<système>/<jeu>
    # (+ un niveau de sous-dossier). rglob sur tout l'array serait très lent.
    scan_max_depth: int = 3

    # Accès. Écoute locale par défaut : l'API modifie les images. Pour
    # l'ouvrir au réseau, poser host=0.0.0.0 ET un mot de passe.
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""

    # Outils (chemins sur la SteamBox)
    squashfuse: str = "squashfuse"
    mksquashfs: str = "mksquashfs"
    unsquashfs: str = "unsquashfs"
    fuse_overlayfs: str = "fuse-overlayfs"

    # Propriétaire des images et sauvegardes : le service doit tourner sous
    # cet utilisateur (il n'y a pas d'élévation de privilèges).
    tool_user: str = "arcade"
    # Options mksquashfs : identiques à --pack du lanceur (zstd, niveau par
    # défaut) — le niveau 19 allongeait nettement les reconstructions.
    mksquashfs_opts: list[str] = ["-comp", "zstd"]


def _read_runtime_roms_dirs(settings: Settings) -> list[str]:
    try:
        data = json.loads(settings.runtime_config_file.read_text())
        roms_dirs = data.get("roms_dirs", [])
    except (json.JSONDecodeError, OSError):
        return []
    return [directory for directory in roms_dirs if isinstance(directory, str)]


def save_roms_dirs(settings: Settings, roms_dirs: list[str]) -> None:
    """Persiste la sélection faite dans l'interface sans écraser ``.env``."""
    target = settings.runtime_config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_text(json.dumps({"roms_dirs": roms_dirs}, indent=2) + "\n")
    part.replace(target)
    settings.roms_dirs = roms_dirs


def get_settings() -> Settings:
    settings = Settings()
    # Une variable d'environnement est prioritaire, ce qui reste pratique
    # pour un déploiement conteneurisé.
    if not settings.roms_dirs:
        settings.roms_dirs = _read_runtime_roms_dirs(settings)
    return settings
