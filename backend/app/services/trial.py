"""Essai chronométré d'un jeu, diagnostic et maintenance du lanceur (SPEC § 2.5, étape 7).

Le client d'essai est là où tourne wsquashfs-launcher (réglage game_host) :
- « local » : le serveur du manager, commandes lancées directement ;
- « agent » : wsquashfs-launcher --agent sur ce client ou sur le réseau ;
- « off » : lancement d'essai, --doctor et runners installés indisponibles.
Les clients eux-mêmes (`LocalHost`, `AgentHost`) sont dans `agent.py` ; ce
module choisit celui qui est configuré (`game_host`).

Le jeu y est lancé par wsquashfs-launcher (environnement de la session
graphique : celui du processus session_process), observé pendant une durée
donnée, puis arrêté. Rapport : le jeu a-t-il tenu, ses fenêtres (minimisée ?), les
erreurs connues du journal, une capture d'écran.

Garde-fous (leçons des essais faits à la main, 01/10) :
- refus si un jeu .wsquashfs tourne déjà sur le client d'essai (lancements
  croisés : les montages de l'un et de l'autre se gêneraient) ;
- processus du jeu trouvés par leur WINEPREFIX et leur ligne de commande,
  jamais par leur nom (un jeu peut renommer son processus) ;
- arrêt par PID, jamais `pkill -f` (qui se trouve lui-même).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models import Task
from .agent import AgentHost, LocalHost
from .errors import ManagerError
from .jobs import Job

# Erreurs connues dans la sortie du lanceur et de Wine (SPEC § 2.5)
KNOWN_ERRORS = [
    (r"SOCK_RAW", "socket ICMP refusé (relais fakeping ?)"),
    (r"install_bpf", "émulation seccomp en échec (pile illimitée ? Proton)"),
    (r"out of memory|Cannot allocate memory", "mémoire insuffisante"),
    (r"page fault", "erreur de page"),
    (r"Assert", "assertion du jeu"),
    (r"Unhandled exception|NtRaiseException Unhandled", "exception non gérée"),
    (r"err:module:import_dll", "DLL manquante"),
    (r"could not load|cannot find", "fichier introuvable"),
    (r"Erreur :", "erreur du lanceur"),
]


@dataclass
class TrialReport:
    elapsed: int = 0
    launcher_alive: bool = False
    game_seen: bool = False
    game: list[str] = field(default_factory=list)
    windows: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    left: str = ""
    log: str = ""

    @property
    def verdict(self) -> tuple[bool, str]:
        if not self.game_seen:
            return False, "aucun processus de jeu vu"
        if not self.launcher_alive:
            return False, f"le jeu s'est arrêté au bout de {self.elapsed} s"
        hidden = [w for w in self.windows if "HIDDEN" in w["state"]]
        if hidden:
            return False, (f"le jeu tourne mais sa fenêtre est minimisée "
                           f"(« {hidden[0]['name']} ») : écran noir probable")
        return True, f"le jeu a tenu {self.elapsed} s"

    def to_dict(self) -> dict:
        ok, why = self.verdict
        return {"ok": ok, "verdict": why, "elapsed": self.elapsed,
                "launcher_alive": self.launcher_alive, "game_seen": self.game_seen,
                "game": self.game, "windows": self.windows, "errors": self.errors,
                "left": self.left, "log_tail": self.log[-6000:]}


def report_from_json(data: dict) -> TrialReport:
    """Rapport du lanceur (`wsquashfs-launcher --trial`, JSON) → TrialReport."""
    log = data.get("log", "")
    left = data.get("left") or {}
    report = TrialReport(
        elapsed=int(data.get("elapsed", 0)),
        launcher_alive=bool(data.get("launcher_alive")),
        game_seen=bool(data.get("game_seen")),
        game=list(data.get("games") or []),
        windows=[{"id": w.get("id", ""), "name": w.get("name", ""), "state": w.get("state", "")}
                 for w in data.get("windows") or []],
        left=f"{left.get('procs', 0)} {left.get('mounts', 0)}",
        log=log,
    )
    seen = set()
    for pattern, meaning in KNOWN_ERRORS:
        for line in log.splitlines():
            if re.search(pattern, line, re.IGNORECASE) and (pattern, line) not in seen:
                seen.add((pattern, line))
                report.errors.append({"meaning": meaning, "line": line.strip()[:240]})
                break
    return report


GAME_HOST_LABELS = {"off": "désactivée", "local": "ce serveur", "agent": "agent réseau"}


def game_host(settings: Settings, timeout: float = 120) -> LocalHost | AgentHost:
    """Client d'essai configuré ; ManagerError s'il est désactivé."""
    if settings.game_host == "local":
        return LocalHost(settings, timeout)
    if settings.game_host == "agent" and settings.agent_url:
        return AgentHost(settings, timeout)
    raise ManagerError("client d'essai non choisi : Sauvegardes › menu d'un client › « Utiliser pour les essais »")


# Maintenance du lanceur (menu d'un client d'essai) : liste fermée, comme celle de l'agent.
# Table action → (arguments de la ligne de commande, règle sur le jeu) :
#   "obligatoire" : une image (fiche du jeu) est requise (save) ;
#   "facultatif"  : sans image, tous les jeux ; avec une image, ce seul jeu (sync) ;
#   "interdit"    : aucune image (list, umount, clean, saves, sync-status).
# Mode agent : même action dans le corps {"action", "rel"} de POST /v1/maintenance.
MAINTENANCE_COMMANDS: dict[str, tuple[tuple[str, ...], str]] = {
    "list": (("--list",), "interdit"),
    "umount": (("--umount",), "interdit"),
    "clean": (("--clean",), "interdit"),
    "save": (("--save",), "obligatoire"),
    "saves": (("--saves",), "interdit"),
    "sync": (("--sync",), "facultatif"),
    "sync-status": (("--sync", "status"), "interdit"),
}
MAINTENANCE_ACTIONS = tuple(MAINTENANCE_COMMANDS)


def maintenance_game_rule(action: str) -> str:
    """Règle sur le jeu d'une action : obligatoire, facultatif ou interdit."""
    if action not in MAINTENANCE_COMMANDS:
        raise ManagerError("action de maintenance non autorisée")
    return MAINTENANCE_COMMANDS[action][1]


