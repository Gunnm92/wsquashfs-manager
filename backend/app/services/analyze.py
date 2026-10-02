"""Analyse d'un dossier de jeu et proposition d'autorun (SPEC § 2.3, § 2.5).

Lecture seule, sans lancer le jeu :

- arborescence : prefix (Batocera `system.reg`, Proton `config_info`),
  moteur (Unreal, Unity, TeknoParrot), lanceurs `.bat` ;
- exécutables : en-têtes PE (32/64 bits, GUI/console, large address aware),
  imports (directs et différés), ressources de version ;
- choix de l'exécutable par score, chaque point justifié ;
- règles YAML (`rules.py`) sur l'exécutable retenu.

La proposition n'est jamais imposée : chaque valeur porte sa justification et
un niveau de confiance, et l'autorun existant est comparé à la proposition.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import math
import mmap
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pefile
import yaml

from .autorun import Autorun, decode_autorun
from .registry import section_values
from .rules import Analysis, load_rules, run_rules

# Valeurs par défaut du lanceur : une proposition égale au défaut n'est pas écrite
LAUNCHER_DEFAULTS = {"DXVK": "1", "VKD3D": "1", "D7VK": "1"}

# Dossiers sans exécutable de jeu (chemins relatifs, minuscules)
_EXCLUDED_DIRS = (
    "drive_c/windows/", "drive_c/programdata/", "drive_c/users/",
    "drive_c/program files/common files/", "drive_c/program files (x86)/common files/",
)
_EXCLUDED_DIR_PARTS = {
    "_commonredist", "__installer", "redist", "redistributables", "directx", "vcredist",
    "dotnet", "easyanticheat", "battleye", "prereqs", "prerequisites", "installers",
    "_redist", "_crack", "_cracks", "crack", "cracks", "_original files", "original files",
    "_original", "__macosx", "tools",
}
# Annexes : présentes dans le dossier du jeu mais ce n'est pas le jeu
_ANNEX_RE = re.compile(r"soundtrack|artbook|bonus|server|dedicated|trial|demo\b|benchmark|"
                       r"editor|mod ?manager|(^|/)mods/")
_EXCLUDED_NAMES = (
    ("vcredist*", "redistribuable"), ("vc_redist*", "redistribuable"),
    ("dxsetup*", "installeur DirectX"), ("dxwebsetup*", "installeur DirectX"),
    ("ue4prereqsetup*", "prérequis Unreal"), ("ueprereqsetup*", "prérequis Unreal"),
    ("dotnetfx*", "installeur .NET"), ("ndp*-kb*", "installeur .NET"),
    ("unins*", "désinstalleur"), ("*uninstall*", "désinstalleur"),
    ("*setup*", "installeur"), ("*install*", "installeur"),
    ("unitycrashhandler*", "rapporteur de plantage"), ("crashreportclient*", "rapporteur de plantage"),
    ("*crashhandler*", "rapporteur de plantage"), ("*crashpad*", "rapporteur de plantage"),
    ("*crashreport*", "rapporteur de plantage"), ("*crack*", "intro de crack"),
    ("quicksfv*", "outil de vérification"), ("dxdiag*", "outil système"),
    ("easyanticheat*", "anti-triche"), ("eac*launcher*", "anti-triche"),
)
_LAUNCHER_BATS = ("launch*.bat", "game.bat", "start*.bat", "run*.bat", "play*.bat")
# Commandes de .bat mal orthographiées déjà rencontrées (le lanceur les exécute telles quelles)
_BAT_TYPOS = {"taskill": "taskkill", "tasskill": "taskkill", "tskill": "taskkill"}
# Marqueurs cherchés dans l'exécutable (règle « strings_any »)
_MARKERS = (b"libScePad", b"DualSense", b"DUALSENSE")
_MARKER_SCAN_LIMIT = 400 * 2**20

RULES_FILE = Path(__file__).resolve().parent.parent.parent / "rules" / "rules.yaml"
GAMES_FILE = RULES_FILE.with_name("games.yaml")
_HASH_LIMIT = 300 * 2**20


# ------------------------------------------------------------------ base de connaissances

def load_games(path: Path = GAMES_FILE) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    games = list(data.get("games", []))
    for i, game in enumerate(games):
        if not (game.get("exe") or game.get("sha256")) or not (game.get("keys")
                                                               or game.get("requires")):
            raise ValueError(f"games.yaml, entrée {i} : exe/sha256 et keys ou requires requis")
    return games


def missing_requirements(files: list[str], exe: str, game: dict) -> list[str]:
    """Fichiers exigés par la base, absents du dossier de l'exécutable (casse ignorée)."""
    directory = exe.rpartition("/")[0]
    present = {f.lower() for f in files}
    return [name for name in game.get("requires") or []
            if f"{directory}/{name}".lstrip("/").lower() not in present]


