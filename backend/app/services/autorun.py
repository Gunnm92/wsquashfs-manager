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

# Aide du formulaire : (description, valeurs proposées). Défauts = ceux du
# lanceur, pas ceux de Batocera (DXVK/VKD3D/D7VK actifs par défaut).
KEY_HELP: dict[str, tuple[str, list[str]]] = {
    "CMD": (("Commande lancée, relative à DIR (casse ignorée). Guillemets si le "
             "nom contient des espaces, arguments ensuite."), []),
    "DIR": ("Dossier de l'exécutable, relatif à la racine de l'image.", []),
    "GAME_VERSION": ("Version du jeu packagé (ignorée par Batocera).", []),
    "WINE": ("Runner Wine imposé. Défaut : wine-tkg pour un prefix Batocera.",
             ["tkg", "system"]),
    "PROTON": ("Version de Proton imposée (prefix Proton ou jeu seul).", []),
    "RUNNER": ("Runner personnalisé (chemin ou nom).", []),
    "ARCH": ("Architecture du prefix créé.", ["win32", "win64"]),
    "HIDRAW": ("1 : manettes en hidraw (DualSense natif) au lieu de XInput.", ["1", "0"]),
    "DXVK": ("D3D9-11 via Vulkan. Actif par défaut ; 0 pour wined3d.", ["1", "0"]),
    "VKD3D": ("D3D12 via Vulkan. Actif par défaut.", ["1", "0"]),
    "D7VK": ("DirectDraw/D3D7 via Vulkan. Actif par défaut.", ["1", "0"]),
    "ESYNC": ("Synchronisation esync (défaut 0).", ["1", "0"]),
    "FSYNC": ("Synchronisation fsync (défaut 0).", ["1", "0"]),
    "VIRTUAL_DESKTOP": ("Bureau virtuel Wine, ex. 1920x1080.", ["1920x1080", "1280x720"]),
    "LANG": ("Langue passée au jeu, ex. fr_FR.UTF-8.", ["fr_FR.UTF-8", "en_US.UTF-8",
                                                         "ja_JP.UTF-8"]),
    "ENV": (("Variables d'environnement supplémentaires (VAR=val VAR2=val). "
             "Jamais WINEDLLOVERRIDES : il écraserait les réglages DLL du lanceur."), []),
    "SAVEDIR": ("Dossier de sauvegarde du jeu, redirigé vers saves/<système>/<jeu>.", []),
    "SAVEFILES": ("Fichiers de sauvegarde (séparés par ;), redirigés comme SAVEDIR.", []),
}

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def decode_autorun(data: bytes) -> tuple[str, str]:
    """Octets → (texte, encodage). UTF-8 si valide, sinon latin-1 : un
    autorun écrit sous Windows (cp1252, commentaires accentués) est relu et
    réécrit octet pour octet au lieu d'être corrompu."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


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

    # ------------------------------------------------------------------ read

    @classmethod
    def parse(cls, data: bytes | str) -> Autorun:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        if not data:
            return cls(lines=[], eol="\r\n", trailing_eol=True)
        # Fin de ligne majoritaire (un fichier CRLF sans saut de ligne final
        # reste CRLF ; un fichier mixte prend la fin la plus fréquente).
        crlf = data.count("\r\n")
        lf_only = data.count("\n") - crlf
        eol = "\r\n" if crlf > lf_only else "\n"
        trailing = data.endswith("\n")
        lines = [ln.removesuffix("\r") for ln in data.split("\n")]
        if trailing:
            lines = lines[:-1]          # élément vide après le dernier \n
        return cls(lines=lines, eol=eol, trailing_eol=trailing)

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

    def encode(self, encoding: str = "utf-8") -> bytes:
        return self.render().encode(encoding)

    def render(self) -> str:
        if not self.lines:
            return ""
        return self.eol.join(self.lines) + (self.eol if self.trailing_eol else "")

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
        # DIR absent : l'exécutable est cherché à la racine (comme le lanceur)
        if d is not None and files and d_norm not in dirs:
            issues.append({"level": "warn", "message": f"DIR={d} n'existe pas dans l'image"})

        if c is None:
            issues.append({"level": "error", "message": "CMD= absent : le lanceur refusera "
                                                         "de démarrer le jeu"})
        elif files:
            exe = exe_from_cmd(c)
            # Le lanceur essaie le chemin depuis la racine, puis depuis DIR
            targets = {normalize_path(exe), normalize_path(f"{d_norm}/{exe}")}
            if exe and not targets & files:
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


def fill_template(template: str, name: str, system: str | None) -> str:
    """Variables d'un modèle d'autorun : {name} (nom de l'image), {system}."""
    return template.replace("{name}", name).replace("{system}", system or "")


def apply_ops(current: Autorun | None, ops: list[dict], name: str,
              system: str | None) -> Autorun:
    """Applique des opérations d'édition à une copie de l'autorun.

    ops : {"op": "set", "key", "value"} | {"op": "remove", "key"} |
    {"op": "replace", "template"}. Un remplacement garde la fin de ligne de
    l'autorun existant (CRLF sinon, comme les autoruns Batocera)."""
    base = current or Autorun()
    out = Autorun(lines=list(base.lines), eol=base.eol, trailing_eol=base.trailing_eol)
    for op in ops:
        kind = op["op"]
        if kind == "set":
            out.set(op["key"], fill_template(op["value"], name, system))
        elif kind == "remove":
            out.remove(op["key"])
        elif kind == "replace":
            text = fill_template(op["template"], name, system).replace("\r\n", "\n")
            replaced = Autorun.parse(text)
            out = Autorun(lines=replaced.lines, eol=base.eol, trailing_eol=True)
        else:
            raise ValueError(f"opération inconnue : {kind}")
    return out


def read_autorun(image_dir: Path) -> Autorun | None:
    """Lit l'autorun depuis un dossier monté. None s'il n'existe pas."""
    p = image_dir / "autorun.cmd"
    if not p.exists():
        return None
    return Autorun.parse(p.read_bytes())


def write_autorun(autorun: Autorun, path: Path) -> None:
    path.write_bytes(autorun.render().encode("utf-8"))
