"""Configuration : chemins, variables d'environnement, réglages par défaut."""
import os
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


APP_NAME = "Spotisync"
VERSION = "0.3.0"

CONFIG_DIR = Path(_env("SPOTISYNC_CONFIG", "/config"))
DEFAULT_MUSIC_DIR = _env("SPOTISYNC_MUSIC", "/music")
STAGING_DIR = CONFIG_DIR / "staging"
LOG_DIR = CONFIG_DIR / "logs"
DB_PATH = CONFIG_DIR / "spotisync.db"

SPOTDL_BIN = _env("SPOTDL_BIN", "spotdl")
FFMPEG_BIN = _env("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = _env("FFPROBE_BIN", "ffprobe")

# Réglages persistés en base (table settings). Une variable d'environnement
# SPOTISYNC_<CLE en majuscules> écrase la valeur par défaut à la création.
DEFAULTS = {
    "spotify_client_id": _env("SPOTIFY_CLIENT_ID", ""),
    "spotify_client_secret": _env("SPOTIFY_CLIENT_SECRET", ""),
    "spotify_redirect_uri": _env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"),
    "music_dir": DEFAULT_MUSIC_DIR,
    "output_template": "{artist}/{album}/{track:02d} - {title}",
    "audio_format": "mp3",
    "audio_bitrate": "320k",
    "threads": "2",
    "js_runtime": "node",
    "normalize_artists": "1",
    "overwrite_mode": "skip",
    "dupe_threshold": "92",
    "quarantine_subdir": "_doublons",
    "autosync_enabled": "0",
    "autosync_interval_min": "360",
    "autosync_download": "1",
}

FORMATS = ["mp3", "opus", "m4a", "flac", "ogg", "wav"]
BITRATES = ["320k", "256k", "192k", "160k", "128k", "auto"]


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, STAGING_DIR, LOG_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
