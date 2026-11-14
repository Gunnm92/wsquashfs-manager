"""Installer une image .wsquashfs sur les machines associées (SPEC § 2.8).

Chaque machine (les mêmes que l'onglet Sauvegardes) a un dossier des jeux, géré
par son agent (`/v1/games/*`, agent v6). Le manager y envoie l'image par blocs
(PUT à partir d'un décalage : ce que la machine a déjà reçu), puis la fait
valider par l'empreinte SHA-256 (commit).

- Espace : le libre doit couvrir le reste de l'envoi plus 1 Gio, sinon rien n'est envoyé.
- Reprise : une coupure ou un décalage différent → jusqu'à 5 reprises (pauses
  croissantes) ; à chaque reprise, la machine dit ce qu'elle a déjà, et l'empreinte
  est recalculée depuis le début de l'image jusqu'à cet endroit.
- Empreinte refusée au commit (422) : la machine a effacé son envoi partiel ; un
  nouvel envoi complet, depuis 0, au plus.
- Annulation : entre deux blocs. Une machine en échec n'empêche pas les autres.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import Settings
from ..models import Task
from .rebuild import Job, RebuildCancelled, RebuildError
from .saves import find_machine, machine_rows, open_machine
from .trial import AgentHost, AgentUnreachable, InstallChecksum, InstallOffset

BLOCK_SIZE = 256 << 20          # octets par PUT
READ_SIZE = 1 << 20             # lecture locale et hachage
RETRY_PAUSES = (1, 2, 4, 8, 16)  # une reprise par pause : 5 au plus
RESERVE = 1 << 30               # espace libre gardé en plus de ce qui reste à envoyer
EXT = ".wsquashfs"


# ------------------------------------------------------------------ noms et tailles

def valid_name(name: object) -> bool:
    """Nom d'image installable : « Jeu.wsquashfs », sans chemin ni point initial."""
    return (isinstance(name, str) and name.endswith(EXT) and len(name) <= 255
            and not name.startswith(".") and not any(c in name for c in "/\\\x00"))


def image_name(path: Path) -> str:
    if not valid_name(path.name):
        raise RebuildError("seules les images .wsquashfs s'installent")
    return path.name


def _size_text(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} Gio".replace(".", ",")
    return f"{n / (1 << 20):.0f} Mio"


def _label(settings: Settings, machine_id: str) -> str:
    try:
        return find_machine(settings, machine_id)["name"]
    except KeyError:
        return machine_id


def _check(job: Job | None) -> None:
    if job:
        job.check()


def _progress(job: Job | None, phase: str, done: int, total: int) -> None:
    if job:
        job.progress(phase, done / total if total else 1.0)


# ------------------------------------------------------------------ envoi

def _digest_prefix(image: Path, upto: int):
    """SHA-256 des `upto` premiers octets de l'image (lus depuis le disque)."""
    digest = hashlib.sha256()
    left = upto
    with image.open("rb") as f:
        while left:
            data = f.read(min(READ_SIZE, left))
            if not data:
                raise RebuildError("image modifiée pendant l'envoi")
            digest.update(data)
            left -= len(data)
    return digest


def _blocks(f, length: int, digest) -> Iterator[bytes]:
    """Octets suivants de `f`, `length` au plus, hachés au fil de la lecture."""
    left = length
    while left:
        data = f.read(min(READ_SIZE, left))
        if not data:
            raise RebuildError("image modifiée pendant l'envoi")
        digest.update(data)
        left -= len(data)
        yield data


def _installed_size(games: dict, name: str) -> int | None:
    for entry in games.get("games") or []:
        if entry.get("name") == name:
            return int(entry.get("size") or 0)
    return None


