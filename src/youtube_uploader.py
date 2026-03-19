"""
youtube_uploader.py — YouTube Data API v3 upload module.

Handles OAuth2 authentication (first-run browser flow + automatic token refresh),
video upload with thumbnail, and scheduled publishing via publishAt.

OAuth2 token is stored at config/token.json (gitignored).
Client credentials come from YOUTUBE_CLIENT_ID + YOUTUBE_CLIENT_SECRET env vars.

Quota: each upload costs ~1,600 units. Free tier = 10,000 units/day → 6 uploads/day max.
At 3 Shorts/day we consume ~4,800 units/day — safely within budget.

Upload modes:
  --dry-run   : validates files exist and metadata is well-formed, no API calls
  --schedule  : uploads with publishAt timestamp (for batch/weekly generation)
  (default)   : uploads immediately as public (for GitHub Actions single-video mode)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "youtube_uploader.log"),
    ],
)
log = logging.getLogger("youtube_uploader")

# ── Constants ─────────────────────────────────────────────────────────────────
# OAuth2 scopes we need:
#   youtube.upload — allows video uploads
#   youtubepartner — allows setting thumbnails (requires channel verification)
# If your channel isn't verified for custom thumbnails yet, remove youtubepartner
# and set thumbnails manually in YouTube Studio after upload.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

TOKEN_PATH   = Path(__file__).parent.parent / "config" / "token.json"
CONFIG_DIR   = Path(__file__).parent.parent / "config"

# YouTube video category IDs — 28 = Science & Technology
CATEGORY_ID  = "28"

# Chunk size for resumable uploads: 8MB is the recommended minimum
CHUNK_SIZE   = 8 * 1024 * 1024

# Max retry attempts on transient upload errors (5xx, connection reset)
MAX_UPLOAD_RETRIES = 5

# HTTP status codes that are safe to retry on
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS   = (IOError, TimeoutError)

# Default tags applied to every CipherPulse video (appended to SEO tags)
BASE_TAGS = ["CipherPulse", "cybersecurity", "shorts", "tech", "hacking"]

# ── Client secrets helper ─────────────────────────────────────────────────────

def _build_client_config() -> dict:
    """
    Build the OAuth2 client config dict from environment variables.

    The standard Google OAuth2 flow expects a client_secrets.json file.
    We construct the equivalent dict from env vars so we never have to commit
    a secrets file. InstalledAppFlow.from_client_config() accepts this dict
    directly instead of a file path.

    The 'installed' key signals this is a "desktop app" OAuth2 client,
    which is the correct type for CLI tools. Web apps use a different flow
    with redirect URIs that must match exactly — desktop apps redirect to
    localhost:PORT which the library handles automatically.
    """
    client_id     = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise ValueError(
            "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env\n"
            "Get these from: Google Cloud Console → APIs & Services → Credentials\n"
            "  1. Create project → Enable 'YouTube Data API v3'\n"
            "  2. Create OAuth2 Client ID → Application type: Desktop app\n"
            "  3. Download JSON → copy client_id and client_secret into .env"
        )

    return {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


# ── OAuth2 authentication ─────────────────────────────────────────────────────

def get_authenticated_service():
    """
    Return an authenticated YouTube API service object.

    Authentication flow:
      1. If config/token.json exists and is valid → load and use it
      2. If token exists but access_token is expired → auto-refresh using refresh_token
      3. If no token exists → run browser-based OAuth2 consent flow (first run only)

    The refresh mechanism (step 2) is why you only need to authenticate once.
    The refresh_token doesn't expire unless you revoke access or go 6 months
    without using it. Our 3-uploads/day schedule keeps it perpetually valid.

    The InstalledAppFlow opens a local HTTP server on a random port, launches
    your browser to the consent URL, waits for the redirect with the auth code,
    exchanges it for tokens, then shuts down the server. All automatic.

    Returns:
        A googleapiclient Resource object for the YouTube Data API v3.
    """
    creds: Optional[Credentials] = None

    # Step 1: Try loading existing token
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            log.info("Loaded existing OAuth2 token from config/token.json")
        except Exception as e:
            log.warning(f"Could not load token file: {e} — will re-authenticate")
            creds = None

    # Step 2: Refresh expired token if we have a valid refresh_token
    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("Access token expired — refreshing automatically")
            try:
                creds.refresh(Request())
                _save_token(creds)
                log.info("Token refreshed successfully")
            except Exception as e:
                log.warning(f"Token refresh failed: {e} — will re-authenticate")
                creds = None

    # Step 3: First-run OAuth2 flow
    if not creds or not creds.valid:
        import socket
        OAUTH_PORT = 8080

        client_config = _build_client_config()
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

        # Generate the auth URL so we can print it before starting the server
        flow.redirect_uri = f"http://localhost:{OAUTH_PORT}/"
        auth_url, _ = flow.authorization_url(prompt="consent")

        # Get VM IP for SSH forwarding instructions
        try:
            vm_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            vm_ip = "YOUR_VM_IP"

        print("\n" + "═" * 70)
        print("  YouTube OAuth2 Authentication — Headless VM Setup")
        print("═" * 70)
        print(f"\n  Step 1 — On your Mac, open a Terminal and run:")
        print(f"\n    ssh -L {OAUTH_PORT}:localhost:{OAUTH_PORT} zero@{vm_ip}")
        print(f"\n  Step 2 — Then open this URL in your Mac browser:\n")
        print(f"    {auth_url}")
        print(f"\n  Step 3 — Sign in with your YouTube channel account.")
        print(f"  Step 4 — After approving, your browser will redirect to")
        print(f"           localhost:{OAUTH_PORT} — the VM will capture the code.")
        print("═" * 70 + "\n")

        log.info(f"Waiting for OAuth2 callback on localhost:{OAUTH_PORT} …")
        creds = flow.run_local_server(
            port=OAUTH_PORT,
            open_browser=False,
            success_message=(
                "CipherPulse authentication successful! "
                "You may close this tab and return to the terminal."
            ),
        )

        _save_token(creds)
        log.info("OAuth2 authentication complete — token saved to config/token.json")

    # Build and return the API service object
    service = build("youtube", "v3", credentials=creds)
    log.info("YouTube API service ready")
    return service


def _save_token(creds: Credentials) -> None:
    """Persist OAuth2 credentials to config/token.json."""
    CONFIG_DIR.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    log.debug(f"Token saved to {TOKEN_PATH}")


# ── Upload helpers ────────────────────────────────────────────────────────────

def _build_video_body(
    title: str,
    description: str,
    tags: list[str],
    publish_at: Optional[str],
    category_id: str = CATEGORY_ID,
) -> dict:
    """
    Build the YouTube API video resource body dict.

    The YouTube Data API v3 uses a 'resource' model where every object
    (video, channel, playlist) has a set of 'parts' — sub-objects like
    'snippet', 'status', 'contentDetails'. You only pay quota for the
    parts you request or update.

    snippet:
      - title, description, tags, categoryId go here
      - defaultLanguage sets the primary language for auto-caption matching

    status:
      - privacyStatus: 'private' | 'public' | 'unlisted'
      - publishAt: ISO 8601 UTC timestamp — only valid when privacyStatus='private'
        YouTube makes the video public automatically at this time.
      - selfDeclaredMadeForKids: False — required field; True triggers restricted mode
        that disables comments, notifications, and monetization.
        CipherPulse content is NOT made for kids.

    Args:
        title:       Video title (≤ 70 chars recommended)
        description: Full description with hashtags
        tags:        Combined SEO + base tag list
        publish_at:  ISO 8601 UTC string like "2026-03-21T08:00:00Z", or None for immediate
        category_id: YouTube category ID string (28 = Science & Technology)
    """
    all_tags = tags + [t for t in BASE_TAGS if t not in tags]

    body: dict = {
        "snippet": {
            "title":           title,
            "description":     description,
            "tags":            all_tags,
            "categoryId":      category_id,
            "defaultLanguage": "en",
        },
        "status": {
            "selfDeclaredMadeForKids": False,
        },
    }

    if publish_at:
        # Scheduled: upload as private, YouTube publishes at the given time
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"]     = publish_at
    else:
        # Immediate: upload as public right now
        body["status"]["privacyStatus"] = "public"

    return body


def _upload_with_retry(
    service,
    video_path: Path,
    body: dict,
) -> str:
    """
    Upload a video file using resumable upload with exponential backoff retry.

    Resumable upload protocol:
      1. POST metadata → YouTube returns a resumable upload URI
      2. PUT chunks to that URI until all bytes are sent
      3. YouTube returns the video ID when complete

    The Google client library handles the chunking transparently via
    MediaFileUpload(resumable=True). We just call .next_chunk() in a loop.

    The retry logic handles:
      - HttpError with 5xx status → transient server error, safe to retry
      - IOError / TimeoutError    → connection dropped, resume from last chunk
      - 4xx errors (403, 404)     → auth or resource error, do NOT retry

    Exponential backoff: wait = 2^attempt seconds (1s, 2s, 4s, 8s, 16s).
    This prevents hammering an already-overloaded API endpoint.

    Args:
        service:    Authenticated YouTube API service object
        video_path: Path to the MP4 file
        body:       Video resource body from _build_video_body()

    Returns:
        YouTube video ID string (e.g. "dQw4w9WgXcQ")

    Raises:
        RuntimeError: If upload fails after all retry attempts
    """
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    insert_request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    video_id = None
    response = None
    error    = None
    attempt  = 0
    file_mb  = video_path.stat().st_size / 1024 / 1024

    log.info(f"Starting upload: {video_path.name} ({file_mb:.1f} MB)")

    while response is None:
        try:
            status, response = insert_request.next_chunk()

            if status:
                pct = int(status.progress() * 100)
                log.info(f"Upload progress: {pct}%")

        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"HTTP {e.resp.status}: {e}"
            else:
                # Non-retriable HTTP error (e.g. 403 Forbidden, 400 Bad Request)
                log.error(f"Non-retriable HTTP error: {e.resp.status} — {e}")
                raise

        except RETRIABLE_EXCEPTIONS as e:
            error = str(e)

        if error:
            attempt += 1
            if attempt > MAX_UPLOAD_RETRIES:
                raise RuntimeError(
                    f"Upload failed after {MAX_UPLOAD_RETRIES} retries. Last error: {error}"
                )
            wait = 2 ** attempt
            log.warning(f"Upload error: {error}. Retrying in {wait}s (attempt {attempt}/{MAX_UPLOAD_RETRIES})")
            time.sleep(wait)
            error = None

    video_id = response.get("id")
    log.info(f"Upload complete — video ID: {video_id}")
    return video_id


def _set_thumbnail(service, video_id: str, thumbnail_path: Path) -> bool:
    """
    Set a custom thumbnail for an uploaded video.

    Requires the YouTube channel to be verified (phone number confirmation
    in YouTube Studio). Unverified channels can only use auto-generated
    thumbnails from video frames.

    Args:
        service:        Authenticated YouTube API service
        video_id:       Video ID returned from the upload
        thumbnail_path: Path to the PNG thumbnail file

    Returns:
        True if thumbnail set successfully, False if it fails (non-fatal).

    Thumbnail specs (YouTube requirements):
      - Resolution: 1280×720 (we produce this exactly)
      - Format:     JPG, GIF, BMP, or PNG
      - Size:       < 2MB
      - Aspect:     16:9
    """
    thumb_size_kb = thumbnail_path.stat().st_size // 1024
    log.info(f"Setting thumbnail: {thumbnail_path.name} ({thumb_size_kb} KB)")

    try:
        service.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(
                str(thumbnail_path),
                mimetype="image/png",
            ),
        ).execute()
        log.info("Thumbnail set successfully")
        return True

    except HttpError as e:
        # 403 with "forbidden" often means channel is unverified
        if e.resp.status == 403:
            log.warning(
                "Thumbnail upload failed (403 Forbidden). "
                "Your YouTube channel may not be verified yet.\n"
                "To verify: YouTube Studio → Settings → Channel → Feature eligibility\n"
                "Verify with a phone number to unlock custom thumbnails."
            )
        else:
            log.warning(f"Thumbnail upload failed: {e.resp.status} — {e}")
        return False


# ── Public upload function ────────────────────────────────────────────────────

def upload_short(
    output_dir: Path,
    publish_at: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Upload a CipherPulse Short to YouTube.

    Reads all required files from output_dir:
      - video.mp4       : the assembled video
      - thumbnail.png   : the branded thumbnail
      - metadata.json   : YouTube title, description, tags from seo_generator

    Args:
        output_dir: Path to the video's output directory
        publish_at: ISO 8601 UTC timestamp for scheduled publishing.
                    None means publish immediately as public.
                    Example: "2026-03-21T08:00:00Z"
        dry_run:    If True, validate all files and print what would be uploaded
                    but make zero API calls. Safe for testing.

    Returns:
        Dict with keys:
          status:    "uploaded" | "scheduled" | "dry_run" | "failed"
          video_id:  YouTube video ID (or None for dry_run/failed)
          url:       Full YouTube URL (or None)
          publish_at: The publishAt value used (or None)

    Raises:
        FileNotFoundError: If required files are missing from output_dir
        ValueError:        If YOUTUBE credentials are not configured
    """
    output_dir = Path(output_dir)

    # ── Locate required files ──────────────────────────────────────────────
    video_path     = output_dir / "video.mp4"
    thumbnail_path = output_dir / "thumbnail.png"
    metadata_path  = output_dir / "metadata.json"

    for path in (video_path, thumbnail_path, metadata_path):
        if not path.exists():
            raise FileNotFoundError(f"Required file missing: {path}")

    # ── Load metadata ──────────────────────────────────────────────────────
    metadata = json.loads(metadata_path.read_text())
    yt = metadata["youtube"]

    title       = yt["title"]
    description = yt["description"]
    tags        = yt["tags"]
    video_mb    = video_path.stat().st_size / 1024 / 1024
    thumb_kb    = thumbnail_path.stat().st_size // 1024

    # ── Dry run ────────────────────────────────────────────────────────────
    if dry_run:
        log.info("DRY RUN — no API calls made")
        mode = "scheduled" if publish_at else "immediate"
        print(f"\n{'═' * 60}")
        print(f"DRY RUN — YouTube Upload Preview")
        print(f"{'═' * 60}")
        print(f"  Video:      {video_path.name} ({video_mb:.1f} MB)")
        print(f"  Thumbnail:  {thumbnail_path.name} ({thumb_kb} KB)")
        print(f"  Title:      {title}")
        print(f"  Tags:       {', '.join(tags)}")
        print(f"  Mode:       {mode}")
        if publish_at:
            print(f"  Publish at: {publish_at}")
        print(f"  Description preview:")
        for line in description.split("\n")[:4]:
            print(f"    {line}")
        print(f"{'═' * 60}\n")
        return {
            "status":     "dry_run",
            "video_id":   None,
            "url":        None,
            "publish_at": publish_at,
        }

    # ── Live upload ────────────────────────────────────────────────────────
    try:
        service = get_authenticated_service()
        body    = _build_video_body(title, description, tags, publish_at)

        video_id = _upload_with_retry(service, video_path, body)

        # Set thumbnail (non-fatal if it fails)
        _set_thumbnail(service, video_id, thumbnail_path)

        url    = f"https://www.youtube.com/shorts/{video_id}"
        status = "scheduled" if publish_at else "uploaded"

        log.info(f"YouTube {status}: {url}")
        if publish_at:
            log.info(f"Will go public at: {publish_at}")

        return {
            "status":     status,
            "video_id":   video_id,
            "url":        url,
            "publish_at": publish_at,
        }

    except Exception as e:
        log.error(f"YouTube upload failed: {e}")
        return {
            "status":     "failed",
            "video_id":   None,
            "url":        None,
            "publish_at": publish_at,
            "error":      str(e),
        }