def maintenance(settings: Settings, action: str, image: Path | None = None) -> dict:
    """Lance une action de maintenance du lanceur sur le client d'essai."""
    rule = maintenance_game_rule(action)
    flags, _ = MAINTENANCE_COMMANDS[action]
    shown = " ".join(flags)
    if rule == "obligatoire" and image is None:
        raise ManagerError(f"{shown} demande un jeu")
    if rule == "interdit" and image is not None:
        raise ManagerError(f"{shown} ne prend pas de jeu")
    host = game_host(settings, 900)
    target = host.path_of(image) if image is not None else None
    if isinstance(host, AgentHost):
        body = {"action": action, **({"rel": target} if target else {})}
        return host._call("POST", "/v1/maintenance", json=body, timeout=900).json()
    args = [*flags, *([target] if target else [])]
    # Couleurs forcées : la sortie est affichée en couleurs par le manager (ansiToHtml).
    code, out = host.exec(["bash", "-lc", 'CLICOLOR_FORCE=1 wsquashfs-launcher "$@"', "wsfs-maint", *args])
    return {"action": action, "code": code, "output": out.decode("utf-8", "replace")}


def host_status(settings: Settings) -> dict:
    """Voyant du client d'essai : ok (connectée), ko (injoignable ou sans
    lanceur) ou off (désactivée). Test léger, sans --doctor."""
    if settings.game_host in (None, "off"):
        return {"state": "off", "name": GAME_HOST_LABELS["off"], "detail": ""}
    name = host_name(settings)
    try:
        host = game_host(settings, 15)
        if isinstance(host, AgentHost):
            info = host.info()
            return {"state": "ok", "name": name,
                    "detail": f"{info.get('host', '')} · utilisateur {info.get('user', '')} · agent {info.get('version', '')}"}
        code, out = host.exec(["bash", "-lc", "command -v wsquashfs-launcher"])
        if code != 0:
            return {"state": "ko", "name": name, "detail": "wsquashfs-launcher introuvable sur le client d'essai"}
        return {"state": "ok", "name": name, "detail": out.decode("utf-8", "replace").strip()}
    except ManagerError as exc:
        return {"state": "ko", "name": name, "detail": str(exc)}


def host_name(settings: Settings) -> str:
    if settings.game_host == "agent":
        return f"l'agent ({settings.agent_url})"
    return GAME_HOST_LABELS[settings.game_host or "off"]


def normalize_doctor(data: object, name: str) -> dict:
    """Rapport --doctor ramené à la forme attendue par la page : chaque section a
    une liste `items`, chaque item un `status`, un `text` et une liste `details`
    (vide si le lanceur ne l'envoie pas) ; `runners` et `verdict` toujours présents."""
    if not isinstance(data, dict):
        raise ManagerError(f"rapport --doctor inattendu sur {name}")
    sections = []
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        items = []
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            details = item.get("details") or []
            items.append({"status": str(item.get("status") or "info"),
                          "text": str(item.get("text") or ""),
                          "details": [str(d) for d in (details if isinstance(details, list) else [details])]})
        sections.append({"title": str(section["title"]) if section.get("title") else None, "items": items})
    runners = [{"name": str(r.get("name") or ""), "path": str(r.get("path") or "")}
               for r in data.get("runners") or [] if isinstance(r, dict)]
    verdict = data.get("verdict")
    return {"code": data.get("code", 0), "sections": sections, "runners": runners,
            "verdict": str(verdict) if verdict else None}


def run_doctor(settings: Settings) -> dict:
    """`wsquashfs-launcher --doctor json` sur le client d'essai, sous l'utilisateur
    des jeux : l'état du lanceur tel qu'il lancera les jeux."""
    host = game_host(settings, 120)
    if isinstance(host, AgentHost):
        data = host.doctor()
    else:
        code, out = host.exec(["bash", "-lc", 'wsquashfs-launcher "$@"', "wsfs-doctor", "--doctor", "json"])
        if code == 127 or "command not found" in out.decode("utf-8", "replace"):
            raise ManagerError(f"wsquashfs-launcher introuvable sur {host.name}")
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise ManagerError(f"rapport --doctor illisible sur {host.name}") from exc
    return {"container": host.name, **normalize_doctor(data, host.name)}


