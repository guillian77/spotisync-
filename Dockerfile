FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SPOTISYNC_CONFIG=/config \
    SPOTISYNC_MUSIC=/music

# ffmpeg : ré-encodage spotDL ; gosu : PUID/PGID ; unzip : installation de Deno
# Deno (runtime JS de yt-dlp) est installé explicitement : le paquet Debian nodejs est trop
# ancien pour les challenges JS de YouTube, d'où « Some YouTube downloads require Deno ».
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs gosu curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH=amd64
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) asset=aarch64-unknown-linux-gnu ;; \
      *) asset=x86_64-unknown-linux-gnu ;; \
    esac; \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${asset}.zip" -o /tmp/deno.zip; \
    unzip -o -q /tmp/deno.zip -d /usr/local/bin; \
    rm -f /tmp/deno.zip; \
    chmod +x /usr/local/bin/deno; \
    /usr/local/bin/deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /music

EXPOSE 8080
VOLUME ["/config", "/music"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD curl -fsS "http://127.0.0.1:${SPOTISYNC_PORT:-8080}/api/status" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