# ── Schedule calculation helper ───────────────────────────────────────────────

def calculate_publish_times(
    count: int,
    start_date: Optional[datetime] = None,
    times_of_day: Optional[list[str]] = None,
    timezone_offset_hours: int = -5,  # EST = UTC-5
) -> list[str]:
    """
    Generate a list of ISO 8601 UTC publishAt timestamps for batch scheduling.

    Called by the orchestrator in batch mode (--count N) to assign each video
    a specific publishing slot across the coming week.

    Args:
        count:                  Number of timestamps to generate
        start_date:             First publishing date (defaults to tomorrow UTC)
        times_of_day:           List of "HH:MM" strings in local time
                                (defaults to ["08:00", "14:00", "20:00"])
        timezone_offset_hours:  Offset from UTC for the local times
                                (-5 = EST, -4 = EDT)

    Returns:
        List of ISO 8601 UTC strings, one per video, in chronological order.

    Example with count=7, EST times 8AM/2PM/8PM:
        ["2026-03-20T13:00:00Z",   # 8AM EST = 1PM UTC
         "2026-03-20T19:00:00Z",   # 2PM EST = 7PM UTC
         "2026-03-21T01:00:00Z",   # 8PM EST = 1AM UTC next day
         "2026-03-21T13:00:00Z",   # 8AM EST next day
         ...]
    """
    if times_of_day is None:
        times_of_day = ["08:00", "14:00", "20:00"]

    if start_date is None:
        # Start tomorrow to give YouTube time to process
        start_date = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        start_date += timedelta(days=1)

    timestamps: list[str] = []
    current_date = start_date.date()

    from datetime import timedelta
    slot_index = 0

    while len(timestamps) < count:
        for time_str in times_of_day:
            if len(timestamps) >= count:
                break
            hour, minute = map(int, time_str.split(":"))
            # Convert local time to UTC
            local_dt = datetime(
                current_date.year, current_date.month, current_date.day,
                hour, minute, 0, tzinfo=timezone.utc
            )
            utc_dt = local_dt - timedelta(hours=timezone_offset_hours)
            timestamps.append(utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        current_date += timedelta(days=1)

    return timestamps[:count]


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Upload a CipherPulse Short to YouTube"
    )
    parser.add_argument(
        "--output-dir", default="output/test", dest="output_dir",
        help="Directory containing video.mp4, thumbnail.png, metadata.json",
    )
    parser.add_argument(
        "--publish-at", dest="publish_at", default=None,
        metavar="TIMESTAMP",
        help='Schedule publishing: ISO 8601 UTC, e.g. "2026-03-21T13:00:00Z"',
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Validate and preview upload without making any API calls",
    )
    parser.add_argument(
        "--auth-only", action="store_true", dest="auth_only",
        help="Run OAuth2 flow and save token, then exit (use on first setup)",
    )
    parser.add_argument(
        "--show-schedule", type=int, dest="show_schedule", metavar="N",
        help="Print N publish timestamps starting tomorrow (for planning)",
    )
    args = parser.parse_args()

    # ── Show schedule preview ──────────────────────────────────────────────
    if args.show_schedule:
        times = calculate_publish_times(args.show_schedule)
        print(f"\n{args.show_schedule} scheduled publish times (EST → UTC):")
        for i, t in enumerate(times, 1):
            # Convert back to readable local time for display
            utc_dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            from datetime import timedelta
            est_dt = utc_dt - timedelta(hours=5)  # UTC → EST
            print(f"  {i:2d}. {t}  (EST: {est_dt.strftime('%a %b %-d at %-I:%M %p')})")
        sys.exit(0)

    # ── Auth only ──────────────────────────────────────────────────────────
    if args.auth_only:
        print("\nStarting YouTube OAuth2 authentication...")
        print("A browser window will open. Sign in with your YouTube channel account.\n")
        try:
            get_authenticated_service()
            print(f"\n✅  Authentication successful!")
            print(f"    Token saved to: {TOKEN_PATH}")
            print(f"    You won't need to authenticate again unless you revoke access.")
        except Exception as e:
            print(f"\n❌  Authentication failed: {e}")
            print("\nSetup checklist:")
            print("  1. Go to console.cloud.google.com")
            print("  2. Create a project → Enable 'YouTube Data API v3'")
            print("  3. OAuth consent screen → External → Add your email as test user")
            print("  4. Credentials → Create OAuth2 Client ID → Desktop app")
            print("  5. Copy client_id and client_secret to .env")
            sys.exit(1)
        sys.exit(0)

    # ── Upload (dry-run or live) ───────────────────────────────────────────
    result = upload_short(
        output_dir=Path(args.output_dir),
        publish_at=args.publish_at,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        print(f"\n{'═' * 60}")
        if result["status"] in ("uploaded", "scheduled"):
            print(f"✅  Status:    {result['status'].upper()}")
            print(f"    Video ID:  {result['video_id']}")
            print(f"    URL:       {result['url']}")
            if result.get("publish_at"):
                print(f"    Scheduled: {result['publish_at']}")
        else:
            print(f"❌  Upload failed: {result.get('error', 'Unknown error')}")
        print("═" * 60)
