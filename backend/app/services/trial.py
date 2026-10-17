"""Lancement d'essai chronométré dans SteamBox (SPEC § 2.5, étape 7).

Le jeu est lancé par wsquashfs-launcher DANS le conteneur SteamBox (API
Docker via docker-socket-proxy, exec sous l'utilisateur arcade, environnement
de la session d'EmulationStation), observé pendant une durée donnée, puis
arrêté. Rapport : le jeu a-t-il tenu, ses fenêtres (minimisée ?), les
erreurs connues du journal, une capture d'écran.

Garde-fous (leçons des essais faits à la main, 01/10) :
- refus si un jeu .wsquashfs tourne déjà dans SteamBox (lancements
  croisés : les montages de l'un et de l'autre se gêneraient) ;
- processus du jeu trouvés par leur WINEPREFIX et leur ligne de commande,
  jamais par leur nom (un jeu peut renommer son processus) ;
- arrêt par PID, jamais `pkill -f` (qui se trouve lui-même).
"""

from __future__ import annotations

import re
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
IMG="$1"; DUR="${2:-60}"; STEM=$(basename "$IMG" .wsquashfs)
# Un jeu .wsquashfs en cours : ses montages et ceux de l'essai se gêneraient
busy=$(ps -eo comm= | grep -cxE 'wsquashfs-launc|umu-run')
if [ "$busy" != 0 ]; then echo "@@BUSY"; exit 3; fi
[ -f "$IMG" ] || { echo "@@NOIMAGE"; exit 4; }
ES=$(ps -eo pid=,comm= | awk '$2=="emulationstatio"{print $1; exit}')
if [ -n "$ES" ]; then
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
    tr '\0' '\n' < "$p/environ" 2>/dev/null | grep -qE "^WINEPREFIX=.*/wsquashfs/wine/${RE}(/.*)?$" && echo "${p#/proc/}"
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
    """Chemin de l'image dans SteamBox : la partie après « roms/ » (format
    Batocera) est rattachée au dossier roms de SteamBox."""
    parts = image.parts
    if "roms" not in parts:
        raise RebuildError("image hors d'un dossier roms/ : chemin dans SteamBox inconnu")
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
            raise RebuildError(f"conteneur {container} injoignable : {exc}") from exc
        return code, _demux(raw.content)


def _demux(stream: bytes) -> bytes:
    """Flux Docker multiplexé : en-têtes de 8 octets (type, taille)."""
    out, i = bytearray(), 0
    while i + 8 <= len(stream):
        size = int.from_bytes(stream[i + 4:i + 8], "big")
        out += stream[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(out)


def trial_dir(settings: Settings) -> Path:
    return settings.state_dir / "trials"


def run_trial(settings: Settings, task: Task, job: Job) -> str:
    """Gestionnaire de la tâche « trial »."""
    if not settings.steambox_container:
        raise RebuildError("lancement d'essai désactivé (WSQUASHFS_MGR_STEAMBOX_CONTAINER vide)")
    image = Path(task.image_path or "")
    duration = int(task.params.get("duration", settings.trial_duration))
    target = steambox_path(settings, image)
    job.log(f"Lancement dans {settings.steambox_container} : {target} ({duration} s)")
    job.progress("lancement", 0.05)
    docker = Docker(settings.docker_host, timeout=duration + 180)
    code, out = docker.exec(settings.steambox_container,
                            ["bash", "-c", _SCRIPT, "wsfs-trial", target, str(duration)])
    text = out.decode("utf-8", "replace")
    if "@@BUSY" in text:
        raise RebuildError("un jeu tourne déjà dans SteamBox : essai reporté")
    if "@@NOIMAGE" in text:
        raise RebuildError(f"image introuvable dans SteamBox : {target}")
    job.check()
    report = parse_report(text)
    job.progress("capture", 0.95)
    _, shot = docker.exec(settings.steambox_container, ["cat", "/tmp/wsfs-trial.png"])
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

