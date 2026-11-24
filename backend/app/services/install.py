"""Installer une image .wsquashfs sur les clients associés (SPEC § 2.8).

Chaque client (les mêmes que l'onglet Sauvegardes) a un dossier des jeux, géré
par son agent (`/v1/games/*`, agent v6). Le manager y envoie l'image par blocs
(PUT à partir d'un décalage : ce que le client a déjà reçu), puis la fait
valider par l'empreinte SHA-256 (commit).

- Espace : le libre doit couvrir le reste de l'envoi plus 1 Gio, sinon rien n'est envoyé.
- Reprise : une coupure ou un décalage différent → reprises (pauses croissantes,
  jusqu'à 30 min sans aucun progrès) ; à chaque reprise, le client dit ce qu'il a déjà, et l'empreinte
  est reprise là où elle en était : seul l'écart entre les deux est relu, jamais le préfixe déjà haché.
- Empreinte refusée au commit (422) : le client a effacé son envoi partiel ; un
  nouvel envoi complet, depuis 0, au plus.
- Annulation : entre deux blocs. Un client en échec n'empêche pas les autres.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import Settings
from ..models import Task
from .agent import AgentHost, AgentUnreachable, InstallChecksum, InstallOffset
from .clients import find_machine, machine_rows, open_machine
from .durable import write_atomic
from .errors import ManagerError
from .jobs import Job, JobCancelled

BLOCK_SIZE = 256 << 20          # octets par PUT
READ_SIZE = 1 << 20             # lecture locale et hachage
RETRY_PAUSES = (5, 10, 20, 30, 60)  # pause avant chaque reprise ; la dernière se répète
MAX_OUTAGE = 30 * 60            # injoignable sans progrès plus de ce temps (s) : envoi abandonné, le .part reste
RESERVE = 1 << 30               # espace libre gardé en plus de ce qui reste à envoyer
EXT = ".wsquashfs"


# ------------------------------------------------------------------ noms et tailles

def valid_name(name: object) -> bool:
    """Nom d'image installable : « Jeu.wsquashfs », sans chemin ni point initial."""
    return (isinstance(name, str) and name.endswith(EXT) and len(name) <= 255
            and not name.startswith(".") and not any(c in name for c in "/\\\x00"))


def image_name(path: Path) -> str:
    if not valid_name(path.name):
        raise ManagerError("seules les images .wsquashfs s'installent")
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


def _duration_text(seconds: float) -> str:
    """Durée lisible et arrondie : « moins d'1 min », « ~12 min », « ~1 h 20 »."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "moins d'1 min"
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes // 60} h {minutes % 60:02d}"


# Débit mesuré par machine (octets/s) : la durée estimée du dialogue « Installer sur… » part
# du dernier envoi réel (le Wi-Fi d'un Deck est loin du filaire), pas d'une valeur fixe.
RATES_FILE = "install-rates.json"
MIN_MEASURED = 64 << 20             # en dessous, un envoi est trop court pour mesurer le débit


def _rates_path(settings: Settings) -> Path:
    return settings.state_dir / RATES_FILE


def measured_rates(settings: Settings) -> dict[str, float]:
    try:
        data = json.loads(_rates_path(settings).read_text())
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v > 0} \
        if isinstance(data, dict) else {}


def _note_rate(settings: Settings, machine_id: str, sent: int, seconds: float) -> None:
    if sent < MIN_MEASURED or seconds <= 0:
        return
    rates = measured_rates(settings)
    rates[machine_id] = round(sent / seconds)
    try:
        write_atomic(_rates_path(settings), json.dumps(rates, indent=2) + "\n")
    except OSError:
        pass                        # une estimation perdue n'empêche rien


def _check(job: Job | None) -> None:
    if job:
        job.check()


def _progress(job: Job | None, phase: str, done: int, total: int) -> None:
    if job:
        job.progress(phase, done / total if total else 1.0)


# ------------------------------------------------------------------ envoi

def _hash_to(stats: dict, image: Path, target: int, job: Job | None, label: str) -> None:
    """Amène le SHA-256 de l'image jusqu'à `target` octets. L'état (`stats["digest"]`, couvrant
    `stats["hashed"]` octets) est gardé pendant toute l'installation : une reprise ne relit que
    l'écart, pas tout le préfixe. Affiche la progression et reste annulable."""
    if target < stats["hashed"]:                      # le client a moins que ce qu'on a haché : on repart de zéro
        stats["digest"], stats["hashed"] = hashlib.sha256(), 0
    if target == stats["hashed"]:
        return
    digest, done = stats["digest"], stats["hashed"]
    with image.open("rb") as f:
        f.seek(done)
        while done < target:
            _check(job)
            data = f.read(min(READ_SIZE, target - done))
            if not data:
                raise ManagerError("image modifiée pendant l'envoi")
            digest.update(data)
            done += len(data)
            stats["hashed"] = done
            if job and (done % (256 << 20) < READ_SIZE or done == target):
                _progress(job, f"vérification de ce qui est déjà reçu sur {label} : "
                               f"{_size_text(done)} / {_size_text(target)}", done, target)


