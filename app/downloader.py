"""Téléchargement via spotDL : staging, dédoublonnage, rangement, tags."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, metadata, spotify
from .config import CONFIG_DIR, FFMPEG_BIN, LOG_DIR, SPOTDL_BIN, STAGING_DIR

STATUS_PENDING = "pending"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_DUPE = "dupe"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"


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
        self._procs: set = set()
        self._procs_lock = threading.Lock()
        self.stopping = False

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
            "stopping": self.stopping,
        }

    # --- cycle de vie -----------------------------------------------------
    def start(self, limit: int | None = None, track_ids: list[int] | None = None,
              include_errors: bool = True) -> int:
        with self._lock:
            if self.active():
                raise RuntimeError("Un téléchargement est déjà en cours")
            if track_ids:
                marks = ",".join("?" * len(track_ids))
                rows = db.q(
                    f"SELECT * FROM tracks WHERE id IN ({marks}) AND status != 'done'",
                    tuple(track_ids))
            else:
                statuses = "('pending','error')" if include_errors else "('pending')"
                sql = (f"SELECT * FROM tracks WHERE wanted=1 AND status IN {statuses} "
                       "ORDER BY id")
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
            self.stopping = False
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
        """Arrêt immédiat : on tue les sous-processus spotDL en cours, pas seulement la file."""
        self.stopping = True
        self._stop.set()
        db.qx("UPDATE tracks SET status='pending' WHERE status='queued'")
        self._kill_running()

    # --- sous-processus en cours -----------------------------------------
    def register(self, proc) -> None:
        with self._procs_lock:
            self._procs.add(proc)

    def unregister(self, proc) -> None:
        with self._procs_lock:
            self._procs.discard(proc)

    def _kill_running(self) -> None:
        with self._procs_lock:
            procs = list(self._procs)
        for proc in procs:
            _kill_tree(proc)
        # les pistes interrompues repartiront au prochain lancement
        db.qx("UPDATE tracks SET status='pending' WHERE status='running' AND file_path IS NULL")

    def _watch(self) -> None:
        pool = self._pool
        if pool:
            pool.shutdown(wait=True)
        with self._lock:
            self._pool = None
        self.stopping = False
        stopped = self._stop.is_set()
        if self.run_id:
            status = "stopped" if stopped else "done"
            db.run_finish(self.run_id, status,
                          f"{self.done} réussis, {self.failed} échecs")
        if not stopped:
            try:
                from . import notify
                notify.download_finished(self.done, self.failed, self.total)
            except Exception:  # noqa: BLE001 - une notification ne casse jamais la fin de lot
                pass

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
        elif status == STATUS_STOPPED:
            db.qx("UPDATE tracks SET status='pending', error=NULL, updated_at=? WHERE id=?",
                  (now, track_id))
            return
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

def js_runtime() -> str:
    """Runtime JS réellement disponible : Deno est requis par yt-dlp pour les challenges
    de YouTube, Node ne suffit pas (et le paquet Debian est trop ancien)."""
    import shutil
    preferred = (db.setting("js_runtime", "deno") or "").strip()
    for candidate in ([preferred] if preferred else []) + ["deno", "node"]:
        if candidate and shutil.which(candidate):
            return candidate
    return ""


def _yt_dlp_args() -> list[str]:
    runtime = js_runtime()
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


def _kill_tree(proc, grace: float = 5.0) -> None:
    """Tue le groupe de processus complet (spotDL + yt-dlp + ffmpeg).

    Un simple terminate() ne tue que le shell intermédiaire : ses enfants gardent le
    tube de sortie ouvert, donc communicate() bloque jusqu'à leur fin naturelle."""
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue


LIVE_DEFAULT = "live,concert,en concert,in concert,session,rehearsal,karaoke,instrumental"


def _lookup_failure(message: str) -> bool:
    """Vrai si l'échec vient de la résolution Spotify interne à spotDL (SpotipyFree),
    et non de YouTube : dans ce cas inutile d'insister sur l'URL, une requête texte marche."""
    m = (message or "").lower()
    return any(k in m for k in ("keyerror: 'uri'", "no results", "could not get song info",
                                "invalid json", "songerror"))


def _result_name(stdout: str) -> str:
    m = re.search(r'Downloaded\s+"([^"]+)"', stdout or "")
    return m.group(1) if m else ""


