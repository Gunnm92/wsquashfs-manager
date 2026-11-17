"""Configuration du manager.

Valeurs par défaut alignées sur les conventions de wsquashfs-launcher
(WSQUASHFS_SAVES_DIR, etc.).
"""

import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .services.durable import write_atomic


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
    # Icône choisie à la main pour la machine d'essai (Sauvegardes) ; None = détection
    saves_game_host_icon: str | None = None

    # Cache de wsquashfs-launcher (son WSQUASHFS_CACHE) : montages des jeux et
    # verrous par jeu (locks/<jeu>.lock), pour reconnaître un jeu en cours
    launcher_cache: Path = Path(os.environ.get("WSQUASHFS_CACHE")
                                or Path.home() / ".cache/wsquashfs")

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
    rebuild_mode: Literal["auto", "stream", "extract"] = "auto"

    # Plage horaire des tâches « de nuit » (HH:MM, peut passer minuit) ;
    # modifiable dans Réglages
    night_start: str = "01:00"
    night_end: str = "07:00"

    # Machine d'essai : là où tourne wsquashfs-launcher pour le lancement
    # d'essai, l'état du lanceur (--doctor) et les runners installés.
    #   off    : fonctions désactivées (défaut d'une installation neuve) ;
    #   local  : sur cette machine (lanceur installé ici, session graphique) ;
    #   agent  : wsquashfs-launcher --agent, sur cette machine ou sur le réseau
    #            (association par code wsq://… ; un conteneur tourne ainsi aussi).
    # Non renseigné : « off ». Modifiable dans Réglages.
    game_host: Literal["off", "local", "agent"] | None = None
    agent_url: str = ""                           # https://hôte:port (mode agent)
    agent_token: str = ""                         # jeton durable de la machine d'essai (config.json, jamais exposé)
    # Processus de la session graphique dont l'environnement (DISPLAY…) est
    # repris pour l'essai ; vide = environnement du gestionnaire
    session_process: str = "emulationstation"
    trial_duration: int = 60

    # Mode du manager. « autonome » : il ne fait que construire (Images, Dossiers) ; aucune
    # machine, aucune écoute des machines, aucune synchro. « connecte » : sauvegardes entre
    # machines, installation, essais. Non renseigné dans config.json : « connecte » s'il y a
    # une machine associée ou une machine d'essai en agent/local, sinon « autonome » (get_settings).
    # Modifiable dans Réglages.
    mode: Literal["autonome", "connecte"] = "autonome"

    # Manager vu par les machines (synchro automatique à la sortie d'un jeu) : écoute HTTPS
    # sur machine_port (0 = désactivé), services/machine_api.py. machine_url : adresse du
    # manager telle que les machines la joignent (https://hôte:port), déclarée à chaque machine.
    machine_host: str = "0.0.0.0"
    machine_port: int = 8790
    machine_url: str = ""
    # Adresse de l'interface donnée aux machines (« Ouvrir le manager » de wsquashfs-launcher --tray) ;
    # vide : http://<hôte de machine_url>:<port>. WSQUASHFS_MGR_UI_URL derrière un proxy.
    ui_url: str = ""
    # Mise à jour automatique des machines en retard (services/saves.py push_behind) : intervalle
    # en secondes entre deux passages ; 0 : désactivée. Modifiable dans Réglages.
    auto_push_interval: int = 120

    # Icônes et jaquettes SteamGridDB (services/sgdb.py) : clé API (config.json, jamais exposée),
    # auteurs préférés (ordre de priorité), seuil de rondeur des icônes (0 à 1). Modifiables dans Réglages.
    sgdb_api_key: str = ""
    sgdb_authors: list[str] = []
    sgdb_round_threshold: float = 0.85

    # Rétention des .old (None = suppression manuelle uniquement)
    old_retention_days: int | None = None

    # Profondeur du scan sous chaque dossier roms/ : roms/<système>/<jeu>
    # (+ un niveau de sous-dossier). rglob sur tout l'array serait très lent.
    scan_max_depth: int = 3

    # Accès. Écoute locale par défaut : l'API modifie les images. Pour
    # l'ouvrir au réseau, poser host=0.0.0.0 ET un mot de passe, ou renoncer
    # explicitement au mot de passe (réseau domestique de confiance).
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""
    allow_no_password: bool = False

    # Outils (chemins sur la SteamBox)
    mksquashfs: str = "mksquashfs"
    unsquashfs: str = "unsquashfs"

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
        # ignore_cleanup_errors : un montage FUSE mort dans le dossier (ENOTCONN)
        # ne doit pas masquer l'erreur qui a interrompu la tâche
        with tempfile.TemporaryDirectory(prefix=prefix, dir=base,
                                         ignore_cleanup_errors=True) as tmp:
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
    write_atomic(target, json.dumps(data, indent=2) + "\n")


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


