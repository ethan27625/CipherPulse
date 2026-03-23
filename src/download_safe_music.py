"""
download_safe_music.py — Download royalty-free music and register each track
in music_licenses.json so the pipeline can verify provenance before use.

Supported sources
-----------------
soundhelix (default, no API key needed)
  16 CC0 public-domain electronic tracks from SoundHelix.com.
  Generated music — zero copyright, zero Content-ID risk.
  Perfect for getting the pipeline running immediately.

jamendo (upgrade — better quality dark ambient)
  Creative Commons music via the Jamendo API.
  Requires a free client_id from https://devportal.jamendo.com/
  Set JAMENDO_CLIENT_ID in your .env file.

Usage:
  python3 -m src.download_safe_music                       # soundhelix, all 16 tracks
  python3 -m src.download_safe_music --source jamendo       # jamendo dark ambient
  python3 -m src.download_safe_music --source jamendo --query "electronic" --count 20
  python3 -m src.download_safe_music --list                 # show registered tracks
  python3 -m src.download_safe_music --purge-unregistered   # remove untracked files

Environment variables:
  JAMENDO_CLIENT_ID  — required only for --source jamendo
                       get free at https://devportal.jamendo.com/
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
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
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
REQUEST_TIMEOUT   = 30    # seconds per HTTP request
DELAY_BETWEEN_DL  = 0.5   # courtesy delay between downloads

# ── SoundHelix CC0 catalogue ──────────────────────────────────────────────────
# 16 programmatically-generated electronic tracks.
# Author: Thomas Schürger (https://www.soundhelix.com)
# License: CC0 1.0 Universal (Public Domain Dedication)
# No attribution required. Zero Content-ID risk.
SOUNDHELIX_BASE    = "https://www.soundhelix.com/examples/mp3"
SOUNDHELIX_TRACKS  = [
    {"id": i, "title": f"SoundHelix Song {i}", "filename": f"soundhelix-song-{i:02d}.mp3",
     "url": f"{SOUNDHELIX_BASE}/SoundHelix-Song-{i}.mp3"}
    for i in range(1, 17)
]
SOUNDHELIX_LICENSE = "CC0 1.0 Universal (Public Domain)"
SOUNDHELIX_URL     = "https://creativecommons.org/publicdomain/zero/1.0/"

# ── Jamendo API ───────────────────────────────────────────────────────────────
JAMENDO_API_BASE   = "https://api.jamendo.com/v3.0"
JAMENDO_LICENSE    = "Creative Commons (Jamendo)"
JAMENDO_LICENSE_URL = "https://creativecommons.org/licenses/"


# ── License registry helpers ──────────────────────────────────────────────────

def _load_registry() -> dict:
    """Load music_licenses.json, creating a fresh one if absent or corrupt."""
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
    """Write the registry to disk with today's date."""
    registry["last_updated"] = date.today().isoformat()
    LICENSES_PATH.write_text(json.dumps(registry, indent=2))


def _is_registered(filename: str, registry: dict) -> bool:
    """Return True if filename appears in the registry."""
    return any(t["filename"] == filename for t in registry["tracks"])


def _register_track(
    filename: str,
    source_url: str,
    license_type: str,
    license_url: str,
    artist: str,
    title: str,
    duration_s: int,
    track_id: str,
    registry: dict,
) -> None:
    """Append a new track entry to the in-memory registry dict."""
    registry["tracks"].append({
        "filename":      filename,
        "title":         title,
        "artist":        artist,
        "duration_s":    duration_s,
        "track_id":      track_id,
        "source_url":    source_url,
        "license_type":  license_type,
        "license_url":   license_url,
        "download_date": date.today().isoformat(),
    })


# ── HTTP helpers ──────────────────────────────────────────────────────────────

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


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _api_get(url: str, params: dict) -> dict:
    """GET a JSON API endpoint and return the parsed response."""
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── SoundHelix source ─────────────────────────────────────────────────────────

