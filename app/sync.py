"""Synchronisation des playlists Spotify vers la base locale."""
import threading
import time

from . import db, spotify

LIKED = "__liked__"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def refresh_playlists() -> int:
    """Récupère la liste des playlists et le total des titres likés."""
    items = spotify.list_playlists()
    liked = spotify.liked_total()
    count = 0
    for p in items:
        row = db.q1("SELECT selected FROM playlists WHERE id=?", (p["id"],))
        selected = row["selected"] if row else 0
        db.qx(
            "INSERT INTO playlists(id,name,owner,description,image,total,selected,is_liked,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,'0',?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, owner=excluded.owner,"
            "description=excluded.description, image=excluded.image, total=excluded.total,"
            "snapshot_id=excluded.snapshot_id",
            (p["id"], p["name"], p["owner"], p["description"], p["image"],
             p["total"], selected, p["snapshot_id"]))
        count += 1
    row = db.q1("SELECT selected FROM playlists WHERE id=?", (LIKED,))
    db.qx(
        "INSERT INTO playlists(id,name,owner,description,image,total,selected,is_liked,"
        "snapshot_id) VALUES(?,?,?,?,?,?,?,'1','') "
        "ON CONFLICT(id) DO UPDATE SET total=excluded.total",
        (LIKED, "Titres likés", "Moi", "Bibliothèque Spotify", "", liked,
         row["selected"] if row else 0))
    return count + 1


def sync_tracks(playlist_id: str) -> dict:
    if playlist_id.startswith("import:"):
        raise RuntimeError("Playlist importée : réimportez le fichier, pas de synchronisation Spotify")
    tracks = spotify.playlist_tracks(playlist_id)
    run_id = db.run_start("sync:" + playlist_id, len(tracks))
    new = known = 0
    for t in tracks:
        exists = db.q1("SELECT id FROM tracks WHERE spotify_id=?", (t["spotify_id"],))
        key = _key(t)
        if exists:
            tid = exists["id"]
            known += 1
        else:
            tid = db.qx(
                "INSERT INTO tracks(spotify_id,isrc,title,artists,album,album_artist,"
                "disc_no,track_no,release_date,duration_ms,explicit,popularity,cover_url,"
                "url,added_at,status,key_norm,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (t["spotify_id"], t["isrc"], t["title"], t["artists"], t["album"],
                 t["album_artist"], t["disc_no"], t["track_no"], t["release_date"],
                 t["duration_ms"], t["explicit"], t["popularity"], t["cover_url"],
                 t["url"], t["added_at"], key, _now()))
            new += 1
        db.qx("INSERT INTO playlist_tracks(playlist_id,track_id,added_at) VALUES(?,?,?) "
              "ON CONFLICT(playlist_id,track_id) DO UPDATE SET added_at=excluded.added_at",
              (playlist_id, tid, t["added_at"]))
    db.qx("UPDATE playlists SET last_synced=? WHERE id=?", (_now(), playlist_id))
    db.run_finish(run_id, "done", f"{new} nouvelles, {known} connues")
    return {"total": len(tracks), "new": new, "known": known}


def _key(t: dict) -> str:
    from .metadata import norm_key
    return norm_key(t.get("title", ""), t.get("artists", ""))


def sync_selected(start_download: bool | None = None) -> dict:
    rows = db.q("SELECT id FROM playlists WHERE selected=1")
    if start_download is None:
        start_download = db.setting("autosync_download", "1") == "1"
    summary = {"playlists": 0, "new": 0, "skipped": 0}
    for r in rows:
        try:
            res = sync_tracks(r["id"])
        except RuntimeError:
            summary["skipped"] += 1
            continue
        summary["playlists"] += 1
        summary["new"] += res["new"]
    if start_download and summary["new"]:
        from .downloader import manager
        if not manager.active():
            try:
                manager.start()
            except RuntimeError:
                pass
    return summary


class Scheduler:
    """Auto-sync périodique léger (thread + sleep), activable dans les réglages."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if db.setting("autosync_enabled", "0") == "1":
                    interval = max(5, db.setting_int("autosync_interval_min", 360)) * 60
                    if db.get_account():
                        from .downloader import manager
                        if not manager.active():
                            sync_selected()
                            self._last = _now()
                    self._stop.wait(interval)
                else:
                    self._stop.wait(60)
            except Exception:  # noqa: BLE001 - le scheduler ne doit jamais mourir
                self._stop.wait(300)

    @property
    def last_run(self):
        return self._last


scheduler = Scheduler()
