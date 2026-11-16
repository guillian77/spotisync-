"""Tests de la détection de type de saisie (recherche unifiée)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import search  # noqa: E402


def test_detect_spotify_playlist_id_and_url():
    assert search.detect("37i9dQZF1DXcBWIGoYBM5M") == ("spotify_playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert search.detect("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")[0] == "spotify_playlist"
    assert search.detect("https://open.spotify.com/intl-fr/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc") \
        == ("spotify_playlist", "37i9dQZF1DXcBWIGoYBM5M")


def test_detect_youtube_video():
    assert search.detect("https://www.youtube.com/watch?v=ksJ8dtaxHR0") == ("youtube_video", "ksJ8dtaxHR0")
    assert search.detect("https://youtu.be/ksJ8dtaxHR0") == ("youtube_video", "ksJ8dtaxHR0")
    assert search.detect("https://www.youtube.com/shorts/ksJ8dtaxHR0") == ("youtube_video", "ksJ8dtaxHR0")
    assert search.detect("ksJ8dtaxHR0") == ("youtube_video", "ksJ8dtaxHR0")


def test_detect_youtube_playlist_is_refused():
    kind, _ = search.detect("https://www.youtube.com/playlist?list=PLabcdefghijklmnop")
    assert kind == "youtube_playlist"


def test_detect_plain_text():
    assert search.detect("Stayin' Alive") == ("text", "Stayin' Alive")
    assert search.detect("GIMS")[0] == "text"
    # un titre de 22 caractères ne doit pas être pris pour un ID Spotify
    assert search.detect("aaaaaaaaaaaaaaaaaaaaaa")[0] == "text"
    assert search.detect("") == ("text", "")


def test_detect_spotify_track_url_is_not_a_playlist():
    kind, _ = search.detect("https://open.spotify.com/track/4UDmDIqJJbrW0hMBQMFOsM")
    assert kind == "text"


def test_invalid_playlist_id_rejected():
    try:
        search.fetch_spotify_playlist("court")
    except ValueError as exc:
        assert "invalide" in str(exc)
    else:
        raise AssertionError("un identifiant invalide doit être refusé")
