"""Tests des garde-fous de correspondance (durée, versions live, ordre des candidats)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import downloader, metadata  # noqa: E402


def test_candidates_order_url_then_search_then_album():
    track = {"url": "https://open.spotify.com/track/abc", "title": "Stayin' Alive",
             "artists": "Bee Gees, Barry Gibb", "album": "Saturday Night Fever"}
    kinds = [kind for kind, _ in downloader._candidates(track)]
    assert kinds == ["spotify", "recherche", "recherche+album"]
    queries = [q for _, q in downloader._candidates(track)]
    assert queries[1] == "Bee Gees - Stayin' Alive"
    assert queries[2] == "Bee Gees - Stayin' Alive Saturday Night Fever"


def test_candidates_without_url_start_with_search():
    kinds = [k for k, _ in downloader._candidates(
        {"title": "Cala Boca", "artists": "SDM", "album": ""})]
    assert kinds[0] == "recherche"


def test_lookup_failure_detection():
    assert downloader._lookup_failure("KeyError: 'uri'")
    assert downloader._lookup_failure("LookupError: No results")
    assert downloader._lookup_failure("SongError: Could not get song info")
    assert not downloader._lookup_failure("YT-DLP download error")
    assert not downloader._lookup_failure("Délai dépassé (15 min)")


def test_alternate_version_flags_live_only_when_unexpected():
    # le résultat YouTube est un live, le titre Spotify ne l'annonce pas
    assert downloader._alternate_version("Bee Gees - Stayin' Alive (Live at Wembley)",
                                         "Stayin' Alive") == "live"
    # le titre Spotify annonce lui-même un live : on accepte
    assert downloader._alternate_version("X - Titre (Live)", "Titre (Live)") == ""
    # rien de suspect
    assert downloader._alternate_version("Bee Gees - Stayin' Alive", "Stayin' Alive") == ""
    # accents et casse
    assert downloader._alternate_version("Artiste - Titre EN CONCERT", "Titre") in ("concert", "en concert")


def test_duration_problem_thresholds(monkeypatch):
    monkeypatch.setattr(downloader.db, "setting_int", lambda key, default=0: 8)
    too_far, delta = downloader._duration_problem(285.4, 285000)
    assert too_far is False and delta < 1

    too_far, delta = downloader._duration_problem(420.0, 285000)  # live de 7 minutes
    assert too_far is True and delta >= 100

    too_far, _ = downloader._duration_problem(0, 285000)  # durée inconnue : on ne bloque pas
    assert too_far is False


def test_keyword_matching_is_word_bounded():
    # « alive » ne doit pas déclencher le mot-clé « live »
    assert downloader._has_keyword("stayin' alive", "live") is False
    assert downloader._has_keyword("live at wembley", "live") is True
    assert downloader._has_keyword("mtv unplugged session", "session") is True


def test_format_artists_for_tags():
    assert metadata.format_artists("GIMS, Damso") == "GIMS; Damso"
    assert metadata.format_artists("AC/DC") == "AC/DC"
