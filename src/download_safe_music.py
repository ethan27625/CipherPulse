"""
download_safe_music.py — Download royalty-free music from Pixabay API and
register each track in music_licenses.json.

Why Pixabay API (not manual downloads)?
  Tracks downloaded via the Pixabay API carry a machine-readable license
  confirming the Pixabay Content License at the moment of download.  We record
  the API response URL and license_type so every file in assets/music/ has an
  auditable provenance chain.

  Note: Pixabay's Content License allows commercial use and does NOT require
  attribution, but individual artists CAN register tracks with YouTube
  Content-ID independently.  This registry exists so we can trace any claim
  back to its source URL and swap out the track.

Usage:
  python3 -m src.download_safe_music              # download 10 dark-ambient tracks
  python3 -m src.download_safe_music --count 20   # download 20 tracks
  python3 -m src.download_safe_music --query "electronic dark"
  python3 -m src.download_safe_music --list       # show currently registered tracks
  python3 -m src.download_safe_music --purge-unregistered  # remove files not in registry

Environment variable required:
  PIXABAY_API_KEY — get a free key at https://pixabay.com/api/docs/
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "download_safe_music.log"),
    ],
)
log = logging.getLogger("download_safe_music")

# ── Paths ─────────────────────────────────────────────────────────────────────
ASSETS_DIR    = Path(__file__).parent.parent / "assets"
MUSIC_DIR     = ASSETS_DIR / "music"
LICENSES_PATH = Path(__file__).parent.parent / "music_licenses.json"

# ── Constants ─────────────────────────────────────────────────────────────────
PIXABAY_MUSIC_API  = "https://pixabay.com/api/music/"
DEFAULT_QUERY      = "dark ambient"
DEFAULT_COUNT      = 10
MIN_DURATION_S     = 60    # reject tracks shorter than 60 seconds
MAX_DURATION_S     = 600   # reject tracks longer than 10 minutes
LICENSE_TYPE       = "Pixabay Content License"
LICENSE_URL        = "https://pixabay.com/service/license-summary/"
REQUEST_TIMEOUT    = 30    # seconds
DELAY_BETWEEN_DL   = 1.0   # seconds between downloads (rate-limit courtesy)


# ── License registry helpers ──────────────────────────────────────────────────

def _load_registry() -> dict:
    """Load music_licenses.json, creating it if absent."""
    if LICENSES_PATH.exists():
        try:
            return json.loads(LICENSES_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("music_licenses.json is corrupt — starting fresh registry")
    return {
        "version": "1.0",
        "description": (
            "Registry of all music tracks used by CipherPulse. "
            "Every file in assets/music/ MUST have an entry here before it "
            "can be used. Tracks without a valid entry are rejected at runtime."
        ),
        "last_updated": None,
        "tracks": [],
    }


def _save_registry(registry: dict) -> None:
    """Persist the registry to disk."""
    registry["last_updated"] = date.today().isoformat()
    LICENSES_PATH.write_text(json.dumps(registry, indent=2))
    log.debug(f"Registry saved: {len(registry['tracks'])} tracks")


def _is_registered(filename: str, registry: dict) -> bool:
    """Return True if filename is present in the registry."""
    return any(t["filename"] == filename for t in registry["tracks"])


def _register_track(
    filename: str,
    source_url: str,
    license_type: str,
    license_url: str,
    artist: str,
    title: str,
    duration_s: int,
    pixabay_id: int,
    registry: dict,
) -> None:
    """Add a new track entry to the in-memory registry dict."""
    registry["tracks"].append({
        "filename":     filename,
        "title":        title,
        "artist":       artist,
        "duration_s":   duration_s,
        "pixabay_id":   pixabay_id,
        "source_url":   source_url,
        "license_type": license_type,
        "license_url":  license_url,
        "download_date": date.today().isoformat(),
    })


# ── Pixabay API ───────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _api_search(query: str, page: int, api_key: str) -> dict:
    """
    Call the Pixabay Music API and return the parsed JSON response.

    Pixabay Music API parameters:
      key        — your API key
      q          — search query (URL-encoded by requests)
      category   — we use 'music' (vs 'sound_effects')
      per_page   — max 200 results per page
      page       — pagination (1-indexed)

    Returns the full JSON dict with 'hits' list and 'totalHits' count.
    """
    params = {
        "key":      api_key,
        "q":        query,
        "per_page": 50,
        "page":     page,
    }
    resp = requests.get(PIXABAY_MUSIC_API, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _download_file(url: str, dest: Path) -> None:
    """Stream-download a file from url to dest path."""
    with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)


# ── Filename builder ──────────────────────────────────────────────────────────

def _safe_filename(artist: str, title: str, pixabay_id: int) -> str:
    """
    Build a filesystem-safe filename that encodes provenance.

    Format: {artist_slug}-{title_slug}-{id}.mp3
    Example: evgeny_bardyuzha-password-infinity-123276.mp3
    """
    def slug(s: str) -> str:
        import re
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")[:40]

    return f"{slug(artist)}-{slug(title)}-{pixabay_id}.mp3"


# ── Main download logic ───────────────────────────────────────────────────────

def download_tracks(
    query: str = DEFAULT_QUERY,
    count: int = DEFAULT_COUNT,
    api_key: Optional[str] = None,
) -> list[str]:
    """
    Search Pixabay for music matching query and download up to count new tracks.

    Only downloads tracks that are:
      - Not already present in music_licenses.json (de-duplicated by Pixabay ID)
      - Between MIN_DURATION_S and MAX_DURATION_S long
      - Have a valid audio_url in the API response

    Each downloaded track is immediately registered in music_licenses.json.

    Args:
        query:   Search terms (default: "dark ambient")
        count:   Maximum number of new tracks to download
        api_key: Pixabay API key (default: read from PIXABAY_API_KEY env var)

    Returns:
        List of downloaded filenames (may be shorter than count if not enough results)

    Raises:
        EnvironmentError: If no API key is available
        RuntimeError: If the API returns an error
    """
    if api_key is None:
        api_key = os.environ.get("PIXABAY_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "PIXABAY_API_KEY environment variable is not set. "
            "Get a free key at https://pixabay.com/api/docs/"
        )

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    registry = _load_registry()

    # Build a set of already-registered Pixabay IDs for fast lookup
    registered_ids: set[int] = {t["pixabay_id"] for t in registry["tracks"]}

    downloaded: list[str] = []
    page = 1

    log.info(f"Searching Pixabay Music: query={query!r}, want={count} new tracks")

    while len(downloaded) < count:
        log.info(f"  Fetching page {page}…")
        try:
            data = _api_search(query, page, api_key)
        except Exception as exc:
            log.error(f"Pixabay API error: {exc}")
            break

        hits = data.get("hits", [])
        if not hits:
            log.info("  No more results from Pixabay.")
            break

        for hit in hits:
            if len(downloaded) >= count:
                break

            pid      = hit.get("id", 0)
            title    = hit.get("title", "unknown")
            artist   = hit.get("user", "unknown")
            duration = hit.get("duration", 0)  # seconds
            audio_url = hit.get("audio", "") or hit.get("previewURL", "")

            # Skip already-registered tracks
            if pid in registered_ids:
                log.debug(f"  Skip #{pid} {title!r} — already registered")
                continue

            # Duration filter
            if not (MIN_DURATION_S <= duration <= MAX_DURATION_S):
                log.debug(
                    f"  Skip #{pid} {title!r} — duration {duration}s "
                    f"outside [{MIN_DURATION_S}, {MAX_DURATION_S}]"
                )
                continue

            # Audio URL check
            if not audio_url:
                log.debug(f"  Skip #{pid} {title!r} — no audio URL in API response")
                continue

            filename = _safe_filename(artist, title, pid)
            dest     = MUSIC_DIR / filename

            # Download
            try:
                log.info(f"  Downloading: {filename} ({duration}s) from {audio_url[:60]}…")
                _download_file(audio_url, dest)
            except Exception as exc:
                log.error(f"  Download failed for #{pid}: {exc}")
                if dest.exists():
                    dest.unlink()
                continue

            # Register
            _register_track(
                filename=filename,
                source_url=audio_url,
                license_type=LICENSE_TYPE,
                license_url=LICENSE_URL,
                artist=artist,
                title=title,
                duration_s=duration,
                pixabay_id=pid,
                registry=registry,
            )
            registered_ids.add(pid)
            _save_registry(registry)
            downloaded.append(filename)

            log.info(f"  ✓ {filename} registered in music_licenses.json")
            time.sleep(DELAY_BETWEEN_DL)

        page += 1
        # Safety: Pixabay API caps at 500 results (10 pages × 50)
        if page > 10:
            break

    log.info(f"Download complete: {len(downloaded)} new tracks added")
    return downloaded


def verify_track(filename: str) -> bool:
    """
    Check that filename is registered in music_licenses.json.

    Called by video_assembler and text_card_assembler before using a track.
    Returns True if the track is registered, False otherwise.
    """
    if not LICENSES_PATH.exists():
        log.warning("music_licenses.json missing — treating all tracks as unregistered")
        return False
    try:
        registry = json.loads(LICENSES_PATH.read_text())
    except json.JSONDecodeError:
        log.error("music_licenses.json is corrupt")
        return False
    return _is_registered(filename, registry)


def purge_unregistered() -> list[str]:
    """
    Remove any .mp3/.wav/.m4a/.flac files from assets/music/ that are not
    in music_licenses.json.  Returns list of deleted filenames.
    """
    registry = _load_registry()
    registered = {t["filename"] for t in registry["tracks"]}

    deleted: list[str] = []
    for p in MUSIC_DIR.iterdir():
        if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}:
            if p.name not in registered:
                log.warning(f"Removing unregistered track: {p.name}")
                p.unlink()
                deleted.append(p.name)

    if deleted:
        log.info(f"Purged {len(deleted)} unregistered track(s)")
    else:
        log.info("No unregistered tracks found")
    return deleted


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Download royalty-free music from Pixabay and register licenses"
    )
    parser.add_argument(
        "--query", default=DEFAULT_QUERY,
        help=f'Search query (default: "{DEFAULT_QUERY}")',
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help=f"Number of tracks to download (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all registered tracks and exit",
    )
    parser.add_argument(
        "--purge-unregistered", action="store_true", dest="purge",
        help="Delete any music files not in music_licenses.json",
    )
    args = parser.parse_args()

    if args.list:
        reg = _load_registry()
        tracks = reg.get("tracks", [])
        if not tracks:
            print("No registered tracks.")
        else:
            print(f"\n{'Registered tracks in music_licenses.json':}")
            print(f"{'─' * 60}")
            for t in tracks:
                print(
                    f"  {t['filename']}\n"
                    f"    Artist: {t['artist']}\n"
                    f"    Duration: {t['duration_s']}s\n"
                    f"    License: {t['license_type']}\n"
                    f"    Downloaded: {t['download_date']}\n"
                )
        print(f"Total: {len(tracks)} tracks")
    elif args.purge:
        deleted = purge_unregistered()
        print(f"Deleted {len(deleted)} unregistered file(s).")
    else:
        files = download_tracks(query=args.query, count=args.count)
        print(f"\nDownloaded and registered {len(files)} track(s):")
        for f in files:
            print(f"  {f}")
        if not files:
            print("  (none — check PIXABAY_API_KEY and try again)")
