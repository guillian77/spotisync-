"""Téléchargement via spotDL : staging, dédoublonnage, rangement, tags."""
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, metadata, spotify
from .config import CONFIG_DIR, LOG_DIR, SPOTDL_BIN, STAGING_DIR

STATUS_PENDING = "pending"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_DUPE = "dupe"
STATUS_ERROR = "error"


class DownloadManager:
    """Un seul lot à la fois ; l'état vit en base, la reprise est possible."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self.run_id: int | None = None
        self.total = 0
        self.done = 0
        self.failed = 0
        self.current: list[str] = []

    # --- état -------------------------------------------------------------
    def active(self) -> bool:
        return self._pool is not None

    def status(self) -> dict:
        return {
            "active": self.active(),
            "run_id": self.run_id,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "current": self.current[-5:],
        }

    # --- cycle de vie -----------------------------------------------------
    def start(self, limit: int | None = None, track_ids: list[int] | None = None) -> int:
        with self._lock:
            if self.active():
                raise RuntimeError("Un téléchargement est déjà en cours")
            if track_ids:
                marks = ",".join("?" * len(track_ids))
                rows = db.q(
                    f"SELECT * FROM tracks WHERE id IN ({marks}) AND status != 'done'",
                    tuple(track_ids))
            else:
                sql = ("SELECT * FROM tracks WHERE wanted=1 AND status IN "
                       "('pending','error') ORDER BY id")
                if limit:
                    sql += f" LIMIT {int(limit)}"
                rows = db.q(sql)
            if not rows:
                raise RuntimeError("Aucune piste à télécharger")
            ids = [r["id"] for r in rows]
            for i in ids:
                db.qx("UPDATE tracks SET status='queued', updated_at=? WHERE id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), i))
            self._stop.clear()
            self.run_id = db.run_start("download", len(ids))
            self.total, self.done, self.failed = len(ids), 0, 0
            self.current = []
            workers = max(1, min(db.setting_int("threads", 2), 6))
            self._pool = ThreadPoolExecutor(max_workers=workers)
            for tid in ids:
                self._pool.submit(self._download_by_id, tid)
            threading.Thread(target=self._watch, daemon=True).start()
            return self.run_id

    def stop(self) -> None:
        self._stop.set()
        db.qx("UPDATE tracks SET status='pending' WHERE status='queued'")

    def _watch(self) -> None:
        pool = self._pool
        if pool:
            pool.shutdown(wait=True)
        with self._lock:
            self._pool = None
        if self.run_id:
            status = "stopped" if self._stop.is_set() else "done"
            db.run_finish(self.run_id, status,
                          f"{self.done} réussis, {self.failed} échecs")

    # --- travail ----------------------------------------------------------
    def _download_by_id(self, track_id: int) -> None:
        if self._stop.is_set():
            return
        row = db.q1("SELECT * FROM tracks WHERE id=?", (track_id,))
        if row is None:
            return
        db.qx("UPDATE tracks SET status='running', updated_at=? WHERE id=?",
              (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), track_id))
        self.current.append(f"{row['artists']} — {row['title']}")
        try:
            outcome = download_one(dict(row))
        except Exception as exc:  # noqa: BLE001 - on journalise tout
            outcome = ("error", str(exc))
            with open(LOG_DIR / "download-errors.log", "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%F %T')}] track {track_id}\n{traceback.format_exc()}\n")
        status, detail = outcome
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if status == STATUS_DONE:
            self.done += 1
            db.qx("UPDATE tracks SET status='done', file_path=?, file_size=?, error=NULL,"
                  " attempts=attempts+1, updated_at=? WHERE id=?",
                  (detail.get("path"), detail.get("size"), now, track_id))
        elif status == STATUS_DUPE:
            self.done += 1
            db.qx("UPDATE tracks SET status='dupe', error=?, attempts=attempts+1,"
                  " updated_at=? WHERE id=?", (detail, now, track_id))
        else:
            self.failed += 1
            db.qx("UPDATE tracks SET status='error', error=?, attempts=attempts+1,"
                  " updated_at=? WHERE id=?", (detail[:500], now, track_id))
        if self.run_id:
            db.run_update(self.run_id, done=self.done, failed=self.failed)


manager = DownloadManager()


# --- une piste -------------------------------------------------------------

def _yt_dlp_args() -> list[str]:
    runtime = db.setting("js_runtime", "node")
    if not runtime:
        return []
    return ["--yt-dlp-args", f"--js-runtimes {runtime}"]


def run_env() -> dict:
    """Environnement explicite pour spotDL/yt-dlp : leur cache et leurs fichiers
    temporaires doivent rester dans /config, jamais dans le HOME de root (uid 99
    n'y a pas accès — cause d'un PermissionError sur /root/.config)."""
    base = str(CONFIG_DIR)
    env = dict(os.environ)
    env["HOME"] = base
    env["XDG_CONFIG_HOME"] = os.path.join(base, ".config")
    env["XDG_CACHE_HOME"] = os.path.join(base, ".cache")
    env["TMPDIR"] = os.path.join(base, "tmp")
    for path in (env["XDG_CONFIG_HOME"], env["XDG_CACHE_HOME"], env["TMPDIR"],
                 os.path.join(base, "staging")):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    return env


def error_excerpt(proc) -> str:
    """Retient la ligne d'exception réelle plutôt que le début de la trace."""
    import re
    text = "\n".join(filter(None, [proc.stderr or "", proc.stdout or ""]))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in reversed(lines):
        if re.search(r"\w*(Error|Exception)\b\s*:", line):
            return line[:400]
    return " | ".join(lines[-3:])[:400] or "Aucun fichier produit"


