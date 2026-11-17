"""Client d'essai et lancement d'essai chronométré (SPEC § 2.5, étape 7).

Le client d'essai est là où tourne wsquashfs-launcher (réglage game_host) :
- « local » : le serveur du manager, commandes lancées directement ;
- « agent » : wsquashfs-launcher --agent sur ce client ou sur le réseau
  (code d'association), y compris dans un conteneur ;
- « off » : lancement d'essai, --doctor et runners installés indisponibles.

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

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import Settings, saves_client_id
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


class LocalHost:
    """Client d'essai = celle du gestionnaire : commandes lancées directement,
    sous l'utilisateur du service."""

    name = "ce serveur"

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
            stdout = proc.stdout
            assert stdout is not None           # stdout=PIPE
            for chunk in iter(lambda: stdout.read(1 << 16), b""):
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


PAIR_CODE_RE = re.compile(r"^wsq://([A-Za-z0-9_-]+)@([^\s/:@#]+):(\d+)#([0-9A-Fa-f]{64})$")
PAIR_VERSION = 7                # premier agent qui échange le secret du code contre un jeton durable
PAIR_EXPIRED = ("code d'association expiré ou déjà utilisé : relancer « wsquashfs-launcher --pair » "
                "sur le client, puis coller le nouveau code")


def parse_pair_code(code: str) -> dict:
    """Code d'association de `wsquashfs-launcher --pair` :
    wsq://SECRET@HÔTE:PORT#EMPREINTE (SHA-256 du certificat, hexadécimal). Le secret ne
    sert qu'une fois : il s'échange contre le jeton durable (pair_agent)."""
    m = PAIR_CODE_RE.match(code.strip())
    if not m:
        raise ValueError("code d'association illisible (wsq://… copié depuis « wsquashfs-launcher --pair »)")
    secret, host, port, fp = m.groups()
    return {"url": f"https://{host}:{int(port)}", "secret": secret, "fingerprint": fp.upper()}


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


