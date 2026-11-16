"""Métadonnées : tags (mutagen), nommage des fichiers, clés de comparaison."""
import os
import re
import subprocess
import unicodedata
from pathlib import Path

from .config import FFMPEG_BIN, FFPROBE_BIN

AUDIO_EXT = {".mp3", ".opus", ".m4a", ".flac", ".ogg", ".wav"}
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FEAT = re.compile(r"\s*[\(\[](feat\.?|ft\.?|featuring|avec|prod\.)[^\)\]]*[\)\]]", re.I)


def sanitize(name: str, max_len: int = 120) -> str:
    name = (name or "").strip()
    name = name.replace(":", " - ")          # « Album : sous-titre » → « Album - sous-titre »
    name = _FORBIDDEN.sub("_", name).strip(" .")
    name = re.sub(r"\s*-\s*-\s*", " - ", name)
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "inconnu"


def split_artists(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\s*(?:;|,|\bfeat\b\.?|\bft\b\.?|&)\s*", value)
    return [p.strip() for p in parts if p.strip()]


def format_artists(value: str) -> str:
    """« GIMS, Damso » -> « GIMS; Damso » (séparateur unique pour les lecteurs)."""
    parts = split_artists(value)
    return "; ".join(parts) if parts else (value or "").strip()


def normalize_artists(tag_value: str, expected: str = "") -> str:
    """spotDL écrit « GIMS/Damso » ; on ne convertit que si le découpage
    correspond au nombre d'artistes attendus (évite de casser AC/DC)."""
    tag_value = (tag_value or "").strip()
    if not tag_value:
        return expected or ""
    expected_list = split_artists(expected)
    if "/" in tag_value and expected_list:
        parts = [p.strip() for p in tag_value.split("/") if p.strip()]
        if len(parts) == len(expected_list):
            return "; ".join(parts)
    if ";" in tag_value:
        return "; ".join(p.strip() for p in tag_value.split(";") if p.strip())
    return tag_value


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "")
                   if unicodedata.category(c) != "Mn")