def match_game(games: list[dict], scan: FolderScan, exe: str) -> dict | None:
    """Entrée de la base pour cet exécutable : empreinte d'abord, nom ensuite."""
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
    return by_name[0] if by_name else None


@dataclass
class ExeInfo:
    bits: int | None = None
    gui: bool | None = None
    large_address_aware: bool = False
    imports: set[str] = field(default_factory=set)
    product_name: str | None = None
    product_version: str | None = None
    file_version: str | None = None
    markers: set[str] = field(default_factory=set)
    error: str | None = None


@dataclass
class Candidate:
    path: str                       # relatif au dossier
    kind: str                       # "exe" | "bat" | "teknoparrot"
    size: int
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    info: ExeInfo | None = None
    cmd: str | None = None          # commande imposée (TeknoParrot)

    def to_dict(self) -> dict:
        info = self.info
        return {"path": self.path, "kind": self.kind, "size": self.size,
                "score": round(self.score, 1), "reasons": self.reasons,
                "bits": info.bits if info else None, "gui": info.gui if info else None,
                "version": (info.product_version or info.file_version) if info else None}


class FolderSource:
    """Lecture des fichiers d'un dossier de jeu."""

    def __init__(self, root: Path):
        self.root = root

    def read_bytes(self, rel: str, limit: int | None = None) -> bytes | None:
        try:
            with open(self.root / rel, "rb") as f:
                return f.read(limit if limit else -1)
        except OSError:
            return None

    @contextmanager
    def local_path(self, rel: str):
        yield self.root / rel


