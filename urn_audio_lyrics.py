import asyncio
import difflib
import hashlib
import math
import os
import re
import shutil
import tempfile
import time
import uuid
import json
import threading
import unicodedata
import urllib.parse
import urllib.request
import urllib.error

import av
import numpy as np
import torch
from aiohttp import web

import folder_paths
from server import PromptServer
from comfy_api.latest import io

try:
    from comfy.utils import ProgressBar
except Exception:
    ProgressBar = None

try:
    import comfy.model_management as model_management
except Exception:
    model_management = None

from .smart_audio_chunker import (
    _analyse_words,
    _export_mel_roformer_vocals,
    _format_srt_timestamp,
    _write_flac,
)
from .urn_smart_seamless_audio_loop import (
    _advanced_feature_bundle,
    _analysis_signal,
    _load_comfy_audio,
    _phrase_match_components,
)


NODE_TAG = "[URN Audio Lyrics]"
LYRICS_OVH_EVENT = "urn_audio_nodes.audio_lyrics.lookup_request"
LYRICS_OVH_RESPONSE_ROUTE = "/urn_audio_nodes/audio_lyrics/lookup_response"
LYRICS_OVH_PENDING_ROUTE = "/urn_audio_nodes/audio_lyrics/lookup_pending"
EMBEDDED_LYRICS_EVENT = "urn_audio_nodes.audio_lyrics.embedded_request"
EMBEDDED_LYRICS_RESPONSE_ROUTE = "/urn_audio_nodes/audio_lyrics/embedded_response"
EMBEDDED_LYRICS_PENDING_ROUTE = "/urn_audio_nodes/audio_lyrics/embedded_pending"
STATUS_EVENT = "urn_audio_nodes.audio_lyrics.status"

# MusicBrainz is community-edited.  Filter terms that are unsuitable for
# music-generation style metadata before they reach Music_Description.
# The JSON blocklist beside this file can be edited by the user.
_MUSICBRAINZ_BLOCKLIST_FILE = os.path.join(os.path.dirname(__file__), "urn_musicbrainz_tag_blocklist.json")
_MUSICBRAINZ_DEFAULT_BLOCKED_WORDS = {
    "queer", "queercore", "gay", "gaycore", "woke", "fag", "faggot",
    "fuck", "fucked", "fucker", "fucking", "motherfucker", "motherfucking",
    "shit", "shitty", "bullshit", "bitch", "cunt", "dick", "cock", "pussy",
    "asshole", "arsehole", "bastard", "slut", "whore",
    "retard", "retarded", "nigger", "nigga", "tranny", "dyke", "kike",
    "chink", "spic",
}
_MUSICBRAINZ_DEFAULT_BLOCKED_PHRASES = {
    "fuck you", "go fuck yourself", "piece of shit", "son of a bitch",
}


