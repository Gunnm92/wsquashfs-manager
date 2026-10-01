#!/usr/bin/env bash
# Lance wsquashfs-manager.
#
#   ./run.sh            service (http://127.0.0.1:8765 par défaut)
#   ./run.sh --dev      rechargement automatique du code (développement)
#
# Au premier lancement, l'environnement Python est créé dans backend/.venv et
# les dépendances installées ; ensuite seulement si pyproject.toml a changé.
# Réglages : variables WSQUASHFS_MGR_* ou fichier backend/.env (voir README).
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BACKEND="$ROOT/backend"
VENV="$BACKEND/.venv"
STAMP="$VENV/.pyproject.sha256"

command -v python3 >/dev/null || { echo "Erreur : python3 introuvable (3.11 ou plus)"; exit 1; }
for tool in unsquashfs mksquashfs; do
    command -v "$tool" >/dev/null || echo "Attention : $tool absent (squashfs-tools) — lecture et reconstruction impossibles"
done

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Création de l'environnement Python ($VENV)…"
    python3 -m venv "$VENV"
fi
sum=$(sha256sum "$BACKEND/pyproject.toml" | cut -d' ' -f1)
if [[ "$(cat "$STAMP" 2>/dev/null)" != "$sum" ]]; then
    echo "Installation des dépendances…"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -e "$BACKEND"
    echo "$sum" > "$STAMP"
fi

cd "$BACKEND"        # backend/.env est lu depuis le dossier courant
if [[ "${1:-}" == "--dev" ]]; then
    exec "$VENV/bin/uvicorn" app.main:app \
        --host "${WSQUASHFS_MGR_HOST:-127.0.0.1}" --port "${WSQUASHFS_MGR_PORT:-8765}" \
        --reload --reload-dir app --reload-dir ../frontend
fi
# Refuse l'écoute réseau sans mot de passe (l'application réécrit les images)
exec "$VENV/bin/wsquashfs-manager"