class ImageSource:
    """Lecture des fichiers d'une image .wsquashfs sans la monter
    (unsquashfs -cat) ; un exécutable est extrait le temps de lire son en-tête."""

    def __init__(self, image: Path, unsquashfs: str = "unsquashfs", tmp_dir: Path | None = None):
        self.root = image
        self.unsquashfs = unsquashfs
        self.tmp_dir = tmp_dir

    def read_bytes(self, rel: str, limit: int | None = None) -> bytes | None:
        try:
            r = subprocess.run([self.unsquashfs, "-cat", str(self.root), rel],
                               capture_output=True, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode:
            return None
        return r.stdout[:limit] if limit else r.stdout

    @contextmanager
    def local_path(self, rel: str):
        if self.tmp_dir:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="exe-", suffix=".bin", dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                subprocess.run([self.unsquashfs, "-cat", str(self.root), rel], stdout=out,
                               stderr=subprocess.DEVNULL, timeout=900, check=False)
            yield Path(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)


@dataclass
class FolderScan:
    root: Path                      # dossier, ou image .wsquashfs
    files: list[str]                # fichiers, dossiers et liens, relatifs
    sizes: dict[str, int]
    total_size: int
    source: FolderSource | ImageSource | None = None

    def __post_init__(self):
        if self.source is None:
            self.source = FolderSource(self.root)

    def read_text(self, rel: str, limit: int | None = None) -> str:
        data = self.source.read_bytes(rel, limit)
        return data.decode("latin-1") if data else ""


# ------------------------------------------------------------------ arborescence

def scan_folder(root: Path) -> FolderScan:
    files: list[str] = []
    sizes: dict[str, int] = {}
    total = 0
    for cur, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(cur, root)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for d in list(dirnames):
            files.append(prefix + d)
            if os.path.islink(os.path.join(cur, d)):
                dirnames.remove(d)          # lien vers un dossier : pas suivi
        for name in filenames:
            rel = prefix + name
            files.append(rel)
            try:
                st = os.lstat(os.path.join(cur, name))
            except OSError:
                continue
            sizes[rel] = st.st_size
            total += st.st_size
    return FolderScan(root=root, files=sorted(files), sizes=sizes, total_size=total)


_LLS_RE = re.compile(r"^(\S)\S*\s+\S+\s+(\d+)\s+\S+\s+\S+\s+squashfs-root/(.+)$")


def scan_image(image: Path, unsquashfs: str = "unsquashfs",
               tmp_dir: Path | None = None) -> FolderScan:
    """Arborescence et tailles d'une image, d'après sa table (unsquashfs -lls)."""
    r = subprocess.run([unsquashfs, "-lls", str(image)], capture_output=True, timeout=300,
                       check=False)
    files: list[str] = []
    sizes: dict[str, int] = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        m = _LLS_RE.match(line)
        if not m:
            continue
        kind, size, rel = m.groups()
        if kind == "l":
            rel = rel.split(" -> ", 1)[0]
        files.append(rel)
        if kind == "-":
            sizes[rel] = int(size)
    return FolderScan(root=image, files=sorted(files), sizes=sizes,
                      total_size=sum(sizes.values()),
                      source=ImageSource(image, unsquashfs, tmp_dir))


def detect_prefix(scan: FolderScan) -> tuple[str, str | None, str | None]:
    """(type, arch, version Proton) : comme detect_type() du scan, config_info d'abord."""
    present = {f.lower() for f in scan.files if "/" not in f}
    if "config_info" in present:
        version = scan.read_text("config_info").splitlines()[:1]
        return "proton", None, version[0].strip() if version else None
    if "system.reg" in present:
        head = scan.read_text("system.reg", limit=4096)
        m = re.search(r"^#arch=(win32|win64)", head, re.MULTILINE)
        return "batocera", m.group(1) if m else None, None
    return "none", None, None


def dll_overrides(scan: FolderScan, prefix: str) -> dict[str, str]:
    reg = "pfx/user.reg" if prefix == "proton" else "user.reg"
    text = scan.read_text(reg) if reg in scan.files else ""
    return section_values(text, "Software\\Wine\\DllOverrides") if text else {}


# ------------------------------------------------------------------ PE

def _version_string(value: bytes | str | None) -> str | None:
    if not value:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = text.strip().strip("\0").replace(", ", ".").replace(",", ".")
    return text or None


def read_exe(path: Path) -> ExeInfo:
    """En-têtes, imports et version d'un exécutable PE (sans l'exécuter)."""
    info = ExeInfo()
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except (pefile.PEFormatError, OSError) as exc:
        info.error = str(exc)
        return info
    try:
        machine = pe.FILE_HEADER.Machine
        info.bits = 64 if machine in (0x8664, 0xAA64) else 32 if machine == 0x14C else None
        info.gui = pe.OPTIONAL_HEADER.Subsystem == 2
        info.large_address_aware = bool(pe.FILE_HEADER.Characteristics & 0x20)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"],
        ])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []) or []:
                if entry.dll:
                    info.imports.add(entry.dll.decode("latin-1").lower())
        for file_info in getattr(pe, "FileInfo", []) or []:
            for block in file_info:
                for table in getattr(block, "StringTable", []) or []:
                    strings = {k.decode("latin-1"): v for k, v in table.entries.items()}
                    info.product_name = info.product_name or _version_string(
                        strings.get("ProductName"))
                    info.product_version = info.product_version or _version_string(
                        strings.get("ProductVersion"))
                    info.file_version = info.file_version or _version_string(
                        strings.get("FileVersion"))
    except Exception as exc:  # noqa: BLE001 — un PE malformé ne bloque pas l'analyse
        info.error = str(exc)
    finally:
        pe.close()
    info.markers = _markers(path)
    return info


def _markers(path: Path) -> set[str]:
    try:
        if path.stat().st_size > _MARKER_SCAN_LIMIT:
            return set()
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return {marker.decode() for marker in _MARKERS if m.find(marker) != -1}
    except (OSError, ValueError):
        return set()