def _download_soundhelix(count: int) -> list[str]:
    """
    Download up to count SoundHelix CC0 tracks.

    All 16 tracks are pre-defined with known URLs — no API call needed.
    Already-registered tracks are skipped so re-running is safe.
    """
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    registry = _load_registry()
    registered = {t["filename"] for t in registry["tracks"]}

    downloaded: list[str] = []
    for track in SOUNDHELIX_TRACKS:
        if len(downloaded) >= count:
            break
        if track["filename"] in registered:
            log.debug(f"Skip {track['filename']} — already registered")
            continue

        dest = MUSIC_DIR / track["filename"]
        log.info(f"Downloading {track['filename']} …")
        try:
            _download_file(track["url"], dest)
        except Exception as exc:
            log.error(f"Download failed: {exc}")
            if dest.exists():
                dest.unlink()
            continue

        _register_track(
            filename=track["filename"],
            source_url=track["url"],
            license_type=SOUNDHELIX_LICENSE,
            license_url=SOUNDHELIX_URL,
            artist="Thomas Schürger / SoundHelix",
            title=track["title"],
            duration_s=0,       # SoundHelix doesn't expose duration; 0 = unknown
            track_id=f"soundhelix-{track['id']}",
            registry=registry,
        )
        _save_registry(registry)
        downloaded.append(track["filename"])
        log.info(f"  ✓ {track['filename']} registered")
        time.sleep(DELAY_BETWEEN_DL)

    return downloaded


# ── Jamendo source ─────────────────────────────────────────────────────────────