class AgentProbeError(RebuildError):
    """Association refusée ou impossible ; `status` est le code HTTP à renvoyer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def fetch_certificate(url: str, expected: str, timeout: float = 10) -> tuple[str, str]:
    """Certificat de l'agent à `url`, contrôlé contre l'empreinte du code d'association
    (sans deux-points). Renvoie (pem, empreinte). Aucune requête HTTP ici : un refus
    arrive avant tout envoi du secret."""
    url = url.strip().rstrip("/")
    match = re.match(r"^https://([^\s/:]+):(\d+)$", url)
    if not match:
        raise AgentProbeError(422, "adresse : https://hôte:port (ex. https://192.168.1.20:8790)")
    host, port = match.group(1), int(match.group(2))
    try:
        pem = ssl.get_server_certificate((host, port), timeout=timeout)
    except OSError as exc:
        raise AgentProbeError(502, f"agent injoignable : {exc}") from exc
    fingerprint = fingerprint_pem(pem)
    if fingerprint.replace(":", "") != expected:
        # Autre machine, ou certificat régénéré depuis le code : refus, rien n'est envoyé
        raise AgentProbeError(422, "le certificat de l'agent ne correspond pas au code d'association : "
                              "relancer « wsquashfs-launcher --pair » et copier le nouveau code")
    return pem, fingerprint


def pair_agent(url: str, secret: str, expected: str, machine_id: str, timeout: float = 15) -> dict:
    """Échange le secret d'un code d'association contre le jeton durable de ce manager
    (POST /v1/pair, protocole de l'agent version 7). Le certificat est contrôlé contre
    l'empreinte du code avant l'envoi du secret. L'agent ne rend le jeton qu'une fois.
    → {url, token, host, fingerprint, pem}. AgentProbeError (code HTTP) sinon."""
    pem, fingerprint = fetch_certificate(url, expected)
    url = url.strip().rstrip("/")
    name = f"wsquashfs-manager ({socket.gethostname()})"[:64]
    try:
        with httpx.Client(base_url=url, verify=_pinned_context(pem),
                          timeout=httpx.Timeout(timeout, connect=10)) as client:
            response = client.post("/v1/pair", json={"name": name},
                                   headers={"Authorization": f"Bearer {secret}", "X-Wsq-Machine": machine_id})
    except httpx.HTTPError as exc:
        raise AgentProbeError(502, f"agent injoignable : {exc}") from exc
    if response.status_code in (404, 405):
        raise AgentProbeError(422, OLD_AGENT_MESSAGE)           # agent trop ancien : pas de /v1/pair
    if response.status_code == 401:
        raise AgentProbeError(422, PAIR_EXPIRED)                 # déjà utilisé, expiré ou faux
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        # 400 : identifiant de machine absent ou invalide ; 422 pour le navigateur (pas 401)
        raise AgentProbeError(422 if response.status_code == 400 else 502, f"agent : {detail}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentProbeError(502, "réponse de l'agent illisible : relancer l'association") from exc
    if not isinstance(data, dict):
        raise AgentProbeError(502, "réponse de l'agent illisible : relancer l'association")
    version = str(data.get("version", ""))
    if not version.isdigit() or int(version) < PAIR_VERSION:
        raise AgentProbeError(422, OLD_AGENT_MESSAGE)
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise AgentProbeError(502, "réponse de l'agent sans jeton : relancer l'association")
    return {"url": url, "token": token, "host": str(data.get("host") or "").strip(),
            "fingerprint": fingerprint, "pem": pem}


class SavesBusy(RebuildError):
    """Un jeu tourne sur le client : ses sauvegardes ne se synchronisent pas maintenant."""


class AgentUnreachable(RebuildError):
    """Le client ne répond pas (réseau, certificat) : l'envoi peut reprendre plus tard."""


class AgentRejected(RebuildError):
    """L'agent refuse la demande (HTTP 400) ; son message est dans str(exc)."""


class InstallOffset(RebuildError):
    """PUT refusé : le décalage ne correspond pas à ce que l'agent a reçu (409 « décalage »)."""

    def __init__(self, part_size: int):
        super().__init__("décalage de l'envoi différent du client")
        self.part_size = part_size


class InstallChecksum(RebuildError):
    """Empreinte de l'image installée différente de celle envoyée (422 au commit)."""


OLD_AGENT_MESSAGE = ("agent trop ancien : wsquashfs-launcher --update sur ce client, "
                     "puis relancer son agent")


class AgentHost:
    """Client d'essai = wsquashfs-agent sur le réseau (HTTPS, jeton, certificat épinglé)."""

    def __init__(self, settings: Settings, timeout: float, pem: str | None = None,
                 url: str | None = None, token: str | None = None, client_id: str | None = None):
        self.settings = settings
        self.url = (url or settings.agent_url).rstrip("/")
        if pem is None:
            cert = agent_cert_path(settings)
            if not cert.exists():
                raise RebuildError("agent non associé : associer le client (bandeau Clients de Sauvegardes)")
            pem = cert.read_text()
        self.name = f"l'agent ({self.url})"
        self.timeout = timeout
        self._client_id = client_id     # identifiant de ce manager (sauvegardes), sinon config.json
        self.client = httpx.Client(base_url=self.url, verify=_pinned_context(pem),
                                   headers={"Authorization": f"Bearer {token or settings.agent_token}"},
                                   timeout=httpx.Timeout(timeout, connect=10))

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**self._head(), **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentUnreachable(f"agent injoignable ({self.url}) : {exc}") from exc
        if response.status_code == 401:
            raise RebuildError("jeton refusé : refaire l'association (wsquashfs-launcher --pair)")
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

    def trial(self, rel: str, duration: int) -> tuple[dict, bytes]:
        data = self._call("POST", "/v1/trial", json={"rel": rel, "duration": duration},
                          timeout=duration + 180).json()
        shot = base64.b64decode(data["screenshot"]) if data.get("screenshot") else b""
        return data["report"], shot

    def listing(self, path: str) -> dict[str, int]:
        return self._call("GET", "/v1/runner/list", params={"path": path}).json()["entries"]

    def fetch(self, path: str, feed: Callable[[bytes], None]) -> tuple[int, bytes]:
        try:
            with self.client.stream("GET", "/v1/runner/tar", params={"path": path}, timeout=3600,
                                    headers=self._head()) as response:
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

    # ---------------------------------------------------------- sauvegardes
    # Routes /v1/saves/* (manager, services/saves.py). Chaque requête porte
    # X-Wsq-Machine : l'agent garde pour ce manager l'empreinte du dernier échange.

    @property
    def client_id(self) -> str:
        if self._client_id is None:
            self._client_id = saves_client_id(self.settings)
        return self._client_id

    def _head(self) -> dict:
        """En-tête X-Wsq-Machine de toute requête : le jeton est lié à cet identifiant
        (agent version 7) ; l'agent refuse un autre identifiant."""
        return {"X-Wsq-Machine": self.client_id}

    def _unreachable(self, exc: httpx.HTTPError) -> RebuildError:
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, ssl.SSLCertVerificationError):
                return RebuildError(f"certificat du client différent de celui associé ({self.url}) : "
                                    "refaire l'association (wsquashfs-launcher --pair)")
            cause = cause.__cause__ or cause.__context__
        return AgentUnreachable(f"client injoignable ({self.url}) : {exc}")

    def _saves_error(self, response: httpx.Response, conflict: str | None = None) -> RebuildError:
        if response.status_code == 401:
            return RebuildError("jeton refusé : refaire l'association (wsquashfs-launcher --pair)")
        try:
            data = response.json()
            detail = str(data.get("detail", "")) if isinstance(data, dict) else ""
        except ValueError:
            detail = ""
        if response.status_code in (404, 405) and not detail:
            # Route inconnue (réponse texte du routeur Go, sans « detail ») : agent d'avant
            # la synchro des sauvegardes (version < 5)
            return RebuildError(OLD_AGENT_MESSAGE)
        detail = detail or response.text[:200] or f"erreur {response.status_code}"
        if response.status_code == 409:
            if detail == "jeu en cours":
                return SavesBusy("jeu en cours sur le client : sauvegardes non synchronisées")
            return RebuildError(conflict or detail)
        return RebuildError(detail)

    def _saves_request(self, method: str, path: str, *, timeout: float, conflict: str | None = None,
                       headers: dict | None = None, **kwargs) -> httpx.Response:
        head = {"X-Wsq-Machine": self.client_id, **(headers or {})}
        try:
            response = self.client.request(method, path, headers=head, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if response.status_code >= 400:
            raise self._saves_error(response, conflict)
        return response

    def saves_list(self, timeout: float = 30) -> list[dict]:
        """Jeux du client : [{name, fingerprint, newest, state}]."""
        data = self._saves_request("GET", "/v1/saves/list", timeout=timeout).json()
        games = data.get("games") if isinstance(data, dict) else None
        if not isinstance(games, list):
            raise RebuildError(f"liste des sauvegardes illisible sur {self.url}")
        return games

    def saves_get(self, game: str, dest: Path) -> tuple[str, str | None]:
        """Archive d'un jeu, écrite (et fsync) dans `dest` ; renvoie (empreinte,
        profil). Le profil est None si le client ne l'envoie pas."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.client.stream("GET", "/v1/saves/tar", params={"game": game},
                                    headers={"X-Wsq-Machine": self.client_id}, timeout=3600) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._saves_error(response)
                fingerprint = response.headers.get("X-Wsq-Fingerprint", "")
                profile = response.headers.get("X-Wsq-Profile")
                if not fingerprint:
                    raise RebuildError("réponse sans empreinte : sauvegarde non reçue")
                with open(dest, "wb") as out:
                    out.writelines(response.iter_bytes())
                    out.flush()
                    os.fsync(out.fileno())
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        return fingerprint, profile

    def saves_put(self, game: str, archive: Path, profile: str | None, dir_path: str | None = None) -> dict:
        """Envoie une archive ; le client archive sa version si elle a changé
        depuis le dernier échange avec ce manager. `dir_path` : chemin d'un dossier déclaré (à
        partir du HOME du client) : un client qui ne l'a pas le déclare (X-Wsq-Dir, encodé
        en pourcentage). → {fingerprint, archived}."""
        headers = {"Content-Type": "application/gzip", "Content-Length": str(archive.stat().st_size)}
        if dir_path:
            headers["X-Wsq-Dir"] = quote(dir_path, safe="")
        if profile:
            try:
                profile.encode("ascii")
            except UnicodeEncodeError as exc:
                raise RebuildError("profil non ASCII : pas transmissible à l'agent") from exc
            headers["X-Wsq-Profile"] = profile
        with archive.open("rb") as body:
            response = self._saves_request("PUT", "/v1/saves/tar", timeout=3600, headers=headers,
                                           params={"game": game}, content=body)
        return response.json()

    def saves_delete(self, game: str, timeout: float = 120) -> dict:
        """Archive puis efface le jeu sur le client (DELETE /v1/saves/tar) → {archived, removed}.
        Un agent plus ancien ne connaît pas la route (404/405) : sa mise à jour est demandée."""
        try:
            response = self.client.request("DELETE", "/v1/saves/tar", params={"game": game},
                                           headers={"X-Wsq-Machine": self.client_id}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if response.status_code in (404, 405):
            raise RebuildError("agent trop ancien : wsquashfs-launcher --update sur ce client")
        if response.status_code == 409:
            raise SavesBusy("jeu en cours sur le client : non supprimée")
        if response.status_code >= 400:
            raise self._saves_error(response)
        return response.json()

    def saves_manager_put(self, name: str, url: str, token: str, fingerprint: str, ui: str = "") -> dict:
        """Déclare le manager à l'agent (version 9) : adresse, jeton propre à ce manager et
        empreinte SHA-256 (hexadécimal, sans « : ») de son certificat ; `ui` : adresse de
        l'interface (ignorée par un agent qui ne la connaît pas). Remplace la déclaration."""
        body = {"name": name, "url": url, "token": token, "fingerprint": fingerprint}
        if ui:
            body["ui"] = ui
        return self._saves_request("PUT", "/v1/saves/manager", timeout=30, json=body).json()

    # ---------------------------------------------------------- dossiers déclarés (agent v8)
    # Dossiers synchronisés comme des jeux (« dossier:<nom> »), déclarés sur le client ; leur
    # chemin est celui du client (sous son HOME, vérifié par l'agent).

    def saves_dirs(self) -> dict:
        """Dossiers déclarés : {dirs: [{name, path}], eden} (eden : dossier d'Eden, "" sinon)."""
        return self._saves_request("GET", "/v1/saves/dirs", timeout=30).json()

    def saves_dir_put(self, name: str, path: str) -> dict:
        """Déclare (ou remplace) un dossier ; l'agent refuse un chemin hors de son HOME (400)."""
        return self._saves_request("PUT", "/v1/saves/dirs", timeout=30, json={"name": name, "path": path}).json()

    def saves_dir_delete(self, name: str) -> dict:
        """Retire la déclaration (les fichiers restent sur le client)."""
        return self._saves_request("DELETE", "/v1/saves/dirs", timeout=30, params={"name": name}).json()

    def saves_ack(self, game: str, fingerprint: str) -> None:
        """Enregistre l'empreinte commune de ce jeu pour ce manager."""
        self._saves_request("POST", "/v1/saves/state", timeout=30,
                            conflict="le client a changé entre-temps : relancer la synchronisation",
                            json={"game": game, "fingerprint": fingerprint})

    # ---------------------------------------------------------- jeux installés (agent v6)
    # Routes /v1/games/* (services/install.py) : jeton seul, comme /v1/info.

    def _games(self, method: str, path: str, *, timeout: float, conflict: str | None = None,
               **kwargs) -> httpx.Response:
        headers = {**self._head(), **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, path, timeout=timeout, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if response.status_code >= 400:
            raise self._games_error(response, conflict)
        return response

    def _games_error(self, response: httpx.Response, conflict: str | None) -> RebuildError:
        if response.status_code == 401:
            return RebuildError("jeton refusé : refaire l'association (wsquashfs-launcher --pair)")
        try:
            data = response.json()
            detail = str(data.get("detail", "")) if isinstance(data, dict) else ""
        except ValueError:
            data, detail = {}, ""
        if response.status_code in (404, 405) and not detail:
            return RebuildError(OLD_AGENT_MESSAGE)
        if response.status_code == 409:
            if detail == "décalage":
                return InstallOffset(int(data.get("part_size") or 0))
            if conflict:
                return RebuildError(conflict)
            if "envoi" in detail:
                return RebuildError("envoi en cours sur le client : réessayer plus tard")
            return RebuildError(detail or "conflit sur le client")
        if response.status_code == 422:
            return InstallChecksum(detail or "empreinte incorrecte")
        if response.status_code == 400:
            return AgentRejected(detail or "demande refusée par l'agent")
        return RebuildError(detail or response.text[:200] or f"erreur {response.status_code}")

    def games_list(self, timeout: float = 30) -> dict:
        """{dir, set, free, games:[{name,size,mtime}], parts:[{name,size}]}."""
        return self._games("GET", "/v1/games/list", timeout=timeout).json()

    def games_set_dir(self, directory: str, timeout: float = 30) -> dict:
        """Dossier des jeux du client (« » : le dossier deviné) → {dir, set}."""
        return self._games("POST", "/v1/games/dir", timeout=timeout, json={"dir": directory}).json()

    def games_part(self, name: str, timeout: float = 30) -> dict:
        """Envoi partiel du jeu → {part_size, installed, size}."""
        return self._games("GET", "/v1/games/part", timeout=timeout, params={"name": name}).json()

    def games_put(self, name: str, offset: int, body, length: int, total: int) -> dict:
        """Un bloc de l'image à partir de `offset` ; `body` : itérable d'octets de `length` octets.
        → {part_size}."""
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(length),
                   "X-Wsq-Total-Size": str(total)}
        return self._games("PUT", "/v1/games/file", timeout=3600, headers=headers, content=body,
                           params={"name": name, "offset": str(offset)}).json()

    def games_commit(self, name: str, size: int, sha256: str) -> dict:
        """Valide l'envoi (taille et empreinte) → {name, size}."""
        return self._games("POST", "/v1/games/commit", timeout=3600,
                           json={"name": name, "size": size, "sha256": sha256}).json()

    def games_delete(self, name: str, timeout: float = 600) -> dict:
        """Désinstalle le jeu → {removed}. 409 : jeu monté ou en cours."""
        return self._games("DELETE", "/v1/games/file", timeout=timeout,
                           conflict="jeu monté ou en cours : suppression refusée",
                           params={"name": name}).json()


