"""Import de listes de titres depuis un fichier (Exportify CSV, export Spotify JSON).

Voie de secours quand l'API Spotify est inaccessible (compte non Premium) :
on lit les métadonnées du fichier, on les range dans les mêmes tables que
l'import API, et le téléchargement/dédoublonnage reste identique.
"""
import csv
import io
import json
import time

from . import db, metadata

# Alias d'en-têtes : Exportify FR / EN et variantes d'export Spotify
ALIASES = {
    "spotify_id": ["uri du titre", "track uri", "track uri(s)", "spotify_track_uri", "uri"],
    "title": ["nom du titre", "track name", "master_metadata_track_name", "title", "titre"],
    "artists": ["nom(s) de l'artiste", "artist name(s)", "artist name",
                "master_metadata_album_artist_name", "artists", "artiste(s)"],
    "album": ["nom de l'album", "album name", "master_metadata_album_album_name", "album"],
    "album_artist": ["nom(s) de l'artiste de l'album", "album artist name(s)",
                     "album artist name", "album_artist"],
    "release_date": ["date de sortie de l'album", "album release date", "release date"],
    "cover_url": ["url de l'image de l'album", "album image url", "image"],
    "disc_no": ["numéro de disque", "disc number"],
    "track_no": ["numéro du titre", "track number"],
    "duration_ms": ["durée du titre (ms)", "track duration (ms)", "duration_ms", "duration"],
    "explicit": ["explicite", "explicit"],
    "popularity": ["popularité", "popularity"],
    "isrc": ["isrc"],
    "added_at": ["ajouté le", "added at", "ts", "added_at"],
}


def _norm_header(name: str) -> str:
    return (name or "").strip().lower().replace("\ufeff", "")


def _build_map(fieldnames: list[str]) -> dict[str, str]:
    """Associe chaque champ interne à l'en-tête réel du fichier."""
    norm = {_norm_header(f): f for f in fieldnames if f}
    out = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in norm:
                out[field] = norm[alias]
                break
    return out


def _spotify_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("spotify:track:"):
        return value.split(":")[-1]
    if "/track/" in value:
        return value.split("/track/")[-1].split("?")[0]
    return value


def _int(value, default=0) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def _truthy(value) -> int:
    return 1 if str(value).strip().lower() in ("true", "1", "oui", "yes") else 0


def _row_to_track(raw: dict, mapping: dict[str, str]) -> dict | None:
    def g(field: str) -> str:
        key = mapping.get(field)
        return str(raw.get(key) or "").strip() if key else ""

    sid = _spotify_id(g("spotify_id"))
    title = g("title")
    if not title and not sid:
        return None
    artists = g("artists")
    album = g("album")
    return {
        "spotify_id": sid,
        "isrc": g("isrc").upper(),
        "title": title or "(sans titre)",
        "artists": artists,
        "album": album,
        "album_artist": g("album_artist") or artists,
        "disc_no": _int(g("disc_no"), 1) or 1,
        "track_no": _int(g("track_no"), 1) or 1,
        "release_date": g("release_date")[:10],
        "duration_ms": _int(g("duration_ms")),
        "explicit": _truthy(g("explicit")),
        "popularity": _int(g("popularity")),
        "cover_url": g("cover_url"),
        "url": f"https://open.spotify.com/track/{sid}" if sid else "",
        "added_at": g("added_at"),
    }


def parse_csv(text: str) -> list[dict]:
    text = text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []
    mapping = _build_map(list(reader.fieldnames))
    if "title" not in mapping:
        raise ValueError(
            "Colonnes non reconnues — export Exportify attendu "
            f"(en-têtes trouvés : {', '.join(reader.fieldnames[:5])}…)")
    rows = [t for t in (_row_to_track(r, mapping) for r in reader) if t]
    return rows