def _safe_filename_jamendo(artist: str, title: str, track_id: str) -> str:
    """Build a safe filename from Jamendo metadata."""
    import re

    def slug(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")[:36]

    return f"jamendo-{slug(artist)}-{slug(title)}-{track_id}.mp3"


def _download_jamendo(
    query: str = "dark ambient",
    count: int = 10,
    client_id: Optional[str] = None,
) -> list[str]:
    """
    Search Jamendo for CC-licensed tracks matching query and download up to count.

    Requires JAMENDO_CLIENT_ID environment variable (free at devportal.jamendo.com).
    Only downloads tracks where audiodownload_allowed is True.
    """
    if client_id is None:
        client_id = os.environ.get("JAMENDO_CLIENT_ID", "")
    if not client_id:
        raise EnvironmentError(
            "JAMENDO_CLIENT_ID is not set. "
            "Get a free client ID at https://devportal.jamendo.com/"
        )

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    registry = _load_registry()
    registered_ids = {t["track_id"] for t in registry["tracks"]}

    downloaded: list[str] = []
    offset = 0
    per_page = 50

    log.info(f"Searching Jamendo: query={query!r}, want={count} tracks")

    while len(downloaded) < count:
        try:
            data = _api_get(
                f"{JAMENDO_API_BASE}/tracks/",
                {
                    "client_id":       client_id,
                    "format":          "json",
                    "limit":           per_page,
                    "offset":          offset,
                    "namesearch":      query,
                    "audioformat":     "mp31",
                    "audiodlformat":   "mp32",
                    "include":         "musicinfo",
                },
            )
        except Exception as exc:
            log.error(f"Jamendo API error: {exc}")
            break

        results = data.get("results", [])
        if not results:
            log.info("No more Jamendo results.")
            break

        for hit in results:
            if len(downloaded) >= count:
                break

            track_id   = str(hit.get("id", ""))
            title      = hit.get("name", "unknown")
            artist     = hit.get("artist_name", "unknown")
            duration   = int(hit.get("duration", 0))
            audio_url  = hit.get("audiodownload", "") or hit.get("audio", "")
            dl_allowed = hit.get("audiodownload_allowed", True)

            jamendo_id = f"jamendo-{track_id}"
            if jamendo_id in registered_ids:
                log.debug(f"Skip Jamendo #{track_id} {title!r} — already registered")
                continue

            if not dl_allowed or not audio_url:
                log.debug(f"Skip Jamendo #{track_id} {title!r} — download not allowed")
                continue

            filename = _safe_filename_jamendo(artist, title, track_id)
            dest     = MUSIC_DIR / filename

            log.info(f"Downloading: {filename} ({duration}s)")
            try:
                _download_file(audio_url, dest)
            except Exception as exc:
                log.error(f"Download failed for #{track_id}: {exc}")
                if dest.exists():
                    dest.unlink()
                continue

            _register_track(
                filename=filename,
                source_url=audio_url,
                license_type=JAMENDO_LICENSE,
                license_url=JAMENDO_LICENSE_URL,
                artist=artist,
                title=title,
                duration_s=duration,
                track_id=jamendo_id,
                registry=registry,
            )
            registered_ids.add(jamendo_id)
            _save_registry(registry)
            downloaded.append(filename)
            log.info(f"  ✓ {filename} registered")
            time.sleep(DELAY_BETWEEN_DL)

        offset += per_page
        if offset > 500:
            break

    return downloaded


# ── Public API ────────────────────────────────────────────────────────────────

def download_tracks(
    source: str = "soundhelix",
    query: str = "dark ambient",
    count: int = 16,
    api_key: Optional[str] = None,
) -> list[str]:
    """
    Download music tracks from the specified source and register them.

    Args:
        source:  "soundhelix" (default, no key needed) or "jamendo"
        query:   Search query — only used for Jamendo
        count:   Maximum tracks to download
        api_key: Jamendo client_id (falls back to JAMENDO_CLIENT_ID env var)

    Returns:
        List of downloaded filenames.
    """
    if source == "soundhelix":
        return _download_soundhelix(count)
    elif source == "jamendo":
        return _download_jamendo(query=query, count=count, client_id=api_key)
    else:
        raise ValueError(f"Unknown source {source!r}. Use 'soundhelix' or 'jamendo'.")


def verify_track(filename: str) -> bool:
    """
    Check that filename is registered in music_licenses.json.

    Called by video_assembler and text_card_assembler before using any track.
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
    Delete music files from assets/music/ that are not in music_licenses.json.
    Returns list of deleted filenames.
    """
    registry = _load_registry()
    registered = {t["filename"] for t in registry["tracks"]}
    deleted: list[str] = []
    for p in MUSIC_DIR.iterdir():
        if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}:
            if p.name not in registered:
                log.warning(f"Removing unregistered: {p.name}")
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

    parser = argparse.ArgumentParser(
        description="Download royalty-free music and register licenses"
    )
    parser.add_argument(
        "--source", default="soundhelix", choices=["soundhelix", "jamendo"],
        help=(
            "Music source. 'soundhelix' = 16 CC0 tracks, no API key needed (default). "
            "'jamendo' = CC-licensed dark ambient, requires JAMENDO_CLIENT_ID in .env."
        ),
    )
    parser.add_argument(
        "--query", default="dark ambient",
        help='Search query for Jamendo (default: "dark ambient")',
    )
    parser.add_argument(
        "--count", type=int, default=16,
        help="Number of tracks to download (default: 16)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List registered tracks and exit",
    )
    parser.add_argument(
        "--purge-unregistered", action="store_true", dest="purge",
        help="Delete music files not in music_licenses.json",
    )
    args = parser.parse_args()

    if args.list:
        reg = _load_registry()
        tracks = reg.get("tracks", [])
        print(f"\nRegistered tracks ({len(tracks)} total):")
        print("─" * 60)
        for t in tracks:
            dur = f"{t['duration_s']}s" if t.get("duration_s") else "unknown duration"
            print(f"  {t['filename']}")
            print(f"    Artist:  {t['artist']}")
            print(f"    License: {t['license_type']}")
            print(f"    Added:   {t['download_date']}")
            print()
    elif args.purge:
        deleted = purge_unregistered()
        print(f"Deleted {len(deleted)} unregistered file(s).")
    else:
        files = download_tracks(source=args.source, query=args.query, count=args.count)
        print(f"\nDownloaded and registered {len(files)} track(s):")
        for f in files:
            print(f"  {f}")
        if not files:
            if args.source == "jamendo":
                print("  Check JAMENDO_CLIENT_ID in .env (devportal.jamendo.com for free key)")
            else:
                print("  Check your internet connection and try again")
