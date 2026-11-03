"""Machine de jeu et lancement d'essai chronométré (SPEC § 2.5, étape 7).

La machine de jeu est là où tourne wsquashfs-launcher (réglage game_host) :
- « local » : la machine du gestionnaire, commandes lancées directement ;
- « docker » : un conteneur (ex. SteamBox), par l'API Docker
  (docker-socket-proxy), sous l'utilisateur des jeux ;
- « off » : lancement d'essai, --doctor et runners installés indisponibles.

Le jeu y est lancé par wsquashfs-launcher (environnement de la session
graphique : celui du processus session_process), observé pendant une durée
donnée, puis arrêté. Rapport : le jeu a-t-il tenu, ses fenêtres (minimisée ?), les
erreurs connues du journal, une capture d'écran.

Garde-fous (leçons des essais faits à la main, 01/10) :
- refus si un jeu .wsquashfs tourne déjà sur la machine de jeu (lancements
  croisés : les montages de l'un et de l'autre se gêneraient) ;
- processus du jeu trouvés par leur WINEPREFIX et leur ligne de commande,
  jamais par leur nom (un jeu peut renommer son processus) ;
- arrêt par PID, jamais `pkill -f` (qui se trouve lui-même).
"""

from __future__ import annotations

import base64
import hashlib
import re
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import Settings
from ..models import Task
from .rebuild import Job, RebuildError

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

_SCRIPT = r'''
IMG="$1"; DUR="${2:-60}"; SESSION="${3:-}"; STEM=$(basename "$IMG" .wsquashfs)
# Un jeu .wsquashfs en cours : ses montages et ceux de l'essai se gêneraient
busy=$(ps -eo comm= | grep -cxE 'wsquashfs-launc|umu-run')
if [ "$busy" != 0 ]; then echo "@@BUSY"; exit 3; fi
[ -f "$IMG" ] || { echo "@@NOIMAGE"; exit 4; }
# Environnement de la session graphique (DISPLAY, WAYLAND_DISPLAY…) ; comm est
# tronqué à 15 caractères par le noyau
ES=""
[ -n "$SESSION" ] && ES=$(ps -eo pid=,comm= | awk -v s="${SESSION:0:15}" '$2==s{print $1; exit}')
if [ -n "$ES" ] && [ -r "/proc/$ES/environ" ]; then
  while IFS= read -r -d '' v; do export "$v"; done < "/proc/$ES/environ"
fi
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
OUT=/tmp/wsfs-trial.out; SHOT=/tmp/wsfs-trial.png
rm -f "$OUT" "$SHOT"
ulimit -s 8192 2>/dev/null
setsid wsquashfs-launcher "$IMG" > "$OUT" 2>&1 < /dev/null &
L=$!
RE=$(printf '%s' "$STEM" | sed 's/[][\.*^$(){}?+|/]/\\&/g')
SYS='services|winedevice|plugplay|svchost|explorer|rpcss|tabtip|xalia|umu|conhost|start|wineboot|winemenubuilder'
prefix_pids() {
  for p in /proc/[0-9]*; do
    tr '\0' '\n' < "$p/environ" 2>/dev/null \
      | grep -qE "^WINEPREFIX=.*/wsquashfs/wine/${RE}(/.*)?$" && echo "${p#/proc/}"
  done
}
game_pids() {
  for p in $(prefix_pids); do
    c=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
    n=$(printf '%s' "$c" | grep -oiE '[^\\/ ]+\.exe' | head -1)
    [ -n "$n" ] || continue
    printf '%s' "$n" | grep -qixE "($SYS)\.exe" || echo "$p"
  done
}
T0=$(date +%s); seen=0
while [ $(( $(date +%s) - T0 )) -lt "$DUR" ]; do
  sleep 3
  kill -0 "$L" 2>/dev/null || break
  [ -n "$(game_pids)" ] && seen=1
done
echo "@@ELAPSED $(( $(date +%s) - T0 ))"
echo "@@LAUNCHER $(kill -0 "$L" 2>/dev/null && echo 1 || echo 0)"
echo "@@GAMESEEN $seen"
ALL=" $(prefix_pids | tr '\n' ' ') "
for p in $(game_pids | head -3); do
  echo "@@GAME $(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-200)"
done
if command -v xprop >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  for w in $(xprop -root _NET_CLIENT_LIST 2>/dev/null | grep -o '0x[0-9a-f]*'); do
    pid=$(xprop -id "$w" _NET_WM_PID 2>/dev/null | grep -o '[0-9]*$')
    case "$ALL" in *" $pid "*) ;; *) continue ;; esac
    name=$(xprop -id "$w" WM_NAME 2>/dev/null | sed -n 's/^WM_NAME([^)]*) = "\(.*\)"$/\1/p')
    st=$(xprop -id "$w" _NET_WM_STATE 2>/dev/null | sed 's/.*= //')
    echo "@@WINDOW $w|$name|$st"
  done
fi
command -v grim >/dev/null 2>&1 && [ -n "${WAYLAND_DISPLAY:-}" ] && grim "$SHOT" 2>/dev/null
for p in $(prefix_pids); do kill -TERM "$p" 2>/dev/null; done
sleep 5
for p in $(prefix_pids); do kill -KILL "$p" 2>/dev/null; done
for i in $(seq 1 30); do kill -0 "$L" 2>/dev/null || break; sleep 1; done
kill -0 "$L" 2>/dev/null && kill -TERM "$L" 2>/dev/null
echo "@@LEFT $(prefix_pids | wc -l) $(mount | grep -c "/wsquashfs/.*/${STEM}")"
echo "@@LOG"
tail -c 60000 "$OUT"
'''


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


