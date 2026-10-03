# wsquashfs-manager — conteneur séparé de SteamBox (SPEC § 7, point 2).
#
# Debian trixie : squashfs-tools 4.6 (mksquashfs/unsquashfs -percentage,
# utilisés pour la progression), squashfuse et fuse-overlayfs pour la
# reconstruction sans extraction (mode overlay, /dev/fuse requis).
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends squashfs-tools squashfuse fuse-overlayfs fuse3 \
 && rm -rf /var/lib/apt/lists/*

# Utilisateur des images et sauvegardes : PUID/PGID (99:100 par défaut,
# nobody:users d'Unraid = arcade dans SteamBox), appliqué au démarrage par
# docker/entrypoint.sh. /data appartient à cet utilisateur.
RUN useradd -o -u 99 -g 100 -d /data -M -s /usr/sbin/nologin arcade \
 && install -d -o 99 -g 100 /data
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Installation éditable : le code reste dans /app, d'où sont calculés les
# chemins de frontend/ et rules/ (une installation classique les perdrait).
WORKDIR /app
COPY backend/pyproject.toml backend/
COPY backend/app backend/app
RUN pip install --no-cache-dir -e ./backend
COPY backend/rules backend/rules
COPY frontend frontend

ENV HOME=/data \
    WSQUASHFS_MGR_HOST=0.0.0.0 \
    WSQUASHFS_MGR_PORT=8765 \
    WSQUASHFS_MGR_ROMS_DIRS=/roms \
    WSQUASHFS_MGR_SAVES_DIR=/saves \
    WSQUASHFS_MGR_STATE_DIR=/data/state \
    PUID=99 \
    PGID=100 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Refuse de démarrer sans WSQUASHFS_MGR_PASSWORD (écoute réseau)
CMD ["wsquashfs-manager"]
