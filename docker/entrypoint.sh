#!/bin/sh
# Démarrage du conteneur, convention Unraid : PUID/PGID (99:100 par défaut,
# nobody:users = arcade de SteamBox). Lancé en root, prépare /data (Unraid le
# crée en root s'il n'existe pas) puis passe à cet utilisateur : les images
# créées ont le même propriétaire que les autres.
set -eu
PUID=${PUID:-99}
PGID=${PGID:-100}

# Réserve des sauvegardes (copie gardée par le manager) : /backup par défaut
# (WSQUASHFS_MGR_RESERVE_DIR pour la mettre ailleurs). Elle doit être montée,
# sinon elle vit dans la couche du conteneur et disparaît à sa mise à jour.
RESERVE=${WSQUASHFS_MGR_RESERVE_DIR:-/backup}
if mountpoint -q "$RESERVE" 2>/dev/null; then
    echo "réserve des sauvegardes : $RESERVE"
else
    echo "ATTENTION : réserve des sauvegardes $RESERVE non montée, perdue à la mise à jour du conteneur" >&2
fi

if [ "$(id -u)" = 0 ]; then
    # Compte pour l'utilisateur du service (outils qui le cherchent dans passwd)
    getent group "$PGID" >/dev/null || groupadd -o -g "$PGID" wsfs
    getent passwd "$PUID" >/dev/null || useradd -o -u "$PUID" -g "$PGID" -d /data -M -s /usr/sbin/nologin wsfs
    mkdir -p /data
    chown "$PUID:$PGID" /data
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
