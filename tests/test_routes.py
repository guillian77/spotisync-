"""Filet de sécurité : toutes les routes répondent, y compris avec filtres.

Ce test existe après une régression réelle : la requête de comptage de /library avait perdu
son alias `t` alors que la clause WHERE utilisait `t.title`, d'où un 500 dès qu'on filtrait.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# l'isolation des chemins est faite une seule fois, dans conftest.py ;
# redéfinir os.environ ici décalerait le dossier vu par l'app et celui vu par le test.

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.main import app  # noqa: E402

config.ensure_dirs()
db.init()

# quelques données pour que les filtres aient de quoi travailler
db.qx("DELETE FROM tracks")
db.qx("INSERT INTO tracks(spotify_id,title,artists,album,status,key_norm) "
      "VALUES('r1','Fichier orphelin','Test','Albums divers','done','test|fichier orphelin')")
db.qx("INSERT INTO tracks(spotify_id,title,artists,album,status,key_norm) "
      "VALUES('r2','Titre en erreur','Test','Albums divers','error','test|titre en erreur')")
db.qx("INSERT INTO rejected_sources(track_id,source_id,kind,reason,created_at) "
      "VALUES((SELECT id FROM tracks WHERE spotify_id='r1'),'vid','recherche','live','2026-01-01')")

client = TestClient(app)


def test_dashboard():
    r = client.get("/")
    assert r.status_code == 200


def test_library_without_filter():
    r = client.get("/library")
    assert r.status_code == 200
    assert "Fichier orphelin" in r.text


def test_library_text_filter():
    r = client.get("/library", params={"q": "orphelin", "status": ""})
    assert r.status_code == 200, r.text[:300]
    assert "Fichier orphelin" in r.text
    assert "Titre en erreur" not in r.text


def test_library_status_filter():
    r = client.get("/library", params={"q": "", "status": "error"})
    assert r.status_code == 200, r.text[:300]
    assert "Titre en erreur" in r.text
    assert "Fichier orphelin" not in r.text


def test_library_actions_present():
    r = client.get("/library", params={"q": "orphelin"})
    assert "Relancer" in r.text and "Remplacer" in r.text and "Supprimer" in r.text


def test_search_page_and_local_search():
    assert client.get("/search").status_code == 200
    r = client.get("/search", params={"q": "orphelin"})
    assert r.status_code == 200
    assert "Fichier orphelin" in r.text


def test_search_replace_card():
    tid = db.q1("SELECT id FROM tracks WHERE spotify_id='r1'")["id"]
    r = client.get("/search", params={"track": tid})
    assert r.status_code == 200
    assert "Remplacer un fichier existant" in r.text


def test_other_pages():
    for path in ("/playlists", "/settings", "/duplicates", "/controls"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    r = client.get("/api/status")
    assert r.status_code == 200 and "stats" in r.json()


def test_import_redirects_to_playlists():
    r = client.get("/import", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/playlists")


def test_delete_removes_track_and_quarantines_file():
    """Supprimer : la fiche disparaît, le fichier part en quarantaine (pas de suppression sèche)."""
    music = Path(db.setting("music_dir"))  # source de vérité : ce que voit l'application
    music.mkdir(parents=True, exist_ok=True)
    audio = music / "Suppr" / "Test" / "01 - A supprimer.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"x" * 2048)

    tid = db.qx("INSERT INTO tracks(spotify_id,title,artists,status,file_path,file_size) "
                "VALUES('del1','A supprimer','Test','done',?,?)", (str(audio), 2048))
    db.qx("INSERT INTO rejected_sources(track_id,source_id,kind,reason,created_at) "
          "VALUES(?,?,?,?,?)", (tid, "v1", "recherche", "test", "2026-01-01"))

    r = client.post(f"/library/{tid}/delete", follow_redirects=False)
    assert r.status_code == 303

    assert db.q1("SELECT id FROM tracks WHERE id=?", (tid,)) is None
    assert db.q1("SELECT 1 FROM rejected_sources WHERE track_id=?", (tid,)) is None
    assert not audio.exists(), "le fichier aurait dû être déplacé"
    quarantine = music / db.setting("quarantine_subdir", "_doublons")
    assert any(quarantine.glob("*A supprimer*")), "fichier absent de la quarantaine"


def test_delete_unknown_track_is_harmless():
    r = client.post("/library/999999/delete", follow_redirects=False)
    assert r.status_code == 303
