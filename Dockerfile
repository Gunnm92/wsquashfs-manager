# wsquashfs-manager — conteneur séparé de SteamBox (SPEC § 7, point 2).
#
# squashfs-tools 4.7 compilé ici (Debian trixie n'a que la 4.6.1, dont
# unsquashfs -pf plante sur une valeur d'attribut étendu binaire) :
# mksquashfs/unsquashfs -percentage pour la progression, unsquashfs -pf pour
# la reconstruction sans montage ni extraction (ni FUSE ni privilège),
# attributs user.* gardés. openssl pour le certificat du port des machines.
ARG SQUASHFS_VERSION=4.7.5
ARG SQUASHFS_SHA256=547b7b7f4d2e44bf91b6fc554664850c69563701deab9fd9cd7e21f694c88ea6

FROM python:3.12-slim-trixie AS squashfs
ARG SQUASHFS_VERSION
ARG SQUASHFS_SHA256
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
        zlib1g-dev liblzma-dev liblz4-dev liblzo2-dev libzstd-dev \
 && curl -fsSL -o /tmp/squashfs.tar.gz \
        "https://github.com/plougher/squashfs-tools/archive/refs/tags/${SQUASHFS_VERSION}.tar.gz" \
 && echo "${SQUASHFS_SHA256}  /tmp/squashfs.tar.gz" | sha256sum -c - \
 && tar -C /tmp -xzf /tmp/squashfs.tar.gz \
 && make -C "/tmp/squashfs-tools-${SQUASHFS_VERSION}/squashfs-tools" -j"$(nproc)" \
 && install -m 755 "/tmp/squashfs-tools-${SQUASHFS_VERSION}/squashfs-tools/mksquashfs" \
        "/tmp/squashfs-tools-${SQUASHFS_VERSION}/squashfs-tools/unsquashfs" /usr/local/bin/

FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl \
        zlib1g liblzma5 liblz4-1 liblzo2-2 libzstd1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=squashfs /usr/local/bin/mksquashfs /usr/local/bin/unsquashfs /usr/local/bin/
RUN unsquashfs -version | head -1 && mksquashfs -version | head -1

# Utilisateur des images et sauvegardes : PUID/PGID (99:100 par défaut,
# nobody:users d'Unraid = arcade dans SteamBox), appliqué au démarrage par
# docker/entrypoint.sh. /data appartient à cet utilisateur.
RUN useradd -o -u 99 -g 100 -d /data -M -s /usr/sbin/nologin arcade \
 && install -d -o 99 -g 100 /data
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Installation éditable : le code reste dans /app, d'où sont calculés les
# chemins de frontend/ et rules/ (une installation classique les perdrait).
WORKDIR /app
COPY backend/pyproject.toml backend/requirements.lock backend/
COPY backend/app backend/app
# Versions figées (requirements.lock) : une reconstruction de l'image installe ce qui a été testé
RUN pip install --no-cache-dir -c backend/requirements.lock -e ./backend
COPY backend/rules backend/rules
COPY frontend frontend

ENV HOME=/data \
    WSQUASHFS_MGR_HOST=0.0.0.0 \
    WSQUASHFS_MGR_PORT=8765 \
    WSQUASHFS_MGR_ROMS_DIRS=/roms \
    WSQUASHFS_MGR_SAVES_DIR=/saves \
    WSQUASHFS_MGR_STATE_DIR=/data/state \
    WSQUASHFS_MGR_RESERVE_DIR=/backup \
    PUID=99 \
    PGID=100 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
EXPOSE 8765 8790
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Refuse de démarrer sans WSQUASHFS_MGR_PASSWORD (écoute réseau)
CMD ["wsquashfs-manager"]
