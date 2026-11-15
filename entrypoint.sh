#!/bin/sh
# Point d'entrée : aligne l'utilisateur sur PUID/PGID (convention Unraid) puis lance l'app.
#
# HOME est critique : spotDL/yt-dlp écrivent leur cache et leurs fichiers temporaires dans
# $HOME/.config. L'utilisateur applicatif n'a pas de répertoire personnel (useradd -M) et
# héritait donc de HOME=/root -> PermissionError dès le premier téléchargement.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
SPOTISYNC_PORT="${SPOTISYNC_PORT:-8080}"

export HOME=/config
export XDG_CONFIG_HOME=/config/.config
export XDG_CACHE_HOME=/config/.cache
export TMPDIR=/config/tmp

if [ "$(id -u)" = "0" ]; then
  if ! getent group app >/dev/null 2>&1; then
    groupadd -g "$PGID" app 2>/dev/null || groupadd app
  fi
  if ! id -u app >/dev/null 2>&1; then
    useradd -u "$PUID" -g app -M -s /usr/sbin/nologin app 2>/dev/null || true
  fi
  mkdir -p /config/staging /config/logs /config/tmp /config/.config /config/.cache
  mkdir -p "$SPOTISYNC_MUSIC"
  chown -R app:app /config 2>/dev/null || true
  umask "$UMASK"
  exec gosu app uvicorn app.main:app \
    --host 0.0.0.0 --port "$SPOTISYNC_PORT" --proxy-headers --forwarded-allow-ips='*'
fi

mkdir -p /config/staging /config/logs /config/tmp /config/.config /config/.cache
exec uvicorn app.main:app --host 0.0.0.0 --port "$SPOTISYNC_PORT" \
  --proxy-headers --forwarded-allow-ips='*'
