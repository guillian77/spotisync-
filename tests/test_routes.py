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
    assert "Corriger" in r.text and "Améliorer" in r.text
    assert 'aria-label="Supprimer"' in r.text
    # les trois actions partagent la même hauteur de boîte, sinon l'icône se décale
    assert r.text.count("h-7 px-2") == 2 and "h-7 w-7" in r.text
    assert 'class="flex items-center justify-end gap-1"' in r.text


def test_search_page_and_local_search():
    assert client.get("/search").status_code == 200
    r = client.get("/search", params={"q": "orphelin"})
    assert r.status_code == 200
    assert "Fichier orphelin" in r.text


def test_search_replace_card():
    tid = db.q1("SELECT id FROM tracks WHERE spotify_id='r1'")["id"]
    r = client.get("/search", params={"track": tid})
    assert r.status_code == 200
    assert "Améliorer un fichier existant" in r.text


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


def test_library_htmx_returns_fragment_only():
    """Le filtre en direct ne doit recevoir que le tableau, pas la page entière."""
    r = client.get("/library", params={"q": "orphelin"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "<table" in r.text and "Fichier orphelin" in r.text
    assert "piste(s) correspondant au filtre" in r.text   # le compteur voyage avec le fragment


def test_library_form_is_live_without_spamming():
    """Délai anti-spam sur la saisie, instantané sur le statut, et annulation des requêtes obsolètes."""
    html = client.get("/library").text
    assert 'hx-trigger="input changed delay:350ms, search"' in html
    assert 'hx-trigger="change"' in html
    assert 'hx-sync="closest form:replace"' in html
    assert 'hx-target="#library-results"' in html
    assert 'id="library-results"' in html


def test_delete_keeps_current_filters():
    tid = db.qx("INSERT INTO tracks(spotify_id,title,artists,status) VALUES('del2','A garder','Test','done')")
    r = client.post(f"/library/{tid}/delete", data={"q": "orphelin", "status": "done"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "q=orphelin" in loc and "status=done" in loc


def test_feedback_uses_toasts_not_native_dialogs():
    """Aucun window.confirm/alert : confirmation en page + toasts."""
    html = client.get("/library").text
    assert 'onsubmit="return confirm(' not in html      # plus de popup natif
    assert "window.alert" not in html
    assert "onsubmit=" not in html
    assert 'data-confirm="' in html            # confirmation portée par le formulaire
    assert 'id="confirm-dialog"' in html and 'id="toasts"' in html


def test_server_message_becomes_a_toast():
    ok = client.get("/library", params={"redl": "« Test » supprimée de la bibliothèque"})
    assert ok.status_code == 200
    assert 'data-flash="« Test » supprimée de la bibliothèque"' in ok.text
    assert 'data-kind="ok"' in ok.text

    err = client.get("/playlists", params={"err": "Playlist introuvable"})
    assert 'data-flash="Playlist introuvable"' in err.text
    assert 'data-kind="err"' in err.text

    # l'ancien format /?msg=k=ok&v=texte reste compris
    legacy = client.get("/", params={"msg": "k=ok", "v": "Analyse lancée"})
    assert 'data-flash="Analyse lancée"' in legacy.text


def test_scan_count_message_is_a_toast():
    r = client.get("/library", params={"scanned": "12"})
    assert 'data-flash="12 fichier(s) indexé(s)."' in r.text
