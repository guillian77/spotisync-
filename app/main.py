"""Application web Spotisync (FastAPI + HTMX)."""
import json
import os
import time
from pathlib import Path
from urllib.parse import quote_plus, urlencode

import threading

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (config, db, downloader, importer, metadata, notify, search,
                spotify, sync)
from .downloader import manager

app = FastAPI(title=config.APP_NAME, version=config.VERSION)
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    db.init()
    sync.scheduler.start()
    threading.Thread(target=_resume_on_start, daemon=True).start()


def _resume_on_start(delay: float = 3.0) -> None:
    """Reprend les téléchargements interrompus par l'arrêt du conteneur."""
    time.sleep(delay)
    if db.setting("resume_on_start", "1") != "1":
        return
    # une piste restée 'queued'/'running' vient forcément d'un arrêt brutal
    db.qx("UPDATE tracks SET status='pending' WHERE status IN ('queued','running') "
          "AND file_path IS NULL")
    pending = db.q1("SELECT COUNT(*) c FROM tracks WHERE wanted=1 AND status='pending'")["c"]
    if not pending:
        return
    try:
        manager.start(include_errors=False)
    except RuntimeError:
        return
    try:
        notify.resume_started(pending)
    except Exception:  # noqa: BLE001
        pass


# --- helpers ---------------------------------------------------------------

def stats() -> dict:
    rows = db.q("SELECT status, COUNT(*) c FROM tracks GROUP BY status")
    by_status = {r["status"]: r["c"] for r in rows}
    total = db.q1("SELECT COUNT(*) c FROM tracks")["c"]
    size = db.q1("SELECT SUM(file_size) s FROM tracks WHERE status='done'")["s"] or 0
    dur = db.q1("SELECT SUM(duration_ms) s FROM tracks WHERE status='done'")["s"] or 0
    return {
        "total": total,
        "pending": by_status.get("pending", 0),
        "queued": by_status.get("queued", 0),
        "running": by_status.get("running", 0),
        "done": by_status.get("done", 0),
        "dupe": by_status.get("dupe", 0),
        "error": by_status.get("error", 0),
        "size_gb": round(size / 1024 ** 3, 2),
        "hours": round(dur / 3_600_000, 1),
        "playlists": db.q1("SELECT COUNT(*) c FROM playlists")["c"],
        "selected": db.q1("SELECT COUNT(*) c FROM playlists WHERE selected=1")["c"],
    }


def account_state() -> dict:
    acc = db.get_account()
    if not acc:
        return {"connected": False}
    profile_ok = bool(acc["spotify_user_id"] or acc["display_name"])
    return {
        "connected": True,
        "profile_ok": profile_ok,
        "name": acc["display_name"] or acc["spotify_user_id"] or "profil refusé",
        "product": acc["product"] or "inconnu",
        "premium": (acc["product"] or "") == "premium",
        "expires_at": acc["expires_at"],
    }


def writable(path: str) -> bool:
    """Test d'écriture réel : un chemin de travers ne doit pas se découvrir à 900 téléchargements."""
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".spotisync-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def sync_state() -> dict:
    """Dernière synchronisation effective + prochaine échéance théorique."""
    import datetime
    row = db.q1("SELECT MAX(finished_at) f FROM runs WHERE kind LIKE 'sync:%' AND status='done'")
    last = (row["f"] if row else "") or ""
    enabled = db.setting("autosync_enabled", "0") == "1"
    interval = db.setting_int("autosync_interval_min", 360)
    nxt = ""
    if enabled and last:
        try:
            dt = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
            nxt = (dt + datetime.timedelta(minutes=interval)).strftime("%d/%m %H:%M")
        except ValueError:
            nxt = ""
    return {
        "last": last[:16].replace("T", " "),
        "enabled": enabled,
        "interval": interval,
        "next": nxt,
        "interval_h": (interval // 60) if interval % 60 == 0 else round(interval / 60, 1),
    }


def controls_state() -> dict:
    return {
        "manager": manager.status(),
        "scan": dict(downloader.scan_state),
        "errors": db.q1("SELECT COUNT(*) c FROM tracks WHERE status='error'")["c"],
        "pending": db.q1("SELECT COUNT(*) c FROM tracks WHERE status IN ('pending','queued')")["c"],
    }


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("stats", stats())
    ctx.setdefault("account", account_state())
    ctx.setdefault("cfg", db.all_settings())
    ctx.setdefault("app_name", config.APP_NAME)
    ctx.setdefault("version", config.VERSION)
    state = controls_state()
    ctx.setdefault("manager", state["manager"])
    ctx.setdefault("scan", state["scan"])
    ctx.setdefault("err_count", state["errors"])
    ctx.setdefault("pending_count", state["pending"])
    ctx.setdefault("sync_state", sync_state())
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/?msg={urlencode({'k': kind, 'v': message})}",
                            status_code=303)