def norm_key(title: str, artists: str) -> str:
    t = _FEAT.sub("", title or "")
    t = strip_accents(t).lower()
    a = strip_accents(split_artists(artists)[0] if split_artists(artists) else artists or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    a = re.sub(r"[^a-z0-9]+", " ", a).strip()
    return f"{a}|{t}"


def similarity(a: str, b: str) -> float:
    """Score 0-100 tolérant à l'ordre des mots (remplace rapidfuzz)."""
    import difflib
    if not a or not b:
        return 0.0

    def tokens(value: str) -> str:
        return " ".join(sorted(re.findall(r"[a-z0-9]+", value.lower())))

    return difflib.SequenceMatcher(None, tokens(a), tokens(b)).ratio() * 100


def build_relpath(template: str, tags: dict) -> str:
    data = {
        "artist": sanitize(tags.get("artist") or tags.get("album_artist") or "Artiste inconnu"),
        "album_artist": sanitize(tags.get("album_artist") or tags.get("artist") or "Artiste inconnu"),
        "album": sanitize(tags.get("album") or "Albums divers"),
        "title": sanitize(tags.get("title") or "Sans titre"),
        "track": int(tags.get("track") or 1),
        "track_raw": str(tags.get("track") or ""),
        "disc": int(tags.get("disc") or 1),
        "year": (str(tags.get("year") or "")[:4] or "0000"),
    }
    try:
        rel = template.format(**data)
    except (KeyError, ValueError):
        rel = "{artist}/{album}/{track:02d} - {title}".format(**data)
    return rel.replace("\\", "/").strip("/")


def unique_path(path: Path) -> Path:
    """Évite d'écraser un fichier existant avec un autre contenu."""
    if not path.exists():
        return path
    stem, ext, n = path.stem, path.suffix, 2
    while True:
        cand = path.with_name(f"{stem} ({n}){ext}")
        if not cand.exists():
            return cand
        n += 1


# --- lecture / écriture des tags -------------------------------------------

def read_tags(path: str | os.PathLike) -> dict:
    p = Path(path)
    info = {"title": p.stem, "artist": "", "album": "", "album_artist": "",
            "track": 0, "disc": 1, "year": "", "isrc": "", "genre": "",
            "duration_s": 0.0, "bitrate": 0, "fmt": p.suffix.lstrip(".").lower()}
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3
        from mutagen.oggopus import OggOpus

        if p.suffix.lower() == ".mp3":
            audio = MP3(p, ID3=EasyID3)
            info["duration_s"] = float(audio.info.length)
            info["bitrate"] = int(audio.info.bitrate)
        elif p.suffix.lower() == ".flac":
            audio = FLAC(p)
            info["duration_s"] = float(audio.info.length)
            info["bitrate"] = int(audio.info.bitrate)
        elif p.suffix.lower() == ".opus":
            audio = OggOpus(p)
            info["duration_s"] = float(audio.info.length)
            info["bitrate"] = int(getattr(audio.info, "bitrate", 0) or 0)
        else:
            audio = mutagen.File(p, easy=True)
            if audio is not None and audio.info:
                info["duration_s"] = float(audio.info.length)
                info["bitrate"] = int(getattr(audio.info, "bitrate", 0) or 0)
        if audio is None:
            return info
        tags = audio.tags or {}

        def g(key):
            val = tags.get(key)
            if isinstance(val, list):
                return val[0] if val else ""
            return val or ""

        info["title"] = g("title") or info["title"]
        info["artist"] = g("artist")
        info["album"] = g("album")
        info["album_artist"] = g("albumartist") or g("album artist") or ""
        info["year"] = str(g("date") or g("year") or "")
        info["isrc"] = g("isrc") or ""
        info["genre"] = g("genre") or ""
        tn = str(g("tracknumber") or "")
        if "/" in tn:
            tn = tn.split("/")[0]
        info["track"] = int(tn) if tn.isdigit() else 0
        dn = str(g("discnumber") or "")
        if "/" in dn:
            dn = dn.split("/")[0]
        info["disc"] = int(dn) if dn.isdigit() else 1
    except Exception:
        pass
    return info


def write_tags(path: str | os.PathLike, tags: dict) -> None:
    try:
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3
        from mutagen.oggopus import OggOpus

        p = Path(path)
        if p.suffix.lower() == ".mp3":
            audio = MP3(p, ID3=EasyID3)
        elif p.suffix.lower() == ".flac":
            audio = FLAC(p)
        elif p.suffix.lower() == ".opus":
            audio = OggOpus(p)
        else:
            import mutagen
            audio = mutagen.File(p, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        mapping = {
            "title": tags.get("title"), "artist": tags.get("artist"),
            "album": tags.get("album"), "albumartist": tags.get("album_artist"),
            "date": tags.get("year"), "isrc": tags.get("isrc"),
            "tracknumber": str(tags.get("track") or ""),
            "discnumber": str(tags.get("disc") or ""),
        }
        for key, val in mapping.items():
            if val:
                try:
                    audio[key] = [str(val)]
                except Exception:
                    pass
        audio.save()
    except Exception:
        pass


def probe(path: str) -> dict:
    """Durée/bitrate fiables via ffprobe (repli si mutagen échoue)."""
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30).stdout
        import json
        fmt = json.loads(out or "{}").get("format", {})
        return {"duration_s": float(fmt.get("duration") or 0),
                "bitrate": int(float(fmt.get("bit_rate") or 0))}
    except Exception:
        return {"duration_s": 0.0, "bitrate": 0}


def find_audio(directory: str | os.PathLike) -> Path | None:
    best = None
    for root, _dirs, files in os.walk(directory):
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in AUDIO_EXT:
                if best is None or p.stat().st_size > best.stat().st_size:
                    best = p
    return best


def cover_embedded(path: str | os.PathLike) -> bool:
    try:
        from mutagen import File as MFile
        audio = MFile(path)
        if audio is None:
            return False
        if getattr(audio, "pictures", None):
            return True
        tags = getattr(audio, "tags", None) or {}
        return any(str(k).startswith("APIC") or str(k).startswith("covr") for k in tags)
    except Exception:
        return False


def ffmpeg_available() -> bool:
    return bool(subprocess.run(["which", FFMPEG_BIN], capture_output=True).stdout)
