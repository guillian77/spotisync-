#!/bin/sh
# Point d'entrée : aligne l'utilisateur sur PUID/PGID (convention Unraid) puis lance l'app.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"

if [ "$(id -u)" = "0" ]; then
  if ! getent group app >/dev/null 2>&1; then
    groupadd -g "$PGID" app 2>/dev/null || groupadd app
  fi
  if ! id -u app >/dev/null 2>&1; then
    useradd -u "$PUID" -g app -M -s /usr/sbin/nologin app 2>/dev/null || true
  fi
  mkdir -p /config/staging /config/logs "$SPOTISYNC_MUSIC"
  chown -R app:app /config 2>/dev/null || true
  umask "$UMASK"
  exec gosu app uvicorn app.main:app \
    --host 0.0.0.0 --port "${SPOTISYNC_PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${SPOTISYNC_PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips='*'