def _musicbrainz_blocklist():
    words = set(_MUSICBRAINZ_DEFAULT_BLOCKED_WORDS)
    phrases = set(_MUSICBRAINZ_DEFAULT_BLOCKED_PHRASES)
    try:
        with open(_MUSICBRAINZ_BLOCKLIST_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for item in data.get("blocked_words") or []:
                value = str(item or "").strip().casefold()
                if value:
                    words.add(value)
            for item in data.get("blocked_phrases") or []:
                value = re.sub(r"\s+", " ", str(item or "")).strip().casefold()
                if value:
                    phrases.add(value)
    except Exception:
        # The built-in defaults still apply if the optional editable file is
        # missing or malformed.  A blocklist problem must never break lyrics.
        pass
    return words, phrases


def _musicbrainz_term_blocked(name: str) -> bool:
    folded = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    folded = folded.casefold()
    folded = folded.replace("@", "a").replace("$", "s")
    folded = folded.replace("0", "o").replace("1", "i").replace("3", "e")
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    words, phrases = _musicbrainz_blocklist()
    tokens = set(folded.split())
    if any(word in tokens for word in words):
        return True
    if folded in words:
        return True
    padded = f" {folded} "
    if any(f" {phrase} " in padded for phrase in phrases):
        return True
    return False


_lyrics_ovh_waiters = {}
_lyrics_ovh_waiters_lock = threading.Lock()
_embedded_lyrics_waiters = {}
_embedded_lyrics_waiters_lock = threading.Lock()


def _log(message: str):
    print(f"{NODE_TAG} {message}")


def _description_console(source: str, success: bool, detail: str = ""):
    """Clear one-line diagnostic showing which description source was used."""
    status = "SUCCESS" if success else "FAILED"
    suffix = f" - {detail}" if detail else ""
    print(f"URN Lyrics: Description {source} - {status}{suffix}")


def _send_status(node_id: str, message: str):
    """Best-effort one-line status update for the URN Audio Lyrics node UI."""
    try:
        PromptServer.instance.send_sync(
            STATUS_EVENT,
            {"node_id": str(node_id), "message": str(message or "")},
        )
    except Exception:
        # Status is display-only and must never interrupt audio processing.
        pass


def _lookup_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _filename_artist_title(filename: str):
    raw = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    if not raw:
        return None
    stem = os.path.splitext(raw)[0].strip()
    # Common library prefixes such as "01 - Artist - Title" or "01. Artist - Title".
    stem = re.sub(r"^\s*\d{1,3}\s*[._-]\s*", "", stem).strip()
    parts = re.split(r"\s+[\-–—]\s+", stem, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, title = parts[0].strip(), parts[1].strip()
    if not artist or not title:
        return None
    return artist, title


def _clean_musicbrainz_title(title: str) -> str:
    """Remove common release/remaster decorations for a fallback MusicBrainz search.

    This is deliberately conservative: live/remix/acoustic/etc. qualifiers are kept
    because they can identify genuinely different recordings.
    """
    value = str(title or "").strip()
    if not value:
        return ""

    # Bracketed suffixes commonly added by streaming services / tagged libraries.
    # Examples: (2015 Remaster), (Remastered 2011), [Remastered], (Deluxe Edition),
    # (Album Version).  Only strip when the entire trailing bracket is a known
    # release/version decoration.
    decoration = (
        r"(?:"
        r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?"
        r"|remaster(?:ed)?(?:\s+version)?(?:\s+\d{4})?"
        r"|deluxe(?:\s+edition)?"
        r"|album\s+version"
        r")"
    )
    previous = None
    while value and value != previous:
        previous = value
        value = re.sub(
            rf"\s*[\(\[]\s*{decoration}\s*[\)\]]\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            rf"\s+[\-–—]\s*{decoration}\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

    return value or str(title or "").strip()


def _musicbrainz_title_variants(title: str):
    original = str(title or "").strip()
    cleaned = _clean_musicbrainz_title(original)
    variants = []
    seen = set()
    for candidate in (original, cleaned):
        key = _lookup_key(candidate)
        if candidate and key and key not in seen:
            seen.add(key)
            variants.append(candidate)
    return variants


def _lyrics_ovh_get_json(url: str, timeout: float = 12.0):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "URN-Audio-Nodes/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def _lyrics_ovh_suggestions(artist: str, guessed_title: str = ""):
    """Return alphabetically sorted unique Deezer titles for the parsed artist."""
    artist_key = _lookup_key(artist)
    if not artist_key:
        return []

    queries = [artist]
    if guessed_title:
        queries.append(f"{artist} {guessed_title}")

    candidates = {}
    for query in queries:
        try:
            encoded = urllib.parse.quote(query, safe="")
            data = _lyrics_ovh_get_json(f"https://api.lyrics.ovh/suggest/{encoded}")
        except Exception as exc:
            _log(f"Lyrics.ovh suggestion query failed for {query!r}: {exc}")
            continue

        for item in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("title_short") or "").strip()
            item_artist = str((item.get("artist") or {}).get("name") or "").strip()
            if not title or not item_artist:
                continue

            item_artist_key = _lookup_key(item_artist)
            artist_similarity = difflib.SequenceMatcher(
                None, artist_key, item_artist_key, autojunk=False
            ).ratio()
            # Deezer searches can mix in similarly named artists. Keep exact/near-exact
            # artist matches only so the dropdown remains useful rather than noisy.
            if not (
                item_artist_key == artist_key
                or artist_similarity >= 0.88
                or artist_key in item_artist_key
                or item_artist_key in artist_key
            ):
                continue

            title_key = _lookup_key(title)
            if not title_key:
                continue
            album = str((item.get("album") or {}).get("title") or "").strip()
            existing = candidates.get(title_key)
            candidate = {
                "artist": item_artist,
                "title": title,
                "album": album,
                "id": str(item.get("id") or ""),
            }
            # Keep the cleaner/shorter title if duplicate search results are returned.
            if existing is None or len(title) < len(existing["title"]):
                candidates[title_key] = candidate

    results = sorted(
        candidates.values(),
        key=lambda item: (item["title"].casefold(), item["album"].casefold()),
    )
    return results


def _lyrics_ovh_best_index(options, guessed_title: str) -> int:
    if not options:
        return 0
    guess = _lookup_key(guessed_title)
    if not guess:
        return 0
    best_index = 0
    best_score = -1.0
    for index, option in enumerate(options):
        score = difflib.SequenceMatcher(
            None, guess, _lookup_key(option.get("title")), autojunk=False
        ).ratio()
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def _lyrics_ovh_fetch(artist: str, title: str) -> str:
    artist_part = urllib.parse.quote(str(artist or "").strip(), safe="")
    title_part = urllib.parse.quote(str(title or "").strip(), safe="")
    if not artist_part or not title_part:
        return ""
    try:
        data = _lyrics_ovh_get_json(f"https://api.lyrics.ovh/v1/{artist_part}/{title_part}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""
        raise
    lyrics = str(data.get("lyrics") or "") if isinstance(data, dict) else ""
    lyrics = lyrics.replace("\r\n", "\n").replace("\r", "\n").strip()
    return lyrics


def _lrclib_get_json(endpoint: str, params=None, timeout: float = 12.0):
    """Small no-key LRCLIB client used only after the user confirms artist/title."""
    params = {k: v for k, v in dict(params or {}).items() if v not in (None, "")}
    query = urllib.parse.urlencode(params)
    url = f"https://lrclib.net/api/{endpoint.lstrip('/')}"
    if query:
        url += "?" + query
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "URN-Audio-Nodes/9.55 (ComfyUI custom node)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def _lrclib_plain_from_synced(synced: str) -> str:
    lines = []
    for raw in str(synced or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Strip one or more LRC timestamps while ignoring metadata rows such as [ar:...].
        line = re.sub(r"^(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*", "", raw).strip()
        if raw.lstrip().startswith("[") and line == raw.strip():
            continue
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _lrclib_synced_to_srt(synced: str) -> str:
    """Convert LRCLIB LRC timing into normal SRT-style text for Transcript_Out."""
    cues = []
    stamp_re = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for raw in str(synced or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stamps = list(stamp_re.finditer(raw))
        if not stamps:
            continue
        text = stamp_re.sub("", raw).strip()
        if not text:
            continue
        for m in stamps:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            # LRC commonly uses centiseconds, but tolerate 1-3 fractional digits.
            millis = int((frac + "000")[:3]) if len(frac) >= 3 else int(frac) * (100 if len(frac) == 1 else 10)
            start = minutes * 60.0 + seconds + millis / 1000.0
            cues.append((start, text))
    if not cues:
        return ""
    cues.sort(key=lambda item: item[0])
    out = []
    for i, (start, text) in enumerate(cues):
        if i + 1 < len(cues):
            next_start = cues[i + 1][0]
            end = max(start + 0.35, next_start - 0.05)
        else:
            end = start + 4.0
        out.append(str(i + 1))
        out.append(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip()


def _lrclib_match_score(item, artist: str, title: str, duration: float, album: str = "") -> float:
    if not isinstance(item, dict):
        return -1.0
    item_artist = str(item.get("artistName") or "").strip()
    item_title = str(item.get("trackName") or "").strip()
    if not item_artist or not item_title:
        return -1.0
    artist_sim = difflib.SequenceMatcher(None, _lookup_key(artist), _lookup_key(item_artist), autojunk=False).ratio()
    title_sim = difflib.SequenceMatcher(None, _lookup_key(title), _lookup_key(item_title), autojunk=False).ratio()
    if artist_sim < 0.70 or title_sim < 0.70:
        return -1.0
    try:
        item_duration = float(item.get("duration") or 0.0)
    except Exception:
        item_duration = 0.0
    if duration > 0 and item_duration > 0:
        diff = abs(item_duration - float(duration))
        duration_score = max(0.0, 1.0 - diff / 30.0)
    else:
        diff = 999.0
        duration_score = 0.35
    album_score = 0.0
    if album and item.get("albumName"):
        album_score = difflib.SequenceMatcher(None, _lookup_key(album), _lookup_key(item.get("albumName")), autojunk=False).ratio()
    score = 0.48 * title_sim + 0.34 * artist_sim + 0.14 * duration_score + 0.04 * album_score
    # A wildly different duration is usually a remix/live/version mismatch.
    if duration > 0 and item_duration > 0 and diff > 45.0 and title_sim < 0.96:
        score -= 0.25
    return score


def _lrclib_fetch(artist: str, title: str, duration: float, album: str = ""):
    """Return the best LRCLIB lyric result or None. Exact lookup first, then ranked search."""
    artist = str(artist or "").strip()
    title = str(title or "").strip()
    album = str(album or "").strip()
    if not artist or not title:
        return None

    exact_params = {
        "track_name": title,
        "artist_name": artist,
        "duration": int(round(float(duration))) if duration else None,
        "album_name": album or None,
    }
    candidate = None
    try:
        data = _lrclib_get_json("get", exact_params)
        if isinstance(data, dict):
            candidate = data
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            _log(f"LRCLIB exact lookup failed: HTTP {exc.code}")
    except Exception as exc:
        _log(f"LRCLIB exact lookup failed: {exc}")

    if candidate is None:
        try:
            results = _lrclib_get_json("search", {
                "track_name": title,
                "artist_name": artist,
            })
            if isinstance(results, list) and results:
                ranked = sorted(
                    (( _lrclib_match_score(item, artist, title, duration, album), item) for item in results),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                if ranked and ranked[0][0] >= 0.74:
                    candidate = ranked[0][1]
        except Exception as exc:
            _log(f"LRCLIB search failed: {exc}")

    if not isinstance(candidate, dict) or candidate.get("instrumental"):
        return None
    plain = str(candidate.get("plainLyrics") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    synced = str(candidate.get("syncedLyrics") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not plain and synced:
        plain = _lrclib_plain_from_synced(synced)
    if not plain and not synced:
        return None
    return {
        "plain": plain,
        "synced": synced,
        "artist": str(candidate.get("artistName") or artist).strip(),
        "title": str(candidate.get("trackName") or title).strip(),
        "album": str(candidate.get("albumName") or album).strip(),
        "duration": candidate.get("duration"),
        "id": candidate.get("id"),
    }


_musicbrainz_lock = threading.Lock()
_musicbrainz_last_request = 0.0


def _musicbrainz_get_json(endpoint: str, params=None, timeout: float = 12.0):
    """Small, rate-limited MusicBrainz JSON client (no API key required)."""
    global _musicbrainz_last_request
    params = dict(params or {})
    params["fmt"] = "json"
    query = urllib.parse.urlencode(params)
    url = f"https://musicbrainz.org/ws/2/{endpoint.lstrip('/')}"
    if query:
        url += "?" + query

    # MusicBrainz asks clients to stay at or below one request per second.
    with _musicbrainz_lock:
        now = time.monotonic()
        wait = 1.05 - (now - _musicbrainz_last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "URN-Audio-Nodes/9.47 (ComfyUI custom node)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            return json.loads(payload.decode("utf-8", errors="replace"))
        finally:
            _musicbrainz_last_request = time.monotonic()


def _metadata_term_ok(name: str) -> bool:
    """Keep tags that are useful as music-generation descriptors, not library trivia."""
    text = re.sub(r"\s+", " ", str(name or "")).strip().strip(",")
    if not text:
        return False
    key = text.casefold()
    if _musicbrainz_term_blocked(text):
        return False
    blocked_exact = {
        "seen live", "favorites", "favourites", "favorite", "favourite", "my music",
        "spotify", "last.fm", "lastfm", "owned", "albums i own", "songs i own",
        "check out", "awesome", "love", "good", "great", "best", "misc",
    }
    if key in blocked_exact:
        return False
    blocked_bits = (
        "seen live", "favorite", "favourite", "spotify", "last.fm", "lastfm",
        "under 2000 listeners", "under 1000 listeners", "my library", "i own",
    )
    if any(bit in key for bit in blocked_bits):
        return False
    # Reject URL-ish / bookkeeping labels while preserving useful era tags such as "80s".
    if "http://" in key or "https://" in key or key.startswith("www."):
        return False
    if len(text) > 64:
        return False
    return True


def _collect_metadata_terms(payload, weight: float, bucket: dict):
    if not isinstance(payload, dict):
        return
    for field, field_boost in (("genres", 3.0), ("tags", 1.0)):
        items = payload.get(field) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip().strip(",")
            if not _metadata_term_ok(name):
                continue
            try:
                count = float(item.get("count") or 0.0)
            except Exception:
                count = 0.0
            # Database count is useful for ranking, but cap it so giant artist tags don't swamp track tags.
            score = float(weight) * float(field_boost) + min(max(count, 0.0), 50.0) / 50.0
            key = name.casefold()
            previous = bucket.get(key)
            if previous is None or score > previous[0]:
                bucket[key] = (score, name)


def _recording_artist_name(recording: dict) -> str:
    credits = recording.get("artist-credit") or [] if isinstance(recording, dict) else []
    parts = []
    for credit in credits:
        if not isinstance(credit, dict):
            continue
        name = str(credit.get("name") or (credit.get("artist") or {}).get("name") or "").strip()
        if name:
            parts.append(name)
    return " ".join(parts).strip()


def _musicbrainz_description_and_recording(artist: str, title: str):
    """
    Return (description, recording_id) for a confirmed song.
    Description is database-backed MusicBrainz genre/style metadata; recording_id
    is reused by the automatic album lookup so the recording search is not repeated.
    """
    artist = str(artist or "").strip()
    title = str(title or "").strip()
    if not artist or not title:
        return "", ""

    try:
        # First try the confirmed title exactly. If that does not produce a confident
        # result, retry with only common release decorations removed. This handles
        # filenames/titles such as "Crucify (2015 Remaster)" while still preserving
        # meaningful qualifiers such as "Live", "Remix" or "Acoustic".
        artist_key = _lookup_key(artist)
        best = None
        best_score = -1.0
        variants = _musicbrainz_title_variants(title)

        for variant_index, query_title in enumerate(variants):
            query = f'recording:"{query_title.replace(chr(34), "")}" AND artist:"{artist.replace(chr(34), "")}"'
            search = _musicbrainz_get_json("recording/", {"query": query, "limit": 5})
            recordings = search.get("recordings") or [] if isinstance(search, dict) else []
            if not recordings:
                if variant_index == 0:
                    _log(f"MusicBrainz: no exact recording match for {artist} - {query_title}.")
                continue

            title_key = _lookup_key(query_title)
            variant_best = None
            variant_best_score = -1.0
            for rec in recordings:
                if not isinstance(rec, dict):
                    continue
                rec_title = str(rec.get("title") or "")
                rec_artist = _recording_artist_name(rec)
                title_similarity = difflib.SequenceMatcher(
                    None, title_key, _lookup_key(rec_title), autojunk=False
                ).ratio()
                artist_similarity = difflib.SequenceMatcher(
                    None, artist_key, _lookup_key(rec_artist), autojunk=False
                ).ratio()
                try:
                    server_score = float(rec.get("score") or 0.0) / 100.0
                except Exception:
                    server_score = 0.0
                score = 0.50 * title_similarity + 0.35 * artist_similarity + 0.15 * server_score
                if score > variant_best_score:
                    variant_best_score = score
                    variant_best = rec

            if variant_best is not None and variant_best_score > best_score:
                best = variant_best
                best_score = variant_best_score

            # A confident exact-title result wins immediately. Otherwise allow the
            # cleaned fallback title to have a chance on the next pass.
            if variant_best is not None and variant_best_score >= 0.62:
                if variant_index > 0:
                    _log(
                        f"MusicBrainz: matched using cleaned title fallback: "
                        f"{title!r} -> {query_title!r}."
                    )
                best = variant_best
                best_score = variant_best_score
                break

        if not best or best_score < 0.62:
            cleaned = _clean_musicbrainz_title(title)
            if cleaned and _lookup_key(cleaned) != _lookup_key(title):
                _log(
                    f"MusicBrainz: recording match confidence was too low for "
                    f"{artist} - {title} (also tried cleaned title {cleaned!r})."
                )
            else:
                _log(f"MusicBrainz: recording match confidence was too low for {artist} - {title}.")
            return "", ""

        recording_id = str(best.get("id") or "").strip()
        artist_id = ""
        for credit in best.get("artist-credit") or []:
            if isinstance(credit, dict):
                candidate = str((credit.get("artist") or {}).get("id") or "").strip()
                if candidate:
                    artist_id = candidate
                    break

        release_group_id = ""
        for release in best.get("releases") or []:
            if not isinstance(release, dict):
                continue
            candidate = str((release.get("release-group") or {}).get("id") or "").strip()
            if candidate:
                release_group_id = candidate
                break

        bucket = {}

        # Track/recording tags are the most specific, so give them the strongest weight.
        if recording_id:
            rec_detail = _musicbrainz_get_json(
                f"recording/{recording_id}",
                {"inc": "genres+tags+artist-credits+releases"},
            )
            _collect_metadata_terms(rec_detail, 4.0, bucket)
            if not artist_id:
                for credit in rec_detail.get("artist-credit") or []:
                    if isinstance(credit, dict):
                        candidate = str((credit.get("artist") or {}).get("id") or "").strip()
                        if candidate:
                            artist_id = candidate
                            break
            if not release_group_id:
                for release in rec_detail.get("releases") or []:
                    if not isinstance(release, dict):
                        continue
                    candidate = str((release.get("release-group") or {}).get("id") or "").strip()
                    if candidate:
                        release_group_id = candidate
                        break

        # Release-group tags often carry the album/single's genre/style when the recording has few tags.
        if release_group_id:
            try:
                release_detail = _musicbrainz_get_json(
                    f"release-group/{release_group_id}", {"inc": "genres+tags"}
                )
                _collect_metadata_terms(release_detail, 2.5, bucket)
            except Exception as exc:
                _log(f"MusicBrainz release metadata unavailable: {exc}")

        # Artist tags are broad fallback context; they rank below track/release metadata.
        if artist_id:
            try:
                artist_detail = _musicbrainz_get_json(
                    f"artist/{artist_id}", {"inc": "genres+tags"}
                )
                _collect_metadata_terms(artist_detail, 1.0, bucket)
            except Exception as exc:
                _log(f"MusicBrainz artist metadata unavailable: {exc}")

        ranked = sorted(bucket.values(), key=lambda item: (-item[0], item[1].casefold()))
        terms = []
        seen = set()
        for _score, term in ranked:
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= 12:
                break

        description = ", ".join(terms)
        if description:
            _log(f"Music_Description from MusicBrainz metadata: {description}")
        else:
            _log(f"MusicBrainz returned no useful genre/style tags for {artist} - {title}.")
        return description, recording_id
    except Exception as exc:
        _log(f"MusicBrainz metadata lookup failed: {exc}")
        return "", str(locals().get("recording_id") or "").strip()


def _musicbrainz_description(artist: str, title: str) -> str:
    # Compatibility wrapper for any external/local callers that only need tags.
    return _musicbrainz_description_and_recording(artist, title)[0]


def _musicbrainz_album_for_recording(recording_id: str, expected_artist: str = "") -> str:
    """Automatically choose the most appropriate original artist release for a recording.

    Hard exclusions:
      - Various Artists releases
      - Compilation release-groups
      - Live / Remix release-groups
      - Releases credited to a different known artist

    Ranking among the remaining candidates is Official -> Album -> EP -> Single ->
    non-edition title -> earliest release.
    """
    recording_id = str(recording_id or "").strip()
    expected_artist = str(expected_artist or "").strip()
    if not recording_id:
        return ""

    def _fetch_releases(official_only=True):
        params = {
            "recording": recording_id,
            "inc": "release-groups+artist-credits",
            "limit": 100,
            "type": "album|ep|single",
        }
        if official_only:
            params["status"] = "official"
        payload = _musicbrainz_get_json("release/", params)
        return payload.get("releases") or [] if isinstance(payload, dict) else []

    def _artist_credit(entity):
        names, ids = [], []
        credits = entity.get("artist-credit") or [] if isinstance(entity, dict) else []
        if not isinstance(credits, list):
            credits = [credits]
        for credit in credits:
            if not isinstance(credit, dict):
                continue
            artist_obj = credit.get("artist") or {}
            if not isinstance(artist_obj, dict):
                artist_obj = {}
            name = str(credit.get("name") or artist_obj.get("name") or "").strip()
            artist_id = str(artist_obj.get("id") or "").strip()
            if name:
                names.append(name)
            if artist_id:
                ids.append(artist_id)
        return names, ids

    def _artist_matches(names):
        expected_key = _lookup_key(expected_artist)
        if not expected_key or not names:
            return True
        for name in names:
            key = _lookup_key(name)
            if not key:
                continue
            if key == expected_key or key in expected_key or expected_key in key:
                return True
            if difflib.SequenceMatcher(None, key, expected_key, autojunk=False).ratio() >= 0.82:
                return True
        return False

    try:
        releases = _fetch_releases(official_only=True)
        if not releases:
            releases = _fetch_releases(official_only=False)
        if not releases:
            _log("MusicBrainz album lookup returned no releases for the matched recording.")
            return ""

        primary_rank = {"album": 0, "ep": 1, "single": 2}
        secondary_penalty = {
            "dj-mix": 25,
            "mixtape/street": 25,
            "demo": 20,
            "soundtrack": 10,
        }
        hard_reject_secondary = {"compilation", "live", "remix"}
        edition_words = (
            "deluxe", "remaster", "remastered", "expanded", "anniversary",
            "special edition", "collector's edition", "collectors edition", "reissue",
        )
        various_artists_id = "89ad4ac3-39f7-470e-963a-56509c546377"
        reject_counts = {"compilation/live/remix": 0, "various artists": 0, "artist mismatch": 0}

        candidates = {}
        for release in releases:
            if not isinstance(release, dict):
                continue
            group = release.get("release-group") or {}
            if not isinstance(group, dict):
                group = {}

            album_title = str(group.get("title") or release.get("title") or "").strip()
            if not album_title:
                continue

            primary_type = str(group.get("primary-type") or "").strip().casefold()
            secondary_types = group.get("secondary-types") or []
            if not isinstance(secondary_types, list):
                secondary_types = [secondary_types]
            secondary_types = [str(x or "").strip().casefold() for x in secondary_types]

            # These are never acceptable as the Album output. There is deliberately
            # no final fallback to compilations, Various Artists, live or remix releases.
            if any(x in hard_reject_secondary for x in secondary_types):
                reject_counts["compilation/live/remix"] += 1
                continue

            credit_names, credit_ids = _artist_credit(group)
            if not credit_names and not credit_ids:
                credit_names, credit_ids = _artist_credit(release)
            credit_keys = {_lookup_key(name) for name in credit_names if name}
            if various_artists_id in credit_ids or "various artists" in credit_keys:
                reject_counts["various artists"] += 1
                continue
            if expected_artist and credit_names and not _artist_matches(credit_names):
                reject_counts["artist mismatch"] += 1
                continue

            type_score = primary_rank.get(primary_type, 3)
            secondary_score = sum(secondary_penalty.get(x, 0) for x in secondary_types)
            title_key = album_title.casefold()
            edition_score = 15 if any(word in title_key for word in edition_words) else 0

            date = str(group.get("first-release-date") or release.get("date") or "").strip()
            date_key = date if re.match(r"^\d{4}(?:-\d{2})?(?:-\d{2})?$", date) else "9999-99-99"

            status = str(release.get("status") or "").strip().casefold()
            status_score = 0 if status in {"", "official"} else 50
            # If artist credits are unexpectedly absent, keep the candidate but rank
            # it below one explicitly credited to the accepted artist.
            artist_score = 0 if credit_names else 5

            sort_key = (
                status_score,
                artist_score,
                type_score,
                secondary_score,
                edition_score,
                date_key,
                title_key,
            )
            group_key = str(group.get("id") or title_key).strip()
            previous = candidates.get(group_key)
            if previous is None or sort_key < previous[0]:
                candidates[group_key] = (sort_key, album_title, primary_type, date)

        rejected = ", ".join(f"{k}={v}" for k, v in reject_counts.items() if v)
        if rejected:
            _log(f"MusicBrainz album lookup exclusions: {rejected}.")

        if not candidates:
            _log(
                "MusicBrainz album lookup found no acceptable original-artist Album/EP/Single; "
                "Album output will remain blank."
            )
            return ""

        _key, album_title, primary_type, date = min(candidates.values(), key=lambda item: item[0])
        kind = primary_type.title() if primary_type else "Release"
        when = f" ({date})" if date else ""
        _log(f"MusicBrainz album lookup selected {kind}: {album_title}{when}.")
        return album_title
    except Exception as exc:
        _log(f"MusicBrainz album lookup failed: {exc}")
        return ""


@PromptServer.instance.routes.post(EMBEDDED_LYRICS_RESPONSE_ROUTE)
async def urn_audio_lyrics_embedded_response(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

    token = str(payload.get("token") or "")
    node_id = str(payload.get("node_id") or "")
    action = str(payload.get("action") or "continue").lower()
    if not token:
        return web.json_response({"ok": False, "error": "Missing embedded lyrics token."}, status=400)
    if action not in {"use", "continue", "stop"}:
        return web.json_response({"ok": False, "error": "Invalid action."}, status=400)

    with _embedded_lyrics_waiters_lock:
        waiter = _embedded_lyrics_waiters.get(token)
        if waiter is None:
            return web.json_response({"ok": False, "error": "This embedded lyrics request is no longer active."}, status=404)
        if node_id and node_id != waiter["node_id"]:
            return web.json_response({"ok": False, "error": "Node ID mismatch."}, status=409)
        waiter["action"] = action
        waiter["event"].set()

    return web.json_response({"ok": True, "complete": True})


@PromptServer.instance.routes.get(EMBEDDED_LYRICS_PENDING_ROUTE)
async def urn_audio_lyrics_embedded_pending(request):
    with _embedded_lyrics_waiters_lock:
        pending = [
            {
                "token": token,
                "node_id": waiter["node_id"],
                "lyrics": waiter["lyrics"],
                "artist": waiter.get("artist", ""),
                "title": waiter.get("title", ""),
            }
            for token, waiter in _embedded_lyrics_waiters.items()
            if not waiter["event"].is_set()
        ]
    return web.json_response({"ok": True, "pending": pending})


def _wait_for_embedded_lyrics_choice(node_id: str, lyrics: str, artist: str = "", title: str = ""):
    token = uuid.uuid4().hex
    event = threading.Event()
    waiter = {
        "event": event,
        "node_id": str(node_id),
        "lyrics": str(lyrics or ""),
        "artist": str(artist or ""),
        "title": str(title or ""),
        "action": "continue",
    }
    with _embedded_lyrics_waiters_lock:
        _embedded_lyrics_waiters[token] = waiter

    try:
        PromptServer.instance.send_sync(
            EMBEDDED_LYRICS_EVENT,
            {
                "token": token,
                "node_id": str(node_id),
                "lyrics": waiter["lyrics"],
                "artist": waiter["artist"],
                "title": waiter["title"],
            },
        )
        _log("Embedded lyrics supplied; waiting for Use Embedded Lyrics or Ignore / Search Online.")
        while not event.wait(0.10):
            if model_management is not None:
                model_management.throw_exception_if_processing_interrupted()

        if waiter["action"] == "stop":
            _send_status(str(node_id), "Workflow stopped by user")
            _log("Embedded lyrics selection stopped by user; interrupting the current workflow.")
            if model_management is not None and hasattr(model_management, "InterruptProcessingException"):
                raise model_management.InterruptProcessingException()
            raise RuntimeError("URN Audio Lyrics: workflow stopped by user.")
        return waiter["action"]
    finally:
        with _embedded_lyrics_waiters_lock:
            _embedded_lyrics_waiters.pop(token, None)


@PromptServer.instance.routes.post(LYRICS_OVH_RESPONSE_ROUTE)
async def urn_audio_lyrics_lookup_response(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

    token = str(payload.get("token") or "")
    node_id = str(payload.get("node_id") or "")
    action = str(payload.get("action") or "reject").lower()
    if not token:
        return web.json_response({"ok": False, "error": "Missing lookup token."}, status=400)
    if action not in {"accept", "manual", "search", "reject", "stop"}:
        return web.json_response({"ok": False, "error": "Invalid action."}, status=400)

    with _lyrics_ovh_waiters_lock:
        waiter = _lyrics_ovh_waiters.get(token)
        if waiter is None:
            return web.json_response(
                {"ok": False, "error": "This lyrics request is no longer active."},
                status=404,
            )
        if node_id and node_id != waiter["node_id"]:
            return web.json_response({"ok": False, "error": "Node ID mismatch."}, status=409)

    # The automatic filename-based match can be rejected without immediately
    # falling back to Whisper.  Switch the SAME pending request into manual
    # artist-search mode so the Comfy workflow remains paused while the user
    # searches for the correct artist/title.
    if action == "manual":
        with _lyrics_ovh_waiters_lock:
            waiter = _lyrics_ovh_waiters.get(token)
            if waiter is None:
                return web.json_response({"ok": False, "error": "Lookup expired."}, status=404)
            waiter["mode"] = "manual"
            waiter["options"] = []
            waiter["selected_index"] = 0
            artist = waiter.get("artist", "")
        return web.json_response({
            "ok": True,
            "mode": "manual",
            "artist": artist,
            "options": [],
            "selected_index": 0,
        })

    if action == "search":
        artist = str(payload.get("artist") or "").strip()
        if not artist:
            return web.json_response({"ok": False, "error": "Enter an artist name first."}, status=400)
        try:
            options = await asyncio.to_thread(_lyrics_ovh_suggestions, artist, "")
        except Exception as exc:
            _log(f"Manual artist search failed for {artist!r}: {exc}")
            options = []
        with _lyrics_ovh_waiters_lock:
            waiter = _lyrics_ovh_waiters.get(token)
            if waiter is None:
                return web.json_response({"ok": False, "error": "Lookup expired."}, status=404)
            waiter["mode"] = "manual"
            waiter["artist"] = artist
            waiter["options"] = options
            waiter["selected_index"] = 0
        _log(f"Manual artist search returned {len(options)} title(s) for {artist!r}.")
        return web.json_response({
            "ok": True,
            "mode": "manual",
            "artist": artist,
            "options": options,
            "selected_index": 0,
            "searched": True,
        })

    with _lyrics_ovh_waiters_lock:
        waiter = _lyrics_ovh_waiters.get(token)
        if waiter is None:
            return web.json_response({"ok": False, "error": "Lookup expired."}, status=404)

        waiter["action"] = action
        if action == "accept":
            if not waiter.get("options"):
                return web.json_response(
                    {"ok": False, "error": "Search for an artist and select a title first."},
                    status=400,
                )
            try:
                selected_index = int(payload.get("selected_index", 0))
            except Exception:
                selected_index = 0
            selected_index = max(0, min(selected_index, len(waiter["options"]) - 1))
            waiter["selected_index"] = selected_index
        waiter["event"].set()

    return web.json_response({"ok": True, "complete": True})


@PromptServer.instance.routes.get(LYRICS_OVH_PENDING_ROUTE)
async def urn_audio_lyrics_lookup_pending(request):
    with _lyrics_ovh_waiters_lock:
        pending = [
            {
                "token": token,
                "node_id": waiter["node_id"],
                "artist": waiter["artist"],
                "guessed_title": waiter["guessed_title"],
                "options": waiter["options"],
                "selected_index": waiter["selected_index"],
                "mode": waiter.get("mode", "automatic"),
            }
            for token, waiter in _lyrics_ovh_waiters.items()
            if not waiter["event"].is_set()
        ]
    return web.json_response({"ok": True, "pending": pending})


def _wait_for_lyrics_ovh_choice(node_id: str, artist: str, guessed_title: str, options, mode: str = "automatic"):
    token = uuid.uuid4().hex
    event = threading.Event()
    waiter = {
        "event": event,
        "node_id": str(node_id),
        "artist": artist,
        "guessed_title": guessed_title,
        "options": options,
        "selected_index": _lyrics_ovh_best_index(options, guessed_title),
        "action": "reject",
        "mode": str(mode or "automatic"),
    }
    with _lyrics_ovh_waiters_lock:
        _lyrics_ovh_waiters[token] = waiter

    try:
        PromptServer.instance.send_sync(
            LYRICS_OVH_EVENT,
            {
                "token": token,
                "node_id": str(node_id),
                "artist": artist,
                "guessed_title": guessed_title,
                "options": options,
                "selected_index": waiter["selected_index"],
                "mode": waiter.get("mode", "automatic"),
            },
        )
        if waiter.get("mode") == "manual":
            _log("Waiting for manual artist/title search before Whisper fallback.")
        else:
            _log(
                f"Lyrics.ovh returned {len(options)} title(s) for {artist!r}; "
                "waiting for Accept or manual search."
            )
        while not event.wait(0.10):
            if model_management is not None:
                model_management.throw_exception_if_processing_interrupted()

        if waiter["action"] == "stop":
            _send_status(str(node_id), "Workflow stopped by user")
            _log("Song selection stopped by user; interrupting the current workflow.")
            if model_management is not None and hasattr(model_management, "InterruptProcessingException"):
                raise model_management.InterruptProcessingException()
            raise RuntimeError("URN Audio Lyrics: workflow stopped by user.")
        if waiter["action"] != "accept":
            return None
        selected = dict(waiter["options"][waiter["selected_index"]])
        selected["_lookup_mode"] = waiter.get("mode", "automatic")
        return selected
    finally:
        with _lyrics_ovh_waiters_lock:
            _lyrics_ovh_waiters.pop(token, None)


def _audio_to_waveform(audio_input):
    """Accept normal ComfyUI AUDIO dictionaries and lazy VHS-style mappings."""
    if audio_input is None:
        raise ValueError("A valid AUDIO input must be connected.")

    try:
        waveform = audio_input["waveform"]
        sample_rate = int(audio_input["sample_rate"])
    except (TypeError, KeyError, AttributeError, IndexError):
        raise ValueError("A valid AUDIO input must be connected.") from None

    if not isinstance(waveform, torch.Tensor):
        waveform = torch.as_tensor(waveform)

    waveform = waveform.detach().float().cpu()
    if waveform.ndim == 3:
        waveform = waveform[0]
    elif waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim != 2:
        raise ValueError(f"Unsupported AUDIO waveform shape: {tuple(waveform.shape)}")

    return waveform.contiguous(), sample_rate


def _probe_duration(filepath: str) -> float:
    """Read duration without decoding the whole file when container metadata is available."""
    with av.open(filepath) as container:
        if not container.streams.audio:
            raise ValueError("No audio stream found in the selected file.")

        stream = container.streams.audio[0]
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
            if duration > 0:
                return duration

        if container.duration is not None:
            duration = float(container.duration) / float(av.time_base)
            if duration > 0:
                return duration

        sample_rate = int(stream.codec_context.sample_rate or 0)
        if sample_rate <= 0:
            raise ValueError("Could not determine the audio duration.")

        total_samples = 0
        for frame in container.decode(audio=0):
            total_samples += int(getattr(frame, "samples", 0) or 0)
        if total_samples <= 0:
            raise ValueError("Could not determine the audio duration.")
        return total_samples / float(sample_rate)


def _segments_to_srt(transcript_segments, section_starts=None) -> str:
    """Create standard SRT. Optional section labels are placed inside the first cue text."""
    section_starts = section_starts or {}
    blocks = []
    cue_number = 1
    for segment_index, (start, end, text_value) in enumerate(transcript_segments):
        clean_text = str(text_value or "").strip()
        if not clean_text:
            continue
        label = section_starts.get(segment_index)
        if label:
            clean_text = f"[{label}]\n{clean_text}"
        blocks.append(
            f"{cue_number}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{clean_text}"
        )
        cue_number += 1
    return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")


def _segments_to_plain_text(transcript_segments) -> str:
    """Keep Whisper's natural segment breaks so lyrics remain easy to read downstream."""
    lines = []
    for _start, _end, text_value in transcript_segments:
        clean_text = str(text_value or "").strip()
        if clean_text:
            lines.append(clean_text)
    return "\n".join(lines).strip()


def _normalise_lyric_text(value: str) -> str:
    """
    Normalise text for repeat matching without altering user-visible output.

    This intentionally handles common transcription differences such as punctuation,
    curly apostrophes, repeated whitespace, and simple filler-only artefacts. It does
    not rewrite the lyrics themselves.
    """
    value = str(value or "").strip().lower()
    if not value:
        return ""

    # Whisper sometimes emits bracketed non-lyric cues. They are not useful for
    # chorus matching and can otherwise make repeated music breaks look like lyrics.
    if re.fullmatch(r"\s*[\[(].{0,40}(music|instrumental|applause|cheering).{0,40}[\])]\s*", value):
        return ""

    value = (
        value.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("–", "-")
        .replace("—", "-")
    )
    value = re.sub(r"\b(gonna)\b", "going to", value)
    value = re.sub(r"\b(wanna)\b", "want to", value)
    value = re.sub(r"\b(gotta)\b", "got to", value)
    value = re.sub(r"\b('cause|cuz|cos)\b", "because", value)
    value = re.sub(r"[^\w\s']+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _window_text(normalised, start: int, length: int) -> str:
    return " ".join(x for x in normalised[start:start + length] if x).strip()


def _word_tokens(value: str):
    return [token for token in str(value or "").split() if token]


def _repeat_similarity(a: str, b: str) -> float:
    """Fuzzy lyric similarity robust to small Whisper wording/punctuation differences."""
    if not a or not b:
        return 0.0

    a_words = _word_tokens(a)
    b_words = _word_tokens(b)
    if not a_words or not b_words:
        return 0.0

    word_sequence = difflib.SequenceMatcher(None, a_words, b_words, autojunk=False).ratio()
    char_sequence = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    a_set = set(a_words)
    b_set = set(b_words)
    overlap = len(a_set & b_set)
    if overlap:
        precision = overlap / max(1, len(b_set))
        recall = overlap / max(1, len(a_set))
        token_f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    else:
        token_f1 = 0.0

    return (0.52 * word_sequence) + (0.28 * char_sequence) + (0.20 * token_f1)


def _edge_similarity(a: str, b: str) -> float:
    """Check that the beginnings and ends also agree so verses are not swallowed into a chorus."""
    a_words = _word_tokens(a)
    b_words = _word_tokens(b)
    if not a_words or not b_words:
        return 0.0

    edge_count = max(2, min(8, int(round(min(len(a_words), len(b_words)) * 0.30))))
    a_head = " ".join(a_words[:edge_count])
    b_head = " ".join(b_words[:edge_count])
    a_tail = " ".join(a_words[-edge_count:])
    b_tail = " ".join(b_words[-edge_count:])
    return min(_repeat_similarity(a_head, b_head), _repeat_similarity(a_tail, b_tail))


def _gap_before(transcript_segments, index: int) -> float:
    if index <= 0:
        return 1.0
    return max(0.0, float(transcript_segments[index][0]) - float(transcript_segments[index - 1][1]))


def _gap_after(transcript_segments, end_index: int) -> float:
    if end_index >= len(transcript_segments):
        return 1.0
    return max(0.0, float(transcript_segments[end_index][0]) - float(transcript_segments[end_index - 1][1]))


def _boundary_bonus(transcript_segments, start: int, end: int) -> float:
    """Small preference for repeated blocks that begin/end near natural timing gaps."""
    before = min(1.5, _gap_before(transcript_segments, start)) / 1.5
    after = min(1.5, _gap_after(transcript_segments, end)) / 1.5
    return 0.5 * (before + after)


def _candidate_window(transcript_segments, normalised, start: int, length: int):
    if start < 0 or length <= 0 or start + length > len(transcript_segments):
        return None
    text = _window_text(normalised, start, length)
    if not text:
        return None
    words = len(text.split())
    duration = max(0.0, float(transcript_segments[start + length - 1][1]) - float(transcript_segments[start][0]))
    return {
        "start": start,
        "end": start + length,
        "length": length,
        "text": text,
        "words": words,
        "duration": duration,
        "boundary": _boundary_bonus(transcript_segments, start, start + length),
    }


def _best_window_match(transcript_segments, normalised, base, candidate_start: int):
    """Allow +/- one Whisper segment so the same sung section can have different segmentation."""
    best = None
    for candidate_length in range(max(1, base["length"] - 1), min(7, base["length"] + 1) + 1):
        candidate = _candidate_window(transcript_segments, normalised, candidate_start, candidate_length)
        if candidate is None:
            continue

        # Repeated sections should be broadly similar in size even if Whisper split them differently.
        if base["words"] and candidate["words"]:
            size_ratio = min(base["words"], candidate["words"]) / max(base["words"], candidate["words"])
            if size_ratio < 0.62:
                continue

        sim = _repeat_similarity(base["text"], candidate["text"])
        edge = _edge_similarity(base["text"], candidate["text"])
        combined = (0.84 * sim) + (0.16 * edge)
        if best is None or combined > best[0]:
            best = (combined, sim, edge, candidate)
    return best


def _find_chorus_intervals(transcript_segments):
    """
    Detect a repeated chorus using fuzzy multi-line matching and timing boundaries.

    It remains deliberately conservative. If the evidence is weak, no Verse/Chorus
    labels are emitted rather than guessing song structure.
    """
    cleaned = [str(item[2] or "").strip() for item in transcript_segments]
    normalised = [_normalise_lyric_text(text) for text in cleaned]
    n = len(normalised)
    if n < 3:
        return []

    max_len = min(6, max(1, n // 2))
    best_result = None

    for length in range(1, max_len + 1):
        for i in range(0, n - length + 1):
            base = _candidate_window(transcript_segments, normalised, i, length)
            if base is None:
                continue

            # One-segment hooks need more words and three appearances to avoid false positives.
            min_words = 8 if length == 1 else 11
            if base["words"] < min_words:
                continue
            if base["duration"] < 2.5 or base["duration"] > 45.0:
                continue

            threshold = 0.84 if length == 1 else 0.76
            edge_threshold = 0.46 if length == 1 else 0.50

            hits = [(base["start"], base["end"], 1.0, base["boundary"])]
            cursor = base["end"] + 1
            while cursor < n:
                best_local = None
                # Search a small local band because Whisper can shift section boundaries by one segment.
                for candidate_start in range(cursor, min(n, cursor + 3)):
                    match = _best_window_match(transcript_segments, normalised, base, candidate_start)
                    if match is None:
                        continue
                    combined, sim, edge, candidate = match
                    if sim < threshold or edge < edge_threshold:
                        continue
                    if best_local is None or combined > best_local[0]:
                        best_local = (combined, candidate, sim, edge)

                if best_local is not None:
                    combined, candidate, sim, edge = best_local
                    hits.append((candidate["start"], candidate["end"], combined, candidate["boundary"]))
                    cursor = candidate["end"] + 1
                else:
                    cursor += 1

            min_hits = 3 if length == 1 else 2
            if len(hits) < min_hits:
                continue

            # Ensure the repeated occurrences are genuinely separated by other material.
            separated = any(hits[k][0] - hits[k - 1][1] >= 1 for k in range(1, len(hits)))
            if not separated:
                continue

            avg_similarity = sum(hit[2] for hit in hits[1:]) / max(1, len(hits) - 1)
            avg_boundary = sum(hit[3] for hit in hits) / len(hits)

            # Reward more repeat evidence and natural timing boundaries, but do not simply
            # prefer the largest possible block (which can absorb verse/pre-chorus material).
            useful_words = min(36.0, float(base["words"])) / 36.0
            length_preference = 1.0 - min(0.20, abs(length - 3) * 0.035)
            score = (
                avg_similarity
                + (0.045 * min(3, len(hits) - 2))
                + (0.055 * avg_boundary)
                + (0.035 * useful_words)
            ) * length_preference

            result = {
                "score": score,
                "hits": [(hit[0], hit[1]) for hit in hits],
                "avg_similarity": avg_similarity,
                "words": base["words"],
            }
            if best_result is None or result["score"] > best_result["score"]:
                best_result = result

    if best_result is None:
        return []

    # Last sanity check: keep only sorted, non-overlapping occurrences.
    intervals = []
    for start, end in sorted(best_result["hits"]):
        if start >= end:
            continue
        if intervals and start < intervals[-1][1]:
            continue
        intervals.append((start, end))
    return intervals if len(intervals) >= 2 else []




# Common low-information words are ignored when deciding whether a trailing line
# still belongs to a chorus/refrain.  This is only used for structure detection;
# user-visible lyric text is never rewritten.
_CHORUS_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "to", "of", "in", "on", "at",
    "for", "from", "with", "as", "by", "is", "am", "are", "was", "were", "be", "been", "being",
    "i", "me", "my", "mine", "you", "your", "yours", "we", "our", "ours", "they", "their", "theirs",
    "he", "his", "she", "her", "hers", "it", "its", "this", "that", "these", "those", "do", "does",
    "did", "have", "has", "had", "can", "could", "will", "would", "shall", "should", "may", "might",
    "just", "yeah", "yea", "oh", "ooh", "ah", "uh", "huh", "baby", "hey", "well", "really", "very",
}


def _content_tokens(value: str):
    return [token for token in _word_tokens(value) if token not in _CHORUS_STOPWORDS and len(token) > 1]


def _longest_common_word_run(a: str, b: str) -> int:
    a_words = _word_tokens(a)
    b_words = _word_tokens(b)
    if not a_words or not b_words:
        return 0
    match = difflib.SequenceMatcher(None, a_words, b_words, autojunk=False).find_longest_match(
        0, len(a_words), 0, len(b_words)
    )
    return int(match.size)


def _best_forward_continuation(transcript_segments, normalised, a_pos: int, a_limit: int, b_pos: int, b_limit: int):
    """
    Match the material immediately after two chorus anchors.

    Unlike the original detector, this compares variable groups of Whisper segments
    (1..6 on each side).  That lets one chorus occurrence be split across several
    subtitles while another occurrence is returned as one long subtitle.
    """
    if a_pos >= a_limit or b_pos >= b_limit:
        return None

    best = None
    max_a = min(6, a_limit - a_pos)
    max_b = min(6, b_limit - b_pos)
    for a_len in range(1, max_a + 1):
        a_text = _window_text(normalised, a_pos, a_len)
        if not a_text:
            continue
        a_words = len(_word_tokens(a_text))
        if a_words < 4:
            continue

        for b_len in range(1, max_b + 1):
            b_text = _window_text(normalised, b_pos, b_len)
            if not b_text:
                continue
            b_words = len(_word_tokens(b_text))
            if b_words < 4:
                continue

            size_ratio = min(a_words, b_words) / max(a_words, b_words)
            if size_ratio < 0.42:
                continue

            similarity = _repeat_similarity(a_text, b_text)
            longest_run = _longest_common_word_run(a_text, b_text)
            # A long exact run is strong evidence even when surrounding filler/ad-libs differ.
            run_bonus = min(1.0, longest_run / 8.0)

            a_end = a_pos + a_len
            b_end = b_pos + b_len
            boundary = 0.5 * (
                _boundary_bonus(transcript_segments, a_pos, a_end)
                + _boundary_bonus(transcript_segments, b_pos, b_end)
            )
            score = similarity + (0.035 * size_ratio) + (0.025 * run_bonus) + (0.018 * boundary)

            # Keep this noticeably looser than the anchor detector: at this stage we
            # already know both positions immediately follow a confidently repeated chorus core.
            qualifies = similarity >= 0.62 or (similarity >= 0.56 and longest_run >= 5)
            if not qualifies:
                continue

            candidate = (score, similarity, a_end, b_end, a_words + b_words)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[4] > best[4]):
                best = candidate

    return best


def _chorus_hook_affinity(segment_text: str, chorus_reference: str) -> float:
    """
    Score a short trailing refrain/tag against the already-aligned chorus.

    This is deliberately phrase-driven.  A line such as "can't love me better" can
    stay attached to a chorus containing "love me better than you can", while a new
    verse such as "paint my nails cherry red" has no meaningful phrase overlap.
    """
    if not segment_text or not chorus_reference:
        return 0.0

    longest_run = _longest_common_word_run(segment_text, chorus_reference)
    seg_content = set(_content_tokens(segment_text))
    ref_content = set(_content_tokens(chorus_reference))
    overlap = len(seg_content & ref_content)
    coverage = overlap / max(1, len(seg_content)) if seg_content else 0.0

    # Direct similarity to the whole chorus will normally be low because the chorus
    # is much longer, so phrase length and distinctive-token coverage carry more weight.
    phrase_score = min(1.0, longest_run / 4.0)
    overlap_score = min(1.0, coverage) if overlap >= 2 else 0.0
    return max(phrase_score, overlap_score)


def _expand_chorus_intervals(transcript_segments, intervals):
    """
    Expand confidently repeated chorus *anchors* into fuller chorus regions.

    The anchor detector intentionally stays conservative.  Here we align the text that
    immediately follows neighbouring anchors using variable-size subtitle groups, then
    optionally absorb a few hook/refrain lines that clearly echo the chorus vocabulary.
    This prevents the old failure mode where a three-line repeated anchor caused the
    remaining half of the chorus to be mislabeled as a new verse.
    """
    if len(intervals) < 2:
        return intervals

    normalised = [_normalise_lyric_text(str(item[2] or "")) for item in transcript_segments]
    n = len(normalised)
    starts = [max(0, min(n, int(start))) for start, _end in intervals]
    ends = [max(starts[i], min(n, int(intervals[i][1]))) for i in range(len(intervals))]

    # Repeatedly align forward continuations.  Multiple passes allow three or more
    # chorus occurrences to propagate the best boundary through the chain.
    for _pass in range(8):
        changed = False
        for i in range(len(starts) - 1):
            a_limit = starts[i + 1]
            b_limit = starts[i + 2] if i + 2 < len(starts) else n
            match = _best_forward_continuation(
                transcript_segments, normalised, ends[i], a_limit, ends[i + 1], b_limit
            )
            if match is None:
                continue
            _score, _similarity, a_end, b_end, _word_total = match
            if a_end > ends[i] or b_end > ends[i + 1]:
                ends[i] = max(ends[i], a_end)
                ends[i + 1] = max(ends[i + 1], b_end)
                changed = True
        if not changed:
            break

    # Build a reference from the aligned chorus text, then absorb only a small number
    # of immediately following refrain/tag lines with strong phrase affinity.
    chorus_reference = " ".join(
        _window_text(normalised, starts[i], max(0, ends[i] - starts[i]))
        for i in range(len(starts))
    ).strip()

    for i in range(len(starts)):
        limit = starts[i + 1] if i + 1 < len(starts) else n
        added = 0
        while ends[i] < limit and added < 4:
            idx = ends[i]
            segment_text = normalised[idx]
            if not segment_text:
                break

            affinity = _chorus_hook_affinity(segment_text, chorus_reference)
            # A noticeable pause followed by only borderline similarity is more likely
            # to be a genuine section boundary, so require stronger evidence there.
            gap = _gap_before(transcript_segments, idx)
            threshold = 0.78 if gap >= 1.75 else 0.72
            if affinity < threshold:
                break

            ends[i] += 1
            added += 1
            chorus_reference = f"{chorus_reference} {segment_text}".strip()

    expanded = []
    for i, start in enumerate(starts):
        end = min(ends[i], starts[i + 1] if i + 1 < len(starts) else n)
        if start < end:
            expanded.append((start, end))
    return expanded if len(expanded) >= 2 else intervals



def _segment_interval_times(transcript_segments, interval):
    start, end = interval
    if start < 0 or end <= start or start >= len(transcript_segments):
        return None
    end = min(end, len(transcript_segments))
    return float(transcript_segments[start][0]), float(transcript_segments[end - 1][1])


def _bar_start_beats(bundle):
    """Return beat indices that represent the detected start of each musical bar."""
    n_beats = int(bundle["chroma"].shape[1])
    meter = max(3, int(bundle.get("meter", 4) or 4))
    phase = int(bundle.get("bar_phase", 0) or 0) % meter
    starts = list(range(phase, n_beats, meter))
    # A weak meter estimate should not prevent analysis; 4/4 remains the package's
    # preferred grid, but the Seamless Loop analyser can also return a clear 3/4 grid.
    return starts, meter


def _nearest_bar_candidates(bundle, anchor_time: float, ctx_beats: int, radius_bars: int = 2):
    beat_times = np.asarray(bundle.get("beat_times", []), dtype=np.float64)
    bar_starts, meter = _bar_start_beats(bundle)
    if not bar_starts or beat_times.size < 2:
        return []

    usable = [beat for beat in bar_starts if beat + ctx_beats < beat_times.size]
    if not usable:
        return []
    nearest_pos = min(range(len(usable)), key=lambda pos: abs(float(beat_times[usable[pos]]) - anchor_time))
    lo = max(0, nearest_pos - radius_bars)
    hi = min(len(usable), nearest_pos + radius_bars + 1)
    return usable[lo:hi]


def _music_boundary_strength(bundle, beat_index: int) -> float:
    """0..1 estimate that a bar boundary is a genuine musical section change."""
    n = int(bundle["chroma"].shape[1])
    meter = max(3, int(bundle.get("meter", 4) or 4))
    if beat_index < meter or beat_index + meter > n:
        return 0.45

    comp = _phrase_match_components(bundle, beat_index - meter, beat_index, meter)
    if comp is None:
        return 0.45
    change = 1.0 - float(comp.get("structural", 0.5))

    # Level changes are particularly useful at pop chorus entrances/exits.  They are
    # evidence only: a chorus is never inferred from loudness by itself.
    raw = np.asarray(bundle.get("rms_raw"), dtype=np.float64)
    level_change = 0.0
    if raw.ndim == 2 and raw.shape[1] >= beat_index + meter:
        before = float(np.mean(raw[:, beat_index - meter:beat_index]))
        after = float(np.mean(raw[:, beat_index:beat_index + meter]))
        denom = max(1e-6, abs(before) + abs(after))
        level_change = min(1.0, abs(after - before) / denom * 2.2)

    return float(np.clip((0.76 * change) + (0.24 * level_change), 0.0, 1.0))


def _window_alignment_score(bundle, beat_index: int, anchor_time: float, anchor_end: float, ctx_beats: int) -> float:
    beat_times = np.asarray(bundle.get("beat_times", []), dtype=np.float64)
    if beat_index < 0 or beat_index + ctx_beats >= beat_times.size:
        return 0.0
    start_time = float(beat_times[beat_index])
    end_time = float(beat_times[beat_index + ctx_beats])
    meter = max(3, int(bundle.get("meter", 4) or 4))
    bar_duration = max(0.25, (end_time - start_time) / max(1.0, ctx_beats / meter))

    start_error_bars = abs(start_time - anchor_time) / bar_duration
    start_alignment = math.exp(-0.75 * start_error_bars)
    # The repeated lyric anchor must fit inside (or almost inside) the music window.
    if end_time + 0.75 < anchor_end:
        coverage = max(0.0, 1.0 - (anchor_end - end_time) / max(1.0, 2.0 * bar_duration))
    else:
        coverage = 1.0
    return float(np.clip((0.72 * start_alignment) + (0.28 * coverage), 0.0, 1.0))


def _segment_end_for_time(transcript_segments, end_time: float, minimum_end: int) -> int:
    """Map a musical boundary back onto Whisper segments without splitting a cue."""
    end_index = max(0, int(minimum_end))
    for idx, (start, _end, _text) in enumerate(transcript_segments):
        if idx < end_index:
            continue
        # Keep a subtitle that starts just before the musical boundary; cutting a cue
        # in half is worse than being a few hundred ms late.
        if float(start) < end_time - 0.10:
            end_index = idx + 1
        else:
            break
    return min(len(transcript_segments), end_index)


def _analyse_music_assisted_chorus(source_path: str, transcript_segments, anchors, expanded_intervals, precomputed_bundle=None):
    """
    Refine lyric-derived chorus regions with beat/bar-aware musical repetition.

    Text remains the primary evidence.  This stage uses the original mix to ask:
      * do 4- or 8-bar windows at the lyric anchors repeat musically?
      * do the following 4-bar blocks continue repeating?
      * is there a plausible bar/energy/timbre boundary at the resulting edge?

    It is intentionally conservative and falls back to the text-only result on any
    analysis failure or weak musical agreement.
    """
    if len(anchors) < 2 or len(expanded_intervals) < 2:
        return expanded_intervals, False, None

    try:
        if precomputed_bundle is not None:
            bundle = precomputed_bundle
        else:
            waveform, sample_rate = _load_comfy_audio(source_path)
            mono = _analysis_signal(waveform)
            bundle = _advanced_feature_bundle(mono, sample_rate, "Advanced (Librosa)")
    except Exception as exc:
        _log(f"Music-assisted structure analysis unavailable; keeping text-only sections. {exc}")
        return expanded_intervals, False, None

    anchor_times = [_segment_interval_times(transcript_segments, interval) for interval in anchors]
    if any(value is None for value in anchor_times[:2]):
        return expanded_intervals, False, bundle

    best = None
    meter = max(3, int(bundle.get("meter", 4) or 4))
    beat_times = np.asarray(bundle.get("beat_times", []), dtype=np.float64)

    # Search the two most useful phrase lengths first: 4 bars for compact hooks and
    # 8 bars for the most common pop chorus phrase.  Longer choruses are built by
    # extending in repeated 4-bar blocks after this initial lock-on.
    for bars in (4, 8):
        ctx_beats = bars * meter
        a0, a1 = anchor_times[0], anchor_times[1]
        cands0 = _nearest_bar_candidates(bundle, a0[0], ctx_beats)
        cands1 = _nearest_bar_candidates(bundle, a1[0], ctx_beats)
        for b0 in cands0:
            align0 = _window_alignment_score(bundle, b0, a0[0], a0[1], ctx_beats)
            if align0 < 0.44:
                continue
            for b1 in cands1:
                align1 = _window_alignment_score(bundle, b1, a1[0], a1[1], ctx_beats)
                if align1 < 0.44:
                    continue
                comp = _phrase_match_components(bundle, b0, b1, ctx_beats)
                if comp is None:
                    continue
                structural = float(comp.get("structural", 0.0))
                if structural < 0.60:
                    continue
                alignment = 0.5 * (align0 + align1)
                boundary = 0.5 * (_music_boundary_strength(bundle, b0) + _music_boundary_strength(bundle, b1))
                # Repetition dominates.  Timing and boundary evidence only break ties.
                score = (0.76 * structural) + (0.16 * alignment) + (0.08 * boundary)
                if bars == 8:
                    score += 0.008  # tiny preference for a full phrase when evidence is equal
                item = {
                    "score": score,
                    "structural": structural,
                    "ctx_beats": ctx_beats,
                    "bars": bars,
                    "starts": [b0, b1],
                }
                if best is None or item["score"] > best["score"]:
                    best = item

    if best is None:
        _log("Music-assisted structure: no strong 4/8-bar agreement; keeping text-only sections.")
        return expanded_intervals, False, bundle

    # Align any additional chorus occurrences to the first occurrence.  A later chorus
    # can differ more because of ad-libs/production, so the threshold is slightly looser.
    ctx_beats = int(best["ctx_beats"])
    reference_start = int(best["starts"][0])
    starts = list(best["starts"])
    matched_occurrences = 2
    for idx in range(2, len(anchors)):
        times = anchor_times[idx]
        if times is None:
            starts.append(None)
            continue
        local_best = None
        for candidate in _nearest_bar_candidates(bundle, times[0], ctx_beats):
            comp = _phrase_match_components(bundle, reference_start, candidate, ctx_beats)
            if comp is None:
                continue
            structural = float(comp.get("structural", 0.0))
            align = _window_alignment_score(bundle, candidate, times[0], times[1], ctx_beats)
            score = (0.82 * structural) + (0.18 * align)
            if structural >= 0.55 and (local_best is None or score > local_best[0]):
                local_best = (score, candidate, structural)
        if local_best is None:
            starts.append(None)
        else:
            starts.append(int(local_best[1]))
            matched_occurrences += 1

    # Grow the chorus in 4-bar blocks only when those blocks also repeat musically.
    current_beats = ctx_beats
    max_beats = meter * 16
    while current_beats + (4 * meter) <= max_beats:
        block_beats = 4 * meter
        valid = [start for start in starts if start is not None and start + current_beats + block_beats < beat_times.size]
        if len(valid) < 2:
            break
        ref_block = valid[0] + current_beats
        similarities = []
        for other_start in valid[1:]:
            comp = _phrase_match_components(bundle, ref_block, other_start + current_beats, block_beats)
            if comp is not None:
                similarities.append(float(comp.get("structural", 0.0)))
        if not similarities:
            break
        avg_similarity = sum(similarities) / len(similarities)
        if avg_similarity < 0.61 or min(similarities) < 0.54:
            break
        current_beats += block_beats

    refined = []
    for idx, expanded in enumerate(expanded_intervals):
        start_idx, end_idx = expanded
        music_start = starts[idx] if idx < len(starts) else None
        if music_start is None or music_start + current_beats >= beat_times.size:
            refined.append(expanded)
            continue

        music_end_time = float(beat_times[music_start + current_beats])
        music_end_idx = _segment_end_for_time(transcript_segments, music_end_time, start_idx)
        original_times = _segment_interval_times(transcript_segments, expanded)
        original_end_time = original_times[1] if original_times else music_end_time
        end_boundary = _music_boundary_strength(bundle, music_start + current_beats)

        # Normally music may only extend the lyric-derived boundary.  It is allowed to
        # shorten a clearly over-expanded text region only when a strong repeated phrase
        # and a strong musical boundary agree, and only by more than roughly one bar.
        final_end = max(end_idx, music_end_idx)
        if original_times and original_end_time > music_end_time:
            beat_span = float(beat_times[min(music_start + meter, beat_times.size - 1)] - beat_times[music_start])
            if (
                best["structural"] >= 0.72
                and end_boundary >= 0.62
                and original_end_time - music_end_time > max(1.0, beat_span)
            ):
                final_end = max(start_idx + 1, music_end_idx)

        # Never let one chorus run into the next confidently detected chorus anchor.
        if idx + 1 < len(anchors):
            final_end = min(final_end, anchors[idx + 1][0])
        refined.append((start_idx, max(start_idx + 1, final_end)))

    info = {
        "bpm": float(bundle.get("bpm", 0.0) or 0.0),
        "meter": meter,
        "bars": int(round(current_beats / meter)),
        "structural": float(best["structural"]),
        "matched_occurrences": matched_occurrences,
    }
    return refined, True, info



def _estimate_vocal_prompt(vocal_source_path: str):
    """Best-effort lead-vocal sex/range class for Music_Description.

    Only returns ``female lead vocal`` or ``male lead vocal`` when the isolated
    vocal pitch distribution is sufficiently clear. Ambiguous results are omitted
    rather than adding generic/timbre/register descriptors.
    """
    if not vocal_source_path or not os.path.isfile(vocal_source_path):
        return []
    try:
        import librosa

        waveform, sample_rate = _load_comfy_audio(vocal_source_path)
        mono = _analysis_signal(waveform).detach().float().cpu().numpy().astype(np.float32, copy=False)
        if mono.size < max(4096, sample_rate):
            return []
        analysis_sr = 16000
        if sample_rate != analysis_sr:
            mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=analysis_sr, res_type="kaiser_fast")
        if mono.size > analysis_sr * 180:
            # Three minutes is ample for a stable pitch estimate and avoids expensive YIN on long tracks.
            stride = max(1, int(math.ceil(mono.size / float(analysis_sr * 180))))
            mono = mono[::stride]
            effective_sr = max(4000, int(round(analysis_sr / stride)))
        else:
            effective_sr = analysis_sr

        frame_length = 2048 if effective_sr >= 12000 else 1024
        hop = max(128, frame_length // 4)
        rms = librosa.feature.rms(y=mono, frame_length=frame_length, hop_length=hop)[0]
        if rms.size < 4 or float(np.max(rms)) < 1e-5:
            return []
        active_threshold = max(float(np.percentile(rms, 55)) * 0.55, float(np.max(rms)) * 0.08)
        f0 = librosa.yin(
            mono,
            fmin=70.0,
            fmax=520.0,
            sr=effective_sr,
            frame_length=frame_length,
            hop_length=hop,
        )
        count = min(len(f0), len(rms))
        f0 = np.asarray(f0[:count], dtype=np.float64)
        active = np.asarray(rms[:count], dtype=np.float64) >= active_threshold
        valid = np.isfinite(f0) & active & (f0 >= 70.0) & (f0 <= 520.0)
        if int(np.sum(valid)) < 12:
            return []

        median_f0 = float(np.median(f0[valid]))
        p75_f0 = float(np.percentile(f0[valid], 75))
        if median_f0 >= 180.0 or p75_f0 >= 245.0:
            return ["female lead vocal"]
        if median_f0 <= 145.0 and p75_f0 <= 215.0:
            return ["male lead vocal"]
        return []
    except Exception as exc:
        _log(f"Vocal lead analysis unavailable: {exc}")
        return []


def _music_prompt_analysis(
    source_path: str,
    vocal_source_path: str = None,
    include_bpm: bool = True,
    include_gender: bool = False,
    need_bundle: bool = True,
):
    """Build the local part of Music_Description from Librosa measurements only.

    Song Analysis controls the BPM/music-feature work used for structure detection.
    Gender Vocal Determination independently controls the conservative male/female
    lead-vocal label.  The expensive music feature bundle is skipped when neither
    BPM nor structure analysis needs it.
    """
    try:
        bundle = None
        tags = []

        if include_bpm or need_bundle:
            waveform, sample_rate = _load_comfy_audio(source_path)
            mono_t = _analysis_signal(waveform)
            bundle = _advanced_feature_bundle(mono_t, sample_rate, "Advanced (Librosa)")

            if include_bpm:
                bpm = float(bundle.get("bpm", 0.0) or 0.0) if isinstance(bundle, dict) else 0.0
                if bpm > 0:
                    tags.append(f"{int(round(bpm))} BPM")

        if include_gender:
            tags.extend(_estimate_vocal_prompt(vocal_source_path))

        clean = []
        seen = set()
        for tag in tags:
            tag = re.sub(r"\s+", " ", str(tag or "")).strip().strip(",")
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                clean.append(tag)
        return ", ".join(clean), bundle
    except Exception as exc:
        _log(f"Librosa music analysis unavailable: {exc}")
        return "", None

def _merge_music_descriptions(*parts):
    """Merge comma-separated description parts while preserving order and removing duplicates."""
    clean = []
    seen = set()
    for part in parts:
        for item in str(part or "").split(","):
            item = re.sub(r"\s+", " ", item).strip().strip(",")
            key = item.casefold()
            if item and key not in seen:
                seen.add(key)
                clean.append(item)
    return ", ".join(clean[:20])

def _build_song_sections(transcript_segments, music_source_path=None, music_bundle=None):
    """Return (sections, detected). sections are (label, start_index, end_index)."""
    anchors = _find_chorus_intervals(transcript_segments)
    intervals = _expand_chorus_intervals(transcript_segments, anchors)
    if not intervals:
        return [], False

    if music_source_path and len(anchors) >= 2:
        intervals, used_music, music_info = _analyse_music_assisted_chorus(
            music_source_path, transcript_segments, anchors, intervals, precomputed_bundle=music_bundle
        )
        if used_music and music_info:
            _log(
                "Music-assisted structure: "
                f"{music_info['bpm']:.1f} BPM, {music_info['meter']}/4 grid, "
                f"{music_info['bars']}-bar repeated chorus phrase, "
                f"similarity {music_info['structural']:.2f}."
            )

    sections = []
    verse_number = 1
    cursor = 0
    n = len(transcript_segments)

    for chorus_start, chorus_end in intervals:
        chorus_start = max(cursor, chorus_start)
        chorus_end = min(n, max(chorus_start, chorus_end))

        if cursor < chorus_start:
            sections.append((f"Verse {verse_number}", cursor, chorus_start))
            verse_number += 1

        if chorus_start < chorus_end:
            sections.append(("Chorus", chorus_start, chorus_end))
        cursor = max(cursor, chorus_end)

    if cursor < n:
        sections.append((f"Verse {verse_number}", cursor, n))

    # Drop empty-text sections caused by non-speech Whisper artefacts.
    filtered = []
    for label, start, end in sections:
        has_text = any(str(transcript_segments[idx][2] or "").strip() for idx in range(start, min(end, n)))
        if has_text:
            filtered.append((label, start, end))
    return filtered, bool(filtered)


def _sections_to_plain_text(transcript_segments, sections):
    output = []
    for label, start, end in sections:
        lines = []
        for idx in range(start, min(end, len(transcript_segments))):
            text = str(transcript_segments[idx][2] or "").strip()
            if text:
                lines.append(text)
        if not lines:
            continue
        if output:
            output.append("")
        output.append(f"[{label}]")
        output.extend(lines)
    return "\n".join(output).strip()


def _section_starts(sections):
    return {start: label for label, start, _end in sections}


def _prepare_song_analysis_source(source_path: str):
    """
    Try to isolate vocals for song transcription or vocal-gender analysis. The stem is
    temporary and is never saved as a user output. Failure falls back to the original mix.
    """
    temp_base = folder_paths.get_temp_directory()
    os.makedirs(temp_base, exist_ok=True)
    temp_root = tempfile.mkdtemp(prefix="urn_audio_lyrics_song_", dir=temp_base)
    clean_stem = f"lyrics_{uuid.uuid4().hex[:12]}"
    try:
        paths, device = _export_mel_roformer_vocals(
            source_path=source_path,
            output_root=temp_root,
            clean_stem=clean_stem,
        )
        vocal_path = next((path for path in paths if path.lower().endswith("_vocals.flac")), None)
        if vocal_path and os.path.isfile(vocal_path) and os.path.getsize(vocal_path) > 0:
            _log(f"Vocal isolation: using temporary Mel-RoFormer vocal stem ({device}).")
            return vocal_path, temp_root
        raise RuntimeError("Mel-RoFormer did not return a usable vocal stem.")
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        _log(f"Vocal isolation unavailable; using original mix instead. {exc}")
        return source_path, None


class URNAudioLyrics(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNAudioLyrics",
            display_name="URN Audio Lyrics",
            category="URN Audio Tools",
            description=(
                "Simple audio-to-text transcription from a connected AUDIO input. "
                "When embedded lyrics are connected (normally from URN Load Audio), they are shown first for Use/Ignore confirmation before any external lyric lookup. Optional Artist/Title/Album metadata is preferred over filename parsing. "
                "Optional online lyric lookup then offers an alphabetical song dropdown, and if the automatic match is missing/rejected lets the user manually search by artist before Whisper is used as the final fallback. Stop Workflow remains available at each interactive stage. Accepted titles try LRCLIB first and Lyrics.ovh second. "
                "Song Analysis controls song-specific processing, BPM/music-feature analysis, temporary vocal isolation for Whisper fallback, and conservative Verse/Chorus detection. Gender Vocal Determination independently controls whether the node performs isolated-vocal pitch analysis for a conservative male/female lead-vocal label. "
                "Include timestamps outputs standard SRT text through Transcript_Out when Whisper is used, and also uses LRCLIB synced lyrics when available. Artist, Title and Album outputs expose the identity the user accepted, whether from embedded metadata or the online selection panel. Missing Album metadata is filled automatically from MusicBrainz without an extra user prompt. Music_Description uses MusicBrainz genres/tags when a song is confirmed, BPM when Song Analysis is enabled, and a conservative male/female lead-vocal label when Gender Vocal Determination is enabled and detectable. MusicBrainz tries the confirmed title first, then retries with common remaster/release suffixes removed if needed."
            ),
            is_output_node=True,
            inputs=[
                io.Audio.Input("audio_input"),
                io.Boolean.Input("song", default=True),
                io.Boolean.Input("include_timestamps", default=False),
                io.Boolean.Input("get_lyrics_ovh", default=True),
                io.String.Input(
                    "embedded_lyrics",
                    display_name="Embedded Lyrics",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional embedded lyrics, normally connected from URN Load Audio.",
                ),
                io.String.Input(
                    "artist_in",
                    display_name="Artist",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional artist metadata, normally connected from URN Load Audio.",
                ),
                io.String.Input(
                    "title_in",
                    display_name="Title",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional title metadata, normally connected from URN Load Audio.",
                ),
                io.String.Input(
                    "album_in",
                    display_name="Album",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional album metadata, normally connected from URN Load Audio. If blank, a confirmed Artist/Title is used for automatic MusicBrainz album lookup.",
                ),
                io.String.Input("source_filename_hint", default=""),
                # Append the new widget after every V9.88 widget so saved workflow
                # widget-value positions remain compatible. source_filename_hint is
                # hidden by the frontend but still serialized as a widget value.
                io.Boolean.Input("gender_vocal_determination", default=True),
            ],
            outputs=[
                io.String.Output(display_name="Transcript_Out"),
                io.Float.Output("Duration_Seconds"),
                io.String.Output(display_name="Music_Description"),
                io.Audio.Output("Audio_out"),
                io.String.Output("Artist"),
                io.String.Output("Title"),
                io.String.Output("Album"),
            ],
            hidden=[io.Hidden.unique_id],
        )

    # Do not custom-validate the connected AUDIO payload here. ComfyUI validates
    # links before resolving connected AUDIO values, so validate_inputs() would
    # incorrectly receive None even when a cable is connected. The schema keeps
    # audio_input required, and execute() performs the final runtime sanity check.

    @classmethod
    def fingerprint_inputs(cls, audio_input=None, song=True, include_timestamps=False, get_lyrics_ovh=True, gender_vocal_determination=True, embedded_lyrics="", artist_in="", title_in="", album_in="", source_filename_hint="", **kwargs):
        # Connected AUDIO may change even when the graph shape does not, and lookup mode is interactive.
        return float("nan")

    @classmethod
    def execute(cls, audio_input=None, song=True, include_timestamps=False, get_lyrics_ovh=True, gender_vocal_determination=True, embedded_lyrics="", artist_in="", title_in="", album_in="", source_filename_hint="") -> io.NodeOutput:
        node_id = str(cls.hidden.unique_id)
        _send_status(node_id, "Preparing audio...")
        if audio_input is None:
            _send_status(node_id, "Error: connect an AUDIO input")
            raise ValueError("URN Audio Lyrics requires a connected AUDIO input.")

        waveform, sample_rate = _audio_to_waveform(audio_input)
        total_duration = waveform.shape[-1] / float(sample_rate)

        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        filename = f"urn_audio_lyrics_{uuid.uuid4().hex[:12]}.flac"
        source_path = os.path.join(temp_dir, filename)
        _write_flac(source_path, waveform, sample_rate)
        source_label = "connected AUDIO"

        if total_duration <= 0:
            raise ValueError("The selected audio has no usable duration.")

        # Keep the internal input name ``song`` for workflow compatibility, but its
        # visible label is now Song Analysis.
        song_analysis = bool(song)
        gender_vocal_determination = bool(gender_vocal_determination)
        include_timestamps = bool(include_timestamps)
        get_lyrics_ovh = bool(get_lyrics_ovh)
        embedded_lyrics = str(embedded_lyrics or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        artist_in = str(artist_in or "").strip()
        title_in = str(title_in or "").strip()
        album_in = str(album_in or "").strip()

        # If the user confirms a song identity, keep the resulting
        # MusicBrainz + Librosa description even when Lyrics.ovh itself cannot
        # return lyric text and we later fall back to Whisper for Transcript_Out.
        confirmed_music_description = ""
        confirmed_music_bundle = None
        accepted_artist = ""
        accepted_title = ""
        accepted_album = ""

        def _metadata_album_for_identity(artist="", title=""):
            if not album_in:
                return ""
            # Use connected Album metadata unless the connected Artist/Title explicitly
            # contradict the identity the user accepted in the online selector.
            if artist_in and artist and _lookup_key(artist_in) != _lookup_key(artist):
                return ""
            if title_in and title and _lookup_key(title_in) != _lookup_key(title):
                return ""
            return album_in

        def _build_confirmed_music_description(artist="", title="", album_hint=""):
            nonlocal confirmed_music_description, confirmed_music_bundle, accepted_album

            accepted_album = str(album_hint or "").strip() or _metadata_album_for_identity(artist, title)
            if accepted_album:
                _log(f"Using connected Album metadata: {accepted_album}.")

            mb_description = ""
            recording_id = ""
            if artist and title:
                _send_status(node_id, "Getting MusicBrainz metadata...")
                mb_description, recording_id = _musicbrainz_description_and_recording(artist, title)
                if not accepted_album and recording_id:
                    accepted_album = _musicbrainz_album_for_recording(recording_id, artist)
                if not accepted_album:
                    _log("No confident Album metadata was available; Album output will be blank.")

            librosa_temp_root = None
            librosa_vocal_source = None
            try:
                if gender_vocal_determination:
                    _send_status(node_id, "Determining vocal gender...")
                    librosa_source, librosa_temp_root = _prepare_song_analysis_source(source_path)
                    if librosa_source != source_path:
                        librosa_vocal_source = librosa_source
                if song_analysis:
                    _send_status(node_id, "Analysing music...")
                librosa_description, confirmed_music_bundle = _music_prompt_analysis(
                    source_path,
                    vocal_source_path=librosa_vocal_source,
                    include_bpm=song_analysis,
                    include_gender=gender_vocal_determination,
                    need_bundle=song_analysis,
                )
            except Exception as exc:
                librosa_description = ""
                confirmed_music_bundle = None
                _log(f"Librosa description analysis failed: {exc}")
            finally:
                if librosa_temp_root:
                    shutil.rmtree(librosa_temp_root, ignore_errors=True)

            if mb_description and librosa_description:
                confirmed_music_description = _merge_music_descriptions(
                    mb_description, librosa_description
                )
                _description_console(
                    "MusicBrainz + Librosa", True, confirmed_music_description
                )
            elif mb_description:
                confirmed_music_description = mb_description
                _description_console("MusicBrainz", True, mb_description)
                _description_console(
                    "Librosa", False,
                    "local librosa audio analysis returned no description",
                )
            else:
                confirmed_music_description = librosa_description
                if artist and title:
                    _description_console(
                        "MusicBrainz", False,
                        "no usable genre/style tags; using Librosa only",
                    )
                if confirmed_music_description:
                    _description_console("Librosa", True, confirmed_music_description)
                else:
                    _description_console(
                        "Librosa", False,
                        "local librosa audio analysis returned no description",
                    )

        def _try_confirmed_online_choice(choice):
            nonlocal accepted_artist, accepted_title, accepted_album
            # A title has been confirmed by the user. Preserve that exact accepted
            # identity for the Artist/Title outputs even if both online lyric
            # providers later fail and Whisper becomes the transcript fallback.
            accepted_artist = str(choice.get("artist") or "").strip()
            accepted_title = str(choice.get("title") or "").strip()
            accepted_album = ""

            # MusicBrainz metadata is independent of which lyric provider ultimately succeeds.
            _build_confirmed_music_description(accepted_artist, accepted_title)

            # Confirmed artist/title lyric retrieval: LRCLIB first, then Lyrics.ovh.
            _send_status(node_id, "Getting lyrics from LRCLIB...")
            lrclib_result = _lrclib_fetch(
                accepted_artist,
                accepted_title,
                total_duration,
                choice.get("album") or "",
            )
            if lrclib_result:
                if include_timestamps and lrclib_result.get("synced"):
                    lrclib_text = _lrclib_synced_to_srt(lrclib_result.get("synced") or "")
                    if not lrclib_text:
                        lrclib_text = lrclib_result.get("plain") or ""
                else:
                    lrclib_text = lrclib_result.get("plain") or ""

                if lrclib_text:
                    timing_note = "synced timestamps" if include_timestamps and lrclib_result.get("synced") else "plain lyrics"
                    _log(
                        f"LRCLIB lyrics matched {lrclib_result.get('artist')} - {lrclib_result.get('title')} "
                        f"({timing_note})."
                    )
                    print(f"URN Lyrics: Lyrics LRCLIB - SUCCESS - {timing_note}")
                    _send_status(node_id, "Complete")
                    return io.NodeOutput(
                        lrclib_text, float(total_duration), confirmed_music_description, audio_input,
                        accepted_artist, accepted_title, accepted_album
                    )

            print("URN Lyrics: Lyrics LRCLIB - FAILED - trying Lyrics.ovh")
            _send_status(node_id, "Getting lyrics from Lyrics.ovh...")
            try:
                fetched_lyrics = _lyrics_ovh_fetch(choice["artist"], choice["title"])
            except Exception as exc:
                fetched_lyrics = ""
                _log(f"Lyrics.ovh lyric fetch failed: {exc}")

            if fetched_lyrics:
                _log(
                    f"Using Lyrics.ovh lyrics for {choice['artist']} - {choice['title']}."
                )
                print("URN Lyrics: Lyrics Lyrics.ovh - SUCCESS - plain lyrics")
                if include_timestamps:
                    _log(
                        "Include timestamps is enabled, but Lyrics.ovh does not provide "
                        "timestamps; returning the fetched lyrics as plain text."
                    )
                _send_status(node_id, "Complete")
                return io.NodeOutput(
                    fetched_lyrics, float(total_duration), confirmed_music_description, audio_input,
                    accepted_artist, accepted_title, accepted_album
                )

            print("URN Lyrics: Lyrics Lyrics.ovh - FAILED")
            return None

        # Embedded lyrics supplied by URN Load Audio get first refusal before any
        # online lyric provider. Rejecting them continues into the existing online
        # sequence (or directly to Whisper when Get Online Lyrics is disabled).
        if embedded_lyrics:
            _send_status(node_id, "Embedded lyrics found — waiting for choice...")
            embedded_action = _wait_for_embedded_lyrics_choice(
                node_id, embedded_lyrics, artist_in, title_in
            )
            if embedded_action == "use":
                accepted_artist = artist_in
                accepted_title = title_in
                _log("Using embedded lyrics from the connected audio metadata.")
                print("URN Lyrics: Lyrics Embedded Metadata - SUCCESS - plain lyrics")
                if include_timestamps:
                    _log(
                        "Include timestamps is enabled, but embedded USLT/LYRICS metadata "
                        "does not provide synced timestamps; returning plain lyrics."
                    )
                _build_confirmed_music_description(artist_in, title_in, album_in)
                _send_status(node_id, "Complete")
                return io.NodeOutput(
                    embedded_lyrics, float(total_duration), confirmed_music_description, audio_input,
                    accepted_artist, accepted_title, accepted_album
                )
            _log("Embedded lyrics were ignored; continuing to the normal lyric lookup sequence.")

        # Online lyrics mode is intentionally interactive. The filename is parsed as
        # "Artist - Title" first. If automatic title discovery fails, or the user
        # rejects it, the SAME panel switches to a manual artist search before Whisper.
        if get_lyrics_ovh:
            lookup_name = str(source_filename_hint or "").strip()
            parsed = _filename_artist_title(lookup_name)
            file_artist, file_title = parsed if parsed else ("", "")
            parsed_artist = artist_in or file_artist
            guessed_title = title_in or file_title
            if parsed_artist:
                if artist_in or title_in:
                    _log(
                        f"Using connected metadata for online identity: artist={parsed_artist!r}, "
                        f"title={guessed_title!r}."
                    )
                _send_status(node_id, "Searching online song titles...")
                options = _lyrics_ovh_suggestions(parsed_artist, guessed_title)
                lookup_mode = "automatic" if options else "manual"
                if options:
                    _send_status(node_id, "Waiting for song selection...")
                else:
                    _send_status(node_id, "Automatic lookup failed — enter artist manually...")
                    _log(
                        f"Lyrics.ovh returned no usable titles for {parsed_artist!r}; "
                        "offering manual artist search before Whisper."
                    )
            else:
                parsed_artist, guessed_title = "", ""
                options = []
                lookup_mode = "manual"
                _send_status(node_id, "Enter artist manually...")
                _log(
                    "Could not parse an Artist - Title filename; offering manual artist search "
                    "before Whisper."
                )

            choice = _wait_for_lyrics_ovh_choice(
                node_id, parsed_artist, guessed_title, options, mode=lookup_mode
            )

            while choice is not None:
                online_result = _try_confirmed_online_choice(choice)
                if online_result is not None:
                    return online_result

                # If an automatically selected title did not actually yield lyrics,
                # give the user one manual artist-search stage before Whisper. A choice
                # already made from the manual stage falls through directly to Whisper.
                if choice.get("_lookup_mode") != "automatic":
                    _log(
                        "Manual artist/title selection returned no usable online lyrics; "
                        "falling back to Whisper."
                    )
                    break

                _send_status(node_id, "No lyrics found — search artist manually...")
                _log(
                    "Automatic title was accepted but LRCLIB/Lyrics.ovh returned no lyrics; "
                    "offering manual artist search before Whisper."
                )
                choice = _wait_for_lyrics_ovh_choice(
                    node_id, choice.get("artist") or parsed_artist, "", [], mode="manual"
                )

            _send_status(node_id, "Transcribing with Whisper...")
            if choice is None:
                _log("Manual online lyric search rejected; falling back to Whisper.")

        if not get_lyrics_ovh:
            _send_status(node_id, "Transcribing with Whisper...")

        analysis_source = source_path
        song_temp_root = None
        if song_analysis:
            analysis_source, song_temp_root = _prepare_song_analysis_source(source_path)

        _send_status(node_id, "Transcribing lyrics...")
        _log(
            f"Transcribing {source_label} ({total_duration:.2f}s) | "
            f"Song Analysis={'True' if song_analysis else 'False'} | "
            f"Gender Vocal Determination={'True' if gender_vocal_determination else 'False'} | "
            f"Include timestamps={'True' if include_timestamps else 'False'}."
        )
        pbar = ProgressBar(100) if ProgressBar is not None else None
        if pbar is not None:
            try:
                pbar.update_absolute(3, 100)
            except Exception:
                pass

        music_description = confirmed_music_description
        music_bundle = confirmed_music_bundle
        try:
            _word_regions, _transcript_words, transcript_segments, _speech_segments, analysis_used, analysis_note = _analyse_words(
                source_path=analysis_source,
                model_name="medium",
                requested_device="Auto",
                total_duration=total_duration,
                beam_size=18,
                patience=3.0,
                pbar=pbar,
            )
            if song_analysis or gender_vocal_determination:
                # Reuse the Whisper vocal stem when Song Analysis already created one.
                # When only Gender Vocal Determination is enabled, create a temporary
                # vocal stem solely for the pitch classification.
                vocal_prompt_source = (
                    analysis_source
                    if gender_vocal_determination and analysis_source != source_path
                    else None
                )
                gender_temp_root = None
                if gender_vocal_determination and vocal_prompt_source is None:
                    _send_status(node_id, "Determining vocal gender...")
                    gender_source, gender_temp_root = _prepare_song_analysis_source(source_path)
                    if gender_source != source_path:
                        vocal_prompt_source = gender_source
                try:
                    # If an online identity was already confirmed, MusicBrainz/Librosa
                    # may already have produced the description/bundle. Reuse it.
                    if music_bundle is None or (gender_vocal_determination and not music_description):
                        if song_analysis:
                            _send_status(node_id, "Analysing music...")
                        local_description, local_bundle = _music_prompt_analysis(
                            source_path,
                            vocal_source_path=vocal_prompt_source,
                            include_bpm=song_analysis,
                            include_gender=gender_vocal_determination,
                            need_bundle=song_analysis,
                        )
                        if music_bundle is None:
                            music_bundle = local_bundle
                        if local_description:
                            music_description = _merge_music_descriptions(
                                music_description, local_description
                            )
                finally:
                    if gender_temp_root:
                        shutil.rmtree(gender_temp_root, ignore_errors=True)
        finally:
            if song_temp_root:
                shutil.rmtree(song_temp_root, ignore_errors=True)

        transcript_segments.sort(key=lambda item: item[0])
        if analysis_used == "energy fallback":
            reason = analysis_note or "Faster-Whisper did not return a transcription."
            raise RuntimeError(f"URN Audio Lyrics could not transcribe the audio. {reason}")

        sections = []
        structure_detected = False
        if song_analysis:
            _send_status(node_id, "Detecting song structure...")
            sections, structure_detected = _build_song_sections(
                transcript_segments, music_source_path=source_path, music_bundle=music_bundle
            )

        if include_timestamps:
            if song_analysis and structure_detected:
                transcript_out = _segments_to_srt(transcript_segments, _section_starts(sections))
            else:
                transcript_out = _segments_to_srt(transcript_segments)
        else:
            if song_analysis and structure_detected:
                transcript_out = _sections_to_plain_text(transcript_segments, sections)
            else:
                transcript_out = _segments_to_plain_text(transcript_segments)

        if pbar is not None:
            try:
                pbar.update_absolute(100, 100)
            except Exception:
                pass

        if song_analysis:
            if structure_detected:
                _log("Song structure detected; Transcript_Out includes automatic Verse/Chorus labels.")
            else:
                _log("No confident repeated chorus detected; returning transcription without structure labels.")
        else:
            _log("Song Analysis is off; Verse/Chorus detection skipped.")
        if song_analysis or gender_vocal_determination:
            if music_description:
                _log(f"Music_Description: {music_description}")
                _description_console("Librosa", True, music_description)
            else:
                _log("Music_Description was not available for this audio; returning an empty string.")
                _description_console("Librosa", False, "local music analysis returned no description")
        else:
            _description_console(
                "Librosa", False,
                "Song Analysis and Gender Vocal Determination are both off; local description analysis skipped",
            )
        _log(f"Finished transcription: {len(transcript_segments)} subtitle segments.")
        print("URN Lyrics: Lyrics Whisper - SUCCESS - transcription fallback used")
        _send_status(node_id, "Complete")
        return io.NodeOutput(
            transcript_out, float(total_duration), music_description, audio_input,
            accepted_artist, accepted_title, accepted_album
        )
