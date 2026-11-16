"""Recherche unifiée : détection du type de saisie, recherche locale, playlist Spotify.

Le champ unique du tableau de bord accepte :
  - un nom de titre / d'artiste / d'album  -> recherche locale
  - un nom de playlist                     -> recherche locale
  - un ID ou une URL de playlist Spotify   -> lecture de la page d'intégration (sans API)
  - un ID ou une URL YouTube               -> téléchargement de CETTE vidéo
"""
import json
import re
import urllib.request

from . import db, metadata

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^ ]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")
_YT_LIST = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_SP_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_SP_URL = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]{22})")


def detect(query: str) -> tuple[str, str]:
    """Renvoie (type, valeur) : youtube_video | youtube_playlist | spotify_playlist | text."""
    q = (query or "").strip()
    if not q:
        return "text", ""
    m = _SP_URL.search(q)
    if m:
        return "spotify_playlist", m.group(1)
    m = _YT_URL.search(q)
    if m:
        return ("youtube_playlist", m.group(0)) if _YT_LIST.search(q) else ("youtube_video", m.group(1))
    if _YT_LIST.search(q) and "youtube" in q:
        return "youtube_playlist", _YT_LIST.search(q).group(1)
    if _SP_ID.match(q) and re.search(r"\d", q) and re.search(r"[A-Za-z]", q):
        # un identifiant Spotify mêle lettres et chiffres ; un mot de 22 lettres n'en est pas un
        return "spotify_playlist", q
    if _YT_ID.match(q):
        return "youtube_video", q
    return "text", q


# --- recherche locale ------------------------------------------------------

def search_local(query: str, limit: int = 40) -> dict:
    like = f"%{query}%"
    tracks = db.q(
        "SELECT id, title, artists, album, status, error, spotify_id FROM tracks "
        "WHERE title LIKE ? OR artists LIKE ? OR album LIKE ? "
        "ORDER BY (status='error') DESC, artists, title LIMIT ?",
        (like, like, like, limit))
    playlists = db.q(
        "SELECT id, name, total, selected, is_liked FROM playlists WHERE name LIKE ? "
        "ORDER BY selected DESC, name LIMIT ?", (like, min(limit, 20)))
    return {"tracks": [dict(t) for t in tracks], "playlists": [dict(p) for p in playlists]}


# --- playlist Spotify via page d'intégration (pas d'API, pas de Premium) ---

def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        raise ValueError("Page Spotify illisible (format inattendu)")
    return json.loads(m.group(1))


def _find_key(node, key: str):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def fetch_spotify_playlist(playlist_id: str) -> dict:
    """Lit la playlist publique via open.spotify.com/embed. Limite : 50 premiers titres."""
    if not _SP_ID.match(playlist_id or ""):
        raise ValueError("Identifiant de playlist Spotify invalide")
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    data = _next_data(html)
    entity = _find_key(data, "trackList")
    if not isinstance(entity, list):
        raise ValueError("Aucune piste lisible dans cette page (playlist privée ?)")
    name = _find_key(data, "name") or f"Playlist {playlist_id}"
    cover = ""
    cover_art = _find_key(data, "coverArt") or {}
    if isinstance(cover_art, dict):
        sources = cover_art.get("sources") or []
        if sources:
            cover = sources[0].get("url", "")
    tracks = []
    for item in entity:
        uri = str(item.get("uri") or "")
        sid = uri.split(":")[-1] if uri.startswith("spotify:track:") else ""
        artists = re.sub(r"[\u00a0]", " ", str(item.get("subtitle") or "")).replace("\u2009", " ")
        tracks.append({
            "spotify_id": sid,
            "title": str(item.get("title") or "").strip(),
            "artists": artists.replace(", ", ", ").strip(),
            "duration_ms": int(item.get("duration") or 0),
            "explicit": 1 if item.get("isExplicit") else 0,
            "playable": bool(item.get("isPlayable", True)),
        })
    return {"id": playlist_id, "name": str(name), "cover": cover, "tracks": tracks,
            "truncated": len(tracks) >= 50}


def spotify_embed_tracks(playlist: dict) -> list[dict]:
    """Convertit les pistes lues dans la page en lignes importables (album inconnu)."""
    rows = []
    for t in playlist["tracks"]:
        rows.append({
            "spotify_id": t["spotify_id"],
            "isrc": "",
            "title": t["title"],
            "artists": metadata.format_artists(t["artists"]),
            "album": "",
            "album_artist": metadata.format_artists(t["artists"]),
            "disc_no": 1,
            "track_no": 1,
            "release_date": "",
            "duration_ms": t["duration_ms"],
            "explicit": t["explicit"],
            "popularity": 0,
            "cover_url": "",
            "url": (f"https://open.spotify.com/track/{t['spotify_id']}"
                    if t["spotify_id"] else ""),
            "added_at": "",
        })
    return rows
