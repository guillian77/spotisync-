"""Tests de l'import de fichiers (parsing pur, sans base)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app import importer  # noqa: E402

CSV_FR = """\ufeff"URI du titre","Nom du titre","URI(s) de l'artiste","Nom(s) de l'artiste","URI de l'album","Nom de l'album","URI(s) de l'artiste de l'album","Nom(s) de l'artiste de l'album","Date de sortie de l'album","URL de l'image de l'album","Numéro de disque","Numéro du titre","Durée du titre (ms)","URL de prévisualisation du titre","Explicite","Popularité","ISRC","Ajouté par","Ajouté le"
"spotify:track:7n1H7Rh5aLAmQIAyotb4fm","TU ME RENDS BÊTE","spotify:artist:0GOx72r5AAEKRGQFn3xqXK, spotify:artist:2UwqpfQtNuhBwviIC0f2ie","GIMS, Damso","spotify:album:2s8r5D8uD96aySQJplzuG5","LE NORD SE SOUVIENT : L'ODYSSÉE","spotify:artist:0GOx72r5AAEKRGQFn3xqXK","GIMS","2025-02-20","https://i.scdn.co/image/abc","1","2","165714","https://p.scdn.co/mp3-preview/x","false","63","FR8FB2514300","","2026-11-14T11:55:36Z"
"spotify:track:7E6OkcASMIpkQm80nP17Z8","Cala Boca","spotify:artist:0LKAV3zJ8a8AIGnyc5OvfB","SDM","spotify:album:0yT2Mpus0y4n0fp6V0pfZH","Cala Boca","spotify:artist:0LKAV3zJ8a8AIGnyc5OvfB","SDM","2019-08-01","https://i.scdn.co/image/def","1","1","192762","https://p.scdn.co/mp3-preview/y","false","45","FR9W11917326","","2026-11-14T11:55:01Z"
"""

CSV_EN = """Track URI,Track Name,Artist URI(s),Artist Name(s),Album URI,Album Name,Album Artist URI(s),Album Artist Name(s),Album Release Date,Album Image URL,Disc Number,Track Number,Track Duration (ms),Track Preview URL,Explicit,Popularity,ISRC,Added By,Added At
spotify:track:1,Hello,spotify:artist:1,Adele,spotify:album:1,25,spotify:artist:1,Adele,2015-11-20,http://img,1,1,295000,,false,80,GBBKS1500214,,2016-01-01T00:00:00Z
"""


def test_parse_exportify_french_csv():
    rows = importer.parse_csv(CSV_FR)
    assert len(rows) == 2
    first = rows[0]
    assert first["spotify_id"] == "7n1H7Rh5aLAmQIAyotb4fm"
    assert first["title"] == "TU ME RENDS BÊTE"
    assert first["artists"] == "GIMS, Damso"
    assert first["album"] == "LE NORD SE SOUVIENT : L'ODYSSÉE"
    assert first["track_no"] == 2
    assert first["duration_ms"] == 165714
    assert first["isrc"] == "FR8FB2514300"
    assert first["url"].startswith("https://open.spotify.com/track/")
    assert first["release_date"] == "2025-02-20"


def test_parse_exportify_english_csv():
    rows = importer.parse_csv(CSV_EN)
    assert len(rows) == 1
    assert rows[0]["title"] == "Hello"
    assert rows[0]["artists"] == "Adele"
    assert rows[0]["album_artist"] == "Adele"
    assert rows[0]["explicit"] == 0


def test_parse_csv_semicolon_delimiter():
    text = CSV_EN.replace(",", ";")
    rows = importer.parse_csv(text)
    assert rows[0]["title"] == "Hello"


def test_parse_csv_unknown_headers_raises():
    with pytest.raises(ValueError):
        importer.parse_csv("foo,bar\n1,2\n")


def test_parse_official_spotify_json():
    payload = [
        {"ts": "2026-01-02T03:04:05Z", "master_metadata_track_name": "Cala Boca",
         "master_metadata_album_artist_name": "SDM",
         "master_metadata_album_album_name": "Cala Boca",
         "spotify_track_uri": "spotify:track:7E6OkcASMIpkQm80nP17Z8", "msPlayed": 192000},
    ]
    rows = importer.parse_json(payload)
    assert len(rows) == 1
    assert rows[0]["spotify_id"] == "7E6OkcASMIpkQm80nP17Z8"
    assert rows[0]["title"] == "Cala Boca"
    assert rows[0]["duration_ms"] == 192000


def test_parse_internal_json_and_wrapper():
    payload = {"items": [{"title": "Cala Boca", "artists": "SDM",
                          "url": "https://open.spotify.com/track/abc123"}]}
    rows = importer.parse_json(payload)
    assert rows[0]["spotify_id"] == "abc123"
    assert rows[0]["artists"] == "SDM"


def test_parse_json_rejects_non_list():
    with pytest.raises(ValueError):
        importer.parse_json({"rien": 1})