# --- pages -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    runs = db.last_runs(8)
    recent = db.q(
        "SELECT * FROM tracks WHERE status='done' ORDER BY updated_at DESC LIMIT 8")
    msg = request.query_params.get("msg")
    err = request.query_params.get("err")
    notice = ({"k": "err", "v": err} if err
              else {"k": "info", "v": msg} if msg else None)
    return render(request, "dashboard.html", runs=runs, recent=recent, notice=notice,
                  duplicates_count=len(downloader.duplicates()) if stats()["done"] else 0)


@app.get("/playlists", response_class=HTMLResponse)
def playlists_page(request: Request):
    rows = db.q("SELECT * FROM playlists ORDER BY selected DESC, is_liked DESC, name")
    return render(request, "playlists.html", playlists=rows,
                  selected_count=sum(1 for r in rows if r["selected"]),
                  total_tracks=db.q1("SELECT COUNT(*) c FROM tracks")["c"])


@app.post("/playlists/refresh")
def playlists_refresh():
    try:
        sync.refresh_playlists()
    except spotify.SpotifyError as exc:
        return RedirectResponse("/playlists?err=" + str(exc), status_code=303)
    except spotify.NotConnected:
        return RedirectResponse("/settings?err=Compte+Spotify+non+lié", status_code=303)
    return RedirectResponse("/playlists", status_code=303)


@app.post("/playlists/{pid}/toggle")
def playlists_toggle(pid: str):
    row = db.q1("SELECT selected FROM playlists WHERE id=?", (pid,))
    if row:
        db.qx("UPDATE playlists SET selected=? WHERE id=?",
              (0 if row["selected"] else 1, pid))
    return RedirectResponse("/playlists", status_code=303)


