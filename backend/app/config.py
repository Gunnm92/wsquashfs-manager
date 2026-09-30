"""Configuration du manager.

Valeurs par défaut alignées sur les conventions de wsquashfs-launcher
(WSQUASHFS_SAVES_DIR, etc.).
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WSQUASHFS_MGR_", env_file=".env")

    # Dossiers de jeux à scanner : liste de chemins vers des dossiers roms/
    # (variable d'env : liste séparée par des virgules)
    roms_dirs: Annotated[list[str], NoDecode] = []

    @field_validator("roms_dirs", mode="before")
    @classmethod
    def _split_roms_dirs(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    # Sauvegardes (même convention que wsquashfs-launcher)
    saves_dir: Path = Path.home() / ".local/share/wsquashfs/saves"

    # Dossier temporaire (construction .part, couches overlay)
    tmp_dir: Path = Path("/tmp/wsquashfs-manager")

    # File de tâches
    task_concurrency: int = 1          # 1 : c'est surtout du disque
    task_priority_nice: int = 5        # priorité basse
    task_priority_ionice: int = 3      # idem

    # Rétention des .old (None = manuelle, sinon N jours)
    old_retention_days: int | None = None

    # Profondeur du scan sous chaque dossier roms/ : roms/<système>/<jeu>
    # (+ un niveau de sous-dossier). rglob sur tout l'array serait très lent.
    scan_max_depth: int = 3

    # Accès. Écoute locale par défaut : sans mot de passe, l'API (qui
    # modifiera les images) ne doit pas être ouverte au réseau. Pour l'ouvrir,
    # poser host=0.0.0.0 ET un mot de passe.
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""                 # vide = sans auth (à décider, SPEC §7)

    # Outils (chemins sur la SteamBox)
    squashfuse: str = "squashfuse"
    mksquashfs: str = "mksquashfs"
    unsquashfs: str = "unsquashfs"
    fuse_overlayfs: str = "fuse-overlayfs"

    # Utilisateur sous lequel exécuter les outils (propriétaire des images)
    tool_user: str = "arcade"

    # Options mksquashfs : identiques à --pack du lanceur (zstd, niveau par
    # défaut) — le niveau 19 allongeait nettement les reconstructions.
    mksquashfs_opts: list[str] = ["-comp", "zstd"]


def get_settings() -> Settings:
    return Settings()