def parse_report(output: str) -> TrialReport:
    report = TrialReport()
    head, _, log = output.partition("@@LOG\n")
    report.log = log
    for line in head.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "@@ELAPSED":
            report.elapsed = int(rest or 0)
        elif tag == "@@LAUNCHER":
            report.launcher_alive = rest.strip() == "1"
        elif tag == "@@GAMESEEN":
            report.game_seen = rest.strip() == "1"
        elif tag == "@@GAME":
            report.game.append(rest.strip())
        elif tag == "@@WINDOW":
            wid, _, more = rest.partition("|")
            name, _, state = more.partition("|")
            report.windows.append({"id": wid, "name": name, "state": state})
        elif tag == "@@LEFT":
            report.left = rest.strip()
    seen = set()
    for pattern, meaning in KNOWN_ERRORS:
        for line in log.splitlines():
            if re.search(pattern, line, re.IGNORECASE) and (pattern, line) not in seen:
                seen.add((pattern, line))
                report.errors.append({"meaning": meaning, "line": line.strip()[:240]})
                break
    return report


def steambox_path(settings: Settings, image: Path) -> str:
    """Chemin de l'image dans le conteneur : la partie après « roms/ » (format
    Batocera) est rattachée au dossier roms du conteneur."""
    parts = image.parts
    if "roms" not in parts:
        raise RebuildError("image hors d'un dossier roms/ : chemin dans le conteneur inconnu")
    index = len(parts) - 1 - parts[::-1].index("roms")
    return "/".join([settings.steambox_roms.rstrip("/"), *parts[index + 1:]])