GAME_HOST_LABELS ={"off": "désactivée", "local": "ce serveur", "agent": "agent réseau"}


def game_host(settings: Settings, timeout: float = 120) -> LocalHost | AgentHost:
    """Client d'essai configuré ; RebuildError s'il est désactivé."""
    if settings.game_host == "local":
        return LocalHost(settings, timeout)
    if settings.game_host == "agent" and settings.agent_url:
        return AgentHost(settings, timeout)
    raise RebuildError("client d'essai non choisi : Sauvegardes › menu d'un client › « Utiliser pour les essais »")


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
        raise RebuildError("action de maintenance non autorisée")
    return MAINTENANCE_COMMANDS[action][1]


def maintenance(settings: Settings, action: str, image: Path | None = None) -> dict:
    """Lance une action de maintenance du lanceur sur le client d'essai."""
    rule = maintenance_game_rule(action)
    flags, _ = MAINTENANCE_COMMANDS[action]
    shown = " ".join(flags)
    if rule == "obligatoire" and image is None:
        raise RebuildError(f"{shown} demande un jeu")
    if rule == "interdit" and image is not None:
        raise RebuildError(f"{shown} ne prend pas de jeu")
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
    except RebuildError as exc:
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
        raise RebuildError(f"rapport --doctor inattendu sur {name}")
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
            raise RebuildError(f"wsquashfs-launcher introuvable sur {host.name}")
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise RebuildError(f"rapport --doctor illisible sur {host.name}") from exc
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
        raise RebuildError(f"dossier illisible sur {host.name} : {path}")
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
        raise RebuildError(f"copie depuis {host_name(settings)} en échec : {err.decode('utf-8', 'replace')[:300]}")
    if tar.returncode != 0:
        raise RebuildError(f"extraction du runner en échec : {tar_err[:300]}")


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
            raise RebuildError(f"essai illisible sur {host.name} (code {code})") from exc
    if data.get("error") == "busy":
        raise RebuildError(f"un jeu tourne déjà sur {host.name} : essai reporté")
    if data.get("error") == "noimage":
        raise RebuildError(f"image introuvable sur {host.name} : {target}")
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
        raise RebuildError(f"essai interrompu (code {data.get('code')})")
    message = f"{'Réussi' if ok else 'Échec'} : {why}"
    if not ok:
        raise RebuildError(why)
    return message