"""Création d'une image depuis un dossier (SPEC § 2.3).

Analyse (lecture seule) : type (prefix Batocera `system.reg`, Proton
`config_info`, jeu seul), exécutables candidats classés par score (§ 2.5 :
installeurs et rapporteurs de plantage exclus, Unreal, Unity, lanceurs .bat),
`GAME_VERSION` tirée des ressources de version, règles YAML en suggestions.

Empaquetage : comme `--pack` du lanceur (mksquashfs zstd, `.part` puis
renommage), mais l'autorun validé est injecté par un pseudo-fichier
mksquashfs : le dossier source n'est jamais modifié.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import uuid
from pathlib import Path

from ..config import Settings
from ..pe import parse_pe
from ..prefix import detect_prefix, parse_dll_overrides, read_prefix_file
from . import rebuild
from .autorun import Autorun
from .rebuild import Changes, Context, RebuildError
from .rules import Analysis, load_rules, run_rules

# Exécutables qui ne lancent pas le jeu (SPEC § 2.5)
_EXCLUDED = [
    "vcredist*", "vc_redist*", "dxsetup*", "dxwebsetup*", "ue4prereqsetup*", "ueprereqsetup*",
    "*setup*.exe", "unins*.exe", "*uninstall*.exe", "unitycrashhandler*", "crashreportclient*",
    "*crashreporter*", "*crashhandler*", "dotnetfx*", "oalinst*", "physx*", "directx*",
    "start_protected_game.exe", "easyanticheat*", "quicksfv*", "rapidcrc*", "*webhelper*",
    "scriptinterpreter*", "eosbootstrapper*",
]
# Dossiers sans exécutable de jeu : prefix, redistribuables, outils d'Unreal
_EXCLUDED_DIRS = ("drive_c/windows/", "drive_c/programdata/", "drive_c/users/")
_EXCLUDED_PARTS = {"_redist", "__redist", "redist", "_commonredist", "commonredist",
                   "easyanticheat", "engine", "installers", "installer"}
_BAT_LAUNCHERS = ("launch.bat", "game.bat", "start.bat", "run.bat")
_SUSPICIOUS_BAT = [(re.compile(r"\btaskill\b", re.I), "« taskill » au lieu de « taskkill »")]
# Lecture complète des PE : seulement pour les mieux classés
_PE_READ_TOP = 6
_PE_MAX_BYTES = 512 * 1024 * 1024


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def default_name(folder: Path) -> str:
    return folder.name[:-5] if folder.name.lower().endswith(".wine") else folder.name


def walk(folder: Path) -> tuple[list[str], dict[str, int], int]:
    """Entrées du dossier (chemins relatifs, dossiers compris, comme
    `unsquashfs -l`), taille des fichiers, taille totale."""
    entries: list[str] = []
    sizes: dict[str, int] = {}
    total = 0
    for cur, dirnames, filenames in os.walk(folder):
        rel_dir = Path(cur).relative_to(folder).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames.sort()
        for d in dirnames:
            entries.append(prefix + d)
        for f in sorted(filenames):
            rel = prefix + f
            entries.append(rel)
            try:
                st = (Path(cur) / f).lstat()
            except OSError:
                continue
            if not (Path(cur) / f).is_symlink():
                sizes[rel] = st.st_size
                total += st.st_size
    return entries, sizes, total


def _excluded(rel: str) -> bool:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    return (low.startswith(_EXCLUDED_DIRS) or any(fnmatch.fnmatchcase(name, p) for p in _EXCLUDED)
            or any(part in _EXCLUDED_PARTS for part in low.split("/")[:-1]))


def candidates(entries: list[str], sizes: dict[str, int], name: str) -> list[dict]:
    """Exécutables candidats classés par score, avec la raison du score."""
    lower = {e.lower() for e in entries}
    exes = [e for e in entries if e.lower().endswith(".exe") and e in sizes and not _excluded(e)]
    bats = [e for e in entries if e.rsplit("/", 1)[-1].lower() in _BAT_LAUNCHERS]
    biggest = max((sizes[e] for e in exes), default=0)
    target = _norm(name)
    out = []
    for e in exes + bats:
        d, _, base = e.rpartition("/")
        stem = _norm(base.rsplit(".", 1)[0])
        score, why = 0, []
        if target and stem == target:
            score += 40
            why.append("nom du jeu")
        elif target and stem and (target in stem or stem in target):
            score += 25
            why.append("nom proche du jeu")
        if e in sizes and biggest and sizes[e] == biggest:
            score += 20
            why.append("plus gros exécutable")
        depth = e.count("/")
        score += max(0, 10 - 3 * depth)
        sibling = (d + "/" if d else "")
        if (sibling + "engine").lower() in lower:
            score += 15
            why.append("lanceur Unreal (à côté de Engine/)")
        # Unreal : l'exécutable de <Jeu>/Binaries/Win64 passe avant le lanceur
        # racine (qui ne fait que le relancer), le lanceur reste proposé
        if fnmatch.fnmatchcase(e.lower(), "*/binaries/win64/*.exe"):
            score += 40
            why.append("exécutable Unreal (Binaries/Win64)")
        if "server" in stem:
            score -= 20
            why.append("serveur dédié ?")
        if (sibling + "unityplayer.dll").lower() in lower:
            score += 35
            why.append("à côté de UnityPlayer.dll (Unity)")
        if e.lower().endswith(".bat"):
            score += 15
            why.append("lanceur .bat")
        out.append({"path": e, "dir": d, "exe": base, "size": sizes.get(e, 0), "score": score,
                    "why": why})
    out.sort(key=lambda c: (-c["score"], -c["size"], c["path"]))
    return out


def _read_pe(folder: Path, cand: dict) -> None:
    """Complète un candidat .exe : bits, sous-système, version, imports."""
    p = folder / cand["path"]
    try:
        if p.stat().st_size > _PE_MAX_BYTES:
            return
        pe = parse_pe(p.read_bytes(), cand["path"])
    except OSError:
        return
    if not pe.is_pe:
        cand["why"].append("pas un exécutable Windows lisible")
        cand["score"] -= 30
        return
    cand["bits"] = pe.bits
    cand["subsystem"] = pe.subsystem
    cand["large_address_aware"] = pe.large_address_aware
    cand["version"] = (pe.product_version or pe.file_version or "").strip() or None
    cand["product_name"] = pe.product_name
    cand["imports"] = sorted(pe.imports)
    if pe.subsystem == "console":
        cand["score"] -= 10
        cand["why"].append("console")


def _bat_notes(folder: Path, cand: dict) -> None:
    try:
        text = (folder / cand["path"]).read_bytes()[:20000].decode("utf-8", errors="replace")
    except OSError:
        return
    cand["content"] = text
    cand["warnings"] = [msg for pat, msg in _SUSPICIOUS_BAT if pat.search(text)]


def cmd_for(exe: str) -> str:
    return f'"{exe}"' if " " in exe else exe


def analyze(folder: Path, rules_file: Path, saves_dir: Path) -> dict:
    """Analyse d'un dossier à empaqueter."""
    if not folder.is_dir():
        raise RebuildError(f"{folder} n'est pas un dossier")
    entries, sizes, total = walk(folder)
    name = default_name(folder)
    prefix = detect_prefix(entries, lambda n: read_prefix_file(folder, n))
    cands = candidates(entries, sizes, name)
    for c in cands[:_PE_READ_TOP]:
        if c["path"].lower().endswith(".exe"):
            _read_pe(folder, c)
        else:
            _bat_notes(folder, c)
    cands.sort(key=lambda c: (-c["score"], -c["size"], c["path"]))
    for c in cands:
        c["cmd"] = cmd_for(c["exe"])

    existing = folder / "autorun.cmd"
    autorun = Autorun.parse(existing.read_bytes()) if existing.is_file() else None
    best = cands[0] if cands else None
    if autorun is None:
        autorun = Autorun(lines=[], eol="\r\n")
        if best:
            if best["dir"]:
                autorun.set("DIR", best["dir"])
            autorun.set("CMD", best["cmd"])
        if best and best.get("version"):
            autorun.set("GAME_VERSION", best["version"])
        if prefix.get("arch") == "win32":
            autorun.set("ARCH", "win32")

    suggestions = []
    if best:
        overrides = {}
        user_reg = read_prefix_file(folder, "user.reg")
        if user_reg:
            overrides = parse_dll_overrides(user_reg.decode("utf-8", errors="replace"))
        a = Analysis(entries, imports=set(best.get("imports", [])), exe_name=best["exe"],
                     exe_dir=best["dir"], exe_bits=best.get("bits"),
                     exe_large_address_aware=best.get("large_address_aware", False),
                     prefix_type=prefix["type"], proton_version=prefix.get("proton_version"),
                     arch=prefix.get("arch"), version_resource=best.get("version"),
                     dll_overrides=overrides)
        suggestions = [r.model_dump() for r in run_rules(load_rules(rules_file), a)]

    warnings = []
    if (saves_dir / name).is_dir():
        warnings.append(f"des sauvegardes existent déjà pour « {name} » ({saves_dir / name}) : "
                        "elles seront appliquées par-dessus la nouvelle image")
    if not cands:
        warnings.append("aucun exécutable trouvé")

    return {
        "folder": str(folder),
        "name": name,
        "type": prefix["type"],
        "arch": prefix.get("arch"),
        "proton_version": prefix.get("proton_version"),
        "files": len(entries),
        "size": total,
        "has_autorun": existing.is_file(),
        "candidates": cands[:20],
        "autorun": {"lines": autorun.lines, "eol": autorun.eol,
                    "trailing_eol": autorun.trailing_eol, "encoding": autorun.encoding},
        "issues": autorun.validate(set(entries)),
        "suggestions": suggestions,
        "warnings": warnings,
    }