@app.post("/playlists/{pid}/delete")
def playlists_delete(pid: str):
    if not pid.startswith("import:"):
        return RedirectResponse("/playlists?err=Seules+les+playlists+import%C3%A9es+peuvent+%C3%AAtre+retir%C3%A9es",
                                status_code=303)
    db.qx("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
    db.qx("DELETE FROM playlists WHERE id=?", (pid,))
    return RedirectResponse("/playlists?ok=Import+retir%C3%A9+de+la+liste", status_code=303)


@app.post("/sync")
def do_sync():
    try:
        summary = sync.sync_selected()
    except spotify.NotConnected:
        return RedirectResponse("/settings?err=Compte+Spotify+non+lié", status_code=303)
    except spotify.SpotifyError as exc:
        return RedirectResponse("/playlists?err=" + str(exc), status_code=303)
    msg = f"{summary['new']} nouveau(x) titre(s) ajouté(s) à la file"
    if summary.get("skipped"):
        msg += f" · {summary['skipped']} playlist(s) importée(s) ignorée(s)"
    return RedirectResponse("/playlists?ok=" + quote_plus(msg), status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", status: str = "", page: int = 1):
    page = max(1, page)
    per = 50
    where, args = [], []
    if q:
        where.append("(t.title LIKE ? OR t.artists LIKE ? OR t.album LIKE ?)")
        args += [f"%{q}%"] * 3
    if status:
        where.append("t.status = ?")
        args.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.q1(f"SELECT COUNT(*) c FROM tracks t {clause}", tuple(args))["c"]
    rows = db.q(f"SELECT t.*, (SELECT COUNT(*) FROM rejected_sources r WHERE r.track_id=t.id) "
                f"AS rejected FROM tracks t {clause} ORDER BY t.artists, t.title "
                f"LIMIT {per} OFFSET {(page - 1) * per}", tuple(args))
    pages = max(1, (total + per - 1) // per)
    ctx = dict(tracks=rows, q=q, status=status, page=page, pages=pages, total=total)
    # Filtre en direct : HTMX n'a besoin que du tableau, pas de la page entière
    # (le compteur et lui seul changent, donc il voyage avec le fragment).
    if request.headers.get("HX-Request") == "true":
        return render(request, "_library_results.html", **ctx)
    return render(request, "library.html", **ctx)


@app.post("/library/scan")
def library_scan():
    started = downloader.scan_async()
    if not started:
        return RedirectResponse("/?msg=Analyse%20d%C3%A9j%C3%A0%20en%20cours", status_code=303)
    return RedirectResponse("/?msg=Analyse%20du%20dossier%20musique%20lanc%C3%A9e", status_code=303)



@app.post("/library/{tid}/delete")
def library_delete(tid: int, q: str = Form(""), status: str = Form("")):
    back = f"/library?q={quote_plus(q)}&status={quote_plus(status)}"
    row = db.q1("SELECT * FROM tracks WHERE id=?", (tid,))
    if row is None:
        return RedirectResponse(back, status_code=303)
    old = row["file_path"]
    moved = False
    if old and Path(old).exists():
        moved = downloader.quarantine([old]) > 0  # le fichier reste récupérable
    db.qx("DELETE FROM rejected_sources WHERE track_id=?", (tid,))
    db.qx("DELETE FROM playlist_tracks WHERE track_id=?", (tid,))
    db.qx("DELETE FROM tracks WHERE id=?", (tid,))
    msg = f"« {row['title']} » supprimée de la bibliothèque"
    if moved:
        msg += " · fichier déplacé en quarantaine"
    return RedirectResponse(back + "&redl=" + quote_plus(msg), status_code=303)


@app.post("/library/{tid}/redownload")
def library_redownload(tid: int, q: str = Form(""), status: str = Form("")):
    back = f"/library?q={quote_plus(q)}&status={quote_plus(status)}"
    row = db.q1("SELECT * FROM tracks WHERE id=?", (tid,))
    if row is None:
        return RedirectResponse(back, status_code=303)
    old = row["file_path"]
    moved = False
    if old and Path(old).exists():
        moved = downloader.quarantine([old]) > 0  # réversible depuis la page Doublons
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.qx("UPDATE tracks SET status='pending', file_path=NULL, file_size=NULL, error=NULL,"
          " attempts=0, updated_at=? WHERE id=?", (now, tid))
    rejected = len(downloader.rejected_sources(tid))
    msg = f"« {row['title']} » remise en attente"
    if moved:
        msg += " · ancien fichier en quarantaine"
    if rejected:
        msg += f" · {rejected} vidéo(s) déjà écartée(s) seront ignorées"
    return RedirectResponse(back + "&redl=" + quote_plus(msg), status_code=303)


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request):
    groups = downloader.duplicates()
    total = sum(len(g["items"]) for g in groups)
    return render(request, "duplicates.html", groups=groups, total=total)


@app.post("/duplicates/quarantine")
async def duplicates_quarantine(request: Request):
    form = await request.form()
    paths = [v for k, v in form.multi_items() if k == "paths"]
    moved = downloader.quarantine(paths)
    return RedirectResponse(f"/duplicates?moved={moved}", status_code=303)


@app.post("/download/start")
def download_start(limit: str = Form("")):
    if manager.active():
        return RedirectResponse("/?msg=Un+t%C3%A9l%C3%A9chargement+est+d%C3%A9j%C3%A0+en+cours",
                                status_code=303)
    try:
        manager.start(limit=int(limit) if limit.strip() else None)
    except RuntimeError as exc:
        return RedirectResponse("/?msg=" + str(exc), status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/download/stop")
def download_stop():
    if not manager.active():
        return RedirectResponse("/?msg=Aucun+t%C3%A9l%C3%A9chargement+%C3%A0+arr%C3%AAter",
                                status_code=303)
    manager.stop()
    return RedirectResponse("/", status_code=303)


@app.post("/download/retry")
def download_retry():
    if not db.q1("SELECT 1 FROM tracks WHERE status='error' LIMIT 1"):
        return RedirectResponse("/?msg=Aucun+%C3%A9chec+%C3%A0+relancer", status_code=303)
    db.qx("UPDATE tracks SET status='pending', error=NULL WHERE status='error'")
    try:
        manager.start()
    except RuntimeError:
        pass
    return RedirectResponse("/", status_code=303)


@app.get("/controls", response_class=HTMLResponse)
def controls(request: Request):
    return render(request, "_controls.html")


@app.get("/progress", response_class=HTMLResponse)
def progress(request: Request):
    return render(request, "_controls.html")


@app.get("/api/status")
def api_status():
    return JSONResponse({"stats": stats(), "manager": manager.status(),
                         "account": account_state() if db.get_account() else {"connected": False}})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, err: str = "", ok: str = ""):
    return render(request, "settings.html", err=err, ok=ok,
                  formats=config.FORMATS, bitrates=config.BITRATES,
                  writable_music=writable(db.setting("music_dir")),
                  writable_config=writable(str(config.CONFIG_DIR)))


@app.post("/settings/notify-test")
async def settings_notify_test(request: Request):
    # le bouton est dans le formulaire de réglages : on enregistre l'URL avant de tester
    form = await request.form()
    if "discord_webhook" in form:
        db.set_setting("discord_webhook", str(form["discord_webhook"]).strip())
    ok, detail = notify.test()
    param = "ok" if ok else "err"
    return RedirectResponse(f"/settings?{param}=" + quote_plus("Notification : " + detail),
                            status_code=303)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    keys = ["spotify_client_id", "spotify_client_secret", "spotify_redirect_uri",
            "music_dir", "output_template", "audio_format", "audio_bitrate",
            "threads", "js_runtime", "overwrite_mode", "dupe_threshold",
            "quarantine_subdir", "autosync_interval_min", "discord_webhook",
            "audio_providers", "reject_keywords", "duration_tolerance_s"]
    for k in keys:
        if k in form:
            db.set_setting(k, str(form[k]).strip())
    for k in ("normalize_artists", "autosync_enabled", "autosync_download",
              "notify_download_done", "notify_download_errors", "notify_autosync",
              "notify_resume", "resume_on_start", "trust_db_metadata"):
        db.set_setting(k, "1" if form.get(k) else "0")
    return RedirectResponse("/settings?ok=Réglages+enregistrés", status_code=303)


# --- import de fichiers (voie de secours sans API) -------------------------

@app.get("/import")
def import_page():
    """Ancien onglet : l'import vit désormais sur la page Playlists."""
    return RedirectResponse("/playlists", status_code=303)


@app.post("/import")
async def import_upload(file: UploadFile = File(...), name: str = Form("")):
    try:
        data = await file.read()
        if not data:
            raise ValueError("Fichier vide")
        res = importer.import_file(data, file.filename or "", name)
    except (ValueError, json.JSONDecodeError) as exc:
        return RedirectResponse("/playlists?err=" + quote_plus(str(exc)), status_code=303)
    msg = (f"{res['new']} nouveaux, {res['updated']} mis à jour — "
           f"{res['total']} titres dans « {res['playlist_name']} »")
    return RedirectResponse("/playlists?ok=" + quote_plus(msg), status_code=303)


# --- recherche unifiée -----------------------------------------------------

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", track: int = 0):
    kind, value = search.detect(q) if q.strip() else ("text", "")
    result: dict = {"kind": kind}
    if track:
        row = db.q1("SELECT * FROM tracks WHERE id=?", (track,))
        if row:
            result["replace_track"] = dict(row)
            result["kind"] = "replace"
    if kind == "text" and q.strip():
        result.update(search.search_local(value))
    elif kind == "spotify_playlist":
        try:
            playlist = search.fetch_spotify_playlist(value)
            result.update(playlist=playlist, tracks=playlist["tracks"])
        except Exception as exc:  # noqa: BLE001 - réseau, page absente, playlist privée
            result["error"] = str(exc)
    elif kind == "youtube_video":
        result["video"] = value
        result["error_tracks"] = [dict(r) for r in db.q(
            "SELECT id, title, artists FROM tracks WHERE status='error' ORDER BY id LIMIT 25")]
    elif kind == "youtube_playlist":
        try:
            result.update(yt_playlist=downloader.list_youtube_playlist(value))
        except Exception as exc:  # noqa: BLE001 - yt-dlp réseau, playlist privée, ID invalide
            result["error"] = f"Playlist YouTube illisible : {exc}"
    return render(request, "search.html", q=q, result=result)


@app.post("/search/youtube")
def search_youtube(video: str = Form(...), track_id: str = Form("")):
    track = db.q1("SELECT * FROM tracks WHERE id=?", (int(track_id),)) if track_id.strip() else None
    if track is not None and track["file_path"] and Path(track["file_path"]).exists():
        downloader.quarantine([track["file_path"]])  # remplacement : l'ancien fichier reste récupérable
    status, detail = downloader.download_youtube(video, dict(track) if track else None)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if track is None and status == downloader.STATUS_DONE:
        # téléchargement autonome : sans ligne en base, il n'apparaîtrait pas dans la bibliothèque
        info = detail.get("info") or {}
        db.qx(
            "INSERT INTO tracks(spotify_id,title,artists,album,album_artist,disc_no,track_no,"
            "release_date,duration_ms,url,status,file_path,file_size,wanted,key_norm,"
            "updated_at) VALUES(?,?,?,?,?,1,1,'',?,?,'done',?,?,0,?,?) "
            "ON CONFLICT(spotify_id) DO UPDATE SET status='done', file_path=excluded.file_path,"
            " file_size=excluded.file_size, error=NULL, updated_at=excluded.updated_at",
            (f"yt:{detail.get('video', '')}",
             info.get("title") or detail.get("match") or "Sans titre",
             info.get("artist") or "",
             info.get("album") or "",
             info.get("album_artist") or info.get("artist") or "",
             int(float(info.get("duration_s") or 0) * 1000),
             f"https://www.youtube.com/watch?v={detail.get('video', '')}",
             detail["path"], detail["size"],
             metadata.norm_key(info.get("title") or "", info.get("artist") or ""), now))
    if track is not None:
        if status == downloader.STATUS_DONE:
            db.qx("UPDATE tracks SET status='done', file_path=?, file_size=?, error=NULL,"
                  " updated_at=? WHERE id=?",
                  (detail["path"], detail["size"], now, track["id"]))
        elif status == downloader.STATUS_DUPE:
            db.qx("UPDATE tracks SET status='dupe', error=?, updated_at=? WHERE id=?",
                  (str(detail), now, track["id"]))
        else:
            db.qx("UPDATE tracks SET status='error', error=?, updated_at=? WHERE id=?",
                  (str(detail)[:400], now, track["id"]))
    message = ("Vidéo enregistrée" if status == downloader.STATUS_DONE else str(detail))
    if status == downloader.STATUS_DONE:
        message += " — " + detail["path"]
        expected = int(track["duration_ms"] or 0) if track is not None else 0
        actual = float(detail.get("duration_s") or 0)
        if expected and actual and abs(actual - expected / 1000) > max(
                8, expected / 1000 * 0.04):
            message += (f" · ATTENTION durée {actual:.0f} s contre "
                        f"{expected/1000:.0f} s attendues : vérifiez qu'il ne s'agit pas "
                        f"d'une version live")
    return RedirectResponse("/search?q=" + quote_plus(str(detail.get("video", video))
                                                      if isinstance(detail, dict) else video)
                            + "&msg=" + quote_plus(message), status_code=303)


@app.post("/search/yt-playlist")
def search_yt_playlist(url: str = Form(...)):
    try:
        info = downloader.list_youtube_playlist(url)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/search?q=" + quote_plus(url) + "&err=" + quote_plus(str(exc)),
                                status_code=303)
    entries = info["entries"]
    if not entries:
        return RedirectResponse("/search?q=" + quote_plus(url)
                                + "&err=" + quote_plus("Playlist vide"), status_code=303)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    added = 0
    for e in entries:
        sid = f"yt:{e['id']}"
        if db.q1("SELECT 1 FROM tracks WHERE spotify_id=?", (sid,)):
            continue
        db.qx(
            "INSERT INTO tracks(spotify_id,title,artists,album,disc_no,track_no,duration_ms,"
            "url,status,wanted,key_norm,updated_at) VALUES(?,?,?,?,1,1,?,?,'pending',1,?,?)",
            (sid, metadata.clean_title(e["title"]) or e["id"], "", info["title"],
             e["duration_ms"],
             f"https://www.youtube.com/watch?v={e['id']}",
             metadata.norm_key(metadata.clean_title(e["title"]), ""), now))
        added += 1
    msg = (f"{added} vidéo(s) ajoutée(s) depuis « {info['title']} » — "
           f"lancez le téléchargement depuis le tableau de bord")
    return RedirectResponse("/?msg=" + quote_plus(msg), status_code=303)


@app.post("/search/spotify")
def search_spotify(playlist_id: str = Form(...)):
    try:
        playlist = search.fetch_spotify_playlist(playlist_id)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/search?q=" + quote_plus(playlist_id) + "&err=" + quote_plus(str(exc)),
                                status_code=303)
    rows = search.spotify_embed_tracks(playlist)
    if not rows:
        return RedirectResponse("/search?q=" + quote_plus(playlist_id)
                                + "&err=" + quote_plus("Aucune piste lisible"), status_code=303)
    res = importer.import_tracks(rows, playlist["name"])
    msg = (f"{res['new']} nouveaux, {res['updated']} mis à jour — "
           f"{res['total']} titres dans « {res['playlist_name']} »")
    if playlist.get("truncated"):
        msg += " (50 premiers titres seulement : utilisez Exportify pour la playlist complète)"
    return RedirectResponse("/playlists?ok=" + quote_plus(msg), status_code=303)