GAME_HOST_KEYS = ("game_host", "session_process", "agent_url", "agent_token")


def save_game_host(settings: Settings, values: dict) -> None:
    """Machine d'essai choisie dans « Réglages » (config.json)."""
    clean = {k: str(values[k]).strip() for k in GAME_HOST_KEYS if k in values}
    if clean.get("game_host") not in ("off", "local", "agent"):
        raise ValueError("mode de la machine d'essai : off, local ou agent")
    if clean["game_host"] == "agent":
        # Pas de saisie d'adresse ni de jeton : la machine d'essai est une machine associée
        # (code wsq://, puis choix) ; son adresse et son jeton durable viennent d'elle
        url = clean.get("agent_url", settings.agent_url)
        if not re.match(r"^https://[^\s/]+:\d+$", url or ""):
            raise ValueError("machine d'essai : associer une machine d'abord (code wsq://)")
        if not clean.get("agent_token") and not settings.agent_token:
            raise ValueError("machine d'essai : associer une machine d'abord (code wsq://)")
    _save_runtime(settings, **clean)
    for key, value in clean.items():
        setattr(settings, key, value)


SAVES_MACHINE_FIELDS = ("id", "name", "url", "token", "pem")
ICONS = ("linux", "steamos", "batocera")     # icônes des machines de la synchronisation des sauvegardes
_saves_lock = threading.Lock()      # lectures-écritures de config.json pour le manager de sauvegardes


def saves_machines(settings: Settings) -> list[dict]:
    """Machines de la synchronisation des sauvegardes (config.json) : id, name,
    url, token, pem. Le jeton et le certificat ne sortent jamais de l'API."""
    raw = _read_runtime(settings).get("saves_machines", [])
    if not isinstance(raw, list):
        return []
    return [m for m in raw
            if isinstance(m, dict) and all(isinstance(m.get(k), str) for k in SAVES_MACHINE_FIELDS)]


def save_saves_machines(settings: Settings, machines: list[dict]) -> None:
    with _saves_lock:
        _save_runtime(settings, saves_machines=machines)


def save_saves_game_host_icon(settings: Settings, icon: str | None) -> None:
    """Icône choisie pour la machine d'essai (None : retour à la détection).
    ValueError si la valeur n'est pas dans ICONS."""
    if icon is not None and icon not in ICONS:
        raise ValueError(f"icône inconnue : {icon} (valeurs : {', '.join(ICONS)})")
    with _saves_lock:
        _save_runtime(settings, saves_game_host_icon=icon)
    settings.saves_game_host_icon = icon


_MACHINE_URL = re.compile(r"https://[^\s/]+:(\d{1,5})")


def save_machine_url(settings: Settings, url: str) -> None:
    """Adresse du manager vue par les machines (https://hôte:port), ou "" pour aucune.
    ValueError si elle n'a pas cette forme ou si le port est hors plage."""
    value = (url or "").strip().rstrip("/")
    if value:
        match = _MACHINE_URL.fullmatch(value)
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("adresse du manager : https://hôte:port (ex. https://192.168.1.10:8790), ou vide")
    _save_runtime(settings, machine_url=value)
    settings.machine_url = value


SGDB_AUTHORS_MAX = 20


def save_sgdb(settings: Settings, api_key: str | None, authors: list[str], threshold: float) -> None:
    """Réglages SteamGridDB. `api_key` None : clé inchangée ; "" : clé effacée.
    ValueError si le seuil est hors de 0..1 ou s'il y a plus de SGDB_AUTHORS_MAX auteurs."""
    if not 0 <= threshold <= 1:
        raise ValueError("seuil de rondeur : entre 0 et 1")
    clean: list[str] = []
    for name in authors:
        name = " ".join(str(name).split())
        if name and name.lower() not in (a.lower() for a in clean):
            clean.append(name)
    if len(clean) > SGDB_AUTHORS_MAX:
        raise ValueError(f"auteurs préférés : {SGDB_AUTHORS_MAX} au plus")
    values: dict = {"sgdb_authors": clean, "sgdb_round_threshold": float(threshold)}
    if api_key is not None:
        values["sgdb_api_key"] = api_key.strip()
    _save_runtime(settings, **values)
    settings.sgdb_authors = clean
    settings.sgdb_round_threshold = float(threshold)
    if api_key is not None:
        settings.sgdb_api_key = api_key.strip()


