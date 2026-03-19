"""
file_hoster.py — Upload local MP4 to a public URL for Instagram Graph API.

Primary:  litterbox.catbox.moe — free, no-account, 72h TTL, 1 GB limit, Let's Encrypt TLS.
Fallback: file.io             — free, no-account, 14-day TTL, 2 GB limit.

litterbox API: POST https://litterbox.catbox.moe/resources/internals/api.php
  reqtype=fileupload, time=72h, fileToUpload=<binary>
  Returns plain-text URL: https://litter.catbox.moe/<hash>.<ext>

file.io API: POST https://file.io/
  file=<binary>
  Returns JSON: {"success": true, "link": "https://file.io/<key>"}
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

LITTERBOX_UPLOAD = "https://litterbox.catbox.moe/resources/internals/api.php"
FILEIO_UPLOAD    = "https://file.io/"
TIMEOUT_SECONDS  = 120          # large files can take a while on slow VM
MAX_FILE_MB      = 500


# ── Internal helpers ───────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    stop=stop_after_attempt(3),
)
def _upload_litterbox(file_path: Path) -> str:
    """Upload to litterbox.catbox.moe (72h TTL), return direct URL."""
    with file_path.open("rb") as fh:
        resp = requests.post(
            LITTERBOX_UPLOAD,
            data={"reqtype": "fileupload", "time": "72h"},
            files={"fileToUpload": (file_path.name, fh, "video/mp4")},
            timeout=TIMEOUT_SECONDS,
        )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("https://"):
        raise RuntimeError(f"Unexpected litterbox response: {url!r}")
    log.info(f"litterbox upload OK: {url}")
    return url


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    stop=stop_after_attempt(3),
)
def _upload_fileio(file_path: Path) -> str:
    """Fallback: upload to file.io (14d TTL, single-download), return URL."""
    with file_path.open("rb") as fh:
        resp = requests.post(
            FILEIO_UPLOAD,
            files={"file": (file_path.name, fh, "video/mp4")},
            data={"expires": "14d", "maxDownloads": "10"},
            timeout=TIMEOUT_SECONDS,
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"file.io upload failed: {data}")
    url = data["link"]
    log.info(f"file.io upload OK: {url}")
    return url


# ── Public API ─────────────────────────────────────────────────────────────────

def upload_for_instagram(video_path: Path | str) -> str:
    """
    Upload *video_path* to a public file host and return the HTTPS URL.

    Tries litterbox.catbox.moe first (72h TTL); falls back to file.io.

    Args:
        video_path: Path to the local MP4 file.

    Returns:
        HTTPS URL that Instagram's servers can fetch directly.

    Raises:
        RuntimeError: If all hosts fail.
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file exceeds MAX_FILE_MB.
    """
    video_path = Path(video_path)

    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    size_mb = video_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise ValueError(
            f"File too large for hosting: {size_mb:.1f} MB > {MAX_FILE_MB} MB"
        )

    log.info(f"Uploading {video_path.name} ({size_mb:.1f} MB) to public host…")

    # ── Try litterbox (primary) ────────────────────────────────────────────────
    try:
        return _upload_litterbox(video_path)
    except Exception as exc:
        log.warning(f"litterbox failed ({exc}); trying fallback file.io…")

    # ── Try file.io (fallback) ─────────────────────────────────────────────────
    try:
        return _upload_fileio(video_path)
    except Exception as exc:
        raise RuntimeError(
            f"All file hosts failed. Last error: {exc}"
        ) from exc


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Upload a file to a public temporary host.")
    parser.add_argument("file", help="Path to the MP4 to upload")
    args = parser.parse_args()

    try:
        url = upload_for_instagram(Path(args.file))
        print(f"\nPublic URL: {url}\n")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
