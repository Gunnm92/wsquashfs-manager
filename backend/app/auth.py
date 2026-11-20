"""Authentification par jeton (mot de passe jamais renvoyé à chaque requête) et règle du
mode autonome : quelles routes touchent un client (409 en mode autonome)."""

from __future__ import annotations

import hashlib
import hmac


def token(password: str) -> str:
    """Jeton dérivé du mot de passe : le mot de passe lui-même ne circule
    qu'à la connexion, pas dans chaque requête."""
    return hmac.new(password.encode(), b"wsquashfs-manager", hashlib.sha256).hexdigest()


def same(a: str | bytes, b: str) -> bool:
    """Comparaison à temps constant (compare_digest refuse les str non ASCII)."""
    return hmac.compare_digest(a if isinstance(a, bytes) else a.encode(), b.encode())


# Routes qui touchent un client (sauvegardes, installation, essais, lanceur du client
# d'essai) : 409 en mode autonome. Les vignettes des sauvegardes restent servies.
CONNECTED_PREFIXES = ("/api/saves/", "/api/install", "/api/uninstall", "/api/game_host/", "/api/launcher/")


CONNECTED_EXACT = ("/api/image/trial", "/api/image/runner")


CONNECTED_FREE = ("/api/saves/art",)


def needs_connection(path: str) -> bool:
    if path in CONNECTED_FREE:
        return False
    return path in CONNECTED_EXACT or path.startswith(CONNECTED_PREFIXES)