AUTO_PUSH_MAX = 86400               # un passage au moins par jour, au plus


MODES = ("autonome", "connecte")


def save_mode(settings: Settings, mode: str) -> None:
    """Mode du manager (« autonome » ou « connecte »), dans config.json. ValueError si inconnu."""
    if mode not in MODES:
        raise ValueError("mode : autonome ou connecte")
    _save_runtime(settings, mode=mode)
    settings.mode = mode


def save_auto_push_interval(settings: Settings, seconds: int) -> None:
    """Intervalle de la mise à jour automatique (0 : désactivée). ValueError hors de 0..AUTO_PUSH_MAX."""
    if not 0 <= seconds <= AUTO_PUSH_MAX:
        raise ValueError(f"intervalle de mise à jour automatique : 0 (désactivée) à {AUTO_PUSH_MAX} secondes")
    _save_runtime(settings, auto_push_interval=seconds)
    settings.auto_push_interval = seconds


def set_saves_machine_field(settings: Settings, machine_id: str, key: str, value: str) -> None:
    """Un champ d'une machine enregistrée (lecture-modification-écriture sous verrou) ;
    KeyError si la machine n'existe pas."""
    with _saves_lock:
        machines = saves_machines(settings)
        machine = next((m for m in machines if m["id"] == machine_id), None)
        if machine is None:
            raise KeyError(machine_id)
        machine[key] = value
        _save_runtime(settings, saves_machines=machines)


def saves_client_id(settings: Settings) -> str:
    """Identifiant de ce manager auprès des agents (en-tête X-Wsq-Machine) :
    tiré une fois, puis conservé dans config.json."""
    with _saves_lock:
        current = _read_runtime(settings).get("saves_client_id")
        if isinstance(current, str) and re.fullmatch(r"[0-9a-f]{32}", current):
            return current
        client_id = secrets.token_hex(16)
        _save_runtime(settings, saves_client_id=client_id)
        return client_id


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
    start, end = str(runtime.get("night_start", "")), str(runtime.get("night_end", ""))
    if _HHMM.match(start) and _HHMM.match(end):
        settings.night_start, settings.night_end = runtime["night_start"], runtime["night_end"]
    for key in GAME_HOST_KEYS:
        if isinstance(runtime.get(key), str):
            setattr(settings, key, runtime[key])
    if settings.game_host not in ("off", "local", "agent"):
        # Ancien mode « docker » (retiré) : la machine d'essai est désactivée
        settings.game_host = "off"
    if runtime.get("saves_game_host_icon") in ICONS:
        settings.saves_game_host_icon = runtime["saves_game_host_icon"]
    if isinstance(runtime.get("machine_url"), str):
        settings.machine_url = runtime["machine_url"]
    interval = runtime.get("auto_push_interval")
    if type(interval) is int and 0 <= interval <= AUTO_PUSH_MAX:
        settings.auto_push_interval = interval
    if isinstance(runtime.get("sgdb_api_key"), str):
        settings.sgdb_api_key = runtime["sgdb_api_key"]
    authors = runtime.get("sgdb_authors")
    if isinstance(authors, list):
        settings.sgdb_authors = [a for a in authors if isinstance(a, str)][:SGDB_AUTHORS_MAX]
    threshold = runtime.get("sgdb_round_threshold")
    if type(threshold) in (int, float) and 0 <= threshold <= 1:
        settings.sgdb_round_threshold = float(threshold)
    if runtime.get("mode") in MODES:
        settings.mode = runtime["mode"]
    else:
        # Pas de choix enregistré : connecté seulement s'il y a de quoi se connecter
        connected = bool(saves_machines(settings)) or settings.game_host in ("agent", "local")
        settings.mode = "connecte" if connected else "autonome"
    return settings