# --- OAuth -----------------------------------------------------------------

@app.get("/auth")
def auth_start():
    if not db.setting("spotify_client_id"):
        return RedirectResponse("/settings?err=Client+ID+Spotify+manquant", status_code=303)
    return RedirectResponse(spotify.auth_url())


@app.get("/callback", response_class=HTMLResponse)
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return render(request, "callback.html", error=f"Autorisation refusée : {error}")
    if not code:
        return render(request, "callback.html", error="Code d'autorisation absent")
    if state and state != db.setting("_pkce_state"):
        return render(request, "callback.html", error="État OAuth invalide (relancez la liaison)")
    try:
        me = spotify.exchange_code(code)
        sync.refresh_playlists()
        return RedirectResponse("/playlists?ok=Compte+lié", status_code=303)
    except spotify.SpotifyError as exc:
        hint = ("Votre compte ne semble pas être Premium : l'API Spotify refuse les "
                "applications developer dont le propriétaire n'est pas abonné Premium. "
                "En attendant, importez un CSV Exportify."
                if exc.premium else "")
        return render(request, "callback.html", error=str(exc), hint=hint)
    except Exception as exc:  # noqa: BLE001
        return render(request, "callback.html", error=f"Échec de la liaison : {exc}")


@app.post("/auth/disconnect")
def auth_disconnect():
    db.clear_account()
    return RedirectResponse("/settings", status_code=303)


_static_dir = BASE / "static"
if _static_dir.is_dir():  # le dossier peut être absent (git ne suit pas les dossiers vides)
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