class Docker:
    """Client minimal de l'API Docker (exec), via docker-socket-proxy."""

    def __init__(self, base_url: str, timeout: float):
        self.client = httpx.Client(base_url=base_url.replace("tcp://", "http://"),
                                   timeout=httpx.Timeout(timeout, connect=10))

    def exec(self, container: str, cmd: list[str], user: str = "arcade") -> tuple[int, bytes]:
        try:
            created = self.client.post(f"/containers/{container}/exec", json={
                "Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True})
            created.raise_for_status()
            exec_id = created.json()["Id"]
            raw = self.client.post(f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False})
            raw.raise_for_status()
            code = self.client.get(f"/exec/{exec_id}/json").json().get("ExitCode") or 0
        except httpx.HTTPError as exc:
            raise _unreachable(container, exc) from exc
        return code, _demux(raw.content)

    def exec_stream(self, container: str, cmd: list[str], on_stdout: Callable[[bytes], None],
                    user: str = "arcade") -> tuple[int, bytes]:
        """Comme `exec`, sans tout garder en mémoire : chaque bloc de la sortie
        standard est passé à `on_stdout` dès sa réception. Retourne (code,
        sortie d'erreur)."""
        stderr = bytearray()
        try:
            created = self.client.post(f"/containers/{container}/exec", json={
                "Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True})
            created.raise_for_status()
            exec_id = created.json()["Id"]
            with self.client.stream("POST", f"/exec/{exec_id}/start",
                                    json={"Detach": False, "Tty": False}) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_raw():
                    buffer += chunk
                    while len(buffer) >= 8:
                        size = int.from_bytes(buffer[4:8], "big")
                        if len(buffer) < 8 + size:
                            break
                        kind, payload = buffer[0], bytes(buffer[8:8 + size])
                        del buffer[:8 + size]
                        if kind == 1:
                            on_stdout(payload)
                        else:
                            stderr += payload
            code = self.client.get(f"/exec/{exec_id}/json").json().get("ExitCode") or 0
        except httpx.HTTPError as exc:
            raise _unreachable(container, exc) from exc
        return code, bytes(stderr)


class DockerHost:
    """Machine de jeu dans un conteneur, par l'API Docker."""

    def __init__(self, settings: Settings, timeout: float):
        self.settings = settings
        self.name = settings.steambox_container
        self.docker = Docker(settings.docker_host, timeout=timeout)

    def exec(self, cmd: list[str]) -> tuple[int, bytes]:
        return self.docker.exec(self.name, cmd, user=self.settings.game_user)

    def exec_stream(self, cmd: list[str], on_stdout: Callable[[bytes], None]) -> tuple[int, bytes]:
        return self.docker.exec_stream(self.name, cmd, on_stdout, user=self.settings.game_user)

    def path_of(self, image: Path) -> str:
        return steambox_path(self.settings, image)


class LocalHost:
    """Machine de jeu = celle du gestionnaire : commandes lancées directement,
    sous l'utilisateur du service."""

    name = "cette machine"

    def __init__(self, settings: Settings, timeout: float):
        self.timeout = timeout

    def exec(self, cmd: list[str]) -> tuple[int, bytes]:
        try:
            done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            return 127, f"{exc.filename}: command not found".encode()
        except subprocess.TimeoutExpired as exc:
            raise RebuildError(f"commande trop longue ({int(self.timeout)} s) : {cmd[0]}") from exc
        return done.returncode, done.stdout

    def exec_stream(self, cmd: list[str], on_stdout: Callable[[bytes], None]) -> tuple[int, bytes]:
        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                return 127, f"{cmd[0]}: command not found".encode()
            for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
                on_stdout(chunk)
            code = proc.wait()
            err.seek(0)
            return code, err.read()

    def path_of(self, image: Path) -> str:
        return str(image)


# ------------------------------------------------------------------ agent réseau

def agent_cert_path(settings: Settings) -> Path:
    """Certificat de l'agent, épinglé à l'association (Réglages)."""
    return settings.state_dir / "agent-cert.pem"


def fingerprint_pem(pem: str) -> str:
    """Empreinte SHA-256 du certificat, au format affiché par `wsquashfs-launcher --pair`."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _pinned_context(pem: str) -> ssl.SSLContext:
    """Confiance = ce certificat et lui seul ; le nom d'hôte n'est pas contrôlé
    (l'empreinte confirmée à l'association en tient lieu)."""
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    return context


class AgentHost:
    """Machine de jeu = wsquashfs-agent sur le réseau (HTTPS, jeton, certificat épinglé)."""

    def __init__(self, settings: Settings, timeout: float, pem: str | None = None,
                 url: str | None = None, token: str | None = None):
        self.settings = settings
        self.url = (url or settings.agent_url).rstrip("/")
        if pem is None:
            cert = agent_cert_path(settings)
            if not cert.exists():
                raise RebuildError("agent non associé : Réglages → Machine de jeu → Agent réseau")
            pem = cert.read_text()
        self.name = f"l'agent ({self.url})"
        self.client = httpx.Client(base_url=self.url, verify=_pinned_context(pem),
                                   headers={"Authorization": f"Bearer {token or settings.agent_token}"},
                                   timeout=httpx.Timeout(timeout, connect=10))

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RebuildError(f"agent injoignable ({self.url}) : {exc}") from exc
        if response.status_code == 401:
            raise RebuildError("jeton de l'agent refusé : copier le jeton affiché par « wsquashfs-launcher --pair »")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RebuildError(f"agent : {detail}")
        return response

    def info(self) -> dict:
        return self._call("GET", "/v1/info").json()

    def doctor(self) -> dict:
        return self._call("GET", "/v1/doctor", timeout=120).json()

    def trial(self, rel: str, duration: int) -> tuple[int, bytes, bytes]:
        data = self._call("POST", "/v1/trial", json={"rel": rel, "duration": duration},
                          timeout=duration + 180).json()
        shot = base64.b64decode(data["screenshot"]) if data.get("screenshot") else b""
        return int(data["code"]), data["output"].encode("utf-8"), shot

    def listing(self, path: str) -> dict[str, int]:
        return self._call("GET", "/v1/runner/list", params={"path": path}).json()["entries"]

    def fetch(self, path: str, feed: Callable[[bytes], None]) -> tuple[int, bytes]:
        try:
            with self.client.stream("GET", "/v1/runner/tar", params={"path": path}, timeout=3600) as response:
                if response.status_code >= 400:
                    raise RebuildError(f"agent : copie refusée ({response.status_code})")
                for chunk in response.iter_bytes():
                    feed(chunk)
        except httpx.HTTPError as exc:
            raise RebuildError(f"agent injoignable pendant la copie : {exc}") from exc
        return 0, b""

    def rel_of(self, image: Path) -> str:
        """Chemin de l'image relatif à son dossier roms/ (l'agent le retrouve chez lui)."""
        parts = image.parts
        if "roms" not in parts:
            raise RebuildError("image hors d'un dossier roms/ : impossible à retrouver sur l'agent")
        index = len(parts) - 1 - parts[::-1].index("roms")
        return "/".join(parts[index + 1:])

    def path_of(self, image: Path) -> str:
        return self.rel_of(image)


GAME_HOST_LABELS = {"off": "désactivée", "local": "cette machine", "docker": "conteneur Docker",
                    "agent": "agent réseau"}


def game_host(settings: Settings, timeout: float = 120) -> DockerHost | LocalHost | AgentHost:
    """Machine de jeu configurée ; RebuildError si elle est désactivée."""
    if settings.game_host == "local":
        return LocalHost(settings, timeout)
    if settings.game_host == "docker" and settings.steambox_container:
        return DockerHost(settings, timeout)
    if settings.game_host == "agent" and settings.agent_url:
        return AgentHost(settings, timeout)
    raise RebuildError("machine de jeu désactivée : à activer dans Réglages → Machine de jeu")


# Maintenance du lanceur (Réglages → Machine de jeu) : liste fermée, comme celle de l'agent
MAINTENANCE_ACTIONS = ("list", "umount", "clean", "backup")


def maintenance(settings: Settings, action: str) -> dict:
    """Lance une action de maintenance du lanceur sur la machine de jeu."""
    if action not in MAINTENANCE_ACTIONS:
        raise RebuildError("action de maintenance non autorisée")
    host = game_host(settings, 900)
    if isinstance(host, AgentHost):
        return host._call("POST", "/v1/maintenance", json={"action": action}, timeout=900).json()
    code, out = host.exec(["bash", "-lc", f"wsquashfs-launcher --{action}"])
    return {"action": action, "code": code, "output": out.decode("utf-8", "replace")}


def host_status(settings: Settings) -> dict:
    """Voyant de la machine de jeu : ok (connectée), ko (injoignable ou sans
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
            return {"state": "ko", "name": name, "detail": "wsquashfs-launcher introuvable sur la machine de jeu"}
        return {"state": "ok", "name": name, "detail": out.decode("utf-8", "replace").strip()}
    except RebuildError as exc:
        return {"state": "ko", "name": name, "detail": str(exc)}


def host_name(settings: Settings) -> str:
    if settings.game_host == "docker":
        return settings.steambox_container
    if settings.game_host == "agent":
        return f"l'agent ({settings.agent_url})"
    return GAME_HOST_LABELS[settings.game_host or "off"]


def _unreachable(container: str, exc: httpx.HTTPError) -> RebuildError:
    hint = ""
    if isinstance(exc, httpx.ConnectError):
        # Cas vu le 01/11 : conteneur sur le réseau bridge, proxy sur socket_proxy
        hint = (" — proxy Docker injoignable : le conteneur du manager doit être sur le "
                "réseau du docker-socket-proxy (Unraid : Network Type = socket_proxy)")
    return RebuildError(f"conteneur {container} injoignable : {exc}{hint}")


def _demux(stream: bytes) -> bytes:
    """Flux Docker multiplexé : en-têtes de 8 octets (type, taille)."""
    out, i = bytearray(), 0
    while i + 8 <= len(stream):
        size = int.from_bytes(stream[i + 4:i + 8], "big")
        out += stream[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(out)


_DOCTOR_ITEM_RE = re.compile(r"^  ([✓✗–]) (.*)$")
_DOCTOR_RUNNER_RE = re.compile(r"^      (\S+)\s{2,}(/\S.*)$")
DOCTOR_STATUS = {"✓": "ok", "✗": "missing", "–": "optional"}


def parse_doctor(text: str) -> dict:
    """Sortie de `wsquashfs-launcher --doctor` → sections, points vérifiés
    (ok, missing, optional, avec leurs lignes de détail), runners installés
    et verdict (dernière ligne)."""
    sections: list[dict] = []
    runners: list[dict] = []
    verdict = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if m := _DOCTOR_RUNNER_RE.match(line):
            runners.append({"name": m.group(1), "path": m.group(2).strip()})
        if m := _DOCTOR_ITEM_RE.match(line):
            if not sections:
                sections.append({"title": "", "items": []})
            sections[-1]["items"].append({"status": DOCTOR_STATUS[m.group(1)],
                                          "text": m.group(2), "details": []})
        elif line.startswith("  ") and sections and sections[-1]["items"]:
            sections[-1]["items"][-1]["details"].append(line.strip())
        elif not line.startswith(" "):
            if line[:1] in DOCTOR_STATUS:
                verdict = line
            else:
                sections.append({"title": line.strip(), "items": []})
    return {"sections": sections, "runners": runners, "verdict": verdict}


def run_doctor(settings: Settings) -> dict:
    """`wsquashfs-launcher --doctor` sur la machine de jeu, sous l'utilisateur
    des jeux : l'état du lanceur tel qu'il lancera les jeux."""
    host = game_host(settings, 120)
    if isinstance(host, AgentHost):
        data = host.doctor()
        code, text = int(data["code"]), data["text"]
    else:
        code, out = host.exec(["bash", "-lc", "wsquashfs-launcher --doctor"])
        text = out.decode("utf-8", "replace")
    if code == 127 or "command not found" in text:
        raise RebuildError(f"wsquashfs-launcher introuvable sur {host.name}")
    return {"container": host.name, "code": code, "text": text, **parse_doctor(text)}


# ------------------------------------------------------------------ fichiers de la machine de jeu

def steambox_listing(settings: Settings, path: str) -> dict[str, int]:
    """Arborescence d'un dossier de la machine de jeu (runner) : chemin
    relatif → taille (0 pour un dossier ou un lien)."""
    host = game_host(settings, 120)
    if isinstance(host, AgentHost):
        return host.listing(path)
    code, out = host.exec(["find", path, "-mindepth", "1", "-printf", "%y\t%s\t%P\n"])
    if code != 0:
        raise RebuildError(f"dossier illisible sur {host.name} : {path}")
    listing = {}
    for line in out.decode("utf-8", "replace").splitlines():
        kind, size, rel = line.split("\t", 2)
        listing[rel] = int(size) if kind == "f" else 0
    return listing


def steambox_fetch(settings: Settings, path: str, dest: Path, job: Job | None = None,
                   total: int = 0) -> None:
    """Copie un dossier de la machine de jeu dans `dest` (créé), liens
    symboliques gardés : `tar` sur la machine de jeu, lu en flux (API Docker ou
    processus local) et extrait au fil de l'eau, sans archive intermédiaire."""
    dest.mkdir(parents=True, exist_ok=True)
    parent, name = path.rstrip("/").rsplit("/", 1)
    tar = subprocess.Popen(["tar", "-x", "-C", str(dest), "--strip-components=1",
                            "--no-same-owner"],
                           stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    received = 0

    def feed(chunk: bytes) -> None:
        nonlocal received
        tar.stdin.write(chunk)
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
        tar.stdin.close()
        tar_err = tar.stderr.read().decode("utf-8", "replace")
        tar.wait()
    if code != 0:
        raise RebuildError(f"copie depuis {host_name(settings)} en échec : {err.decode('utf-8', 'replace')[:300]}")
    if tar.returncode != 0:
        raise RebuildError(f"extraction du runner en échec : {tar_err[:300]}")


def trial_dir(settings: Settings) -> Path:
    return settings.state_dir / "trials"


def run_trial(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « trial »."""
    image = Path(task.image_path or "")
    duration = int(task.params.get("duration", settings.trial_duration))
    host = game_host(settings, duration + 180)
    target = host.path_of(image)
    job.log(f"Lancement sur {host.name} : {target} ({duration} s)")
    job.progress("lancement", 0.05)
    if isinstance(host, AgentHost):
        code, out, shot = host.trial(target, duration)
    else:
        code, out = host.exec(["bash", "-c", _SCRIPT, "wsfs-trial", target, str(duration),
                               settings.session_process or ""])
        shot = None
    text = out.decode("utf-8", "replace")
    if "@@BUSY" in text:
        raise RebuildError(f"un jeu tourne déjà sur {host.name} : essai reporté")
    if "@@NOIMAGE" in text:
        raise RebuildError(f"image introuvable sur {host.name} : {target}")
    job.check()
    report = parse_report(text)
    job.progress("capture", 0.95)
    if shot is None:
        _, shot = host.exec(["cat", "/tmp/wsfs-trial.png"])
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
    if code not in (0, 1) and not report.elapsed:
        raise RebuildError(f"essai interrompu (code {code})")
    message = f"{'Réussi' if ok else 'Échec'} : {why}"
    if not ok:
        raise RebuildError(why)
    return message