def log_failure(track: dict, proc) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "download-errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%F %T')} | {track.get('spotify_id')} | "
                     f"{track.get('artists')} — {track.get('title')} | code {proc.returncode}\n")
            fh.write((proc.stderr or "")[-4000:])
            fh.write((proc.stdout or "")[-2000:])
    except OSError:
        pass


def download_one(track: dict) -> tuple[str, dict | str]:
    url = track.get("url") or ""
    if not url and track.get("spotify_id"):
        url = f"https://open.spotify.com/track/{track['spotify_id']}"
    if not url:
        return STATUS_ERROR, "URL Spotify absente"

    staging = Path(STAGING_DIR) / str(track["spotify_id"])
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    fmt = db.setting("audio_format", "mp3")
    bitrate = db.setting("audio_bitrate", "320k")

    cmd = [SPOTDL_BIN, "download", url,
           "--output", "{title}", "--format", fmt, "--bitrate", bitrate,
           "--overwrite", "force", "--simple-tui", "--print-errors"] + _yt_dlp_args()
    proc = subprocess.run(cmd, cwd=staging, capture_output=True, text=True,
                          timeout=900, env=run_env())
    audio = metadata.find_audio(staging)
    if audio is None:
        log_failure(track, proc)
        return STATUS_ERROR, error_excerpt(proc)

    info = metadata.read_tags(audio)
    if not info.get("duration_s"):
        info.update(metadata.probe(audio))
    info["artist"] = metadata.normalize_artists(info.get("artist", ""), track.get("artists", ""))
    if not info.get("album_artist"):
        info["album_artist"] = info["artist"]
    if not info.get("title"):
        info["title"] = track["title"]
    if not info.get("album"):
        info["album"] = track.get("album") or ""
    info["year"] = (info.get("year") or (track.get("release_date") or ""))[:4]
    info["track"] = info.get("track") or track.get("track_no") or 1
    info["isrc"] = info.get("isrc") or track.get("isrc") or ""

    dupe = find_duplicate(track, info)
    if dupe:
        shutil.rmtree(staging, ignore_errors=True)
        return STATUS_DUPE, f"doublon de {dupe}"

    rel = metadata.build_relpath(db.setting("output_template"), info)
    target = Path(db.setting("music_dir")) / f"{rel}.{fmt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target = metadata.unique_path(target)
    shutil.move(str(audio), str(target))
    shutil.rmtree(staging, ignore_errors=True)
    metadata.write_tags(target, info)
    return STATUS_DONE, {"path": str(target), "size": target.stat().st_size}