# ------------------------------------------------------------------ fichiers du client d'essai

def steambox_listing(settings: Settings, path: str) -> dict[str, int]:
    """Arborescence d'un dossier du client d'essai (runner) : chemin
    relatif → taille (0 pour un dossier ou un lien)."""
    host = game_host(settings, 120)
    if isinstance(host, AgentHost):
        return host.listing(path)
    code, out = host.exec(["find", path, "-mindepth", "1", "-printf", "%y\t%s\t%P\n"])
    if code != 0:
        raise ManagerError(f"dossier illisible sur {host.name} : {path}")
    listing = {}
    for line in out.decode("utf-8", "replace").splitlines():
        kind, size, rel = line.split("\t", 2)
        listing[rel] = int(size) if kind == "f" else 0
    return listing


def steambox_fetch(settings: Settings, path: str, dest: Path, job: Job | None = None,
                   total: int = 0) -> None:
    """Copie un dossier du client d'essai dans `dest` (créé), liens
    symboliques gardés : `tar` sur le client d'essai, lu en flux (API Docker ou
    processus local) et extrait au fil de l'eau, sans archive intermédiaire."""
    dest.mkdir(parents=True, exist_ok=True)
    parent, name = path.rstrip("/").rsplit("/", 1)
    tar = subprocess.Popen(["tar", "-x", "-C", str(dest), "--strip-components=1",
                            "--no-same-owner"],
                           stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert tar.stdin is not None and tar.stderr is not None        # PIPE
    tar_in, tar_errs = tar.stdin, tar.stderr
    received = 0

    def feed(chunk: bytes) -> None:
        nonlocal received
        tar_in.write(chunk)
        received += len(chunk)
        if job and total:
            job.progress("copie du runner", min(0.99, received / total) * 0.3)

    try:
        host = game_host(settings, 3600)
        if isinstance(host, AgentHost):
            code, err = host.fetch(path.rstrip("/"), feed)
        else:
            code, err = host.exec_stream(["tar", "-C", parent, "-cf", "-", name], feed)
    finally:
        tar_in.close()
        tar_err = tar_errs.read().decode("utf-8", "replace")
        tar.wait()
    if code != 0:
        raise ManagerError(f"copie depuis {host_name(settings)} en échec : {err.decode('utf-8', 'replace')[:300]}")
    if tar.returncode != 0:
        raise ManagerError(f"extraction du runner en échec : {tar_err[:300]}")


def trial_dir(settings: Settings) -> Path:
    return settings.state_dir / "trials"


def run_trial(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « trial » : `wsquashfs-launcher --trial` sur la
    client d'essai (local, Docker, ou agent) ; rapport JSON."""
    image = Path(task.image_path or "")
    duration = int(task.params.get("duration", settings.trial_duration))
    host = game_host(settings, duration + 180)
    target = host.path_of(image)
    job.log(f"Lancement sur {host.name} : {target} ({duration} s)")
    job.progress("lancement", 0.05)
    shot: bytes | None = None
    if isinstance(host, AgentHost):
        data, shot = host.trial(target, duration)
    else:
        args = ["--trial", target, str(duration), "session", settings.session_process or "",
                "screenshot", "/tmp/wsfs-trial.png"]
        code, out = host.exec(["bash", "-lc", 'wsquashfs-launcher "$@"', "wsfs-trial", *args])
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise ManagerError(f"essai illisible sur {host.name} (code {code})") from exc
    if data.get("error") == "busy":
        raise ManagerError(f"un jeu tourne déjà sur {host.name} : essai reporté")
    if data.get("error") == "noimage":
        raise ManagerError(f"image introuvable sur {host.name} : {target}")
    job.check()
    report = report_from_json(data)
    job.progress("capture", 0.95)
    if shot is None and isinstance(host, LocalHost) and data.get("screenshot"):
        _, shot = host.exec(["cat", "/tmp/wsfs-trial.png"])
    shot = shot or b""
    if shot.startswith(b"\x89PNG"):
        trial_dir(settings).mkdir(parents=True, exist_ok=True)
        (trial_dir(settings) / f"{task.id}.png").write_bytes(shot)
    result = report.to_dict()
    result["screenshot"] = shot.startswith(b"\x89PNG")
    task.params["result"] = result
    for e in report.errors:
        job.log(f"Erreur connue : {e['meaning']} — {e['line'][:160]}")
    for w in report.windows:
        job.log(f"Fenêtre : {w['name']} [{w['state'] or 'normale'}]")
    ok, why = report.verdict
    if int(data.get("code", 0)) not in (0, 1) and not report.elapsed:
        raise ManagerError(f"essai interrompu (code {data.get('code')})")
    message = f"{'Réussi' if ok else 'Échec'} : {why}"
    if not ok:
        raise ManagerError(why)
    return message