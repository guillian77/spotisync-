FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SPOTISYNC_CONFIG=/config \
    SPOTISYNC_MUSIC=/music

# ffmpeg : ré-encodage spotDL ; nodejs : runtime JS exigé par yt-dlp ; gosu : PUID/PGID
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs gosu curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

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