def _reject_keywords() -> list[str]:
    try:
        raw = db.setting("reject_keywords", LIVE_DEFAULT) or ""
    except Exception:  # noqa: BLE001 - fonction pure : utilisable hors base (tests, outils)
        raw = LIVE_DEFAULT
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _has_keyword(text: str, keyword: str) -> bool:
    """Correspondance sur mot entier : « live » ne doit pas matcher « alive »."""
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text) is not None


def _alternate_version(result_name: str, title: str) -> str:
    """Repère une version live/concert là où le titre Spotify n'en annonce pas."""
    if not result_name:
        return ""
    name = metadata.strip_accents(result_name.lower())
    expected = metadata.strip_accents((title or "").lower())
    for kw in _reject_keywords():
        if _has_keyword(name, kw) and not _has_keyword(expected, kw):
            return kw
    return ""


def _duration_problem(actual_s: float, expected_ms: int) -> tuple[bool, float]:
    if not actual_s or not expected_ms:
        return False, 0.0
    expected = expected_ms / 1000.0
    delta = abs(actual_s - expected)
    try:
        configured = db.setting_int("duration_tolerance_s", 8)
    except Exception:  # noqa: BLE001
        configured = 8
    tolerance = max(configured, expected * 0.04)
    return delta > tolerance, round(delta, 1)


def _candidates(track: dict) -> list[tuple[str, str]]:
    """Ordre d'essai : URL Spotify, puis recherche texte (contourne SpotipyFree cassé)."""
    out: list[tuple[str, str]] = []
    url = track.get("url") or (
        f"https://open.spotify.com/track/{track['spotify_id']}" if track.get("spotify_id") else "")
    if url:
        out.append(("spotify", url))
    artists = metadata.split_artists(track.get("artists") or "")
    title = (track.get("title") or "").strip()
    if title:
        base = f"{artists[0]} - {title}" if artists else title
        out.append(("recherche", base))
        album = (track.get("album") or "").strip()
        if album:
            out.append(("recherche+album", f"{base} {album}"))
    return out


def _run_spotdl(query: str, staging: Path, fmt: str, bitrate: str):
    """Exécute spotDL une fois. Renvoie (audio|None, stdout, stderr, code, interrompu)."""
    providers = [p for p in (db.setting("audio_providers", "youtube-music youtube") or "").split()
                 if p]
    cmd = [SPOTDL_BIN, "download", query,
           "--output", "{title}", "--format", fmt, "--bitrate", bitrate,
           "--overwrite", "force", "--simple-tui", "--print-errors"]
    if providers:
        cmd += ["--audio", *providers]
    cmd += _yt_dlp_args()

    from types import SimpleNamespace
    proc = subprocess.Popen(cmd, cwd=staging, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=run_env(),
                            start_new_session=True)
    manager.register(proc)
    try:
        out, err = proc.communicate(timeout=900)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        code = -1
    finally:
        manager.unregister(proc)

    interrupted = manager.stopping or manager._stop.is_set()
    audio = None if interrupted else metadata.find_audio(staging)
    if audio is None and not interrupted and code == -1:
        err = (err or "") + "\ndélai dépassé"
    return audio, out, err, code, interrupted