def parse_json(payload) -> list[dict]:
    """Accepte l'export officiel Spotify (liste de titres écoutés/likés) ou
    une liste d'objets déjà au format interne."""
    if isinstance(payload, dict):
        for key in ("items", "tracks", "songs", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("JSON non reconnu : liste de titres attendue")
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "title" in item and ("artists" in item or "artist" in item):
            # déjà au format interne (ou proche)
            artists = item.get("artists") or item.get("artist") or ""
            if isinstance(artists, list):
                artists = ", ".join(str(a) for a in artists)
            sid = _spotify_id(str(item.get("spotify_id") or item.get("uri") or item.get("url") or ""))
            out.append({
                "spotify_id": sid, "isrc": str(item.get("isrc") or "").upper(),
                "title": str(item.get("title") or "(sans titre)"), "artists": artists,
                "album": str(item.get("album") or ""),
                "album_artist": str(item.get("album_artist") or artists),
                "disc_no": _int(item.get("disc_no"), 1) or 1,
                "track_no": _int(item.get("track_no"), 1) or 1,
                "release_date": str(item.get("release_date") or "")[:10],
                "duration_ms": _int(item.get("duration_ms")),
                "explicit": _truthy(item.get("explicit")),
                "popularity": _int(item.get("popularity")),
                "cover_url": str(item.get("cover_url") or ""),
                "url": str(item.get("url") or
                           (f"https://open.spotify.com/track/{sid}" if sid else "")),
                "added_at": str(item.get("added_at") or ""),
            })
            continue
        # export officiel Spotify (historique / titres likés)
        title = item.get("master_metadata_track_name") or item.get("trackName") or ""
        artists = (item.get("master_metadata_album_artist_name")
                   or item.get("artistName") or "")
        album = (item.get("master_metadata_album_album_name")
                 or item.get("albumName") or "")
        sid = _spotify_id(str(item.get("spotify_track_uri") or ""))
        if not (title or sid):
            continue
        out.append({
            "spotify_id": sid, "isrc": "", "title": title or "(sans titre)",
            "artists": artists, "album": album, "album_artist": artists,
            "disc_no": 1, "track_no": 1,
            "release_date": str(item.get("releaseDate") or "")[:10],
            "duration_ms": _int(item.get("msPlayed") or item.get("duration_ms")),
            "explicit": 0, "popularity": 0, "cover_url": "",
            "url": f"https://open.spotify.com/track/{sid}" if sid else "",
            "added_at": str(item.get("ts") or ""),
        })
    return out


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    return "-".join(filter(None, keep.split("-")))[:48] or "import"


def import_tracks(rows: list[dict], playlist_name: str = "") -> dict:
    """Insère/met à jour les titres et crée une playlist d'import sélectionnée."""
    if not rows:
        return {"total": 0, "new": 0, "updated": 0, "skipped": 0}
    name = (playlist_name or "").strip() or ("Import " + time.strftime("%Y-%m-%d %H:%M"))
    pid = "import:" + _slug(name) + "-" + time.strftime("%Y%m%d%H%M%S")
    run_id = db.run_start("import:" + name, len(rows))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new = updated = skipped = 0
    db.qx("INSERT INTO playlists(id,name,owner,description,image,total,selected,is_liked,"
          "snapshot_id,last_synced) VALUES(?,?,?,?,?,?,1,'0','',?)",
          (pid, name, "Import", "Importé depuis un fichier", "", 0, now))
    for t in rows:
        key = metadata.norm_key(t["title"], t.get("artists", ""))
        existing = None
        if t["spotify_id"]:
            existing = db.q1("SELECT id FROM tracks WHERE spotify_id=?", (t["spotify_id"],))
        if existing is None and key:
            existing = db.q1("SELECT id FROM tracks WHERE key_norm=? AND title=?", (key, t["title"]))
        if existing:
            tid = existing["id"]
            db.qx("UPDATE tracks SET isrc=COALESCE(NULLIF(?,''),isrc), album=?, "
                  "album_artist=?, url=COALESCE(NULLIF(?,''),url), duration_ms=?, "
                  "release_date=?, cover_url=COALESCE(NULLIF(?,''),cover_url), updated_at=? "
                  "WHERE id=?",
                  (t["isrc"], t["album"], t["album_artist"], t["url"], t["duration_ms"],
                   t["release_date"], t["cover_url"], now, tid))
            updated += 1
        else:
            tid = db.qx(
                "INSERT INTO tracks(spotify_id,isrc,title,artists,album,album_artist,disc_no,"
                "track_no,release_date,duration_ms,explicit,popularity,cover_url,url,added_at,"
                "status,key_norm,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (t["spotify_id"] or None, t["isrc"], t["title"], t["artists"], t["album"],
                 t["album_artist"], t["disc_no"], t["track_no"], t["release_date"],
                 t["duration_ms"], t["explicit"], t["popularity"], t["cover_url"],
                 t["url"], t["added_at"], key, now))
            new += 1
        if not tid:
            skipped += 1
            continue
        db.qx("INSERT INTO playlist_tracks(playlist_id,track_id,added_at) VALUES(?,?,?) "
              "ON CONFLICT(playlist_id,track_id) DO NOTHING", (pid, tid, t["added_at"]))
    total = db.q1("SELECT COUNT(*) c FROM playlist_tracks WHERE playlist_id=?", (pid,))["c"]
    db.qx("UPDATE playlists SET total=? WHERE id=?", (total, pid))
    db.run_finish(run_id, "done", f"{new} nouveaux, {updated} mis à jour")
    return {"total": total, "new": new, "updated": updated,
            "skipped": skipped, "playlist_id": pid, "playlist_name": name}


def import_file(data: bytes, filename: str, playlist_name: str = "") -> dict:
    name = (filename or "").lower()
    if name.endswith(".json"):
        rows = parse_json(json.loads(data.decode("utf-8-sig")))
    elif name.endswith((".csv", ".txt", ".tsv")):
        rows = parse_csv(data.decode("utf-8-sig", errors="replace"))
    else:
        raise ValueError("Format non pris en charge : CSV Exportify ou JSON attendu")
    return import_tracks(rows, playlist_name)
