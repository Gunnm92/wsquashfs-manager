"""Modèles de données (pydantic)."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ImageType(str, Enum):
    WINE_BATOCERA = "wine_batocera"     # prefix Batocera (system.reg)
    WINE_PROTON = "wine_proton"         # prefix Proton (config_info + version)
    WINE_OTHER = "wine_other"           # autre prefix Wine
    GAME_ONLY = "game_only"             # jeu seul, pas de prefix
    UNKNOWN = "unknown"


class RunnerKind(str, Enum):
    WINE = "wine"
    PROTON = "proton"
    CUSTOM = "custom"       # RUNNER=
    DEFAULT = "default"


class ImageState(str, Enum):
    OK = "ok"
    IN_USE = "in_use"         # monté / lancé
    MODIFIED = "modified"     # modifié depuis le scan (à relire)
    HAS_OLD = "has_old"       # .old présent


class AutorunKey(BaseModel):
    key: str
    value: str
    line: int  # numéro de ligne 1-indexé dans le fichier original
    raw: str   # ligne brute (avec CRLF éventuel conservé séparément)


class ImageInfo(BaseModel):
    id: str = ""                   # "<système>/<nom>" : le nom seul n'est pas unique
    path: Path
    name: str
    system: str | None = None      # dossier roms/<système>
    size: int = 0
    mtime: float = 0.0
    version: str | None = None     # GAME_VERSION lu sans monter
    type: ImageType = ImageType.UNKNOWN
    runner: RunnerKind = RunnerKind.DEFAULT
    wine: str | None = None
    proton: str | None = None
    hidraw: bool | None = None
    xinput: bool = False           # vrai si HIDRAW n'est pas à 1 (mode par défaut)
    cmd: str | None = None
    dir: str | None = None
    dxvk: bool = False
    vkd3d: bool = False
    extras: list[str] = Field(default_factory=list)   # .keys, fakeping, DllOverrides...
    saves_size: int = 0
    saves_present: bool = False
    has_old: bool = False
    state: ImageState = ImageState.OK


class ScanCacheEntry(BaseModel):
    path: str
    size: int
    mtime: float
    info: ImageInfo


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    id: str
    kind: str                        # "rebuild" | "edit_autorun" | "mass_action" | ...
    image: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0            # 0..1
    log: list[str] = Field(default_factory=list)
    created_at: float = 0.0


class RuleResult(BaseModel):
    """Résultat d'une règle YAML : une clé, un fichier, ou un avertissement."""
    key: str | None = None           # clé d'autorun proposée
    value: str | None = None
    file: str | None = None          # fichier à ajouter (chemin dans l'image)
    warning: str | None = None
    justification: str = ""
    confidence: str = "high"         # high | medium | low
    source: str = ""                 # nom de la règle