def download_one(track: dict) -> tuple[str, dict | str]:
    staging = Path(STAGING_DIR) / str(track.get("spotify_id") or "unknown")
    fmt = db.setting("audio_format", "mp3")
    bitrate = db.setting("audio_bitrate", "320k")
    reasons: list[str] = []

    for kind, query in _candidates(track):
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        audio, out, err, code, interrupted = _run_spotdl(query, staging, fmt, bitrate)
        if interrupted:
            shutil.rmtree(staging, ignore_errors=True)
            return STATUS_STOPPED, "interrompu par l'utilisateur"
        if audio is None:
            from types import SimpleNamespace
            result = SimpleNamespace(returncode=code, stdout=out, stderr=err)
            message = error_excerpt(result)
            reasons.append(f"{kind} : {message}")
            if kind == "spotify" and _lookup_failure(message):
                log_failure(track, result)
            continue

        # --- contrôle du candidat : durée et version live -------------------
        name = _result_name(out)
        keyword = _alternate_version(name, track.get("title") or "")
        if keyword:
            reasons.append(f"{kind} : « {name} » semble une version {keyword}")
            shutil.rmtree(staging, ignore_errors=True)
            continue
        info = metadata.read_tags(audio)
        if not info.get("duration_s"):
            info.update(metadata.probe(audio))
        too_far, delta = _duration_problem(info.get("duration_s") or 0,
                                          int(track.get("duration_ms") or 0))
        if too_far:
            reasons.append(f"{kind} : durée {delta:.0f} s d'écart")
            shutil.rmtree(staging, ignore_errors=True)
            continue

        # --- tags : la base fait foi (Spotify), YouTube ne fournit que l'audio
        if db.setting("trust_db_metadata", "1") == "1":
            info["title"] = track.get("title") or info.get("title") or ""
            info["artist"] = (metadata.format_artists(track.get("artists") or "")
                              or info.get("artist") or "")
            info["album"] = track.get("album") or info.get("album") or ""
            info["album_artist"] = (metadata.format_artists(
                track.get("album_artist") or track.get("artists") or "")
                or info.get("album_artist") or info["artist"])
            info["track"] = track.get("track_no") or info.get("track") or 1
            info["disc"] = track.get("disc_no") or info.get("disc") or 1
            info["year"] = (track.get("release_date") or info.get("year") or "")[:4]
            info["isrc"] = track.get("isrc") or info.get("isrc") or ""
        else:
            info["artist"] = metadata.normalize_artists(
                info.get("artist", ""), track.get("artists", ""))
            if not info.get("album_artist"):
                info["album_artist"] = info["artist"]
            if not info.get("title"):
                info["title"] = track.get("title") or ""
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
        return STATUS_DONE, {"path": str(target), "size": target.stat().st_size,
                             "source": kind, "match": name,
                             "rejected": reasons[:]}

    shutil.rmtree(staging, ignore_errors=True)
    if not reasons:
        return STATUS_ERROR, "Aucun candidat exploitable"
    return STATUS_ERROR, ("Correspondance refusée — " + " | ".join(reasons[-2:]))[:400]


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

# --- scan de la bibliothèque (arrière-plan) --------------------------------

scan_state = {"active": False, "started": "", "found": 0, "indexed": 0, "error": ""}
_scan_lock = threading.Lock()


def scan_async() -> bool:
    """Lance l'indexation sans bloquer la requête HTTP (l'UI peut suivre l'état)."""
    with _scan_lock:
        if scan_state["active"]:
            return False
        scan_state.update(active=True, found=0, indexed=0, error="",
                          started=time.strftime("%H:%M:%S"))

    def _run() -> None:
        try:
            res = scan_library()
            scan_state.update(found=res["found"], indexed=res["indexed"])
        except Exception as exc:  # noqa: BLE001
            scan_state["error"] = str(exc)[:300]
        finally:
            scan_state["active"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True

# --- téléchargement direct d'une vidéo YouTube (voie exacte) ----------------

_PROMO = re.compile(r"[\[\]\(\)](?:official|clip|video|audio|lyrics?|hd|hq|4k|"
                    r"remaster(?:ed)?|explicit|visualizer|live)[^\]\)]*[\]\)]", re.I)


def _ytdlp_bin() -> str:
    import shutil
    return shutil.which("yt-dlp") or "yt-dlp"


def _run(cmd: list[str], cwd: Path, timeout: int = 600):
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=run_env(),
                            start_new_session=True)
    manager.register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        code = -1
    finally:
        manager.unregister(proc)
    return code, out, err


def _split_youtube_title(raw: str) -> tuple[str, str]:
    """« Artiste - Titre (Official Video) » -> (« Artiste », « Titre »)."""
    clean = _PROMO.sub("", raw or "").strip(" -")
    if " - " in clean:
        artist, title = clean.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", clean or "Sans titre"


_MEDIA_EXT = {".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3", ".aac", ".flac", ".wav"}


def _find_any_media(directory) -> Path | None:
    """yt-dlp sort souvent du .webm ou du .m4a : on prend le plus gros fichier média."""
    best = None
    for root, _dirs, files in os.walk(directory):
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() in _MEDIA_EXT and not name.startswith("out."):
                if best is None or p.stat().st_size > best.stat().st_size:
                    best = p
    return best