def _blocks(f, length: int, digest) -> Iterator[bytes]:
    """Octets suivants de `f`, `length` au plus, hachés au fil de la lecture."""
    left = length
    while left:
        data = f.read(min(READ_SIZE, left))
        if not data:
            raise ManagerError("image modifiée pendant l'envoi")
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
    """Un essai : reprend l'envoi là où le client en est, puis valide l'image.
    Lève AgentUnreachable / InstallOffset (à reprendre) ou InstallChecksum."""
    part = int(host.games_part(name)["part_size"])
    if part > size:                     # envoi plus gros que l'image : on repart de zéro
        host.games_delete(name)
        part = 0
    if part and job:
        job.log(f"reprise de l'envoi sur {label} : {_size_text(part)} déjà reçus sur {_size_text(size)}")
    _hash_to(stats, image, part, job, label)
    with image.open("rb") as f:
        while part < size:
            _check(job)
            length = min(BLOCK_SIZE, size - part)
            f.seek(part)
            trial = stats["digest"].copy()           # ne devient l'état qu'une fois le bloc confirmé
            reply = host.games_put(name, part, _blocks(f, length, trial), length, size)
            new = int(reply.get("part_size", -1))
            if new != part + length:
                raise InstallOffset(new)
            stats["digest"], stats["hashed"] = trial, part + length
            stats["sent"] += length
            part = new
            elapsed = max(time.monotonic() - stats["t0"], 1e-3)
            rate = stats["sent"] / elapsed                    # octets/s, depuis le début de l'envoi
            left = _duration_text((size - part) / rate) if part < size else "validation"
            _progress(job, f"envoi sur {label} : {_size_text(part)} / {_size_text(size)} "
                           f"· {rate / (1 << 20):.1f}".replace(".", ",") + f" Mio/s · reste {left}", part, size)
    _hash_to(stats, image, size, job, label)
    host.games_commit(name, size, stats["digest"].hexdigest())


def _send(host: AgentHost, settings: Settings, machine_id: str, name: str, size: int, image: Path,
          job: Job | None, label: str, stats: dict) -> str:
    """Envoi avec reprises. Renvoie « installé » ou « déjà installé » (une reprise
    après un commit perdu trouve l'image déjà en place)."""
    attempt = 0
    restarted = False
    outage_at = None                  # début de l'injoignabilité en cours (None : rien ne va mal)
    sent_at_outage = 0
    while True:
        try:
            if attempt and _installed_size(host.games_list(), name) == size:
                return "déjà installé"
            _transfer(host, name, size, image, job, label, stats)
            return "installé"
        except InstallChecksum as exc:
            # Le .part a été effacé par le client : un seul nouvel envoi complet
            if restarted:
                raise ManagerError(f"empreinte refusée deux fois par {label} : envoi abandonné") from exc
            restarted = True
        except (AgentUnreachable, InstallOffset) as exc:
            now = time.monotonic()
            if outage_at is None or stats["sent"] > sent_at_outage:
                outage_at, sent_at_outage = now, stats["sent"]     # des octets sont passés : nouvelle fenêtre
            if now - outage_at > MAX_OUTAGE:
                raise ManagerError(f"envoi interrompu, reprises épuisées après {int(now - outage_at)} s "
                                   f"sans progrès : {exc}") from exc
            pause = RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)]
            if job:
                job.log(f"coupure ({exc}) : nouvel essai dans {int(pause)} s, l'envoi reprendra là où {label} en est")
            _pause(job, pause)
            attempt += 1