# ------------------------------------------------------------------ candidats

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _acronym(text: str) -> str:
    return "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", text)).lower()


def name_similarity(exe: str, game: str) -> float:
    stem = _norm(exe.rsplit(".", 1)[0].replace("-Win64-Shipping", "").replace("-Win32-Shipping", ""))
    if not stem:
        return 0.0
    game_norm = _norm(game)
    ratios = [difflib.SequenceMatcher(None, stem, game_norm).ratio(),
              difflib.SequenceMatcher(None, stem, _acronym(game)).ratio()]
    if len(stem) >= 4 and (stem in game_norm or game_norm in stem):
        ratios.append(0.9)
    return max(ratios)


def _excluded(rel: str) -> str | None:
    low = rel.lower()
    if low.startswith(_EXCLUDED_DIRS):
        return "dossier système du prefix"
    parts = low.split("/")
    if any(p in _EXCLUDED_DIR_PARTS for p in parts[:-1]):
        return "dossier d'installeurs ou de redistribuables"
    if "engine/binaries/" in low or "engine/extras/" in low:
        return "outil du moteur Unreal"
    for pattern, reason in _EXCLUDED_NAMES:
        if fnmatch.fnmatchcase(parts[-1], pattern):
            return reason
    return None


def find_candidates(scan: FolderScan, game: str) -> tuple[list[Candidate], list[dict], str | None]:
    """Candidats classés (meilleur d'abord), exclus, moteur détecté."""
    lower = {f.lower(): f for f in scan.files}
    exes = [f for f in scan.files if f.lower().endswith(".exe") and f in scan.sizes]
    unity_dirs = {f.rpartition("/")[0].lower() for f in scan.files
                  if f.lower().rsplit("/", 1)[-1] == "unityplayer.dll"}
    unreal = any(re.search(r"(^|/)binaries/win(64|32)/[^/]+-win(64|32)-shipping\.exe$", f.lower())
                 for f in exes) or any(f.lower() in ("engine", "engine/binaries") for f in scan.files)
    tekno = next((lower[k] for k in lower if k.endswith("teknoparrot/teknoparrotui.exe")), None)
    engine = "teknoparrot" if tekno else "unity" if unity_dirs else "unreal" if unreal else None

    # Dossiers qui contiennent <Projet>/Binaries/Win64/ : le lanceur Unreal y est
    ue_bases = {_ue_base(f) for f in exes if _ue_base(f) is not None}
    candidates: list[Candidate] = []
    excluded: list[dict] = []
    largest = max((scan.sizes[e] for e in exes), default=0)
    for rel in exes:
        reason = _excluded(rel)
        if reason:
            excluded.append({"path": rel, "reason": reason})
            continue
        c = Candidate(path=rel, kind="exe", size=scan.sizes[rel])
        name = rel.rsplit("/", 1)[-1]
        depth = rel.count("/")
        sim = name_similarity(name, game)
        if sim >= 0.5:
            c.score += 40 * sim
            c.reasons.append(f"nom proche du jeu ({sim:.0%})")
        c.score += min(25, max(0, 6 * math.log10(max(c.size, 1) / 1e5)))
        if c.size == largest:
            c.score += 8
            c.reasons.append("plus gros exécutable")
        if depth:
            c.score -= 3 * depth
        else:
            c.reasons.append("à la racine du jeu")
        low = rel.lower()
        # Unreal : le petit lanceur à la racine démarre le -Shipping avec les
        # bons arguments ; c'est lui que portent les autoruns existants.
        if engine == "unreal" and rel.rpartition("/")[0].lower() in ue_bases \
                and c.size < 10 * 2**20:
            c.score += 65
            c.reasons.append("lanceur racine Unreal (démarre l'exécutable Shipping)")
        elif engine == "unreal" and re.search(r"binaries/win(64|32)/[^/]+-win(64|32)-shipping\.exe$", low):
            c.score += 40
            c.reasons.append("exécutable Shipping d'Unreal Engine (alternative au lanceur racine)")
        elif engine == "unreal" and re.search(r"binaries/win(64|32)/[^/]+\.exe$", low):
            c.score += 30
            c.reasons.append("exécutable du jeu Unreal (Binaries/Win64)")
        if rel.rpartition("/")[0].lower() in unity_dirs:
            c.score += 45
            c.reasons.append("à côté de UnityPlayer.dll (Unity)")
        if "launcher" in name.lower():
            c.reasons.append("lanceur du jeu : à préférer s'il est nécessaire")
        annex = _ANNEX_RE.search(low)
        if annex:
            c.score -= 30
            c.reasons.append(f"annexe ({annex.group(0).strip('/')})")
        if re.search(r"config|settings|editor|server|benchmark|tool", name.lower()):
            c.score -= 25
            c.reasons.append("outil de configuration ou annexe")
        candidates.append(c)

    for rel in scan.files:
        name = rel.rsplit("/", 1)[-1].lower()
        if (rel in scan.sizes and rel.count("/") <= 1 and not _excluded(rel)
                and not _ANNEX_RE.search(rel.lower())
                and any(fnmatch.fnmatchcase(name, p) for p in _LAUNCHER_BATS)):
            c = Candidate(path=rel, kind="bat", size=scan.sizes[rel], score=30 - 3 * rel.count("/"),
                          reasons=["lanceur .bat"])
            candidates.append(c)

    if tekno:
        c = Candidate(path=tekno, kind="teknoparrot", size=scan.sizes.get(tekno, 0), score=200,
                      reasons=["TeknoParrot"])
        profile = _tekno_profile(scan, tekno, game)
        if profile:
            c.cmd = f'"TeknoParrotUi.exe" --profile={profile} --startMinimized'
            c.reasons.append(f"profil {profile}")
        else:
            c.reasons.append("profil introuvable dans UserProfiles/ ni GameProfiles/")
        candidates.append(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates, excluded, engine


def _tekno_profile(scan: FolderScan, tekno: str, game: str) -> str | None:
    """Profil TeknoParrot : UserProfiles/ puis GameProfiles/, celui dont le
    contenu cite un exécutable présent, sinon le nom le plus proche du jeu."""
    base = tekno.rpartition("/")[0]
    exe_names = {f.rsplit("/", 1)[-1].lower() for f in scan.files
                 if f.lower().endswith(".exe") and not f.startswith(base + "/")}
    for folder in ("UserProfiles", "GameProfiles"):
        profiles = [f for f in scan.files if f.lower().startswith(f"{base}/{folder}/".lower())
                    and f.lower().endswith(".xml")]
        if not profiles:
            continue
        for profile in profiles:
            text = scan.read_text(profile).lower()
            m = re.search(r"<gamepath>([^<]+)</gamepath>", text)
            if m and m.group(1).replace("\\", "/").rsplit("/", 1)[-1] in exe_names:
                return profile.rsplit("/", 1)[-1]
        if folder == "UserProfiles":
            best = max(profiles, key=lambda p: name_similarity(p.rsplit("/", 1)[-1], game))
            return best.rsplit("/", 1)[-1]
    return None


def bat_details(scan: FolderScan | Path, rel: str) -> dict:
    if isinstance(scan, Path):
        scan = FolderScan(root=scan, files=[], sizes={}, total_size=0)
    text = scan.read_text(rel, limit=64 * 1024)
    suspicious = []
    for number, line in enumerate(text.splitlines(), start=1):
        for typo, fix in _BAT_TYPOS.items():
            if re.search(rf"\b{typo}\b", line, re.IGNORECASE):
                suspicious.append(f"ligne {number} : « {typo} » au lieu de « {fix} »")
        if re.search(r"\b[a-z]:\\", line, re.IGNORECASE) and "%~dp0" not in line:
            suspicious.append(f"ligne {number} : chemin absolu Windows ({line.strip()[:80]})")
    return {"path": rel, "content": text, "suspicious": suspicious}


# ------------------------------------------------------------------ proposition

# Version du moteur et non du jeu : Unity (2022.3.31f1 (…), 6000.0.66…),
# Unreal (++UE4+Release-4.27-CL-0), ou simple numéro de build (6566)
_ENGINE_VERSION_RE = re.compile(r"^\+\+UE|^(20\d\d|6000)\.\d+\.\d+|\d+f\d+|^\d+$")
_ENGINE_PRODUCTS = {"bootstrappackagedgame", "unreal engine", "unity player", "unityplayer"}


def _game_version(info: ExeInfo | None, engine: str | None) -> tuple[str | None, str]:
    """Version du jeu d'après les ressources de l'exécutable.

    Constat sur les jeux réels : une vraie version s'accompagne d'un
    ProductName (« SAND LAND » 1.0.3.0) ; sans lui, sous Unity ou Unreal,
    c'est la version du moteur (Toki : 2018.3.8…, Storybook : ++UE4…)."""
    if info is None:
        return None, ""
    version = info.product_version or info.file_version
    if not version or set(version) <= set("0."):
        return None, "aucune version dans les ressources de l'exécutable"
    product = (info.product_name or "").strip().lower()
    if _ENGINE_VERSION_RE.search(version) or product in _ENGINE_PRODUCTS:
        return None, f"{version} : version du moteur ou numéro de build, pas celle du jeu — à saisir"
    if not re.fullmatch(r"v?\d+(?:[.\-_]\d+){1,4}[a-z]?", version, re.IGNORECASE):
        return None, f"« {version} » n'est pas un numéro de version (chaîne de build ?) — à saisir"
    if re.fullmatch(r"1(\.0){1,3}", version):
        return None, f"{version} : valeur par défaut des projets, rarement la vraie version — à saisir"
    if engine in ("unreal", "unity") and not product:
        return None, f"{version} sans nom de produit : probablement la version du moteur — à saisir"
    source = "ProductVersion" if info.product_version else "FileVersion"
    return version, f"ressources de l'exécutable ({source})"


def quote_cmd(exe: str) -> str:
    return f'"{exe}"' if " " in exe else exe


@dataclass
class Proposal:
    autorun: Autorun
    items: list[dict]               # {key, value, justification, confidence, written}
    warnings: list[str]
    exe: str | None
    knowledge: dict | None = None   # entrée de la base de connaissances appliquée


def propose(scan: FolderScan, candidate: Candidate | None, prefix: str, arch: str | None,
            proton: str | None, engine: str | None, overrides: dict[str, str],
            rules_file: Path = RULES_FILE, games_file: Path = GAMES_FILE) -> Proposal:
    lines: list[str] = []
    items: list[dict] = []
    warnings: list[str] = []

    def add(key: str, value: str, justification: str, confidence: str = "high") -> None:
        written = LAUNCHER_DEFAULTS.get(key) != value
        if written and not any(line.startswith(f"{key}=") for line in lines):
            lines.append(f"{key}={value}")
        items.append({"key": key, "value": value, "justification": justification,
                      "confidence": confidence, "written": written})

    if candidate is None:
        warnings.append("aucun exécutable candidat : CMD à saisir")
        return Proposal(Autorun(lines=lines), items, warnings, None)

    directory, _, exe_name = candidate.path.rpartition("/")
    if directory:
        add("DIR", directory, "dossier de l'exécutable retenu")
    add("CMD", candidate.cmd or quote_cmd(exe_name), "; ".join(candidate.reasons) or "exécutable",
        "high" if candidate.score >= 40 else "medium" if candidate.score >= 20 else "low")
    info = candidate.info
    version, why = _game_version(info, engine)
    if version:
        add("GAME_VERSION", version, f"{why}, à confirmer", "medium")
    elif why:
        warnings.append(f"GAME_VERSION : {why}")

    analysis = Analysis(
        files=scan.files, imports=info.imports if info else set(), exe_name=exe_name,
        exe_dir=directory, exe_bits=info.bits if info else None,
        exe_large_address_aware=info.large_address_aware if info else False,
        prefix_type=prefix, proton_version=proton, arch=arch,
        version_resource=version, strings=info.markers if info else set(),
        dll_overrides=overrides,
    )
    for result in run_rules(load_rules(rules_file), analysis):
        if result.key and result.value is not None:
            add(result.key, result.value, f"{result.justification} (règle {result.source})",
                result.confidence)
        if result.warning:
            warnings.append(result.warning)
        if result.file:
            warnings.append(f"fichier à ajouter : {result.file} (règle {result.source})")
    # Base de connaissances : prioritaire sur les règles générales
    knowledge = match_game(load_games(games_file), scan, candidate.path)
    if knowledge:
        how = "empreinte" if knowledge.get("sha256") else "nom de l'exécutable"
        for key, value in (knowledge.get("keys") or {}).items():
            value = str(value)
            lines[:] = [line for line in lines if not line.startswith(f"{key}=")]
            items[:] = [i for i in items if i["key"] != key]
            add(key, value, f"base de connaissances ({how}) : {knowledge.get('note', '')}".strip())
            if LAUNCHER_DEFAULTS.get(key) == value:
                items[-1]["written"] = False
        for missing in missing_requirements(scan.files, candidate.path, knowledge):
            warnings.append(f"base de connaissances : {missing} manquant à côté de "
                            f"l'exécutable — {knowledge.get('note', '')}".strip(" —"))
    if candidate.kind == "bat":
        warnings.append("lanceur .bat : vérifier son contenu ci-dessous")
    if info and info.error:
        warnings.append(f"en-tête PE illisible : {info.error}")
    return Proposal(Autorun(lines=lines, eol="\r\n"), items, warnings, candidate.path, knowledge)


# ------------------------------------------------------------------ point d'entrée

@dataclass
class FolderAnalysis:
    scan: FolderScan
    game: str
    prefix: str
    arch: str | None
    proton: str | None
    engine: str | None
    overrides: dict[str, str]
    candidates: list[Candidate]
    excluded: list[dict]
    existing: bytes | None

    def candidate(self, path: str | None) -> Candidate | None:
        if path:
            return next((c for c in self.candidates if c.path == path), None)
        return self.candidates[0] if self.candidates else None


_PE_CANDIDATES = 8      # en-têtes PE lus pour les meilleurs candidats seulement


_UE_BINARY_RE = re.compile(r"^(?:(.*)/)?[^/]+/binaries/win(?:64|32)/[^/]+\.exe$")


def _ue_base(path: str) -> str | None:
    """« Windows/Bates/Binaries/Win64/x.exe » → « windows » (dossier du lanceur)."""
    m = _UE_BINARY_RE.match(path.lower())
    return (m.group(1) or "") if m else None


def _merge_shipping(candidates: list[Candidate]) -> None:
    """Unreal : le lanceur racine ne fait que démarrer le -Shipping.exe, où se
    trouve le code du jeu. Ses imports et signatures (DualSense, HID…) sont
    reportés sur le lanceur, pour que les règles les voient (Until Dawn :
    Bates.exe 430 Ko, tout est dans Bates-Win64-Shipping.exe)."""
    shipping = [c for c in candidates if c.info and re.search(
        r"binaries/win(64|32)/[^/]+-win(64|32)-shipping\.exe$", c.path.lower())]
    if not shipping:
        return
    for ship in shipping:
        base = _ue_base(ship.path)
        for c in candidates:
            if c.info and c is not ship and c.path.rpartition("/")[0].lower() == base:
                c.info.imports |= ship.info.imports
                c.info.markers |= ship.info.markers


def _read_pe(scan: FolderScan, rel: str) -> ExeInfo:
    with scan.source.local_path(rel) as path:
        return read_exe(path)


def _analyze(scan: FolderScan, game: str, existing: bytes | None,
             pe_limit: int = _PE_CANDIDATES) -> FolderAnalysis:
    prefix, arch, proton = detect_prefix(scan)
    candidates, excluded, engine = find_candidates(scan, game)
    exes = [c for c in candidates if c.kind == "exe"]
    to_read = exes[:pe_limit]
    if engine == "unreal":         # le -Shipping porte le code du jeu (voir _merge_shipping)
        to_read += [c for c in exes if c not in to_read and _ue_base(c.path) is not None
                    and "shipping" in c.path.lower()][:2]
    for c in to_read:
        c.info = _read_pe(scan, c.path)
        if c.info.gui is False:
            c.score -= 15
            c.reasons.append("programme console")
        elif c.info.gui:
            c.score += 5
    if engine == "unreal":
        _merge_shipping(candidates)
    tekno = next((c for c in candidates if c.kind == "teknoparrot"), None)
    if tekno:
        tekno.info = _read_pe(scan, tekno.path)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return FolderAnalysis(scan=scan, game=game, prefix=prefix, arch=arch, proton=proton,
                          engine=engine, overrides=dll_overrides(scan, prefix),
                          candidates=candidates, excluded=excluded, existing=existing)


def analyze_folder(root: Path, game: str) -> FolderAnalysis:
    scan = scan_folder(root)
    existing_rel = next((f for f in scan.files if f.lower() == "autorun.cmd"), None)
    existing = scan.source.read_bytes(existing_rel) if existing_rel else None
    return _analyze(scan, game, existing)


def analyze_image(image: Path, game: str, unsquashfs: str = "unsquashfs",
                  tmp_dir: Path | None = None, pe_limit: int = 3) -> FolderAnalysis:
    """Même analyse sur une image existante : table de l'image, exécutables
    extraits un à un le temps de lire leur en-tête (moins de candidats lus
    que pour un dossier : chaque lecture décompresse l'exécutable)."""
    scan = scan_image(image, unsquashfs, tmp_dir)
    existing = scan.source.read_bytes("autorun.cmd") if "autorun.cmd" in scan.files else None
    return _analyze(scan, game, existing, pe_limit)


def report(fa: FolderAnalysis, exe: str | None = None) -> dict:
    """Résultat pour l'API : proposition, autorun existant validé, recommandation."""
    chosen = fa.candidate(exe)
    proposal = propose(fa.scan, chosen, fa.prefix, fa.arch, fa.proton, fa.engine, fa.overrides)
    file_set = set(fa.scan.files)
    generated_issues = proposal.autorun.validate(file_set)
    existing = None
    recommended = "generated"
    if fa.existing is not None:
        text, encoding = decode_autorun(fa.existing)
        parsed = Autorun.parse(text)
        issues = parsed.validate(file_set)
        if proposal.knowledge:
            for missing in missing_requirements(fa.scan.files, proposal.exe or "",
                                                proposal.knowledge):
                issues.append({"level": "error", "message": (
                    f"base de connaissances : {missing} manquant à côté de l'exécutable"
                    f" — {proposal.knowledge.get('note', '')}").strip(" —")})
            for key, value in (proposal.knowledge.get("keys") or {}).items():
                current = parsed.get(key) or LAUNCHER_DEFAULTS.get(key)
                if current != str(value):
                    issues.append({"level": "error", "message": (
                        f"base de connaissances : {key}={value} attendu pour ce jeu"
                        f" — {proposal.knowledge.get('note', '')}").strip(" —")})
        existing = {"text": parsed.render().replace("\r\n", "\n"), "encoding": encoding,
                    "issues": issues}
        # Réglages que les règles ajoutent et que l'autorun existant n'a pas
        for item in proposal.items:
            if (item["written"] and item["key"] not in ("DIR", "CMD", "GAME_VERSION")
                    and item["confidence"] in ("high", "medium")
                    and parsed.get(item["key"]) != item["value"]):
                issues.append({"level": "warn", "message": (
                    f"la proposition ajoute {item['key']}={item['value']} : "
                    f"{item['justification']}")})
        broken = any(i["level"] == "error" or "introuvable" in i["message"]
                     or i["message"].startswith("la proposition ajoute") for i in issues)
        if not broken:
            recommended = "existing"
    return {
        "game": fa.game,
        "prefix": fa.prefix, "arch": fa.arch, "proton": fa.proton, "engine": fa.engine,
        "size": fa.scan.total_size, "file_count": len(fa.scan.sizes),
        "candidates": [c.to_dict() for c in fa.candidates[:15]],
        "excluded": fa.excluded[:30],
        "chosen": proposal.exe,
        "generated": {"text": proposal.autorun.render().replace("\r\n", "\n"),
                      "items": proposal.items, "warnings": proposal.warnings,
                      "issues": generated_issues},
        "existing": existing,
        "recommended": recommended,
        "bats": [bat_details(fa.scan, c.path) for c in fa.candidates if c.kind == "bat"][:3],
    }
