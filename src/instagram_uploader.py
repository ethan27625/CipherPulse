"""
instagram_uploader.py — Publish MP4 to Instagram Reels via the Graph API.

Flow:
  1. Upload video to a public URL (file_hoster.py)
  2. POST /{user_id}/media  → create Reels container (returns container_id)
  3. Poll GET /{container_id}?fields=status_code until FINISHED
  4. POST /{user_id}/media_publish  → publish the container

Auth:
  Instagram Graph API uses a long-lived User Access Token (60-day TTL) that
  you exchange via Facebook Developer Console — no OAuth redirect flow needed
  for a personal project.  The token is stored in config/instagram_token.json
  and refreshed automatically (Graph API issues a new 60-day token each time
  you hit the /refresh_access_token endpoint).

Requirements:
  - Facebook App with instagram_basic + instagram_content_publish permissions
  - Instagram Professional (Business or Creator) account linked to a FB Page
  - Enabled in config/platforms.json  (instagram.enabled == true)

All behaviour is gated behind is_enabled() — if False the module is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.file_hoster import upload_for_instagram

load_dotenv()
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

GRAPH_API_BASE     = "https://graph.instagram.com/v21.0"
TOKEN_PATH         = Path("config/instagram_token.json")
PLATFORMS_PATH     = Path("config/platforms.json")

# Container status polling
POLL_INTERVAL_S    = 15          # seconds between status checks
POLL_TIMEOUT_S     = 600         # 10 minutes max (Instagram can be slow)

# Token refresh: Graph API 60-day long-lived tokens auto-renew after 10 days
TOKEN_REFRESH_DAYS = 50          # refresh if < 10 days remain → renew at day 50


# ── Platform gate ──────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Return True if Instagram is enabled in config/platforms.json."""
    try:
        data = json.loads(PLATFORMS_PATH.read_text())
        return bool(data.get("instagram", {}).get("enabled", False))
    except Exception:
        return False


# ── Token management ───────────────────────────────────────────────────────────

