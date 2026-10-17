"""Lecture / écriture de l'`autorun.cmd`.

Conventions identiques au lanceur (SPEC § 3.2) :
- clé en début de ligne, forme `CLÉ=Valeur` ;
- première occurrence retenue ;
- le `\r` final de ligne est retiré à la lecture ;
- la réécriture est LIGNE À LIGNE : commentaires (REM, #), ordre des lignes,
  fins de ligne (CRLF/LF détectées, présence ou non d'un saut de ligne final)
  et espaces sont conservés ;
- une clé modifiée garde sa place ; une clé ajoutée va en fin de fichier ;
- les clés inconnues sont signalées mais non bloquées (Batocera les ignore).

`DIR=` et `CMD=` sont interprétés comme le lanceur : guillemets retirés de
`DIR`, `\\` → `/`, composants `.` ignorés, casse ignorée ; l'exécutable de
`CMD` est le texte entre guillemets s'il commence par un guillemet, sinon le
premier mot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Clés connues du lanceur / Batocera (SPEC § 2.2).
KNOWN_KEYS = {
    "CMD", "DIR", "GAME_VERSION", "WINE", "PROTON", "RUNNER", "ARCH", "HIDRAW",
    "DXVK", "VKD3D", "D7VK", "ESYNC", "FSYNC", "VIRTUAL_DESKTOP", "LANG",
    "ENV", "SAVEDIR", "SAVEFILES",
}

# Aide du formulaire (SPEC § 2.2) : description et valeurs possibles, d'après
# la documentation du lanceur. `values` vide = saisie libre.
KEY_HELP: dict[str, dict] = {
    "CMD": {"help": "Exécutable et arguments, ex. game.exe -fullscreen ou "
                    "\"My Game.exe\" -fullscreen", "values": []},
    "DIR": {"help": "Dossier de l'exécutable, relatif à la racine de l'image", "values": []},
    "GAME_VERSION": {"help": "Version du jeu packagé (ignorée par Batocera)", "values": []},
    "WINE": {"help": "Wine de /opt/wine-<version>, ou system (Wine du système, WoW64)",
             "values": ["tkg", "system"]},
    "PROTON": {"help": "Runner Proton (nom du dossier ou version) ; défaut proton-cachyos",
               "values": ["proton-cachyos-slr", "GE-Proton8-25"]},
    "RUNNER": {"help": "Chemin absolu d'un runner personnalisé", "values": []},
    "ARCH": {"help": "Architecture Wine (défaut win64)", "values": ["win32", "win64"]},
    "HIDRAW": {"help": "1 : manettes Sony par hidraw si le jeu gère la DualSense lui-même ; "
                       "bt : DualSense Bluetooth en direct ; absent : XInput",
               "values": ["1", "bt"]},
    "DXVK": {"help": "DirectX 8-11 → Vulkan (activé par défaut)", "values": ["0", "1"]},
    "VKD3D": {"help": "DirectX 12 → Vulkan (activé par défaut)", "values": ["0", "1"]},
    "D7VK": {"help": "DirectDraw / DirectX 7 → Vulkan (activé par défaut)", "values": ["0", "1"]},
    "ESYNC": {"help": "Event synchronization", "values": ["0", "1"]},
    "FSYNC": {"help": "Futex synchronization", "values": ["0", "1"]},
    "VIRTUAL_DESKTOP": {"help": "Bureau virtuel Wine, ex. 1920x1080", "values": []},
    "LANG": {"help": "Langue (LC_ALL), ex. fr_FR.UTF-8", "values": []},
    "ENV": {"help": "Variables d'environnement, ex. VAR1=val1 VAR2=val2 (pas de "
                    "WINEDLLOVERRIDES)", "values": []},
    "SAVEDIR": {"help": "Dossier de sauvegarde du jeu, relié à saves/<système>/<jeu>",
                "values": []},
    "SAVEFILES": {"help": "Fichiers de sauvegarde séparés par ;, relatifs au prefix",
                  "values": []},
}

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def decode(data: bytes) -> tuple[str, str]:
    """UTF-8 si le fichier en est, sinon cp1252 (autorun écrit sous Windows),
    à défaut latin-1 (décode tout octet, sans perte)."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 décode tout")