def _transfer(host: AgentHost, name: str, size: int, image: Path, job: Job | None,
              label: str, stats: dict) -> None:
    """Un essai : reprend l'envoi là où la machine en est, puis valide l'image.
    Lève AgentUnreachable / InstallOffset (à reprendre) ou InstallChecksum."""
    part = int(host.games_part(name)["part_size"])
    if part > size:                     # envoi plus gros que l'image : on repart de zéro
        host.games_delete(name)
        part = 0
    digest = _digest_prefix(image, part)
    with image.open("rb") as f:
        while part < size:
            _check(job)
            length = min(BLOCK_SIZE, size - part)
            f.seek(part)
            reply = host.games_put(name, part, _blocks(f, length, digest), length, size)
            new = int(reply.get("part_size", -1))
            if new != part + length:
                raise InstallOffset(new)
            stats["sent"] += length
            part = new
            elapsed = max(time.monotonic() - stats["t0"], 1e-3)
            rate = stats["sent"] / elapsed / (1 << 20)
            _progress(job, f"envoi sur {label} : {_size_text(part)} / {_size_text(size)} "
                           f"· {rate:.1f} Mio/s", part, size)
    host.games_commit(name, size, digest.hexdigest())


def _send(host: AgentHost, settings: Settings, machine_id: str, name: str, size: int, image: Path,
          job: Job | None, label: str, stats: dict) -> str:
    """Envoi avec reprises. Renvoie « installé » ou « déjà installé » (une reprise
    après un commit perdu trouve l'image déjà en place)."""
    attempt = 0
    restarted = False
    while True:
        try:
            if attempt and _installed_size(host.games_list(), name) == size:
                return "déjà installé"
            _transfer(host, name, size, image, job, label, stats)
            return "installé"
        except InstallChecksum as exc:
            # Le .part a été effacé par la machine : un seul nouvel envoi complet
            if restarted:
                raise RebuildError(f"empreinte refusée deux fois par {label} : envoi abandonné") from exc
            restarted = True
        except (AgentUnreachable, InstallOffset) as exc:
            if attempt >= len(RETRY_PAUSES):
                raise RebuildError(f"envoi interrompu, reprises épuisées : {exc}") from exc
            time.sleep(RETRY_PAUSES[attempt])
            attempt += 1
            _check(job)


def _result(machine_id: str, label: str) -> dict:
    return {"machine": label, "machine_id": machine_id, "ok": False, "message": "",
            "sent": 0, "seconds": 0.0}


def _install_one(settings: Settings, machine_id: str, name: str, size: int, image: Path,
                 job: Job | None) -> dict:
    label = _label(settings, machine_id)
    out = _result(machine_id, label)
    stats = {"sent": 0, "t0": time.monotonic()}
    try:
        _install_on(settings, machine_id, name, size, image, job, label, out, stats)
    except RebuildCancelled:
        raise
    except RebuildError as exc:
        out["message"] = str(exc)
    except OSError as exc:
        out["message"] = f"image illisible : {exc}"
    out["sent"] = stats["sent"]
    out["seconds"] = round(time.monotonic() - stats["t0"], 1)
    return out


def _install_on(settings: Settings, machine_id: str, name: str, size: int, image: Path,
                job: Job | None, label: str, out: dict, stats: dict) -> None:
    """Corps de l'installation sur une machine ; le résultat est écrit dans `out`."""
    host = open_machine(settings, machine_id)
    games = host.games_list()
    installed = _installed_size(games, name)
    if installed is not None:
        if installed == size:
            out.update(ok=True, message=f"déjà installé sur {label}")
        else:
            out["message"] = (f"une autre version de {name} est installée sur {label} "
                              f"({_size_text(installed)}) : la désinstaller d'abord")
        return
    part = int(host.games_part(name)["part_size"])
    existing = part if part <= size else 0          # un envoi plus gros sera effacé
    free = int(games.get("free") or 0)
    need = size - existing + RESERVE
    if free + existing < need:
        out["message"] = (f"espace insuffisant sur {label} (libre : {_size_text(free + existing)}, "
                          f"il en faut {_size_text(need)})")
        return
    outcome = _send(host, settings, machine_id, name, size, image, job, label, stats)
    out.update(ok=True, message=(f"installé sur {label}" if outcome == "installé"
                                 else f"déjà installé sur {label}"))