def _load_token() -> Optional[dict]:
    if TOKEN_PATH.exists():
        try:
            return json.loads(TOKEN_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("instagram_token.json is corrupt — re-authenticating")
    return None


def _save_token(token_data: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    log.info(f"Instagram token saved to {TOKEN_PATH}")


def _is_token_valid(token_data: dict) -> bool:
    expires_at = token_data.get("expires_at", 0)
    return time.time() < (expires_at - 86_400)   # 1-day safety margin


def _refresh_token(token_data: dict) -> dict:
    """
    Exchange current long-lived token for a fresh 60-day token.
    Graph endpoint: GET /refresh_access_token
    """
    log.info("Refreshing Instagram long-lived token…")
    resp = requests.get(
        f"{GRAPH_API_BASE}/refresh_access_token",
        params={
            "grant_type":   "ig_refresh_token",
            "access_token": token_data["access_token"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # Response: {"access_token": "...", "token_type": "bearer", "expires_in": 5183944}
    new_token = {
        "access_token": data["access_token"],
        "token_type":   data.get("token_type", "bearer"),
        "expires_in":   data["expires_in"],
        "expires_at":   int(time.time()) + data["expires_in"],
    }
    _save_token(new_token)
    log.info("Token refreshed — new expiry in ~60 days")
    return new_token


def get_valid_token() -> str:
    """
    Load the stored access token, refresh if near expiry, return the token string.

    If no token is stored, print instructions for obtaining one manually.
    """
    token_data = _load_token()

    if token_data is None:
        _print_auth_instructions()
        raise RuntimeError(
            "No Instagram token found. Follow the instructions above to obtain one."
        )

    if not _is_token_valid(token_data):
        token_data = _refresh_token(token_data)

    return token_data["access_token"]


def save_initial_token(access_token: str, expires_in: int = 5_183_944) -> None:
    """
    Persist an initial long-lived token obtained via Facebook Graph API Explorer
    or the exchange endpoint.  Call this once during setup.

    Args:
        access_token: The long-lived User Access Token.
        expires_in:   Lifetime in seconds (default ≈ 60 days).
    """
    token_data = {
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   expires_in,
        "expires_at":   int(time.time()) + expires_in,
    }
    _save_token(token_data)
    print(f"Token saved to {TOKEN_PATH}. Expires in ~{expires_in // 86400} days.")


def _print_auth_instructions() -> None:
    print("""
══════════════════════════════════════════════════════════════
  Instagram Reels Setup — One-time token acquisition
══════════════════════════════════════════════════════════════

Prerequisites:
  1. Facebook App at developers.facebook.com → Products → Instagram
  2. Instagram Professional account linked to a Facebook Page
  3. App has permissions: instagram_basic, instagram_content_publish,
     pages_read_engagement

Steps:
  a) Open Graph API Explorer: developers.facebook.com/tools/explorer
  b) Select your app → Set permissions → Generate User Token
  c) Exchange for a long-lived token:
       GET https://graph.facebook.com/v21.0/oauth/access_token
           ?grant_type=fb_exchange_token
           &client_id=YOUR_APP_ID
           &client_secret=YOUR_APP_SECRET
           &fb_exchange_token=SHORT_LIVED_TOKEN
  d) Save it to CipherPulse:
       python3 -m src.instagram_uploader --save-token YOUR_LONG_LIVED_TOKEN

  The token lasts 60 days; CipherPulse refreshes it automatically.
══════════════════════════════════════════════════════════════
""")


# ── Graph API helpers ──────────────────────────────────────────────────────────

def _get_user_id(access_token: str) -> str:
    """Return the Instagram Business Account user ID for the token."""
    resp = requests.get(
        f"{GRAPH_API_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    uid = data["id"]
    log.info(f"Instagram user: @{data.get('username', '?')} (id={uid})")
    return uid


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(4),
)
def _create_container(
    user_id: str,
    video_url: str,
    caption: str,
    access_token: str,
    share_to_feed: bool = True,
) -> str:
    """
    Step 1: Create a Reels media container.
    Returns container_id.
    """
    log.info("Creating Instagram Reels container…")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media",
        data={
            "media_type":    "REELS",
            "video_url":     video_url,
            "caption":       caption,
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token":  access_token,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        log.error(f"Container creation failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    container_id = resp.json()["id"]
    log.info(f"Container created: {container_id}")
    return container_id


def _poll_container_status(container_id: str, access_token: str) -> str:
    """
    Poll until status_code == FINISHED (or ERROR).
    Returns final status_code string.

    Possible values: EXPIRED, ERROR, FINISHED, IN_PROGRESS, PUBLISHED
    """
    log.info(f"Polling container {container_id} for FINISHED status…")
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{container_id}",
            params={
                "fields":       "status_code,status",
                "access_token": access_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status_code = data.get("status_code", "UNKNOWN")
        log.debug(f"  Container status: {status_code}")

        if status_code == "FINISHED":
            log.info("Container is FINISHED — ready to publish.")
            return status_code
        if status_code in ("ERROR", "EXPIRED"):
            detail = data.get("status", "no detail")
            raise RuntimeError(
                f"Instagram container failed with status {status_code}: {detail}"
            )

        time.sleep(POLL_INTERVAL_S)

    raise TimeoutError(
        f"Container {container_id} did not reach FINISHED within "
        f"{POLL_TIMEOUT_S // 60} minutes."
    )


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(4),
)
def _publish_container(user_id: str, container_id: str, access_token: str) -> str:
    """
    Step 2: Publish the FINISHED container.
    Returns the published media_id.
    """
    log.info(f"Publishing container {container_id}…")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media_publish",
        data={
            "creation_id":  container_id,
            "access_token": access_token,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        log.error(f"Publish failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    media_id = resp.json()["id"]
    log.info(f"Reel published! media_id={media_id}")
    return media_id


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class InstagramResult:
    status:       str            # "published" | "skipped" | "dry_run"
    media_id:     Optional[str]  # Graph API media_id
    container_id: Optional[str]  # container_id (useful for debugging)
    public_url:   Optional[str]  # tmpfiles.org URL used as video source
    reel_url:     Optional[str]  # instagram.com permalink (best effort)

    def to_dict(self) -> dict:
        return {
            "status":       self.status,
            "media_id":     self.media_id,
            "container_id": self.container_id,
            "public_url":   self.public_url,
            "reel_url":     self.reel_url,
        }


# ── Main entry point ───────────────────────────────────────────────────────────

def upload_short(
    output_dir: Path | str,
    dry_run: bool = False,
) -> InstagramResult:
    """
    Upload the video in *output_dir* to Instagram Reels.

    Expected files inside output_dir:
      - video.mp4       (the assembled Short)
      - metadata.json   (from seo_generator; uses instagram.caption / instagram.hashtags)

    Args:
        output_dir: Directory produced by the pipeline (e.g. output/20240315_143022/).
        dry_run:    If True, show what would be uploaded but skip real API calls.

    Returns:
        InstagramResult with status and media identifiers.
    """
    output_dir = Path(output_dir)
    video_path = output_dir / "video.mp4"
    meta_path  = output_dir / "metadata.json"

    # ── Guard: platform enabled? ───────────────────────────────────────────────
    if not dry_run and not is_enabled():
        log.info("Instagram is disabled in platforms.json — skipping.")
        return InstagramResult(
            status="skipped", media_id=None,
            container_id=None, public_url=None, reel_url=None,
        )

    # ── Load metadata ──────────────────────────────────────────────────────────
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {output_dir}")
    meta = json.loads(meta_path.read_text())
    ig_meta = meta.get("instagram", {})

    caption_text  = ig_meta.get("caption", "")
    hashtags      = ig_meta.get("hashtags", [])
    # Combine caption and hashtags (Instagram displays them together)
    full_caption  = caption_text
    if hashtags:
        hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
        full_caption = f"{caption_text}\n.\n.\n.\n{hashtag_str}"

    # ── Dry run ────────────────────────────────────────────────────────────────
    if dry_run:
        size_mb = video_path.stat().st_size / (1024 * 1024) if video_path.exists() else 0
        print("═" * 60)
        print("DRY RUN — Instagram Upload Preview")
        print("═" * 60)
        print(f"  Video:   {video_path.name} ({size_mb:.1f} MB)")
        print(f"  Caption: ({len(full_caption)} chars)")
        print(f"  {full_caption[:200]}")
        print(f"  Enabled: {is_enabled()}")
        print("═" * 60)
        return InstagramResult(
            status="dry_run", media_id=None,
            container_id=None, public_url=None, reel_url=None,
        )

    # ── Validate inputs ────────────────────────────────────────────────────────
    if not video_path.exists():
        raise FileNotFoundError(f"video.mp4 not found in {output_dir}")

    # ── Upload video to temporary public host ──────────────────────────────────
    public_url = upload_for_instagram(video_path)

    # ── Authenticate ───────────────────────────────────────────────────────────
    access_token = get_valid_token()
    user_id = _get_user_id(access_token)

    # ── Create container ───────────────────────────────────────────────────────
    container_id = _create_container(
        user_id=user_id,
        video_url=public_url,
        caption=full_caption,
        access_token=access_token,
    )

    # ── Poll until FINISHED ────────────────────────────────────────────────────
    _poll_container_status(container_id, access_token)

    # ── Publish ────────────────────────────────────────────────────────────────
    media_id = _publish_container(user_id, container_id, access_token)

    # Build permalink (best-effort; requires media_id)
    reel_url = f"https://www.instagram.com/reels/{media_id}/"

    return InstagramResult(
        status="published",
        media_id=media_id,
        container_id=container_id,
        public_url=public_url,
        reel_url=reel_url,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Upload a Reel to Instagram.")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default="output/test",
        help="Pipeline output directory (default: output/test)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument(
        "--save-token",
        metavar="TOKEN",
        help="Save a long-lived Instagram access token",
    )
    parser.add_argument(
        "--auth-instructions",
        action="store_true",
        help="Print step-by-step auth setup guide",
    )
    args = parser.parse_args()

    if args.save_token:
        save_initial_token(args.save_token)
        sys.exit(0)

    if args.auth_instructions:
        _print_auth_instructions()
        sys.exit(0)

    try:
        result = upload_short(Path(args.output_dir), dry_run=args.dry_run)
        print(f"\nResult: {result.to_dict()}\n")
    except Exception as e:
        log.error(f"Upload failed: {e}")
        sys.exit(1)
