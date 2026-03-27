"""
footage_downloader.py — Stock video clip fetcher via Pexels API.

Takes visual tag keyword phrases from script_writer.py and returns local paths
to downloaded MP4 clips, using a persistent category-based cache to avoid
re-downloading clips across multiple video generations.

3-phase search strategy to collect TARGET_CLIPS_PER_VIDEO unique clips:
  Phase 1 — Primary:  one clip per [VISUAL] tag from the script
  Phase 2 — Synonyms: additional searches using related terms for each tag's category
  Phase 3 — Fallback: generic dark/tech terms to fill any remaining slots

Within one video, NO clip is reused — each phase excludes Pexels IDs already
selected, ensuring every segment of the finished video shows different footage.

Each downloaded clip gets a .meta.json sidecar recording dimensions, orientation,
pexels ID, and search term — used by video_assembler to decide crop strategy.

Pexels free tier limits:
  - 200 requests/hour, 20,000 requests/month
  - We use ~15-20 requests per video (phases 1-3), so ~800 videos/month before limits
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "footage_downloader.log"),
    ],
)
log = logging.getLogger("footage_downloader")

# ── Constants ─────────────────────────────────────────────────────────────────
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
CACHE_DIR = Path(__file__).parent.parent / "assets" / "footage_cache"
RESULTS_PER_SEARCH = 10       # Fetch this many results, then pick the best
REQUEST_DELAY_SECONDS = 0.4   # Politeness delay between API calls
DOWNLOAD_CHUNK_SIZE = 1024 * 512  # 512 KB streaming chunks

# Minimum clip duration — clips shorter than this are skipped
MIN_CLIP_DURATION_SECONDS = 4
# Maximum clip duration to download — very long clips waste storage
MAX_CLIP_DURATION_SECONDS = 30

# Preferred video file quality labels from Pexels (in preference order)
QUALITY_PREFERENCE = ["hd", "sd", "uhd"]

# ── Category mapping ──────────────────────────────────────────────────────────
# Maps visual tag keywords → cache subdirectory category.
# The assembler organizes footage by category, not by exact search term,
# enabling reuse: "hacker typing" and "hacker at computer" both map to "hacking".
#
# Matching is keyword-based: first category whose keywords appear in the tag wins.
# Tags with no keyword match go to "dark-tech" (generic cyberpunk visuals).

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "hacking": [
        "hack", "hacker", "cyber attack", "malware", "ransomware", "phishing",
        "exploit", "breach", "intrusion", "dark room", "typing code",
    ],
    "servers": [
        "server", "data center", "rack", "network", "infrastructure",
        "cloud", "hardware", "mainframe",
    ],
    "code": [
        "code", "programming", "terminal", "command line", "software",
        "binary", "matrix", "developer", "screen code",
    ],
    "AI": [
        "ai", "artificial intelligence", "neural", "robot", "machine learning",
        "deepfake", "algorithm", "chatbot", "automation",
    ],
    "city": [
        "city", "cityscape", "urban", "night", "skyline", "street",
        "building", "downtown", "metropolis",
    ],
    "surveillance": [
        "surveillance", "camera", "cctv", "spy", "tracking", "monitoring",
        "face recognition", "facial", "watch",
    ],
    "data": [
        "data", "database", "analytics", "chart", "graph", "information",
        "privacy", "leak", "breach notification",
    ],
    "dark-tech": [
        # Generic fallback — matches everything else
        "lock", "shield", "security", "password", "encryption", "vpn",
        "firewall", "phone", "mobile", "device",
    ],
}

FALLBACK_CATEGORY = "dark-tech"

# ── Target clip count ─────────────────────────────────────────────────────────
# How many unique clips to collect per video. With ~54s of audio and 10 clips,
# each clip fills ~5-6s. Increase for faster cuts, but each extra clip costs
# one Pexels API request.
TARGET_CLIPS_PER_VIDEO = 10

# ── Synonym expansion ─────────────────────────────────────────────────────────
# When Phase 1 (primary tags) doesn't yield TARGET_CLIPS_PER_VIDEO clips,
# Phase 2 searches these related terms per category to find more unique footage.
# Ordered from most-relevant to least — stops as soon as target is reached.
CATEGORY_SYNONYMS: dict[str, list[str]] = {
    "hacking": [
        "cybersecurity hacker dark",
        "computer security breach",
        "cyber attack network",
        "malware virus screen",
        "dark web anonymous",
    ],
    "servers": [
        "data center server room",
        "server rack hardware",
        "network infrastructure cables",
        "cloud computing center",
        "mainframe computer room",
    ],
    "code": [
        "programmer coding dark screen",
        "terminal command line screen",
        "matrix binary code rain",
        "software developer night",
        "computer screen green code",
    ],
    "AI": [
        "artificial intelligence technology",
        "machine learning neural network",
        "robot futuristic",
        "deep learning algorithm",
        "ai technology abstract",
    ],
    "city": [
        "city lights night timelapse",
        "urban skyline aerial night",
        "downtown cityscape dark",
        "night city street traffic",
        "metropolitan city rain",
    ],
    "surveillance": [
        "security camera cctv footage",
        "facial recognition scan",
        "surveillance monitoring room",
        "security guard monitoring",
        "cctv dark corridor",
    ],
    "data": [
        "data analytics visualization",
        "digital data stream network",
        "database server abstract",
        "big data network connections",
        "data encryption security",
    ],
    "dark-tech": [
        "hacker typing terminal dark",
        "cybersecurity dark abstract",
        "server room dark blue",
        "code screen dark terminal",
        "network security dark",
    ],
}

# ── Master dark/cyber search pool ──────────────────────────────────────────────
# ALL Pexels searches (both news and edu modes) draw exclusively from this list.
# Every term is proven to return dark, cybersecurity-aesthetic clips on Pexels.
# NEVER use script visual_tags or edu curriculum search_terms for Pexels —
# they produce irrelevant bright footage (robots, offices, kids, headsets).
#
# Phase 0 picks 1 opener; Phase 1 picks PHASE1_POOL_COUNT more (all random).
# Phases 2-3 fall back to DARK_CYBER_POOL synonyms/generics if still short.
DARK_CYBER_SEARCH_POOL: list[str] = [
    "hacker dark room computer",
    "code on screen dark room",
    "server room dark lights",
    "typing keyboard dark",
    "cybersecurity dark screen",
    "programming code dark monitor",
    "data center dark server",
    "matrix code green screen dark",
    "hooded person computer dark",
    "dark room multiple monitors",
    "cyber attack visualization dark",
    "encrypted data screen dark",
]
PHASE1_POOL_COUNT = 3   # How many random pool terms to use in Phase 1

# ── Dark/cyber footage pool (Phase 2-3 synonyms & fallbacks) ───────────────────
DARK_CYBER_POOL: list[str] = [
    "hacker dark room",
    "cybersecurity dark",
    "code on computer screen dark",
    "server room dark",
    "typing keyboard dark room",
    "computer hacking dark",
    "digital data dark",
    "network security dark",
    "programming dark screen",
    "hooded hacker computer",
]

# ── Generic fallback search terms (Phase 3) ────────────────────────────────────
# When Phases 1+2 still haven't reached TARGET_CLIPS_PER_VIDEO, these dark/cyber
# terms are searched in order until the target is met.
# All terms guaranteed to return dark-aesthetic cybersecurity footage.
GENERIC_CLIP_TERMS: list[str] = DARK_CYBER_POOL + [
    "data center server room dark",
    "cyber attack visualization dark",
    "dark web screen glow",
    "binary code dark screen",
    "cybersecurity abstract dark blue",
    "malware code dark terminal",
    "encrypted data network dark",
    "hacker typing terminal dark",
]

# Search queries to use when seeding the fallback category cache
FALLBACK_SEARCH_TERMS = [
    "cybersecurity abstract dark",
    "hacker dark room typing",
    "server room dark blue",
    "code terminal dark screen",
    "network security dark abstract",
]


# ── Category resolution ────────────────────────────────────────────────────────

def resolve_category(tag: str) -> str:
    """
    Map a visual tag string to its footage cache category.

    Iterates through CATEGORY_KEYWORDS in definition order, returning the first
    category whose keyword list has any match in the tag (case-insensitive).
    Falls back to FALLBACK_CATEGORY if nothing matches.

    Args:
        tag: Raw visual tag string, e.g. "hacker typing dark room"

    Returns:
        Category directory name, e.g. "hacking"

    Example:
        resolve_category("server room blue light")  → "servers"
        resolve_category("deepfake voice clone")    → "AI"
        resolve_category("random unmatched thing")  → "dark-tech"
    """
    tag_lower = tag.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in tag_lower for kw in keywords):
            return category
    return FALLBACK_CATEGORY


# ── API interaction ────────────────────────────────────────────────────────────

def _get_pexels_headers() -> dict[str, str]:
    """Build Pexels API request headers with the Authorization key."""
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        raise ValueError("PEXELS_API_KEY not set in environment or .env file")
    return {"Authorization": api_key}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _search_pexels_videos(
    query: str,
    orientation: str = "portrait",
    per_page: int = RESULTS_PER_SEARCH,
) -> list[dict]:
    """
    Search the Pexels Video API and return a list of raw video result dicts.

    Args:
        query:       Search term, e.g. "hacker typing dark room"
        orientation: "portrait" (9:16), "landscape" (16:9), or "square"
        per_page:    Number of results to request (max 80)

    Returns:
        List of Pexels video objects from the API response.
        Each object has: id, duration, width, height, video_files, user, etc.
        Returns empty list if no results or on API errors.

    The Pexels API is RESTful — one GET request, JSON response.
    Authorization is via a header, not a query param (unlike many APIs).
    Rate limit headers (X-Ratelimit-Remaining) are available but we don't
    parse them — our conservative per-call delays keep us well within limits.
    """
    params = {
        "query": query,
        "orientation": orientation,
        "size": "medium",   # 'medium' = HD (720p-1080p), not 4K — faster download
        "per_page": per_page,
    }
    resp = requests.get(
        PEXELS_VIDEO_SEARCH_URL,
        headers=_get_pexels_headers(),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    videos = data.get("videos", [])
    log.debug(f"Pexels search '{query}' [{orientation}]: {len(videos)} results")
    return videos


def _get_clip_id(clip_path: Path) -> Optional[int]:
    """
    Extract the Pexels video ID from a cached clip filename.

    Clip filenames follow the pattern 'pexels-{id}.mp4'.
    Returns None if the filename doesn't match this pattern (e.g. manually added clips).
    """
    try:
        return int(clip_path.stem.split("-")[1])
    except (IndexError, ValueError):
        return None


def _pick_best_video(
    videos: list[dict],
    exclude_ids: Optional[set[int]] = None,
) -> Optional[dict]:
    """
    Select the best video from Pexels search results.

    Selection criteria (in order):
    1. Duration within [MIN_CLIP_DURATION_SECONDS, MAX_CLIP_DURATION_SECONDS]
    2. Not already in exclude_ids (clips already chosen for this video)
    3. Prefer clips with width close to 1080 (full HD but not massive)
    4. Among valid clips, prefer shorter duration (less storage, easier to trim)

    Args:
        videos:      Raw Pexels API result list
        exclude_ids: Set of Pexels IDs already selected for this video.
                     These are skipped so each video segment shows unique footage.

    Returns None if no valid, non-excluded clip found.
    """
    if exclude_ids is None:
        exclude_ids = set()

    valid = [
        v for v in videos
        if (MIN_CLIP_DURATION_SECONDS <= v.get("duration", 0) <= MAX_CLIP_DURATION_SECONDS
            and v.get("id") not in exclude_ids)
    ]
    if not valid:
        return None
    # Sort by how close width is to 1080, then by duration ascending
    valid.sort(key=lambda v: (abs(v.get("width", 0) - 1080), v.get("duration", 99)))
    return valid[0]


def _pick_video_file(video: dict) -> Optional[dict]:
    """
    Pick the best-quality MP4 file from a Pexels video's video_files list.

    Pexels provides multiple renditions per video (hd, sd, uhd).
    We prefer hd → sd → uhd (uhd last — file size is huge for marginal benefit).
    Within a quality tier, prefer the file with height closest to 1080.

    Returns a single video_file dict with keys: id, quality, file_type, link, width, height.
    """
    files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
    if not files:
        return None

    for quality in QUALITY_PREFERENCE:
        tier = [f for f in files if f.get("quality") == quality]
        if tier:
            # Within quality tier, pick closest to 1080px tall
            tier.sort(key=lambda f: abs(f.get("height", 0) - 1080))
            return tier[0]

    # Fallback: any mp4
    return files[0]


# ── Download ───────────────────────────────────────────────────────────────────

def _download_clip(url: str, dest_path: Path) -> None:
    """
    Stream-download a video file to dest_path.

    Uses streaming=True so we don't load the entire file into memory before
    writing — important for HD video files that can be 50-200MB each.
    We write in 512KB chunks, which balances memory efficiency with
    the overhead of many small write() syscalls.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Downloading: {url}")
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total_bytes = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                f.write(chunk)
                total_bytes += len(chunk)
    size_mb = total_bytes / 1024 / 1024
    log.info(f"Downloaded: {dest_path.name} ({size_mb:.1f} MB)")