def download_youtube(video: str, track: dict | None = None) -> tuple[str, dict | str]:
    """Télécharge une vidéo YouTube précise (aucune recherche, aucun risque de live).

    Si `track` est fourni, l'audio remplace la piste en base : les tags Spotify priment.
    """
    import uuid
    ref = video.strip()
    url = ref if ref.startswith("http") else f"https://www.youtube.com/watch?v={ref}"
    staging = Path(STAGING_DIR) / f"yt-{uuid.uuid4().hex[:10]}"
    staging.mkdir(parents=True, exist_ok=True)
    fmt = db.setting("audio_format", "mp3")
    bitrate = db.setting("audio_bitrate", "320k")
    runtime = js_runtime()
    js_args = ["--js-runtimes", runtime] if runtime else []

    try:
        code, out, err = _run(
            [_ytdlp_bin(), "--no-playlist", "--skip-download", *js_args, "--print",
             "%(id)s\n%(title)s\n%(uploader)s\n%(duration)s", url], staging, timeout=180)
        if code != 0:
            return STATUS_ERROR, ("yt-dlp : " + (err or out or "échec").strip().splitlines()[-1])[:300]
        lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
        video_id, yt_title = (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")
        yt_uploader = lines[2] if len(lines) > 2 else ""
        yt_duration = float(lines[3]) if len(lines) > 3 and lines[3].replace(".", "").isdigit() else 0.0

        if manager.stopping or manager._stop.is_set():
            return STATUS_STOPPED, "interrompu par l'utilisateur"

        code, out, err = _run(
            [_ytdlp_bin(), "--no-playlist", *js_args, "-f", "bestaudio/best",
             "-o", "src.%(ext)s", url], staging, timeout=900)
        source = _find_any_media(staging)
        if source is None:
            return STATUS_ERROR, ("yt-dlp : " + (err or out or "aucun fichier").strip().splitlines()[-1])[:300]
        if manager.stopping or manager._stop.is_set():
            shutil.rmtree(staging, ignore_errors=True)
            return STATUS_STOPPED, "interrompu par l'utilisateur"

        converted = staging / f"out.{fmt}"
        ff_args = ["-y", "-i", str(source), "-vn"]
        if fmt == "mp3":
            ff_args += ["-codec:a", "libmp3lame", "-b:a", bitrate if bitrate != "auto" else "320k"]
        elif fmt in ("m4a",):
            ff_args += ["-codec:a", "aac", "-b:a", bitrate if bitrate != "auto" else "256k"]
        elif fmt in ("opus", "ogg"):
            ff_args += ["-codec:a", "libopus", "-b:a", bitrate if bitrate != "auto" else "160k"]
        else:
            ff_args += ["-codec:a", "flac"]
        code, out, err = _run([FFMPEG_BIN, *ff_args, str(converted)],
                              staging, timeout=600)
        if not converted.exists():
            return STATUS_ERROR, ("ffmpeg : " + (err or "").strip().splitlines()[-1])[:300]

        if track:
            info = {
                "title": track.get("title") or yt_title,
                "artist": metadata.format_artists(track.get("artists") or "") or yt_uploader,
                "album": track.get("album") or "",
                "album_artist": metadata.format_artists(
                    track.get("album_artist") or track.get("artists") or "") or yt_uploader,
                "track": track.get("track_no") or 1,
                "disc": track.get("disc_no") or 1,
                "year": (track.get("release_date") or "")[:4],
                "isrc": track.get("isrc") or "",
                "duration_s": yt_duration,
                "cover_url": track.get("cover_url") or "",
            }
        else:
            artist, title = _split_youtube_title(yt_title)
            info = {"title": title, "artist": artist or yt_uploader,
                    "album": "", "album_artist": artist or yt_uploader,
                    "track": 1, "disc": 1, "year": "", "isrc": "",
                    "duration_s": yt_duration}

        dupe = find_duplicate(track or {"spotify_id": f"yt:{video_id}"}, info)
        if dupe:
            shutil.rmtree(staging, ignore_errors=True)
            return STATUS_DUPE, f"doublon de {dupe}"

        rel = metadata.build_relpath(db.setting("output_template"), info)
        target = Path(db.setting("music_dir")) / f"{rel}.{fmt}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target = metadata.unique_path(target)
        shutil.move(str(converted), str(target))
        metadata.write_tags(target, info)
        return STATUS_DONE, {"path": str(target), "size": target.stat().st_size,
                             "source": "youtube", "video": video_id,
                             "match": yt_title, "duration_s": yt_duration}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
