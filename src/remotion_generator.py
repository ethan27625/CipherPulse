"""
remotion_generator.py — Renders CipherPulse animated compositions via Remotion.

Writes scene_data.json into the Remotion public/ folder, then invokes
`npx remotion render` headlessly (requires Chromium + xvfb on Linux).

Props are written to a temp JSON file and passed via --props <filepath> to
avoid shell-quoting issues with captions that contain apostrophes, quotes, or
other special characters.

Public API:
    render_remotion_video(scenes, title, hook, output_dir, music_track) -> Path
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
REMOTION_DIR = Path(__file__).parent.parent / "remotion-video"
REMOTION_PUBLIC_DIR = REMOTION_DIR / "public"
COMPOSITION_ID = "CipherPulse"
FPS = 30
WIDTH = 1080
HEIGHT = 1920

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH", ""),
    "/snap/bin/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def _find_chromium() -> Optional[str]:
    for candidate in CHROMIUM_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    found = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    return found


def _total_frames(scenes: list[dict]) -> int:
    total_seconds = sum(s["duration_seconds"] for s in scenes)
    return max(1, round(total_seconds * FPS))


def _copy_music(music_track: Optional[Path]) -> Optional[str]:
    """Copy a music file into remotion-video/public/ so Remotion can serve it."""
    if not music_track or not music_track.exists():
        return None
    REMOTION_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = REMOTION_PUBLIC_DIR / music_track.name
    shutil.copy2(music_track, dest)
    log.info("Copied music track to public/: %s", music_track.name)
    return music_track.name


def render_remotion_video(
    scenes: list[dict],
    title: str,
    hook: str,
    output_dir: Path,
    music_track: Optional[Path] = None,
) -> Path:
    """Render an animated composition via Remotion and return the output MP4 path.

    Props are written to a temporary JSON file to avoid shell-quoting issues
    with captions that contain apostrophes, quotes, or other special characters.

    Args:
        scenes: List of SceneDescription dicts (id, type, caption, duration_seconds, accent_color, keyword).
        title: Video title (used in default props / watermark).
        hook: Opening hook line.
        output_dir: Directory to write the final video.mp4.
        music_track: Optional path to a music file to embed at low volume.

    Returns:
        Path to the rendered video.mp4.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "video_remotion.mp4"

    chromium = _find_chromium()
    if not chromium:
        raise RuntimeError(
            "Chromium not found. Install it with:\n"
            "  sudo apt-get install -y chromium-browser\n"
            "  # or: sudo snap install chromium\n"
            "Then set CHROMIUM_PATH=/path/to/chromium in .env"
        )
    log.info("Using Chromium at: %s", chromium)

    # Stage 1: copy music into public/ so Remotion can serve it as a staticFile
    music_file = _copy_music(music_track)

    # Stage 2: build props JSON and write to a temp file.
    # Passing complex JSON inline via --props breaks on apostrophes, quotes, and
    # other shell-special characters in captions.  Remotion 4.x accepts a file
    # path directly: --props /tmp/remotion_props.json
    props = {
        "scenes": scenes,
        "title": title,
        "hook": hook,
        "musicFile": music_file,
    }
    props_json = json.dumps(props, ensure_ascii=False)

    # Write to a named temp file that persists until we clean it up after the render
    props_fd, props_path = tempfile.mkstemp(suffix=".json", prefix="remotion_props_")
    try:
        with os.fdopen(props_fd, "w", encoding="utf-8") as fh:
            fh.write(props_json)
        log.info("Props written to temp file: %s (%d bytes)", props_path, len(props_json))

        total_frames = _total_frames(scenes)
        log.info("Expected total frames: %d (%.1fs @ %d fps)", total_frames, total_frames / FPS, FPS)
        cmd = [
            "npx", "--yes", "remotion", "render",
            "src/index.ts",
            COMPOSITION_ID,
            str(output_path.resolve()),
            "--props", props_path,        # ← file path, not inline JSON
            # No --frames: the composition's calculateMetadata() already knows
            # the full duration from the props.  Passing --frames 1-N causes an
            # off-by-one error because Remotion uses 0-indexed frame numbers
            # (valid range is 0 to N-1, not 1 to N).
            "--browser-executable", chromium,
            "--log", "verbose",
        ]

        # Wrap in xvfb-run on Linux headless environments
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            xvfb = shutil.which("xvfb-run")
            if xvfb:
                cmd = [xvfb, "-a", "--server-args=-screen 0 1280x1024x24"] + cmd
                log.info("Running under xvfb-run (headless)")
            else:
                log.warning(
                    "xvfb-run not found — rendering may fail without DISPLAY. "
                    "Install: sudo apt-get install -y xvfb"
                )

        log.info("Remotion render command:\n  %s", " ".join(cmd))
        log.info("Working directory: %s", REMOTION_DIR)
        _run_render(cmd)
    finally:
        # Always clean up the temp file, even if the render fails
        try:
            os.unlink(props_path)
            log.debug("Removed temp props file: %s", props_path)
        except OSError:
            pass

    if not output_path.exists():
        raise RuntimeError(f"Remotion render completed but output file not found: {output_path}")

    log.info("Remotion render complete: %s (%.1f MB)", output_path, output_path.stat().st_size / 1e6)
    return output_path


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=5, max=30))
def _run_render(cmd: list[str]) -> None:
    """Execute the remotion render subprocess, streaming stdout and capturing stderr."""
    env = {**os.environ, "NODE_ENV": "production"}

    log.info("[remotion] Starting subprocess…")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,   # ← capture stderr separately (was STDOUT before)
        text=True,
        cwd=str(REMOTION_DIR),
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    # Stream stdout line-by-line so we see progress in real time
    stdout_lines: list[str] = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            stdout_lines.append(line)
            log.debug("[remotion stdout] %s", line)

    # Read all stderr after stdout closes
    stderr_output = process.stderr.read()
    process.wait()

    # Always log stderr — it's where Remotion prints TypeScript errors, missing
    # modules, and other fatal messages that don't appear on stdout
    if stderr_output.strip():
        for line in stderr_output.splitlines():
            if line.strip():
                log.error("[remotion stderr] %s", line)

    if process.returncode != 0:
        # Surface the last 30 stdout lines so the error is visible even when
        # the caller only logs at INFO level
        tail = "\n".join(stdout_lines[-30:]) if stdout_lines else "(no stdout)"
        raise RuntimeError(
            f"Remotion render failed with exit code {process.returncode}\n"
            f"--- last stdout ---\n{tail}\n"
            f"--- stderr ---\n{stderr_output.strip()}"
        )