def pack(settings: Settings, folder: Path, dest: Path, autorun: bytes,
         ctx: Context | None = None) -> None:
    """Empaquette `folder` en `dest` avec `autorun` comme autorun.cmd."""
    ctx = ctx or Context()
    if dest.exists():
        raise RebuildError(f"{dest.name} existe déjà (jamais écrasé)")
    if not folder.is_dir():
        raise RebuildError(f"{folder} n'est pas un dossier")
    entries, _, total = walk(folder)
    part = rebuild.part_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    # Pire cas : rien ne se compresse
    rebuild._check_space(dest.parent, total + rebuild._SPACE_MARGIN, str(dest.parent))
    work = settings.tmp_dir / f"pack-{uuid.uuid4().hex[:12]}"
    try:
        work.mkdir(parents=True)
        staged = work / "autorun.cmd"
        staged.write_bytes(autorun)
        pseudo = work / "pseudo"
        pseudo.write_text(f"autorun.cmd f 644 {os.getuid()} {os.getgid()} "
                          f"cat {shlex.quote(str(staged))}\n")
        exclude = work / "exclude.txt"
        exclude.write_text("autorun.cmd\n")         # celui du dossier, s'il existe
        ctx.log(f"empaquetage de {folder} ({total / 1e9:.1f} Go, {len(entries)} entrées)")
        rebuild._mksquashfs(settings, folder, part, exclude, ctx, extra=["-pf", str(pseudo)])
        ctx.progress(1.0)
        changes = Changes(files={"autorun.cmd": autorun})
        rebuild.verify(settings, part, rebuild.expected_listing(entries, changes), changes, ctx)
        if ctx.cancel.is_set():
            raise rebuild.Cancelled("annulé")
        if dest.exists():
            raise RebuildError(f"{dest.name} est apparu pendant l'empaquetage")
        os.replace(part, dest)
        ctx.log(f"créée : {dest} ({dest.stat().st_size / 1e6:.0f} Mo)")
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
