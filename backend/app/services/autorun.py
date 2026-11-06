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
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

# Clés connues du lanceur / Batocera (SPEC § 2.2).
KNOWN_KEYS = {
    "CMD", "DIR", "GAME_VERSION", "WINE", "PROTON", "RUNNER", "ARCH", "HIDRAW",
    "DXVK", "VKD3D", "D7VK", "NVAPI", "VKREFLEX", "FSR", "FSR4", "ESYNC", "FSYNC",
    "VIRTUAL_DESKTOP", "LANG",
    "ENV", "SAVEDIR", "SAVEFILES", "PRE", "ATTACH", "ATTACH_PROCESS",
}

# Aide du formulaire : (description, valeurs proposées). Défauts = ceux du
# lanceur, pas ceux de Batocera (DXVK/VKD3D/D7VK actifs par défaut).
KEY_HELP: dict[str, tuple[str, list[str]]] = {
    "CMD": (("Commande lancée, relative à DIR (casse ignorée). Guillemets si le "
             "nom contient des espaces, arguments ensuite."), []),
    "DIR": ("Dossier de l'exécutable, relatif à la racine de l'image.", []),
    "GAME_VERSION": ("Version du jeu packagé (ignorée par Batocera).", []),
    "WINE": (("Runner Wine imposé : runners/<nom> joint à l'image d'abord, puis la "
              "machine ; introuvable : erreur. Défaut : wine-tkg pour un prefix Batocera."),
             ["tkg", "system"]),
    "PROTON": (("Proton imposé (prefix Proton ou jeu seul) : runners/<nom> joint à "
                "l'image d'abord, puis Heroic/Steam ; introuvable : erreur."), []),
    "RUNNER": (("Chemin absolu d'un binaire wine de la machine. Runner joint au jeu : "
                "runners/<nom>/ et PROTON= ou WINE=."), []),
    "ARCH": ("Architecture du prefix créé.", ["win32", "win64"]),
    "HIDRAW": (("Manette Sony gérée par le jeu lui-même (DualSense native) au lieu de "
                "XInput : 1 (pont USB), bt (hidraw direct), 0."), ["1", "bt", "0"]),
    "DXVK": ("D3D9-11 via Vulkan. Actif par défaut ; 0 pour wined3d.", ["1", "0"]),
    "VKD3D": ("D3D12 via Vulkan. Actif par défaut.", ["1", "0"]),
    "D7VK": ("DirectDraw/D3D7 via Vulkan. Actif par défaut.", ["1", "0"]),
    "NVAPI": (("NVAPI / DLSS (dxvk-nvapi). auto (défaut) : actif si la machine qui lance "
               "le jeu a une carte NVIDIA et dxvk-nvapi ; ignoré sur AMD."), ["auto", "1", "0"]),
    "VKREFLEX": (("Couche Reflex de dxvk-nvapi (NVIDIA). auto (défaut) : chargée si elle est "
                  "installée ; 1 force aussi NVAPI."), ["auto", "1", "0"]),
    "FSR": ("FSR de Wine en plein écran (WINE_FULLSCREEN_FSR). Défaut 0.", ["1", "0"]),
    "FSR4": (("FSR4 : pose amdxcffx64.dll dans le prefix, carte AMD seulement ; le jeu "
              "décide de s'en servir. Défaut 0."), ["1", "0"]),
    "ESYNC": ("Synchronisation esync (défaut 0).", ["1", "0"]),
    "FSYNC": ("Synchronisation fsync (défaut 0).", ["1", "0"]),
    "VIRTUAL_DESKTOP": ("Bureau virtuel Wine, ex. 1920x1080.", ["1920x1080", "1280x720"]),
    "LANG": ("Langue passée au jeu, ex. fr_FR.UTF-8.", ["fr_FR.UTF-8", "en_US.UTF-8",
                                                         "ja_JP.UTF-8"]),
    "ENV": (("Variables d'environnement supplémentaires (VAR=val VAR2=val). Fusionnées "
             "variable par variable avec celles des autres couches ; -VAR en retire une. "
             "Jamais WINEDLLOVERRIDES : il écraserait les réglages DLL du lanceur."), []),
    "SAVEDIR": ("Dossier de sauvegarde du jeu, redirigé vers saves/<système>/<jeu>.", []),
    "SAVEFILES": ("Fichiers de sauvegarde (séparés par ;), redirigés comme SAVEDIR.", []),
    "PRE": (("Commande Linux lancée une fois avant le jeu, dans DIR ; un échec arrête "
             "le lancement. Exécutée sur la machine, hors de Wine : images de confiance "
             "seulement. Ignorée par Batocera."), []),
    "ATTACH": (("Commande Linux lancée pendant le jeu (GAME_PID = son PID), arrêtée "
                "avec lui. Exécutée sur la machine, hors de Wine : images de confiance "
                "seulement. Ignorée par Batocera."), []),
    "ATTACH_PROCESS": (("Processus attendu par ATTACH (15 caractères au plus). Défaut : "
                        "l'exécutable de CMD ; à renseigner si CMD est un lanceur."), []),
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

    # `set` désigne ici la méthode Autorun.set : annotation en Collection
    def validate(self, file_paths: Collection[str] | None = None) -> list[dict]:
        """Validations (SPEC § 2.2).

        `file_paths` : fichiers de l'image, chemins relatifs à sa racine (sans
        `squashfs-root/`), pour vérifier DIR= et CMD= sans tenir compte de la
        casse. Les dossiers sont déduits des chemins de fichiers. Sans liste,
        seules les vérifications de forme sont faites.
        Retourne une liste de {level: warn|error, message}."""
        issues: list[dict] = []
        files = {normalize_path(p) for p in (file_paths or ())}
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
        process = unquote(self.get("ATTACH_PROCESS") or "")
        if process and not self.get("ATTACH"):
            issues.append({"level": "warn", "message": "ATTACH_PROCESS= sans ATTACH= : sans effet"})
        if len(process) > 15:
            issues.append({"level": "warn",
                           "message": f"ATTACH_PROCESS={process} dépasse 15 caractères : le noyau "
                                      f"tronque les noms de processus, ATTACH attendrait en vain"})
        for k in self.unknown_keys():
            issues.append({"level": "warn",
                           "message": f"clé inconnue : {k} (ignorée par Batocera)"})
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


# ------------------------------------------------------------------ surcharge
# <jeu>.wsquashfs.autorun à côté de l'image, lu par le lanceur : une clé qui y
# figure l'emporte sur celle de l'image, CLÉ= vide la ramène au défaut du
# lanceur (même règle que read_layer() de wsquashfs-launcher). ENV= se
# fusionne variable par variable (merge_env_layer du lanceur, 06/11) :
# NOM=valeur ajoute ou remplace, -NOM retire, ENV= vide retire tout.

def override_path(image: Path) -> Path:
    return image.with_name(image.name + ".autorun")


def env_tokens(raw: str) -> dict[str, str]:
    """ENV= → {nom: jeton NOM=valeur}, dans l'ordre (dernier jeton d'un nom
    retenu, à la place du premier). Les jetons -NOM ne sont pas gardés."""
    out: dict[str, str] = {}
    for tok in raw.split():
        if tok.startswith("-") and "=" not in tok:
            out.pop(tok[1:], None)
        elif (name := tok.split("=", 1)[0]):
            out[name] = tok
    return out


def merge_env(base: str | None, layer: str) -> str:
    """ENV= de `base` corrigé par l'ENV= d'une couche plus forte."""
    if not layer.strip():
        return ""
    merged = env_tokens(base or "")
    for tok in layer.split():
        if tok.startswith("-") and "=" not in tok:
            merged.pop(tok[1:], None)
        elif (name := tok.split("=", 1)[0]):
            merged[name] = tok
    return " ".join(merged.values())


def merge_override(base: Autorun | None, override: Autorun) -> Autorun:
    """Autorun effectif : celui de l'image, corrigé par la surcharge (une clé
    modifiée garde sa place, une clé ajoutée va en fin de fichier)."""
    out = Autorun(lines=list(base.lines) if base else [], eol=base.eol if base else "\r\n",
                  trailing_eol=base.trailing_eol if base else True)
    for key in dict.fromkeys(k for k, _, _ in override.items()):
        value = override.get(key) or ""
        if key == "ENV":
            value = merge_env(out.get("ENV"), value)
        if value.strip():
            out.set(key, value)
        else:
            out.remove(key)
    return out


def env_override(base: str | None, wanted: str | None) -> str | None:
    """ENV= de surcharge minimal pour passer de `base` à `wanted`, None si
    rien ne change. Variables ajoutées ou modifiées, -NOM pour les retirées ;
    ENV= vide si plus aucune variable."""
    old, new = env_tokens(base or ""), env_tokens(wanted or "")
    if old == new:
        return None
    if not new:
        return ""
    toks = [tok for name, tok in new.items() if old.get(name) != tok]
    toks += [f"-{name}" for name in old if name not in new]
    return " ".join(toks)


def override_from(base: Autorun | None, wanted: Autorun) -> Autorun:
    """Surcharge minimale pour obtenir `wanted` à partir de l'autorun de
    l'image : clés ajoutées ou modifiées, et CLÉ= pour les clés retirées."""
    base_keys = {k: base.get(k) for k, _, _ in base.items()} if base else {}
    lines = []
    for key in dict.fromkeys([*(k for k, _, _ in wanted.items()), *base_keys]):
        if key == "ENV":
            env = env_override(base_keys.get("ENV"), wanted.get("ENV"))
            if env is not None:
                lines.append(f"ENV={env}")
        elif key not in base_keys or wanted.get(key) != base_keys[key]:
            lines.append(f"{key}={wanted.get(key) or ''}")
    return Autorun(lines=lines, eol="\r\n", trailing_eol=True)


def read_autorun(image_dir: Path) -> Autorun | None:
    """Lit l'autorun depuis un dossier monté. None s'il n'existe pas."""
    p = image_dir / "autorun.cmd"
    if not p.exists():
        return None
    return Autorun.parse(p.read_bytes())


def write_autorun(autorun: Autorun, path: Path) -> None:
    path.write_bytes(autorun.render().encode("utf-8"))
