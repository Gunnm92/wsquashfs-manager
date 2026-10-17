"""Configuration du manager.

Valeurs par défaut alignées sur les conventions de wsquashfs-launcher
(WSQUASHFS_SAVES_DIR, etc.).
"""

import json
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Réglages modifiables depuis l'interface, enregistrés dans
# <data_dir>/config.json ; ils priment sur les variables d'environnement.
EDITABLE = ("roms_dirs", "saves_dir", "tmp_dir")


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

    # Données du manager (file de tâches)
    data_dir: Path = Path.home() / ".local/share/wsquashfs-manager"

    # Dossier temporaire (construction .part, couches overlay)
    tmp_dir: Path = Path("/tmp/wsquashfs-manager")

    # File de tâches
    task_concurrency: int = 1          # 1 : c'est surtout du disque
    task_priority_nice: int = 5        # priorité basse
    task_priority_ionice: int = 3      # classe ionice : 3 = au repos (idle)

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


def overrides_file(settings: Settings) -> Path:
    return settings.data_dir / "config.json"


def get_settings() -> Settings:
    s = Settings()
    try:
        data = json.loads(overrides_file(s).read_text())
    except (OSError, json.JSONDecodeError):
        return s
    return s.model_copy(update=Settings.model_validate(
        {**s.model_dump(), **{k: v for k, v in data.items() if k in EDITABLE}}).model_dump())


def save_overrides(settings: Settings, values: dict) -> None:
    f = overrides_file(settings)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: values[k] for k in EDITABLE if k in values}, indent=1,
                              default=str))
    tmp.replace(f)
