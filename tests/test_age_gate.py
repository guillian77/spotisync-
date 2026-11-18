"""Vidéos « 18+ » : elles doivent être écartées et non bloquer la piste.

YouTube renvoie « Sign in to confirm your age » pour les vidéos réservées aux adultes.
Sans cookies, elles sont inaccessibles : le téléchargement doit passer à la source
suivante, et non échouer sur la piste entière.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, downloader  # noqa: E402

config.ensure_dirs()
db.init()          # download_one lit des réglages en base

AGE_ERROR = (
    "ERROR: [youtube] 7-iAUfN0u70: Sign in to confirm your age. This video may be "
    "inappropriate for some users. Use --cookies-from-browser or --cookies for the "
    "authentication."
)


def test_detects_the_real_youtube_message():
    assert downloader._age_gate(AGE_ERROR)
    assert downloader._age_gate("Sign in to confirm your age")
    assert downloader._age_gate("This video is age-restricted")
    # messages qui ne doivent pas être confondus
    assert not downloader._age_gate("HTTP Error 403: Forbidden")
    assert not downloader._age_gate("Video unavailable")
    assert not downloader._age_gate("")


def test_probe_reports_age_limit(monkeypatch):
    """La sonde lit `age_limit` sans télécharger."""
    monkeypatch.setattr(downloader, "_run", lambda *a, **k: (0, '{"age_limit": 18}', ""))
    assert downloader._age_limited("7-iAUfN0u70")

    monkeypatch.setattr(downloader, "_run", lambda *a, **k: (0, '{"age_limit": 0}', ""))
    assert not downloader._age_limited("7-iAUfN0u70")

    # yt-dlp refuse lui-même : c'est un signal suffisant
    monkeypatch.setattr(downloader, "_run", lambda *a, **k: (1, "", AGE_ERROR))
    assert downloader._age_limited("7-iAUfN0u70")

    # une sonde cassée ne doit jamais bloquer
    def boom(*a, **k):
        raise OSError("pas de réseau")
    monkeypatch.setattr(downloader, "_run", boom)
    assert downloader._age_limited("7-iAUfN0u70") is False


def test_download_skips_age_gated_source_and_tries_next(monkeypatch):
    """1re source : 18+ -> écartée ; 2e source : doit être tentée."""
    played = []

    def fake_resolve(query, staging):
        return "AAAAAAAAAAA" if "recherche" in query or True else ""

    def fake_spotdl(query, staging, fmt, bitrate):
        played.append(query)
        if len(played) == 1:                      # 1re tentative : refus d'âge
            return None, "", AGE_ERROR, 1, False
        # 2e tentative : autre échec (on ne va pas jusqu'au fichier final ici)
        return None, "", "ERROR: Video unavailable", 1, False

    rejected = []
    monkeypatch.setattr(downloader, "resolve_video", fake_resolve)
    monkeypatch.setattr(downloader, "_run_spotdl", fake_spotdl)
    monkeypatch.setattr(downloader, "_age_limited", lambda v: False)
    monkeypatch.setattr(downloader, "reject_source",
                        lambda tid, vid, kind, reason: rejected.append((kind, reason)))
    # on arrête juste après le dépassement de l'erreur d'âge
    monkeypatch.setattr(downloader, "metadata", downloader.metadata)

    track = {"id": 1, "spotify_id": "abc", "title": "Titre", "artists": "Artiste",
             "album": "", "duration_ms": 200000}

    status, detail = downloader.download_one(track)
    assert status == downloader.STATUS_ERROR
    assert len(played) >= 2, "la source suivante doit être tentée"
    assert rejected and rejected[0][1] == "vérification d'âge"
    assert "vérification d'âge" in str(detail) or "réservées aux adultes" in str(detail)


def test_cookies_are_optional_and_auto_detected(tmp_path, monkeypatch):
    """Aucune configuration : un cookies.txt déposé dans /config est utilisé, rien d'autre à faire."""
    monkeypatch.setattr(downloader, "db", downloader.db)
    downloader.db.set_setting("cookies_file", "")
    monkeypatch.setenv("SPOTISYNC_CONFIG", str(tmp_path))
    assert downloader.cookies_file() == "" and downloader.cookies_args() == []

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    assert downloader.cookies_file() == str(jar)
    assert downloader.cookies_args() == ["--cookies", str(jar)]

    # un chemin explicite prime, et un fichier absent est ignoré
    downloader.db.set_setting("cookies_file", str(tmp_path / "absent.txt"))
    assert downloader.cookies_args() == []
