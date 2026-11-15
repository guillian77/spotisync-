"""Client Spotify : OAuth PKCE, refresh de jeton, appels API et normalisation."""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import httpx

from . import db

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"

TRACK_FIELDS = (
    "id,name,duration_ms,explicit,popularity,isrc,external_ids,external_urls,"
    "track_number,disc_number,artists(name),album(name,release_date,images,"
    "artists(name))"
)


class SpotifyError(Exception):
    def __init__(self, status: int, message: str, premium: bool = False):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.premium = premium


class NotConnected(Exception):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    db.set_setting("_pkce_verifier", verifier)
    db.set_setting("_pkce_state", secrets.token_urlsafe(16))
    return verifier, challenge


def auth_url() -> str:
    _, challenge = new_pkce()
    params = {
        "client_id": db.setting("spotify_client_id"),
        "response_type": "code",
        "redirect_uri": db.setting("spotify_redirect_uri"),
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": db.setting("_pkce_state"),
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _token_request(data: dict) -> dict:
    with httpx.Client(timeout=30) as c:
        r = c.post(TOKEN_URL, data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    body = r.json() if r.content else {}
    if r.status_code != 200:
        raise SpotifyError(r.status_code, body.get("error_description") or str(body))
    return body


def exchange_code(code: str) -> dict:
    body = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": db.setting("spotify_redirect_uri"),
        "client_id": db.setting("spotify_client_id"),
        "code_verifier": db.setting("_pkce_verifier"),
    })
    expires_at = time.time() + int(body.get("expires_in", 3600)) - 30
    db.save_account(access_token=body["access_token"],
                    refresh_token=body.get("refresh_token"),
                    scope=body.get("scope"), expires_at=expires_at)
    me = api("/me")
    db.save_account(spotify_user_id=me.get("id"), display_name=me.get("display_name"),
                    product=me.get("product"))
    return me


def _refresh() -> str:
    acc = db.get_account()
    if acc is None or not acc["refresh_token"]:
        raise NotConnected("Aucun compte Spotify lié")
    body = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": acc["refresh_token"],
        "client_id": db.setting("spotify_client_id"),
        "client_secret": db.setting("spotify_client_secret"),
    })
    expires_at = time.time() + int(body.get("expires_in", 3600)) - 30
    db.save_account(access_token=body["access_token"],
                    refresh_token=body.get("refresh_token") or acc["refresh_token"],
                    expires_at=expires_at)
    return body["access_token"]


def token() -> str:
    acc = db.get_account()
    if acc is None or not acc["access_token"]:
        raise NotConnected("Aucun compte Spotify lié")
    if float(acc["expires_at"] or 0) < time.time():
        return _refresh()
    return acc["access_token"]


def api(path: str, params: dict | None = None, _retry: int = 0) -> dict:
    url = path if path.startswith("http") else API + path
    headers = {"Authorization": "Bearer " + token()}
    with httpx.Client(timeout=45) as c:
        r = c.get(url, params=params, headers=headers)
    if r.status_code == 401 and _retry < 1:
        _refresh()
        return api(path, params, _retry + 1)
    if r.status_code == 429 and _retry < 3:
        time.sleep(min(30, int(r.headers.get("Retry-After", "2")) + 1))
        return api(path, params, _retry + 1)
    if r.status_code >= 400:
        msg, premium = "", False
        try:
            body = r.json()
            err = body.get("error", body)
            msg = err.get("message") if isinstance(err, dict) else str(err)
            premium = "premium" in (msg or "").lower()
        except (ValueError, AttributeError):
            msg = r.text[:300]
        raise SpotifyError(r.status_code, msg or r.reason_phrase, premium)
    return r.json()


def paginate(path: str, params: dict | None = None, max_items: int | None = None):
    params = dict(params or {})
    params.setdefault("limit", 50)
    out = []
    url = path
    while url:
        page = api(url, params if not url.startswith("http") else None)
        out.extend(page.get("items", []))
        if max_items and len(out) >= max_items:
            return out[:max_items]
        url = page.get("next")
        params = None
    return out


def test_connection() -> dict:
    me = api("/me")
    db.save_account(spotify_user_id=me.get("id"), display_name=me.get("display_name"),
                    product=me.get("product"))
    return me


# --- playlists -------------------------------------------------------------

def list_playlists() -> list[dict]:
    items = paginate("/me/playlists")
    out = []
    for p in items:
        if not p:
            continue
        images = p.get("images") or []
        out.append({
            "id": p["id"],
            "name": p.get("name") or "Sans titre",
            "owner": (p.get("owner") or {}).get("display_name") or "",
            "description": p.get("description") or "",
            "image": images[0]["url"] if images else "",
            "total": ((p.get("tracks") or {}).get("total") or 0),
            "snapshot_id": p.get("snapshot_id") or "",
        })
    return out


def liked_total() -> int:
    data = api("/me/tracks", {"limit": 1})
    return int(data.get("total") or 0)


def _norm_track(t: dict, added_at: str | None = None) -> dict | None:
    if not t or t.get("is_local") or not t.get("id"):
        return None
    album = t.get("album") or {}
    images = album.get("images") or []
    album_artists = album.get("artists") or []
    return {
        "spotify_id": t["id"],
        "isrc": t.get("isrc") or (t.get("external_ids") or {}).get("isrc") or "",
        "title": t.get("name") or "",
        "artists": ", ".join(a.get("name", "") for a in (t.get("artists") or [])),
        "album": album.get("name") or "",
        "album_artist": ", ".join(a.get("name", "") for a in album_artists),
        "disc_no": int(t.get("disc_number") or 1),
        "track_no": int(t.get("track_number") or 1),
        "release_date": album.get("release_date") or "",
        "duration_ms": int(t.get("duration_ms") or 0),
        "explicit": 1 if t.get("explicit") else 0,
        "popularity": int(t.get("popularity") or 0),
        "cover_url": images[-1]["url"] if images else "",
        "url": (t.get("external_urls") or {}).get("spotify") or "",
        "added_at": added_at or "",
    }


def playlist_tracks(playlist_id: str) -> list[dict]:
    if playlist_id == "__liked__":
        items = paginate("/me/tracks", {"fields": "next,items(added_at,track(" + TRACK_FIELDS + "))"})
        return [x for x in (_norm_track(i.get("track"), i.get("added_at")) for i in items) if x]
    fields = "next,items(added_at,track(" + TRACK_FIELDS + "))"
    items = paginate(f"/playlists/{playlist_id}/tracks", {"fields": fields})
    return [x for x in (_norm_track(i.get("track"), i.get("added_at")) for i in items) if x]
