#!/bin/sh
# Démarrage du conteneur, convention Unraid : PUID/PGID (99:100 par défaut,
# nobody:users = arcade de SteamBox). Lancé en root, prépare /data (Unraid le
# crée en root s'il n'existe pas) puis passe à cet utilisateur : les images
# créées ont le même propriétaire que les autres.
set -eu
PUID=${PUID:-99}
PGID=${PGID:-100}

if [ "$(id -u)" = 0 ]; then
    # fusermount3 (montages FUSE) exige un compte pour l'utilisateur
    getent group "$PGID" >/dev/null || groupadd -o -g "$PGID" wsfs
    getent passwd "$PUID" >/dev/null || useradd -o -u "$PUID" -g "$PGID" -d /data -M -s /usr/sbin/nologin wsfs
    mkdir -p /data
    chown "$PUID:$PGID" /data
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
