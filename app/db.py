"""Accès SQLite (WAL), schéma et helpers."""
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import CONFIG_DIR, DB_PATH, DEFAULTS

_local = threading.local()
_global_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    spotify_user_id TEXT,
    display_name    TEXT,
    product         TEXT,
    scope           TEXT,
    access_token    TEXT,
    refresh_token   TEXT,
    expires_at      REAL,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS playlists (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    owner        TEXT,
    description  TEXT,
    image        TEXT,
    total        INTEGER DEFAULT 0,
    selected     INTEGER DEFAULT 0,
    is_liked     INTEGER DEFAULT 0,
    snapshot_id  TEXT,
    last_synced  TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_id   TEXT UNIQUE,
    isrc         TEXT,
    title        TEXT NOT NULL,
    artists      TEXT,
    album        TEXT,
    album_artist TEXT,
    disc_no      INTEGER DEFAULT 1,
    track_no     INTEGER DEFAULT 1,
    release_date TEXT,
    duration_ms  INTEGER DEFAULT 0,
    explicit     INTEGER DEFAULT 0,
    popularity   INTEGER DEFAULT 0,
    cover_url    TEXT,
    url          TEXT,
    added_at     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    file_path    TEXT,
    file_size    INTEGER,
    wanted       INTEGER NOT NULL DEFAULT 1,
    key_norm     TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);
CREATE INDEX IF NOT EXISTS idx_tracks_norm ON tracks(key_norm);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL,
    track_id     INTEGER NOT NULL,
    added_at     TEXT,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE,
    size        INTEGER,
    mtime       REAL,
    isrc        TEXT,
    title       TEXT,
    artists     TEXT,
    album       TEXT,
    duration_s  REAL,
    bitrate     INTEGER,
    fmt         TEXT,
    key_norm    TEXT,
    quarantined INTEGER DEFAULT 0,
    scanned_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_isrc ON files(isrc);

CREATE TABLE IF NOT EXISTS rejected_sources (
    track_id   INTEGER NOT NULL,
    source_id  TEXT NOT NULL,
    kind       TEXT,
    reason     TEXT,
    created_at TEXT,
    PRIMARY KEY (track_id, source_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    message     TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    with _global_lock:
        if _conn is None:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=10000")
        return _conn


@contextmanager
def tx():
    conn = _connect()
    with _global_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, args: tuple = ()):
    conn = _connect()
    with _global_lock:
        return conn.execute(sql, args).fetchall()


def q1(sql: str, args: tuple = ()):
    rows = q(sql, args)
    return rows[0] if rows else None


def qx(sql: str, args: tuple = ()) -> int:
    """Exécute une écriture, renvoie lastrowid."""
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid or 0


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
    for k, v in DEFAULTS.items():
        if q1("SELECT 1 FROM settings WHERE key=?", (k,)) is None:
            qx("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))


# --- réglages ---------------------------------------------------------------

def setting(key: str, default: str = "") -> str:
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if r is None or r["value"] == "":
        return DEFAULTS.get(key, default)
    return r["value"]


def setting_int(key: str, default: int = 0) -> int:
    try:
        return int(setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value) -> None:
    qx(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)),
    )


def all_settings() -> dict:
    out = dict(DEFAULTS)
    for r in q("SELECT key,value FROM settings"):
        if r["value"] != "":
            out[r["key"]] = r["value"]
    return out


# --- compte ----------------------------------------------------------------

def get_account():
    return q1("SELECT * FROM account WHERE id=1")


def save_account(**kw) -> None:
    fields = ["spotify_user_id", "display_name", "product", "scope",
              "access_token", "refresh_token", "expires_at"]
    row = get_account()
    vals = {f: kw.get(f, row[f] if row else None) for f in fields}
    vals["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if row is None:
        qx(
            "INSERT INTO account(id,spotify_user_id,display_name,product,scope,"
            "access_token,refresh_token,expires_at,updated_at) VALUES(1,?,?,?,?,?,?,?,?)",
            tuple(vals[f] for f in fields) + (vals["updated_at"],),
        )
    else:
        qx(
            "UPDATE account SET spotify_user_id=?,display_name=?,product=?,scope=?,"
            "access_token=?,refresh_token=?,expires_at=?,updated_at=? WHERE id=1",
            tuple(vals[f] for f in fields) + (vals["updated_at"],),
        )


def clear_account() -> None:
    qx("DELETE FROM account WHERE id=1")


# --- runs ------------------------------------------------------------------

def run_start(kind: str, total: int = 0) -> int:
    return qx("INSERT INTO runs(kind,status,total) VALUES(?, 'running', ?)", (kind, total))


def run_update(run_id: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    qx(f"UPDATE runs SET {cols} WHERE id=?", tuple(kw.values()) + (run_id,))


def run_finish(run_id: int, status: str = "done", message: str = "") -> None:
    qx(
        "UPDATE runs SET status=?, message=?, finished_at=? WHERE id=?",
        (status, message, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), run_id),
    )


def last_runs(limit: int = 10):
    return q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