def install(settings: Settings, image_path: Path, machine_ids: list[str],
            job: Job | None = None) -> dict:
    """Installe l'image sur chaque machine. → {name, size, results:[{machine, machine_id,
    ok, message, sent, seconds}]}. Une machine en échec n'arrête pas les autres."""
    name = image_name(image_path)
    try:
        size = image_path.stat().st_size
    except OSError as exc:
        raise RebuildError(f"image illisible : {exc}") from exc
    results = []
    for machine_id in dict.fromkeys(machine_ids):
        _check(job)
        results.append(_install_one(settings, machine_id, name, size, image_path, job))
    return {"name": name, "size": size, "results": results}


# ------------------------------------------------------------------ désinstallation

def uninstall(settings: Settings, name: str, machine_ids: list[str],
              job: Job | None = None) -> dict:
    """Désinstalle le jeu des machines → {name, results:[{machine, machine_id, ok, message}]}.
    Pas installé : réussite sans effet. Jeu monté ou en cours : échec pour cette machine."""
    if not valid_name(name):
        raise RebuildError(f"nom d'image invalide : {name!r}")
    results = []
    for machine_id in dict.fromkeys(machine_ids):
        _check(job)
        label = _label(settings, machine_id)
        out = {"machine": label, "machine_id": machine_id, "ok": False, "message": ""}
        try:
            reply = open_machine(settings, machine_id).games_delete(name)
        except RebuildError as exc:
            out["message"] = str(exc)
        else:
            if reply.get("removed"):
                out.update(ok=True, message=f"désinstallé de {label}")
            else:
                out.update(ok=True, message=f"pas installé sur {label}")
        results.append(out)
    return {"name": name, "results": results}


# ------------------------------------------------------------------ états et dossier

def status(settings: Settings) -> dict:
    """Jeux installés par machine : {machines:[{id, name, ok, message, dir, set, free,
    games:[noms], parts:[{name, size}]}]}. Une machine injoignable est signalée seule."""
    rows = machine_rows(settings)

    def fetch(row: dict) -> dict:
        entry = {"id": row["id"], "name": row["name"], "ok": False, "message": "", "dir": "",
                 "set": False, "free": 0, "games": [], "parts": []}
        try:
            games = open_machine(settings, row["id"], timeout=15).games_list(timeout=15)
        except RebuildError as exc:
            entry["message"] = str(exc)
            return entry
        entry.update(
            ok=True, dir=str(games.get("dir") or ""), set=bool(games.get("set")),
            free=int(games.get("free") or 0),
            games=[str(g.get("name")) for g in games.get("games") or []],
            parts=[{"name": str(p.get("name")), "size": int(p.get("size") or 0)}
                   for p in games.get("parts") or []])
        return entry

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as pool:
        machines = list(pool.map(fetch, rows))
    return {"machines": machines}


def set_dir(settings: Settings, machine_id: str, directory: str) -> dict:
    """Dossier des jeux de la machine (« » : retour au dossier deviné) → {dir, set}.
    AgentRejected (400) : message de l'agent, par exemple hors du HOME."""
    return open_machine(settings, machine_id, timeout=30).games_set_dir(directory)


# ------------------------------------------------------------------ tâche

def run_install(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « install » (file de tâches).
    params : {op: install, machines} avec l'image de la tâche (image_path) ;
    {op: uninstall, name, machines}. Résultat dans params.result ; un échec partiel
    fait échouer la tâche (le détail reste lisible)."""
    p = task.params
    op = p.get("op")
    machines = list(p.get("machines") or [])
    if op == "install":
        if not task.image_path:
            raise RebuildError("image absente de la tâche")
        result = install(settings, Path(task.image_path), machines, job)
        label = f"sur {len(machines)} machine(s)"
    elif op == "uninstall":
        result = uninstall(settings, str(p.get("name") or ""), machines, job)
        label = f"de {len(machines)} machine(s)"
    else:
        raise RebuildError(f"opération d'installation inconnue : {op}")
    task.params["result"] = result
    items = result["results"]
    for item in items:
        job.log(f"{item['machine']} : {item['message']}")
    failed = [item for item in items if not item["ok"]]
    if not items:
        return "rien à faire"
    if failed:
        raise RebuildError(f"{len(failed)} échec(s) sur {len(items)} ({label}) : "
                           f"{failed[0]['machine']} : {failed[0]['message']}")
    return f"{len(items)} opération(s) réussie(s) {label}"