def find_duplicate(track: dict, info: dict) -> str | None:
    """Doublon = même ISRC, ou titre/artiste très proche + durée voisine,
    parmi les fichiers déjà récupérés (base tracks) et scannés (table files)."""
    isrc = (info.get("isrc") or track.get("isrc") or "").strip().upper()
    if isrc:
        hit = db.q1("SELECT file_path FROM tracks WHERE isrc=? AND status='done' "
                    "AND spotify_id != ?", (isrc, track.get("spotify_id")))
        if hit:
            return hit["file_path"]
        hit = db.q1("SELECT path FROM files WHERE isrc=? AND quarantined=0", (isrc,))
        if hit:
            return hit["path"]

    key = metadata.norm_key(info.get("title") or track["title"], info.get("artist") or "")
    dur = int(info.get("duration_s") or 0)
    threshold = db.setting_int("dupe_threshold", 92)
    candidates = db.q(
        "SELECT title, artists, duration_ms, file_path, isrc FROM tracks "
        "WHERE status='done' AND spotify_id != ? "
        "AND (key_norm = ? OR key_norm LIKE ?) LIMIT 40",
        (track.get("spotify_id"), key, key.split("|")[0] + "|%"))
    for c in candidates:
        if abs(int(c["duration_ms"] or 0) // 1000 - dur) > 6:
            continue
        score = metadata.similarity(
            metadata.norm_key(c["title"], c["artists"]), key)
        if score >= threshold:
            return f"{c['file_path']} (score {score:.0f})"
    return None


# --- scan de la bibliothèque ----------------------------------------------

def scan_library() -> dict:
    root = Path(db.setting("music_dir"))
    if not root.exists():
        raise RuntimeError(f"Dossier musique introuvable : {root}")
    run_id = db.run_start("scan")
    found = added = 0
    seen = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            if p.suffix.lower() not in metadata.AUDIO_EXT:
                continue
            found += 1
            seen.add(str(p))
            st = p.stat()
            old = db.q1("SELECT id, mtime FROM files WHERE path=?", (str(p),))
            if old and abs((old["mtime"] or 0) - st.st_mtime) < 1:
                continue
            info = metadata.read_tags(p)
            if not info.get("duration_s"):
                info.update(metadata.probe(p))
            key = metadata.norm_key(info.get("title", ""), info.get("artist", ""))
            if old:
                db.qx("UPDATE files SET size=?,mtime=?,isrc=?,title=?,artists=?,album=?,"
                      "duration_s=?,bitrate=?,fmt=?,key_norm=?,scanned_at=? WHERE id=?",
                      (st.st_size, st.st_mtime, info.get("isrc", ""), info.get("title", ""),
                       info.get("artist", ""), info.get("album", ""), info.get("duration_s"),
                       info.get("bitrate"), info.get("fmt"), key,
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), old["id"]))
            else:
                db.qx("INSERT INTO files(path,size,mtime,isrc,title,artists,album,duration_s,"
                      "bitrate,fmt,key_norm,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (str(p), st.st_size, st.st_mtime, info.get("isrc", ""),
                       info.get("title", ""), info.get("artist", ""), info.get("album", ""),
                       info.get("duration_s"), info.get("bitrate"), info.get("fmt"), key,
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            added += 1
    db.run_finish(run_id, "done", f"{found} fichiers, {added} indexés")
    return {"found": found, "indexed": added}


def duplicates(threshold: int | None = None) -> list[dict]:
    """Groupes de doublons : ISRC identique, sinon clé normalisée proche."""
    threshold = threshold or db.setting_int("dupe_threshold", 92)
    rows = db.q("SELECT * FROM files WHERE quarantined=0 ORDER BY artists, title")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        isrc = (r["isrc"] or "").strip().upper()
        key = f"isrc:{isrc}" if isrc else f"key:{r['key_norm'] or ''}"
        groups.setdefault(key, []).append(dict(r))
    out = []
    for key, items in groups.items():
        if len(items) > 1:
            out.append({"reason": "ISRC" if key.startswith("isrc:") else "clé",
                        "key": key.split(":", 1)[1], "items": items})
    # fusion floue des groupes « clé » proches
    fuzzy = [g for g in out if g["reason"] == "clé"]
    merged: list[dict] = []
    while fuzzy:
        base = fuzzy.pop(0)
        i = 0
        while i < len(fuzzy):
            other = fuzzy[i]
            if metadata.similarity(base["key"], other["key"]) >= threshold:
                base["items"].extend(other["items"])
                fuzzy.pop(i)
            else:
                i += 1
        merged.append(base)
    out = [g for g in out if g["reason"] != "clé"] + merged
    return [g for g in out if len(g["items"]) > 1]


def quarantine(paths: list[str]) -> int:
    root = Path(db.setting("music_dir"))
    qdir = root / db.setting("quarantine_subdir", "_doublons")
    moved = 0
    for p in paths:
        src = Path(p)
        if not src.exists():
            continue
        dest = qdir / src.name
        qdir.mkdir(parents=True, exist_ok=True)
        dest = metadata.unique_path(dest)
        try:
            shutil.move(str(src), str(dest))
            db.qx("UPDATE files SET quarantined=1, path=? WHERE path=?", (str(dest), str(src)))
            moved += 1
        except OSError:
            continue
    return moved
