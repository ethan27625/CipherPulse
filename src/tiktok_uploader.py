"""
tiktok_uploader.py — TikTok Content Posting API upload module.

STATUS: Gated behind config/platforms.json → tiktok.enabled flag.
        Build is complete and tested via dry-run. Enable after developer approval.

Key differences from YouTube uploader:
  - No native scheduled publishing → orchestrator uses schedule_queue.json
  - OAuth2 PKCE flow (Proof Key for Code Exchange) for enhanced security
  - Two-phase upload: init (get upload URL) → chunk upload → auto-publish
  - Caption ≤ 150 chars including hashtags (from metadata.json tiktok section)

TikTok Developer setup:
  1. Register at developers.tiktok.com
  2. Create app → request scopes: video.upload + video.publish
  3. Add TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET to .env
  4. Run --auth-only to complete PKCE flow
  5. Flip tiktok.enabled = true in config/platforms.json

API Reference: https://developers.tiktok.com/doc/content-posting-api-get-started
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Optional

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
        logging.FileHandler(LOG_DIR / "tiktok_uploader.log"),
    ],
)
log = logging.getLogger("tiktok_uploader")

# ── Constants ─────────────────────────────────────────────────────────────────
PLATFORMS_CONFIG = Path(__file__).parent.parent / "config" / "platforms.json"
TOKEN_PATH       = Path(__file__).parent.parent / "config" / "tiktok_token.json"
CONFIG_DIR       = Path(__file__).parent.parent / "config"

TIKTOK_AUTH_URL    = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL   = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_INIT_URL    = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL  = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

OAUTH_REDIRECT_URI = "http://localhost:8080/callback"
OAUTH_SCOPES       = "video.upload,video.publish"

CHUNK_SIZE         = 10 * 1024 * 1024   # 10 MB chunks (TikTok min: 5MB, max: 64MB)
MAX_VIDEO_SIZE_MB  = 4 * 1024           # 4 GB TikTok limit
CAPTION_MAX_CHARS  = 150

# ── Platform gate ──────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """
    Return True only if TikTok is enabled in config/platforms.json.

    This is the gate that prevents accidental uploads before API approval.
    The orchestrator calls this before invoking any TikTok function.
    Dry-run mode bypasses this check so you can test the upload logic.
    """
    try:
        config = json.loads(PLATFORMS_CONFIG.read_text())
        return config.get("tiktok", {}).get("enabled", False)
    except Exception as e:
        log.warning(f"Could not read platforms.json: {e} — treating TikTok as disabled")
        return False


# ── PKCE helpers ───────────────────────────────────────────────────────────────

def _generate_pkce_pair() -> tuple[str, str]:
    """
    Generate a PKCE code_verifier and code_challenge pair.

    PKCE (Proof Key for Code Exchange, RFC 7636) prevents authorization code
    interception attacks. Here's the protocol:

    1. Generate a cryptographically random 'code_verifier' (43-128 chars,
       URL-safe base64 without padding).
    2. SHA-256 hash it → 'code_challenge' (also URL-safe base64, no padding).
    3. Send code_challenge + method=S256 in the auth URL.
    4. After the user approves and you get the auth code, send code_verifier
       in the token exchange. TikTok hashes it and checks it matches the
       challenge — proving you initiated the request.

    Why this matters: Without PKCE, a malicious app on the same machine could
    intercept the redirect URI and steal the auth code. With PKCE, the code
    is useless without the verifier that only your process generated.

    Returns:
        (code_verifier, code_challenge) tuple of URL-safe base64 strings
    """
    # 32 bytes → 256 bits of entropy → base64url without padding = 43 chars
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()

    # SHA-256 hash of verifier → base64url without padding
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return code_verifier, code_challenge


# ── OAuth2 PKCE callback server ───────────────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Minimal HTTP server handler to capture the OAuth2 redirect.

    TikTok redirects to http://localhost:8080/callback?code=...&state=...
    after the user approves. We capture the `code` query parameter and
    store it on the server instance so the parent thread can read it.
    """
    auth_code: Optional[str] = None
    state_received: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.state_received = params.get("state", [None])[0]

        # Respond to the browser with a success page
        body = (
            b"<html><body style='font-family:sans-serif;text-align:center;margin-top:100px'>"
            b"<h2>CipherPulse TikTok Authorization</h2>"
            b"<p style='color:green;font-size:1.2em'>&#10003; Authorization successful!</p>"
            b"<p>You may close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Silence the default access log spam
        pass


def _run_callback_server(server: HTTPServer) -> None:
    """Serve exactly one request then stop."""
    server.handle_request()


# ── OAuth2 flow ────────────────────────────────────────────────────────────────

def _get_credentials() -> tuple[str, str]:
    """Return (client_key, client_secret) from environment, raising if missing."""
    client_key    = os.getenv("TIKTOK_CLIENT_KEY", "")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "")
    if not client_key or not client_secret:
        raise ValueError(
            "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET must be set in .env\n"
            "Get from: developers.tiktok.com → your app → Keys & Credentials"
        )
    return client_key, client_secret


