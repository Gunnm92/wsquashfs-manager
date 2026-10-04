"""Configuration du manager.

Valeurs par défaut alignées sur les conventions de wsquashfs-launcher
(WSQUASHFS_SAVES_DIR, etc.).
"""

import json
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WSQUASHFS_MGR_", env_file=".env")

    # Dossiers de jeux à scanner : liste de chemins vers des dossiers roms/
    # (variable d'env : liste séparée par des virgules)
    roms_dirs: Annotated[list[str], NoDecode] = []
    # Dossiers de base (variable d'environnement), proposés dans « Réglages »
    roms_roots: list[str] = []

    @field_validator("roms_dirs", mode="before")
    @classmethod
    def _split_roms_dirs(cls, value):
        if isinstance(value, str):
            return [path.strip() for path in value.split(",") if path.strip()]
        return value

    # Sauvegardes (même convention que wsquashfs-launcher)
    saves_dir: Path = Path.home() / ".local/share/wsquashfs/saves"

    # Dossier de travail (couches overlay, extraction de repli, exécutables
    # extraits pour l'analyse). Par défaut, un dossier caché .wsquashfs-manager
    # À CÔTÉ de l'image : même disque, aucune donnée ne transite ailleurs (le
    # .part y est aussi construit, pour un échange atomique).
    tmp_dir: Path | None = None
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

    # Plage horaire des tâches « de nuit » (HH:MM, peut passer minuit) ;
    # modifiable dans Réglages
    night_start: str = "01:00"
    night_end: str = "07:00"

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


WORK_DIR_NAME = ".wsquashfs-manager"


@contextmanager
def work_area(settings: Settings, near: Path, prefix: str):
    """Dossier de travail temporaire, à côté de `near` (image ou dossier de
    jeu) sauf si tmp_dir est imposé ; supprimé ensuite, son parent aussi s'il
    est vide."""
    base = settings.tmp_dir or near.parent / WORK_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=prefix, dir=base) as tmp:
            yield Path(tmp)
    finally:
        if settings.tmp_dir is None:
            try:
                base.rmdir()
            except OSError:
                pass            # utilisé par une autre tâche, ou non vide


def _read_runtime(settings: Settings) -> dict:
    try:
        data = json.loads(settings.runtime_config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_runtime_roms_dirs(settings: Settings) -> list[str]:
    roms_dirs = _read_runtime(settings).get("roms_dirs", [])
    return [directory for directory in roms_dirs if isinstance(directory, str)]


def _save_runtime(settings: Settings, **values) -> None:
    """Réglages faits dans l'interface (config.json), sans écraser ``.env``."""
    data = _read_runtime(settings)
    data.update(values)
    target = settings.runtime_config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_text(json.dumps(data, indent=2) + "\n")
    part.replace(target)


def save_roms_dirs(settings: Settings, roms_dirs: list[str]) -> None:
    _save_runtime(settings, roms_dirs=roms_dirs)
    settings.roms_dirs = roms_dirs


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def save_night(settings: Settings, start: str, end: str) -> None:
    if not (_HHMM.match(start) and _HHMM.match(end)) or start == end:
        raise ValueError("plage horaire invalide (HH:MM, début différent de la fin)")
    _save_runtime(settings, night_start=start, night_end=end)
    settings.night_start, settings.night_end = start, end


def in_night(settings: Settings, now: datetime | None = None) -> bool:
    """Vrai pendant la plage de nuit ; elle peut passer minuit (01:00-07:00,
    ou 22:00-06:00)."""
    current = (now or datetime.now().astimezone()).strftime("%H:%M")
    start, end = settings.night_start, settings.night_end
    if start < end:
        return start <= current < end
    return current >= start or current < end


def get_settings() -> Settings:
    settings = Settings()
    # WSQUASHFS_MGR_ROMS_DIRS : dossiers montés (ex. /roms dans le conteneur),
    # où l'interface (« Réglages ») choisit ; son choix l'emporte, la
    # variable ne sert que tant que rien n'a été choisi.
    settings.roms_roots = list(settings.roms_dirs)
    chosen = _read_runtime_roms_dirs(settings)
    if chosen:
        settings.roms_dirs = chosen
    runtime = _read_runtime(settings)
    if _HHMM.match(str(runtime.get("night_start", ""))) and _HHMM.match(str(runtime.get("night_end", ""))):
        settings.night_start, settings.night_end = runtime["night_start"], runtime["night_end"]
    return settings