def unquote(value: str) -> str:
    """Retire une paire de guillemets entourant la valeur (comme `unquote` du
    lanceur, utilisé pour DIR/SAVEDIR/SAVEFILES)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def normalize_path(value: str) -> str:
    """Chemin d'autorun → forme comparable à la liste des fichiers de l'image :
    guillemets retirés, `\\` → `/`, composants vides et `.` ignorés, minuscules."""
    parts = unquote(value.strip()).replace("\\", "/").split("/")
    return "/".join(p for p in parts if p not in ("", ".")).lower()


def exe_from_cmd(cmd: str) -> str:
    """Exécutable de `CMD=`, comme le lanceur : texte entre guillemets si la
    commande commence par un guillemet, sinon le premier mot."""
    cmd = cmd.strip()
    m = re.match(r'^"([^"]+)"', cmd)
    if m:
        return m.group(1)
    return cmd.split()[0] if cmd else ""


@dataclass
class Autorun:
    """Contenu d'un autorun, ligne à ligne."""
    lines: list[str] = field(default_factory=list)   # sans le caractère de fin de ligne
    eol: str = "\r\n"                                # "\r\n" ou "\n" (détecté)
    trailing_eol: bool = True                        # saut de ligne après la dernière ligne
    encoding: str = "utf-8"                          # conservé à la réécriture (cp1252 : Windows)

    # ------------------------------------------------------------------ read

    @classmethod
    def parse(cls, data: bytes | str) -> "Autorun":
        encoding = "utf-8"
        if isinstance(data, bytes):
            data, encoding = decode(data)
        if not data:
            return cls(lines=[], eol="\r\n", trailing_eol=True, encoding=encoding)
        # Fin de ligne majoritaire (un fichier CRLF sans saut de ligne final
        # reste CRLF ; un fichier mixte prend la fin la plus fréquente).
        crlf = data.count("\r\n")
        lf_only = data.count("\n") - crlf
        eol = "\r\n" if crlf > lf_only else "\n"
        trailing = data.endswith("\n")
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in data.split("\n")]
        if trailing:
            lines = lines[:-1]          # élément vide après le dernier \n
        return cls(lines=lines, eol=eol, trailing_eol=trailing, encoding=encoding)

    def get(self, key: str) -> str | None:
        """Première occurrence de la clé (comme le lanceur)."""
        for line in self.lines:
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                return m.group(2)
        return None

    def all(self, key: str) -> list[str]:
        return [
            m.group(2)
            for line in self.lines
            if (m := _KEY_RE.match(line)) and m.group(1) == key
        ]

    def items(self) -> list[tuple[str, str, int]]:
        """Toutes les occurrences (clé, valeur, ligne 1-indexée), dans l'ordre."""
        out = []
        for i, line in enumerate(self.lines, start=1):
            m = _KEY_RE.match(line)
            if m:
                out.append((m.group(1), m.group(2), i))
        return out

    def keys(self) -> set[str]:
        return {k for k, _, _ in self.items()}

    def unknown_keys(self) -> list[str]:
        return [k for k in sorted(self.keys()) if k not in KNOWN_KEYS]

    # ---------------------------------------------------------------- write

    def set(self, key: str, value: str) -> None:
        """Modifier la première occurrence (place conservée) ou ajouter en fin."""
        for i, line in enumerate(self.lines):
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                self.lines[i] = f"{key}={value}"
                return
        self.lines.append(f"{key}={value}")

    def remove(self, key: str) -> int:
        """Retirer toutes les occurrences de la clé. Retourne le nombre retiré."""
        kept = []
        removed = 0
        for line in self.lines:
            m = _KEY_RE.match(line)
            if m and m.group(1) == key:
                removed += 1
            else:
                kept.append(line)
        self.lines = kept
        return removed

    def render(self) -> str:
        if not self.lines:
            return ""
        return self.eol.join(self.lines) + (self.eol if self.trailing_eol else "")

    def encode(self) -> bytes:
        """Contenu à écrire dans l'image, dans l'encodage d'origine. Un
        caractère absent de cet encodage fait passer le fichier en UTF-8
        plutôt que d'être perdu."""
        try:
            return self.render().encode(self.encoding)
        except UnicodeEncodeError:
            return self.render().encode("utf-8")

    # ------------------------------------------------------------- validation

    @staticmethod
    def is_comment(line: str) -> bool:
        stripped = line.lstrip()
        return stripped.upper().startswith("REM") or stripped.startswith("#")

    def validate(self, file_paths: set[str] | None = None) -> list[dict]:
        """Validations (SPEC § 2.2).

        `file_paths` : fichiers de l'image, chemins relatifs à sa racine (sans
        `squashfs-root/`), pour vérifier DIR= et CMD= sans tenir compte de la
        casse. Les dossiers sont déduits des chemins de fichiers. Sans liste,
        seules les vérifications de forme sont faites.
        Retourne une liste de {level: warn|error, message}."""
        issues: list[dict] = []
        files = {normalize_path(p) for p in (file_paths or set())}
        dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
        dirs.add("")                    # racine

        d = self.get("DIR")
        c = self.get("CMD")
        d_norm = normalize_path(d) if d is not None else ""
        # DIR= est facultatif : sans lui, l'exécutable est à la racine
        if d is not None and files and d_norm not in dirs:
            issues.append({"level": "warn", "message": f"DIR={d} n'existe pas dans l'image"})

        if c is None:
            issues.append({"level": "warn", "message": "CMD= absent"})
        elif files:
            exe = exe_from_cmd(c)
            target = normalize_path(f"{d_norm}/{exe}" if d_norm else exe)
            if exe and target not in files:
                issues.append(
                    {"level": "warn", "message": f"exécutable {exe} introuvable (casse ignorée)"}
                )

        env = self.get("ENV") or ""
        if "WINEDLLOVERRIDES" in env:
            issues.append(
                {"level": "warn", "message": "ENV= contient WINEDLLOVERRIDES : il écraserait "
                                              "les réglages DLL du lanceur"}
            )
        for k in self.unknown_keys():
            issues.append({"level": "warn", "message": f"clé inconnue : {k} (ignorée par Batocera)"})
        return issues


def read_autorun(image_dir: Path) -> Autorun | None:
    """Lit l'autorun depuis un dossier monté. None s'il n'existe pas."""
    p = image_dir / "autorun.cmd"
    if not p.exists():
        return None
    return Autorun.parse(p.read_bytes())


def write_autorun(autorun: Autorun, path: Path) -> None:
    path.write_bytes(autorun.encode())