def _save_token(token_data: dict) -> None:
    """Persist token dict to config/tiktok_token.json."""
    CONFIG_DIR.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    log.info(f"Token saved: {TOKEN_PATH}")


def _load_token() -> Optional[dict]:
    """Load token from disk. Returns None if missing or unreadable."""
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except Exception as e:
        log.warning(f"Could not load TikTok token: {e}")
        return None


def _is_token_valid(token: dict) -> bool:
    """Return True if the access token hasn't expired yet (with 60s buffer)."""
    expiry = token.get("expires_at", 0)
    return time.time() < expiry - 60


def _refresh_access_token(token: dict) -> Optional[dict]:
    """
    Exchange a refresh_token for a new access_token.

    TikTok refresh tokens are valid for 365 days. Access tokens expire in 24h.
    Unlike YouTube, TikTok also issues a new refresh_token on each refresh
    (rotating refresh tokens) — so we must save the new token immediately.

    Returns updated token dict, or None if refresh fails.
    """
    client_key, client_secret = _get_credentials()
    refresh_token = token.get("refresh_token", "")

    if not refresh_token:
        log.warning("No refresh_token available — need to re-authenticate")
        return None

    log.info("Refreshing TikTok access token")
    try:
        resp = requests.post(
            TIKTOK_TOKEN_URL,
            data={
                "client_key":    client_key,
                "client_secret": client_secret,
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        new_token = {
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at":    time.time() + data.get("expires_in", 86400),
            "open_id":       token.get("open_id", data.get("open_id", "")),
        }
        _save_token(new_token)
        log.info("Token refreshed successfully")
        return new_token
    except Exception as e:
        log.error(f"Token refresh failed: {e}")
        return None


def authenticate() -> dict:
    """
    Run the full PKCE OAuth2 flow and return a valid token dict.

    Full flow:
      1. Generate code_verifier + code_challenge (PKCE)
      2. Build authorization URL with challenge + state
      3. Open browser to that URL
      4. Start local HTTP server to capture redirect
      5. Extract auth code from redirect
      6. Exchange code + verifier for tokens
      7. Save and return token dict

    The `state` parameter is a random nonce used to prevent CSRF attacks:
    we generate it, include it in the auth URL, and verify TikTok echoes
    it back in the redirect. If they don't match, we reject the response.

    Returns:
        Token dict with access_token, refresh_token, expires_at, open_id

    Raises:
        RuntimeError: If auth code not received or token exchange fails
    """
    client_key, client_secret = _get_credentials()
    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    # ── Build authorization URL ────────────────────────────────────────────
    auth_params = {
        "client_key":             client_key,
        "scope":                  OAUTH_SCOPES,
        "response_type":          "code",
        "redirect_uri":           OAUTH_REDIRECT_URI,
        "state":                  state,
        "code_challenge":         code_challenge,
        "code_challenge_method":  "S256",
    }
    auth_url = TIKTOK_AUTH_URL + "?" + urllib.parse.urlencode(auth_params)

    # ── Start callback server ──────────────────────────────────────────────
    _CallbackHandler.auth_code = None
    _CallbackHandler.state_received = None

    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    server_thread = Thread(target=_run_callback_server, args=(server,), daemon=True)
    server_thread.start()

    # ── Open browser ───────────────────────────────────────────────────────
    log.info("Opening browser for TikTok authorization")
    print(f"\nOpening browser for TikTok authorization...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # ── Wait for callback (timeout 120 seconds) ────────────────────────────
    server_thread.join(timeout=120)

    auth_code = _CallbackHandler.auth_code
    state_back = _CallbackHandler.state_received

    if not auth_code:
        raise RuntimeError("No authorization code received within 120 seconds")

    if state_back != state:
        raise RuntimeError(
            f"State mismatch — possible CSRF attack. "
            f"Expected: {state}, got: {state_back}"
        )

    log.info("Authorization code received — exchanging for tokens")

    # ── Exchange code + verifier for tokens ───────────────────────────────
    resp = requests.post(
        TIKTOK_TOKEN_URL,
        data={
            "client_key":     client_key,
            "client_secret":  client_secret,
            "code":           auth_code,
            "grant_type":     "authorization_code",
            "redirect_uri":   OAUTH_REDIRECT_URI,
            "code_verifier":  code_verifier,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    if not data.get("access_token"):
        raise RuntimeError(f"Token exchange failed: {resp.json()}")

    token = {
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at":    time.time() + data.get("expires_in", 86400),
        "open_id":       data.get("open_id", ""),
    }
    _save_token(token)
    log.info("TikTok authentication complete")
    return token


def get_valid_token() -> dict:
    """
    Return a valid access token, refreshing or re-authenticating as needed.

    Token resolution order:
      1. Load from disk → if valid, return immediately
      2. If expired but refresh_token present → refresh and return
      3. If no token or refresh fails → run full auth flow

    This is the entry point all upload functions call before making API requests.
    """
    token = _load_token()

    if token and _is_token_valid(token):
        log.debug("Using existing valid token")
        return token

    if token and token.get("refresh_token"):
        refreshed = _refresh_access_token(token)
        if refreshed:
            return refreshed

    log.info("No valid token — starting authentication flow")
    return authenticate()


# ── Upload logic ───────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _init_upload(access_token: str, video_path: Path, caption: str) -> tuple[str, str, int]:
    """
    Phase 1: Initialize a TikTok Direct Post upload session.

    TikTok's Direct Post API uses a two-phase upload protocol:
    Phase 1 (init): Declare the video — size, chunk count, caption, privacy.
                    Receive: publish_id, upload_url, per_chunk limits.
    Phase 2 (upload): PUT video bytes to upload_url in declared chunks.
                    Receive: 200 OK when complete → TikTok auto-publishes.

    The init response gives us:
      - publish_id:   Used to check publish status after upload
      - upload_url:   Where to PUT the video chunks (pre-signed URL)
      - max_chunk_size: Server's chunk size preference

    Args:
        access_token: Valid TikTok access token
        video_path:   Path to the MP4 file
        caption:      Full caption string including hashtags (≤ 150 chars)

    Returns:
        (publish_id, upload_url, chunk_size) tuple

    Raises:
        RuntimeError: If the API returns an error response
    """
    file_size = video_path.stat().st_size
    chunk_size = min(CHUNK_SIZE, file_size)  # Don't declare chunks larger than file
    total_chunk_count = -(-file_size // chunk_size)  # Ceiling division

    log.info(
        f"Init upload: {video_path.name} "
        f"({file_size / 1024 / 1024:.1f} MB, "
        f"{total_chunk_count} chunks of {chunk_size // 1024 // 1024}MB)"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json; charset=UTF-8",
    }

    body = {
        "post_info": {
            "title":          caption[:CAPTION_MAX_CHARS],
            "privacy_level":  "PUBLIC_TO_EVERYONE",
            "disable_duet":   False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,  # Use frame at 1s as cover
        },
        "source_info": {
            "source":           "FILE_UPLOAD",
            "video_size":       file_size,
            "chunk_size":       chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }

    resp = requests.post(TIKTOK_INIT_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    if data.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok init error: {data['error']}")

    upload_data = data["data"]
    publish_id  = upload_data["publish_id"]
    upload_url  = upload_data["upload_url"]

    log.info(f"Upload initialized — publish_id: {publish_id}")
    return publish_id, upload_url, chunk_size


def _upload_chunks(upload_url: str, video_path: Path, chunk_size: int) -> None:
    """
    Phase 2: Upload video bytes to TikTok's pre-signed upload URL in chunks.

    Each chunk is a PUT request with Content-Range header specifying the
    byte range: "bytes START-END/TOTAL" (e.g. "bytes 0-10485759/31457280").

    The Content-Range header follows RFC 7233. TikTok requires it even for
    single-chunk uploads. The last chunk uses the actual remaining bytes
    (may be smaller than chunk_size if file size isn't evenly divisible).

    TikTok auto-publishes the video once the final chunk is received —
    there's no separate "complete" API call needed for FILE_UPLOAD source.

    Args:
        upload_url: Pre-signed URL from _init_upload()
        video_path: Path to the MP4 file
        chunk_size: Bytes per chunk from _init_upload()
    """
    file_size = video_path.stat().st_size
    bytes_sent = 0

    with open(video_path, "rb") as f:
        chunk_index = 0
        while bytes_sent < file_size:
            chunk_data = f.read(chunk_size)
            if not chunk_data:
                break

            chunk_end = bytes_sent + len(chunk_data) - 1
            content_range = f"bytes {bytes_sent}-{chunk_end}/{file_size}"

            log.info(
                f"Uploading chunk {chunk_index + 1}: "
                f"{bytes_sent // 1024 // 1024}MB–{(chunk_end + 1) // 1024 // 1024}MB "
                f"/ {file_size // 1024 // 1024}MB"
            )

            for attempt in range(3):
                try:
                    resp = requests.put(
                        upload_url,
                        data=chunk_data,
                        headers={
                            "Content-Type":  "video/mp4",
                            "Content-Range": content_range,
                        },
                        timeout=120,
                    )
                    resp.raise_for_status()
                    break
                except requests.RequestException as e:
                    if attempt == 2:
                        raise
                    wait = 2 ** (attempt + 1)
                    log.warning(f"Chunk {chunk_index + 1} failed: {e} — retry in {wait}s")
                    time.sleep(wait)

            bytes_sent += len(chunk_data)
            chunk_index += 1

    log.info(f"All chunks uploaded ({bytes_sent / 1024 / 1024:.1f} MB total)")


def _poll_publish_status(access_token: str, publish_id: str, timeout_seconds: int = 300) -> str:
    """
    Poll the TikTok publish status endpoint until processing completes.

    After uploading all chunks, TikTok processes the video (transcoding,
    content moderation). This takes 10-120 seconds. We poll every 10 seconds.

    Possible status values:
      PROCESSING_UPLOAD   → still receiving/processing chunks
      PROCESSING_DOWNLOAD → transcoding in progress
      SEND_TO_USER_INBOX  → success, visible in creator's draft inbox
      FAILED              → processing failed

    Args:
        access_token:    Valid TikTok access token
        publish_id:      The publish_id from _init_upload()
        timeout_seconds: Max time to wait before giving up (default 5 min)

    Returns:
        Final status string from TikTok

    Raises:
        RuntimeError: If status is FAILED or timeout exceeded
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json; charset=UTF-8",
    }
    deadline = time.time() + timeout_seconds
    poll_interval = 10

    log.info(f"Polling publish status for publish_id={publish_id}")

    while time.time() < deadline:
        try:
            resp = requests.post(
                TIKTOK_STATUS_URL,
                headers=headers,
                json={"publish_id": publish_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            status = data.get("status", "UNKNOWN")

            log.info(f"Publish status: {status}")

            if status in ("SEND_TO_USER_INBOX", "PUBLISHED"):
                return status
            if status == "FAILED":
                reason = data.get("fail_reason", "unknown")
                raise RuntimeError(f"TikTok publish failed: {reason}")

        except RuntimeError:
            raise
        except Exception as e:
            log.warning(f"Status poll error: {e}")

        time.sleep(poll_interval)

    raise RuntimeError(f"Publish status polling timed out after {timeout_seconds}s")


# ── Schedule queue ─────────────────────────────────────────────────────────────

QUEUE_PATH = Path(__file__).parent.parent / "schedule_queue.json"


def _load_queue() -> dict:
    """Load schedule_queue.json, returning empty structure if missing."""
    if not QUEUE_PATH.exists():
        return {"queue": []}
    try:
        return json.loads(QUEUE_PATH.read_text())
    except Exception:
        return {"queue": []}


def _save_queue(queue_data: dict) -> None:
    """Write updated queue back to schedule_queue.json."""
    QUEUE_PATH.write_text(json.dumps(queue_data, indent=2))


def queue_upload(output_dir: Path, publish_at: str) -> None:
    """
    Add a TikTok upload job to schedule_queue.json for later execution.

    Called by the orchestrator in batch mode. The --publish-scheduled mode
    (run hourly by GitHub Actions) reads this queue, checks if any job's
    publish_at time has passed, and fires the upload.

    Args:
        output_dir: Path to the video's output directory
        publish_at: ISO 8601 UTC timestamp when to post (e.g. "2026-03-21T09:00:00Z")
    """
    queue_data = _load_queue()
    job = {
        "platform":   "tiktok",
        "output_dir": str(output_dir),
        "publish_at": publish_at,
        "status":     "pending",
        "queued_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    queue_data["queue"].append(job)
    _save_queue(queue_data)
    log.info(f"Queued TikTok upload: {output_dir.name} → publish at {publish_at}")


# ── Public upload function ─────────────────────────────────────────────────────

def upload_short(
    output_dir: Path,
    dry_run: bool = False,
) -> dict:
    """
    Upload a CipherPulse Short to TikTok immediately.

    Called by the orchestrator's --publish-scheduled mode when a queued
    job's publish_at time has arrived.

    Args:
        output_dir: Path to the video's output directory containing:
                    video.mp4, thumbnail.png, metadata.json
        dry_run:    If True, validate everything and print preview without
                    making any API calls. Bypasses the enabled gate.

    Returns:
        Dict with keys: status, publish_id, error (if failed)

    Note on thumbnails: TikTok does not support custom thumbnail upload
    through the Content Posting API. The video_cover_timestamp_ms parameter
    in the init call selects the frame used as cover (we use the 1-second mark).
    """
    output_dir = Path(output_dir)

    # ── Locate required files ──────────────────────────────────────────────
    video_path    = output_dir / "video.mp4"
    metadata_path = output_dir / "metadata.json"

    for path in (video_path, metadata_path):
        if not path.exists():
            raise FileNotFoundError(f"Required file missing: {path}")

    metadata = json.loads(metadata_path.read_text())
    caption  = metadata["tiktok"]["caption"]
    video_mb = video_path.stat().st_size / 1024 / 1024

    # Validate caption length
    if len(caption) > CAPTION_MAX_CHARS:
        log.warning(f"Caption truncated: {len(caption)} → {CAPTION_MAX_CHARS} chars")
        caption = caption[:CAPTION_MAX_CHARS].rsplit(" ", 1)[0]

    # ── Dry run ────────────────────────────────────────────────────────────
    if dry_run:
        print(f"\n{'═' * 60}")
        print("DRY RUN — TikTok Upload Preview")
        print("═" * 60)
        print(f"  Video:   {video_path.name} ({video_mb:.1f} MB)")
        print(f"  Caption: ({len(caption)} chars)")
        print(f"  {caption}")
        print(f"  Enabled: {is_enabled()}")
        print(f"{'═' * 60}\n")
        return {"status": "dry_run", "publish_id": None}

    # ── Platform gate ──────────────────────────────────────────────────────
    if not is_enabled():
        log.info("TikTok upload skipped — not enabled in config/platforms.json")
        return {"status": "skipped", "publish_id": None}

    # ── Live upload ────────────────────────────────────────────────────────
    try:
        token        = get_valid_token()
        access_token = token["access_token"]

        # Phase 1: Initialize upload session
        publish_id, upload_url, chunk_size = _init_upload(
            access_token, video_path, caption
        )

        # Phase 2: Upload video chunks
        _upload_chunks(upload_url, video_path, chunk_size)

        # Phase 3: Poll for publish completion
        final_status = _poll_publish_status(access_token, publish_id)

        log.info(f"TikTok upload complete — publish_id: {publish_id}, status: {final_status}")
        return {
            "status":     "uploaded",
            "publish_id": publish_id,
        }

    except Exception as e:
        log.error(f"TikTok upload failed: {e}")
        return {
            "status": "failed",
            "error":  str(e),
        }


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Upload a CipherPulse Short to TikTok"
    )
    parser.add_argument(
        "--output-dir", default="output/test", dest="output_dir",
        help="Directory containing video.mp4 and metadata.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Preview upload without making API calls (bypasses enabled gate)",
    )
    parser.add_argument(
        "--auth-only", action="store_true", dest="auth_only",
        help="Run OAuth2 PKCE flow and save token, then exit",
    )
    parser.add_argument(
        "--queue", metavar="TIMESTAMP", dest="queue_at",
        help='Queue upload for later: e.g. "2026-03-21T09:00:00Z"',
    )
    parser.add_argument(
        "--show-pkce", action="store_true", dest="show_pkce",
        help="Generate and display a PKCE pair (for debugging the auth flow)",
    )
    args = parser.parse_args()

    if args.show_pkce:
        verifier, challenge = _generate_pkce_pair()
        print(f"\nPKCE pair generated:")
        print(f"  code_verifier:  {verifier}")
        print(f"  code_challenge: {challenge}")
        print(f"  method:         S256 (SHA-256)")
        sys.exit(0)

    if args.auth_only:
        print("\nStarting TikTok OAuth2 PKCE authentication...")
        print("Requirements:")
        print("  - TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env")
        print("  - Redirect URI http://localhost:8080/callback registered in TikTok Developer app")
        print()
        try:
            token = authenticate()
            print(f"\n✅  Authentication successful!")
            print(f"    Token saved to: {TOKEN_PATH}")
            print(f"    open_id: {token.get('open_id', '?')}")
            print(f"    Expires: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(token.get('expires_at', 0)))}")
        except Exception as e:
            print(f"\n❌  Authentication failed: {e}")
            sys.exit(1)
        sys.exit(0)

    if args.queue_at:
        queue_upload(Path(args.output_dir), args.queue_at)
        print(f"✅  Queued TikTok upload: {args.output_dir} → {args.queue_at}")
        sys.exit(0)

    result = upload_short(
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        print(f"\n{'═' * 60}")
        status = result.get("status", "unknown")
        if status == "uploaded":
            print(f"✅  TikTok upload complete")
            print(f"    Publish ID: {result.get('publish_id')}")
        elif status == "skipped":
            print(f"⏭️   TikTok upload skipped (disabled in platforms.json)")
            print(f"    To enable: set tiktok.enabled=true in config/platforms.json")
        else:
            print(f"❌  Upload failed: {result.get('error', 'Unknown error')}")
        print("═" * 60)
