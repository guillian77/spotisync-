"""Tests unitaires des fonctions pures (métadonnées, nommage, similarité)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import metadata  # noqa: E402


def test_sanitize_replaces_forbidden_chars():
    assert metadata.sanitize('AC/DC: Live?') == "AC_DC - Live_"


def test_sanitize_collapses_double_dash():
    assert metadata.sanitize("Album : sous-titre") == "Album - sous-titre"


def test_split_artists_separators():
    assert metadata.split_artists("GIMS, Damso") == ["GIMS", "Damso"]
    assert metadata.split_artists("A; B") == ["A", "B"]
    assert metadata.split_artists("A feat. B") == ["A", "B"]


def test_normalize_artists_matches_expected_count():
    # spotDL renvoie « GIMS/Damso », on attend 2 artistes
    assert metadata.normalize_artists("GIMS/Damso", "GIMS, Damso") == "GIMS; Damso"
    # AC/DC : le découpage ne correspond pas au nombre attendu, on ne touche à rien
    assert metadata.normalize_artists("AC/DC", "AC/DC") == "AC/DC"
    # un seul artiste avec slash dans le nom reste intact
    assert metadata.normalize_artists("AC/DC", "AC/DC") == "AC/DC"


def test_norm_key_ignores_accents_and_features():
    a = metadata.norm_key("TU ME RENDS BÊTE", "GIMS, Damso")
    b = metadata.norm_key("Tu me rends bete (feat. Damso)", "GIMS")
    assert a == b == "gims|tu me rends bete"


def test_build_relpath_default_template():
    tags = {"artist": "GIMS; Damso", "album": "LE NORD SE SOUVIENT : L'ODYSSÉE",
            "title": "TU ME RENDS BÊTE", "track": 2}
    rel = metadata.build_relpath("{artist}/{album}/{track:02d} - {title}", tags)
    assert rel == "GIMS; Damso/LE NORD SE SOUVIENT - L'ODYSSÉE/02 - TU ME RENDS BÊTE"


def test_build_relpath_handles_missing_values():
    rel = metadata.build_relpath("{artist}/{album}/{track:02d} - {title}", {})
    assert rel == "Artiste inconnu/Albums divers/01 - Sans titre"


def test_similarity_order_insensitive():
    a = metadata.norm_key("Cala Boca", "SDM")
    b = metadata.norm_key("Boca Cala", "SDM")
    assert metadata.similarity(a, b) > 85
    assert metadata.similarity(a, metadata.norm_key("Autre chose", "Personne")) < 40