def _pause(job: Job | None, seconds: float) -> None:
    """Attend `seconds` en restant annulable (une annulation coupe la pause)."""
    end = time.monotonic() + seconds
    while True:
        _check(job)
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _result(machine_id: str, label: str) -> dict:
    return {"machine": label, "machine_id": machine_id, "ok": False, "message": "",
            "sent": 0, "seconds": 0.0}


def _install_one(settings: Settings, machine_id: str, name: str, size: int, image: Path,
                 job: Job | None) -> dict:
    label = _label(settings, machine_id)
    out = _result(machine_id, label)
    stats = {"sent": 0, "t0": time.monotonic(), "digest": hashlib.sha256(), "hashed": 0}
    try:
        _install_on(settings, machine_id, name, size, image, job, label, out, stats)
    except JobCancelled:
        raise
    except ManagerError as exc:
        out["message"] = str(exc)
    except OSError as exc:
        out["message"] = f"image illisible : {exc}"
    elapsed = time.monotonic() - stats["t0"]
    out["sent"] = stats["sent"]
    out["seconds"] = round(elapsed, 1)
    if out["ok"]:
        _note_rate(settings, machine_id, out["sent"], elapsed)
    return out


def _install_on(settings: Settings, machine_id: str, name: str, size: int, image: Path,
                job: Job | None, label: str, out: dict, stats: dict) -> None:
    """Corps de l'installation sur un client ; le résultat est écrit dans `out`."""
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
    """Installe l'image sur chaque client. → {name, size, results:[{machine, machine_id,
    ok, message, sent, seconds}]}. Un client en échec n'arrête pas les autres."""
    name = image_name(image_path)
    try:
        size = image_path.stat().st_size
    except OSError as exc:
        raise ManagerError(f"image illisible : {exc}") from exc
    results = []
    for machine_id in dict.fromkeys(machine_ids):
        _check(job)
        results.append(_install_one(settings, machine_id, name, size, image_path, job))
    return {"name": name, "size": size, "results": results}


# ------------------------------------------------------------------ désinstallation

def uninstall(settings: Settings, name: str, machine_ids: list[str],
              job: Job | None = None) -> dict:
    """Désinstalle le jeu des clients → {name, results:[{machine, machine_id, ok, message}]}.
    Pas installé : réussite sans effet. Jeu monté ou en cours : échec pour ce client."""
    if not valid_name(name):
        raise ManagerError(f"nom d'image invalide : {name!r}")
    results = []
    for machine_id in dict.fromkeys(machine_ids):
        _check(job)
        label = _label(settings, machine_id)
        out = {"machine": label, "machine_id": machine_id, "ok": False, "message": ""}
        try:
            reply = open_machine(settings, machine_id).games_delete(name)
        except ManagerError as exc:
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
    games:[noms], parts:[{name, size}], rate}]} ; `rate` : dernier débit mesuré vers elle
    (octets/s, 0 si aucun envoi encore). Un client injoignable est signalé seul."""
    rows = machine_rows(settings)
    rates = measured_rates(settings)

    def fetch(row: dict) -> dict:
        entry = {"id": row["id"], "name": row["name"], "ok": False, "message": "", "dir": "",
                 "set": False, "free": 0, "games": [], "parts": [], "rate": rates.get(row["id"], 0)}
        try:
            games = open_machine(settings, row["id"], timeout=15).games_list(timeout=15)
        except ManagerError as exc:
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
    """Dossier des jeux du client (« » : retour au dossier deviné) → {dir, set}.
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
            raise ManagerError("image absente de la tâche")
        result = install(settings, Path(task.image_path), machines, job)
        label = f"sur {len(machines)} client(s)"
    elif op == "uninstall":
        result = uninstall(settings, str(p.get("name") or ""), machines, job)
        label = f"de {len(machines)} client(s)"
    else:
        raise ManagerError(f"opération d'installation inconnue : {op}")
    task.params["result"] = result
    items = result["results"]
    for item in items:
        job.log(f"{item['machine']} : {item['message']}")
    failed = [item for item in items if not item["ok"]]
    if not items:
        return "rien à faire"
    if failed:
        raise ManagerError(f"{len(failed)} échec(s) sur {len(items)} ({label}) : "
                           f"{failed[0]['machine']} : {failed[0]['message']}")
    return f"{len(items)} opération(s) réussie(s) {label}"
