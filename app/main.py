"""Application web Spotisync (FastAPI + HTMX)."""
import json
import os
import time
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, downloader, importer, metadata, spotify, sync
from .downloader import manager

app = FastAPI(title=config.APP_NAME, version=config.VERSION)
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    db.init()
    sync.scheduler.start()


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


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("stats", stats())
    ctx.setdefault("account", account_state())
    ctx.setdefault("cfg", db.all_settings())
    ctx.setdefault("app_name", config.APP_NAME)
    ctx.setdefault("version", config.VERSION)
    ctx.setdefault("manager", manager.status())
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
    notice = {"k": "info", "v": msg} if msg else None
    return render(request, "dashboard.html", runs=runs, recent=recent, notice=notice,
                  duplicates_count=len(downloader.duplicates()) if stats()["done"] else 0)


@app.get("/playlists", response_class=HTMLResponse)
def playlists_page(request: Request, err: str = ""):
    rows = db.q("SELECT * FROM playlists ORDER BY selected DESC, is_liked DESC, name")
    return render(request, "playlists.html", playlists=rows, err=err)


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


@app.post("/sync")
def do_sync():
    try:
        summary = sync.sync_selected()
    except spotify.NotConnected:
        return RedirectResponse("/settings?err=Compte+Spotify+non+lié", status_code=303)
    except spotify.SpotifyError as exc:
        return RedirectResponse("/playlists?err=" + str(exc), status_code=303)
    return RedirectResponse(f"/playlists?ok={summary['new']}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", status: str = "", page: int = 1):
    page = max(1, page)
    per = 50
    where, args = [], []
    if q:
        where.append("(title LIKE ? OR artists LIKE ? OR album LIKE ?)")
        args += [f"%{q}%"] * 3
    if status:
        where.append("status = ?")
        args.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.q1(f"SELECT COUNT(*) c FROM tracks {clause}", tuple(args))["c"]
    rows = db.q(f"SELECT * FROM tracks {clause} ORDER BY artists, title "
                f"LIMIT {per} OFFSET {(page - 1) * per}", tuple(args))
    pages = max(1, (total + per - 1) // per)
    return render(request, "library.html", tracks=rows, q=q, status=status,
                  page=page, pages=pages, total=total)


@app.post("/library/scan")
def library_scan():
    try:
        res = downloader.scan_library()
    except RuntimeError as exc:
        return RedirectResponse(f"/library?q=&status=&scan_err={exc}", status_code=303)
    return RedirectResponse(f"/library?scanned={res['indexed']}", status_code=303)


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
    try:
        manager.start(limit=int(limit) if limit.strip() else None)
    except RuntimeError as exc:
        return RedirectResponse("/?msg=" + str(exc), status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/download/stop")
def download_stop():
    manager.stop()
    return RedirectResponse("/", status_code=303)


@app.post("/download/retry")
def download_retry():
    db.qx("UPDATE tracks SET status='pending', error=NULL WHERE status='error'")
    try:
        manager.start()
    except RuntimeError:
        pass
    return RedirectResponse("/", status_code=303)


@app.get("/progress", response_class=HTMLResponse)
def progress(request: Request):
    return render(request, "_progress.html")


@app.get("/api/status")
def api_status():
    return JSONResponse({"stats": stats(), "manager": manager.status(),
                         "account": account_state() if db.get_account() else {"connected": False}})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, err: str = "", ok: str = ""):
    return render(request, "settings.html", err=err, ok=ok,
                  formats=config.FORMATS, bitrates=config.BITRATES)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    keys = ["spotify_client_id", "spotify_client_secret", "spotify_redirect_uri",
            "music_dir", "output_template", "audio_format", "audio_bitrate",
            "threads", "js_runtime", "overwrite_mode", "dupe_threshold",
            "quarantine_subdir", "autosync_interval_min"]
    for k in keys:
        if k in form:
            db.set_setting(k, str(form[k]).strip())
    for k in ("normalize_artists", "autosync_enabled", "autosync_download"):
        db.set_setting(k, "1" if form.get(k) else "0")
    return RedirectResponse("/settings?ok=Réglages+enregistrés", status_code=303)


# --- import de fichiers (voie de secours sans API) -------------------------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, err: str = "", ok: str = ""):
    imports = db.q("SELECT * FROM playlists WHERE id LIKE 'import:%' "
                   "ORDER BY id DESC LIMIT 10")
    return render(request, "import.html", err=err, ok=ok, imports=imports)


@app.post("/import")
async def import_upload(file: UploadFile = File(...), name: str = Form("")):
    try:
        data = await file.read()
        if not data:
            raise ValueError("Fichier vide")
        res = importer.import_file(data, file.filename or "", name)
    except (ValueError, json.JSONDecodeError) as exc:
        return RedirectResponse("/import?err=" + quote_plus(str(exc)), status_code=303)
    msg = (f"{res['new']} nouveaux, {res['updated']} mis à jour — "
           f"{res['total']} titres dans « {res['playlist_name']} »")
    return RedirectResponse("/import?ok=" + quote_plus(msg), status_code=303)


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