def _save_meta(clip_path: Path, video: dict, video_file: dict, search_tag: str) -> None:
    """
    Write a .meta.json sidecar file alongside the downloaded clip.

    video_assembler.py reads this to decide whether to center-crop (landscape)
    or use as-is (portrait). Storing metadata here avoids re-querying the API.

    Sidecar path: footage_cache/hacking/pexels-12345.mp4
    Meta path:    footage_cache/hacking/pexels-12345.meta.json
    """
    width = video_file.get("width", 0)
    height = video_file.get("height", 0)
    orientation = "portrait" if height > width else "landscape"

    meta = {
        "pexels_id": video.get("id"),
        "search_tag": search_tag,
        "duration_seconds": video.get("duration"),
        "width": width,
        "height": height,
        "orientation": orientation,
        "quality": video_file.get("quality"),
        "photographer": video.get("user", {}).get("name", ""),
        "pexels_url": video.get("url", ""),
    }
    meta_path = clip_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))


# ── Audio strip ────────────────────────────────────────────────────────────────

def _strip_audio(clip_path: Path) -> None:
    """
    Remove the audio track from a Pexels clip in-place.

    Pexels clips sometimes contain copyrighted background music.  We only want
    our own audio (TTS voice + ambient drone), so we strip the clip's original
    audio at download time using FFmpeg's -an flag and -c:v copy (no re-encode).

    Writes to a temp file then atomically renames over the original so a failed
    strip never corrupts the cached clip.
    """
    tmp = clip_path.with_suffix(".noaudio.mp4")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(clip_path),
             "-c:v", "copy", "-an", str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(clip_path)
            log.debug(f"Audio stripped: {clip_path.name}")
        else:
            log.warning(f"Audio strip failed for {clip_path.name}: {r.stderr[-200:]}")
            tmp.unlink(missing_ok=True)
    except Exception as exc:
        log.warning(f"Audio strip error for {clip_path.name}: {exc}")
        tmp.unlink(missing_ok=True)


# ── Cache management ───────────────────────────────────────────────────────────

def get_cached_clips(category: str, limit: int = 20) -> list[Path]:
    """
    Return all cached MP4 files for a given category directory.

    Args:
        category: Category name matching a CACHE_DIR subdirectory
        limit:    Maximum number of paths to return

    Returns:
        List of Path objects for existing .mp4 files, newest-first.
    """
    cat_dir = CACHE_DIR / category
    if not cat_dir.exists():
        return []
    clips = sorted(cat_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return clips[:limit]


def is_already_cached(pexels_id: int, category: str) -> Optional[Path]:
    """
    Check if a specific Pexels video ID is already downloaded in cache.

    Returns the Path to the MP4 if found, None otherwise.
    This prevents re-downloading the same clip if the same visual tag
    appears in multiple videos generated in the same session.
    """
    cat_dir = CACHE_DIR / category
    expected_path = cat_dir / f"pexels-{pexels_id}.mp4"
    return expected_path if expected_path.exists() else None


# ── Main download logic ────────────────────────────────────────────────────────

def fetch_clip_for_tag(
    tag: str,
    exclude_ids: Optional[set[int]] = None,
) -> Optional[Path]:
    """
    Fetch a single stock video clip for one visual tag keyword phrase.

    Full search strategy:
      1. Resolve tag → category (e.g. "hacker typing" → "hacking")
      2. Search Pexels portrait orientation, skipping IDs in exclude_ids
      3. If no valid portrait results → search landscape
      4. If Pexels has a valid result → check cache, download if needed
      5. If Pexels has nothing → pick from category cache (excluding seen IDs)
      6. Last resort → FALLBACK_CATEGORY cache (excluding seen IDs)

    Args:
        tag:         Visual tag string from script, e.g. "server room blue light"
        exclude_ids: Set of Pexels IDs already selected for this video.
                     Clips with these IDs are skipped even if Pexels returns them,
                     ensuring no footage appears twice in the same video.

    Returns:
        Path to a local MP4 file, or None if completely unable to find footage.
    """
    if exclude_ids is None:
        exclude_ids = set()

    category = resolve_category(tag)
    cat_dir = CACHE_DIR / category
    cat_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Fetching clip for tag: '{tag}' → category: '{category}'")

    # ── Try portrait orientation first ─────────────────────────────────────────
    videos = []
    try:
        videos = _search_pexels_videos(tag, orientation="portrait")
        time.sleep(REQUEST_DELAY_SECONDS)
    except Exception as e:
        log.warning(f"Portrait search failed for '{tag}': {e}")

    # ── Fall back to landscape if portrait returned nothing valid ──────────────
    if not _pick_best_video(videos, exclude_ids):
        try:
            log.info(f"No valid portrait clips for '{tag}' — trying landscape")
            landscape = _search_pexels_videos(tag, orientation="landscape")
            time.sleep(REQUEST_DELAY_SECONDS)
            videos = videos + landscape  # combine — portrait preferred via sort
        except Exception as e:
            log.warning(f"Landscape search also failed for '{tag}': {e}")

    # ── Pick best video (not in exclude_ids) and download if not cached ────────
    video = _pick_best_video(videos, exclude_ids)
    if video:
        pexels_id = video.get("id")
        cached = is_already_cached(pexels_id, category)
        if cached:
            log.info(f"Cache hit: {cached.name}")
            return cached

        video_file = _pick_video_file(video)
        if video_file:
            dest_path = cat_dir / f"pexels-{pexels_id}.mp4"
            try:
                _download_clip(video_file["link"], dest_path)
                _strip_audio(dest_path)
                _save_meta(dest_path, video, video_file, tag)
                return dest_path
            except Exception as e:
                log.error(f"Download failed for pexels-{pexels_id}: {e}")
                if dest_path.exists():
                    dest_path.unlink()  # Remove partial download

    # ── Pexels returned nothing valid — try category cache ─────────────────────
    log.warning(f"No Pexels results for '{tag}' — checking category cache")
    cached_clips = [c for c in get_cached_clips(category) if _get_clip_id(c) not in exclude_ids]
    if cached_clips:
        chosen = cached_clips[0]
        log.info(f"Using cached fallback: {chosen.name}")
        return chosen

    # ── Last resort: FALLBACK_CATEGORY cache (excluding seen IDs) ─────────────
    if category != FALLBACK_CATEGORY:
        log.warning(f"Category '{category}' cache empty — using '{FALLBACK_CATEGORY}' fallback")
        fallback_clips = [
            c for c in get_cached_clips(FALLBACK_CATEGORY)
            if _get_clip_id(c) not in exclude_ids
        ]
        if fallback_clips:
            return fallback_clips[0]

    log.error(f"No footage found for tag '{tag}'. Skipping.")
    return None


def fetch_clips_for_script(
    visual_tags: list[str],
    target_clips: int = TARGET_CLIPS_PER_VIDEO,
    guaranteed_dark_first: bool = True,
) -> list[Path]:
    """
    Fetch enough unique video clips to fill an entire video with no repetition.

    Uses a 3-phase strategy to reach target_clips unique clips.
    ALL searches draw exclusively from DARK_CYBER_SEARCH_POOL — script visual_tags
    and edu curriculum search_terms are intentionally ignored because topic-specific
    Pexels searches return irrelevant bright footage (robots, offices, kids, etc.).

    Phase 1 — Pool sample:
        Randomly samples PHASE1_POOL_COUNT+1 terms from DARK_CYBER_SEARCH_POOL.
        Guarantees every clip comes from the curated dark/cyber aesthetic list.

    Phase 2 — DARK_CYBER_POOL synonyms:
        Iterates DARK_CYBER_POOL to fill remaining slots with more dark clips.

    Phase 3 — Generic fallbacks:
        Searches GENERIC_CLIP_TERMS to fill any remaining slots.

    Args:
        visual_tags:           Accepted for backward compatibility — IGNORED.
                               Searches now always use DARK_CYBER_SEARCH_POOL.
        target_clips:          How many unique clips to collect (default TARGET_CLIPS_PER_VIDEO)
        guaranteed_dark_first: Accepted for backward compatibility — IGNORED.
                               Pool-only mode already guarantees dark clips.

    Returns:
        List of unique local MP4 paths. Length is min(target_clips, available_clips).
        Every path in the list is a DIFFERENT Pexels clip — safe to display
        sequentially with no visual repetition within the video.
    """
    import random as _rnd
    seen_ids: set[int] = set()
    clip_paths: list[Path] = []

    def _try_fetch(tag: str) -> bool:
        """Fetch one unique clip. Returns True if a new clip was added."""
        if len(clip_paths) >= target_clips:
            return False
        path = fetch_clip_for_tag(tag, exclude_ids=seen_ids)
        if path is None:
            return False
        pid = _get_clip_id(path)
        if pid is not None and pid in seen_ids:
            # Shouldn't happen (fetch_clip_for_tag respects exclude_ids) but guard
            log.warning(f"Duplicate clip detected for '{tag}' — skipping")
            return False
        if pid is not None:
            seen_ids.add(pid)
        clip_paths.append(path)
        return True

    # ── Phase 1: Random sample from DARK_CYBER_SEARCH_POOL ────────────────────
    # Both news and edu modes ONLY use these curated dark/cyber terms.
    # Script visual_tags and edu curriculum search_terms are ignored for Pexels
    # because they produce irrelevant bright footage (robots, offices, etc.).
    pool_sample = _rnd.sample(
        DARK_CYBER_SEARCH_POOL,
        min(PHASE1_POOL_COUNT + 1, len(DARK_CYBER_SEARCH_POOL)),  # +1 for variety
    )
    log.info(
        f"Phase 1 — dark/cyber pool: {len(pool_sample)} term(s) "
        f"(ignoring script tags — pool-only mode)"
    )
    for i, tag in enumerate(pool_sample):
        log.info(f"  [{i + 1}/{len(pool_sample)}] '{tag}'")
        _try_fetch(tag)
        if len(clip_paths) >= target_clips:
            break

    # ── Phase 2: Synonym expansion from DARK_CYBER_POOL ───────────────────────
    if len(clip_paths) < target_clips:
        log.info(
            f"Phase 2 — synonyms: have {len(clip_paths)}/{target_clips} clips, "
            f"searching related terms"
        )
        for synonym in DARK_CYBER_POOL:
            if len(clip_paths) >= target_clips:
                break
            log.info(f"  pool synonym: '{synonym}'")
            _try_fetch(synonym)

    # ── Phase 3: Generic dark/tech fallbacks ──────────────────────────────────
    if len(clip_paths) < target_clips:
        log.info(
            f"Phase 3 — generic fallbacks: have {len(clip_paths)}/{target_clips} clips"
        )
        for fallback in GENERIC_CLIP_TERMS:
            if len(clip_paths) >= target_clips:
                break
            log.info(f"  fallback: '{fallback}'")
            _try_fetch(fallback)

    log.info(
        f"Footage ready: {len(clip_paths)} unique clips "
        f"(target was {target_clips}) — no clip appears twice"
    )
    return clip_paths


def seed_fallback_cache() -> None:
    """
    Pre-populate the fallback category cache with generic cyberpunk visuals.

    Call this once during project setup so the fallback is never empty.
    Downloads up to 3 clips per fallback search term.
    """
    log.info("Seeding fallback cache with generic dark-tech clips…")
    fallback_dir = CACHE_DIR / FALLBACK_CATEGORY
    fallback_dir.mkdir(parents=True, exist_ok=True)

    for term in FALLBACK_SEARCH_TERMS:
        try:
            videos = _search_pexels_videos(term, orientation="landscape", per_page=5)
            time.sleep(REQUEST_DELAY_SECONDS)
            for video in videos[:2]:
                pexels_id = video.get("id")
                if is_already_cached(pexels_id, FALLBACK_CATEGORY):
                    continue
                video_file = _pick_video_file(video)
                if video_file:
                    dest = fallback_dir / f"pexels-{pexels_id}.mp4"
                    try:
                        _download_clip(video_file["link"], dest)
                        _save_meta(dest, video, video_file, term)
                        time.sleep(REQUEST_DELAY_SECONDS)
                    except Exception as e:
                        log.warning(f"Seed download failed: {e}")
        except Exception as e:
            log.warning(f"Seed search failed for '{term}': {e}")

    total = len(list(fallback_dir.glob("*.mp4")))
    log.info(f"Fallback cache seeded: {total} clips in '{FALLBACK_CATEGORY}/'")


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Download Pexels stock footage for CipherPulse visual tags"
    )
    parser.add_argument(
        "--tags", nargs="+", metavar="TAG",
        help='Visual tag phrases to fetch, e.g. --tags "hacker typing" "server room"',
    )
    parser.add_argument(
        "--seed-fallback", action="store_true", dest="seed_fallback",
        help="Pre-populate the fallback cache with generic cyberpunk clips",
    )
    parser.add_argument(
        "--list-cache", action="store_true", dest="list_cache",
        help="Print a summary of all cached clips by category",
    )
    args = parser.parse_args()

    if args.list_cache:
        print("\n── Footage Cache Summary ───────────────────────────────────")
        total = 0
        for cat in sorted(CATEGORY_KEYWORDS.keys()):
            clips = get_cached_clips(cat)
            total += len(clips)
            if clips:
                size_mb = sum(p.stat().st_size for p in clips) / 1024 / 1024
                print(f"  {cat:<15} {len(clips):>3} clips  ({size_mb:.0f} MB)")
            else:
                print(f"  {cat:<15}   0 clips")
        print(f"  {'TOTAL':<15} {total:>3} clips")

    elif args.seed_fallback:
        seed_fallback_cache()

    elif args.tags:
        print(f"\nFetching {len(args.tags)} clip(s)…\n")
        paths = fetch_clips_for_script(args.tags)

        print(f"\n{'═' * 55}")
        print(f"RESULT: {len(paths)}/{len(args.tags)} clips ready")
        print("═" * 55)
        for i, path in enumerate(paths):
            meta_path = path.with_suffix(".meta.json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                orient = meta.get("orientation", "?")
                w, h = meta.get("width", "?"), meta.get("height", "?")
                dur = meta.get("duration_seconds", "?")
                print(f"  [{i+1}] {path.name}")
                print(f"       {orient} {w}×{h}  {dur}s  [{meta.get('quality','')}]")
                print(f"       tag: '{meta.get('search_tag','')}'")
            else:
                print(f"  [{i+1}] {path}")

    else:
        parser.print_help()
