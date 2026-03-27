"""
video_assembler.py — FFmpeg-based video assembly pipeline.

Combines stock footage clips, voiceover audio, ASS karaoke subtitles, and
optional background music into a finished 1080×1920 MP4 ready for upload.

Pipeline stages (all handled in a single FFmpeg invocation):
  1. SCALE        — scale each clip 5% oversized (1134×2016) to create pan room
  2. DRIFT PAN    — slow horizontal drift via crop with time expression (alternates L↔R)
  3. COLOR GRADE  — dark cinematic look: eq brightness/contrast + colorbalance
  4. VIGNETTE     — dark edges for cinematic framing
  5. CONCAT       — join clips (cycled to hit TARGET_CLIP_DURATION per cut)
  6. SUBTITLES    — burn ASS karaoke captions via libass (lower third, cyan highlight)
  7. AUDIO MIX    — voiceover at full volume + music track at 22% volume

Key design decisions:
  - Uses -stream_loop -1 on all video inputs so short clips loop to fill their slot
  - Landscape clips are automatically center-cropped to 9:16 via scale+crop
  - Music is optional: if assets/music/ is empty, voiceover-only is produced
  - Single FFmpeg call avoids intermediate temp files (faster, less disk I/O)
  - All paths passed as subprocess list args — no shell=True, no injection risk
"""

from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "video_assembler.log"),
    ],
)
log = logging.getLogger("video_assembler")

# ── Constants ─────────────────────────────────────────────────────────────────
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
VIDEO_CRF = 23          # H.264 quality: 18=near-lossless, 23=great, 28=lower quality
VIDEO_PRESET = "fast"   # Encoding speed vs. compression: ultrafast→veryslow
AUDIO_BITRATE = "192k"  # AAC audio bitrate — 192k is transparent for speech+music
MUSIC_VOLUME = 0.25     # Background music at 25% under voiceover

MUSIC_DIR = Path(__file__).parent.parent / "assets" / "music"
FONTS_DIR = Path(__file__).parent.parent / "assets" / "fonts"

# ── Branded intro frame (baked-in thumbnail for Shorts) ───────────────────────
# YouTube does not support custom thumbnails for Shorts via the API.
# Instead, we prepend a 0.5-second branded frame so the auto-generated
# Shorts thumbnail captures the hook text on a dark branded background.
INTRO_DURATION     = 1.5    # seconds of branded frame at START — more frames = higher
                            # chance YouTube's thumbnail sampler lands on our hook text
OUTRO_DURATION     = 1.0    # seconds of same branded frame at END — YouTube sometimes
                            # picks ending frames; doubling coverage doubles our odds
INTRO_FRAME_FPS    = 30
INTRO_FONT_SIZE    = 88     # px — hook text height on 1080×1920 portrait frame
INTRO_OVERLAY_ALPHA = 168   # 0-255; 168 ≈ 66% black overlay over the footage frame

# ── Dark cinematic color grade ────────────────────────────────────────────────
# Applied to every footage clip before concat.
# eq filter:         brightness/contrast adjustment
# colorbalance:      push shadows toward blue/teal — CipherPulse brand aesthetic
# vignette:          darken edges for cinematic framing (angle in radians)
COLOR_BRIGHTNESS  = -0.02   # Very subtle darkening — visibility first
COLOR_CONTRAST    =  1.20   # Keep punch without crushing blacks
COLOR_RS          = -0.02   # Red in shadows (gentle reduction)
COLOR_GS          = -0.01   # Green in shadows (minimal)
COLOR_BS          =  0.04   # Blue in shadows (half-strength teal tint)
VIGNETTE_ANGLE    = "PI/10" # ~18° — light edge falloff, half the previous strength

# ── Clip sequencing ───────────────────────────────────────────────────────────
# footage_downloader.py guarantees all clips are unique (no repeated Pexels IDs).
# video_assembler uses EVERY clip exactly once — no cycling, no repetition.
# With 10 unique clips and ~54s of audio: ~5-6s per clip.
# For shorter cuts, increase TARGET_CLIPS_PER_VIDEO in footage_downloader.py.

# ── Cinematic drift pan ───────────────────────────────────────────────────────
# Each clip is scaled 5% larger than the output frame, then slowly panned
# horizontally using a time-expression on FFmpeg's crop filter.
# Even-indexed segments drift left→right; odd-indexed drift right→left.
# When the same source clip appears twice, opposite pan directions make it
# look like different footage. No zoompan — no zoom-and-hold artifacts.
#
# Scale math:
#   SCALE_W = 1080 * 1.05 = 1134  → 54px of horizontal slack (PAN_SLACK_X)
#   SCALE_H = 1920 * 1.05 = 2016  → 96px of vertical slack, centered at y=48
SCALE_W      = int(VIDEO_WIDTH  * 1.05)  # 1134 — source scale width
SCALE_H      = int(VIDEO_HEIGHT * 1.05)  # 2016 — source scale height
PAN_SLACK_X  = SCALE_W - VIDEO_WIDTH     # 54px — full horizontal drift range
PAN_CENTER_Y = (SCALE_H - VIDEO_HEIGHT) // 2  # 48px — keep crop centered vertically


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_music_track() -> Optional[Path]:
    """
    Randomly select a dark/ambient licensed track from assets/music/.

    Only tracks registered in music_licenses.json are eligible, AND only tracks
    whose filename contains one of the dark keywords (dark, ambient, slow,
    atmospheric, cinematic) are used — never upbeat SoundHelix tracks.
    Silent anullsrc placeholder files are excluded.

    Falls back to a sine-wave drone (generated on first use) if no real dark
    tracks are found.  Returns None only if drone generation also fails.
    """
    if not MUSIC_DIR.exists():
        return None

    import json as _json
    from src.download_safe_music import verify_track

    DARK_KEYWORDS = {"dark", "ambient", "slow", "atmospheric", "cinematic"}

    registry_path = Path(__file__).parent.parent / "music_licenses.json"
    silent_files: set[str] = set()
    if registry_path.exists():
        try:
            reg = _json.loads(registry_path.read_text())
            silent_files = {
                t["filename"] for t in reg.get("tracks", [])
                if "anullsrc" in t.get("source_url", "")
            }
        except Exception:
            pass

    all_tracks = [
        p for p in MUSIC_DIR.iterdir()
        if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}
    ]
    licensed = [p for p in all_tracks if verify_track(p.name)]
    dark_tracks = [
        p for p in licensed
        if any(kw in p.stem.lower() for kw in DARK_KEYWORDS)
        and p.name not in silent_files
    ]

    if dark_tracks:
        chosen = random.choice(dark_tracks)
        log.info(f"Background music: {chosen.name}")
        return chosen

    log.info("No real dark/ambient tracks — using sine-wave drone fallback")

    drone_path = MUSIC_DIR / "dark_ambient_drone.wav"
    if not drone_path.exists():
        log.info("Generating sine-wave drone fallback (80 Hz, lowpass 200 Hz)…")
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=80:duration=60",
             "-af", "lowpass=f=200,volume=0.3", str(drone_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.warning("Drone generation failed — producing voice-only audio")
            return None
        log.info(f"Drone saved: {drone_path.name}")

    if drone_path.exists():
        log.info(f"Background music: {drone_path.name} (generated fallback)")
        return drone_path

    log.warning("No dark/ambient tracks available — producing voice-only audio")
    return None


def _get_video_duration(path: Path) -> float:
    """
    Use ffprobe to read a video file's exact duration in seconds.

    ffprobe is FFmpeg's companion tool for media inspection. The -v quiet flag
    suppresses banner output. -show_entries stream=duration reads just the
    duration field. -of csv=p=0 outputs just the raw number.

    We try the video stream first, then fall back to the container duration,
    since some files store duration at the container level rather than per-stream.
    """
    for entry in ("stream=duration", "format=duration"):
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", entry,
            "-of", "csv=p=0",
            str(path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            raw = result.stdout.strip().split("\n")[0].strip()
            if raw and raw != "N/A":
                return float(raw)
        except (ValueError, subprocess.TimeoutExpired):
            pass
    # Last resort: use mutagen if available
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(str(path))
        if m and m.info:
            return float(m.info.length)
    except Exception:
        pass
    raise RuntimeError(f"Could not determine duration of {path}")


def _escape_filter_path(path: Path) -> str:
    """
    Escape a file path for use inside an FFmpeg filter option value.

    Inside FFmpeg filter_complex strings, the characters : \\ ' are special.
    On Linux, paths almost never contain these, but we escape defensively.
    The path is embedded as:  subtitles=PATH:force_style=...
    where : is the filter option separator — so PATH must not contain raw :.
    """
    s = str(path.resolve())
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace(":", "\\:")
    return s




# ── Filter graph builder ───────────────────────────────────────────────────────

def _build_filter_complex(
    n_clips: int,
    seg_duration: float,
    subtitle_path: Path,
    voice_index: int,
    music_index: Optional[int],
    voice_duration: float = 0.0,
) -> tuple[str, str, str]:
    """
    Build the FFmpeg -filter_complex string for the full pipeline.

    Args:
        n_clips:        Number of video clip inputs (indices 0..n_clips-1)
        seg_duration:   Duration in seconds each clip should fill
        subtitle_path:  Path to the ASS subtitle file (karaoke style)
        voice_index:    FFmpeg input index for the voiceover MP3
        music_index:    FFmpeg input index for the music track (None if no music)
        voice_duration: Total voiceover duration (s) — used to time the CTA overlay

    Returns:
        Tuple of (filter_complex_string, video_map_label, audio_map_label)

    Filter graph per clip:
      trim → setpts → scale(5% oversized) → fps → crop(drift pan) → setsar
        → eq (brightness/contrast)
        → colorbalance (blue/teal shadow tint)
        → vignette (dark edge falloff)
        → format=yuv420p

    The drift pan works by scaling each clip to SCALE_W×SCALE_H (5% larger than
    the output), then using a time-expression on the crop x-offset to slowly pan
    the 1080×1920 window across the source. Even clips pan left→right, odd clips
    pan right→left. When the same source clip appears twice due to cycling, the
    reversed direction makes it read as different footage.

    Then: concat → subtitles (ASS karaoke) → drawtext overlays → [vout]
    """
    parts: list[str] = []
    seg = f"{seg_duration:.3f}"

    # ── Per-clip: scale (oversized) → fps → drift pan → color grade ────────
    #
    # Scale to SCALE_W×SCALE_H (5% larger) so there's PAN_SLACK_X=54px of
    # horizontal room for the pan without revealing black borders.
    #
    # fps before crop: normalise frame rate so `t` in the crop expression
    # increments cleanly at 1/30s intervals. (Without this, variable-rate
    # input can make `t` jump unevenly and produce a jerky pan.)
    #
    # crop x expression: `t/{seg}` goes 0→1 over the clip duration.
    #   even (left→right): x = PAN_SLACK_X * t/seg
    #   odd  (right→left): x = PAN_SLACK_X * (1 - t/seg)
    # No clamp needed — trim guarantees t stays in [0, seg].
    #
    # Color operations happen BEFORE format=yuv420p so they work in RGB space.

    for i in range(n_clips):
        # trim guarantees t stays in [0, seg], so no clamp needed.
        if i % 2 == 0:
            x_expr = f"{PAN_SLACK_X}*t/{seg}"        # left → right
        else:
            x_expr = f"{PAN_SLACK_X}*(1-t/{seg})"   # right → left

        parts.append(
            f"[{i}:v]"
            f"trim=0:{seg},setpts=PTS-STARTPTS,"
            f"scale={SCALE_W}:{SCALE_H}:force_original_aspect_ratio=increase,"
            f"fps={VIDEO_FPS},"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:x='{x_expr}':y={PAN_CENTER_Y},"
            f"setsar=1,"
            f"eq=brightness={COLOR_BRIGHTNESS}:contrast={COLOR_CONTRAST},"
            f"colorbalance=rs={COLOR_RS}:gs={COLOR_GS}:bs={COLOR_BS},"
            f"vignette=angle={VIGNETTE_ANGLE},"
            f"format=yuv420p"
            f"[v{i}]"
        )

    # ── Concatenate all clips ──────────────────────────────────────────────
    concat_inputs = "".join(f"[v{i}]" for i in range(n_clips))
    parts.append(f"{concat_inputs}concat=n={n_clips}:v=1:a=0[vcat]")

    # ── Burn ASS karaoke subtitles ─────────────────────────────────────────
    # The ASS file contains all styling (font, size, color, position, karaoke).
    # fontsdir tells libass where to find our Oswald font.
    # No force_style needed — ASS embeds its own [V4+ Styles] section.
    sub_escaped   = _escape_filter_path(subtitle_path)
    fonts_escaped = _escape_filter_path(FONTS_DIR)
    parts.append(
        f"[vcat]subtitles='{sub_escaped}':fontsdir='{fonts_escaped}'[vsub]"
    )

    # ── Text overlays via drawtext ─────────────────────────────────────────
    # Two overlays chained after subtitles:
    #   1. CP watermark — always shown, top-left, 50% opacity
    #   2. CTA footer   — last 3 s, bottom 10%, light-gray centered text
    bebas_path  = FONTS_DIR / "BebasNeue-Regular.ttf"
    oswald_path = FONTS_DIR / "Oswald-Variable.ttf"
    if bebas_path.exists():
        ov_font: Optional[str] = _escape_filter_path(bebas_path)
    elif oswald_path.exists():
        ov_font = _escape_filter_path(oswald_path)
    else:
        ov_font = None

    def _dt_font(f: Optional[str]) -> str:
        return f"fontfile='{f}':" if f else ""

    dt_parts: list[str] = []

    # CP watermark — permanent, top-left
    dt_parts.append(
        f"drawtext={_dt_font(ov_font)}"
        f"text='CP':"
        f"fontsize=20:"
        f"fontcolor=white@0.50:"
        f"x=20:y=20"
    )

    # CTA — shown for the last 3 s, centered, bottom 10%
    cta_start = max(0.0, voice_duration - 3.0)
    dt_parts.append(
        f"drawtext={_dt_font(ov_font)}"
        f"text='Follow @CipherPulse':"
        f"fontsize=28:"
        f"fontcolor=0xB4B4B4@0.85:"
        f"x=(w-text_w)/2:y=h*0.90:"
        f"enable='gte(t,{cta_start:.3f})'"
    )

    parts.append(f"[vsub]{','.join(dt_parts)}[vout]")

    # ── Stage 5: Audio mixing ──────────────────────────────────────────────
    if music_index is not None:
        # aloop=loop=-1:size=2147483647 → loop music indefinitely
        # amix=duration=first → output length = voiceover length
        parts.append(
            f"[{voice_index}:a]volume=1.0[voice_full];"
            f"[{music_index}:a]aloop=loop=-1:size=2147483647[music_looped];"
            f"[music_looped]volume={MUSIC_VOLUME}[music_vol];"
            f"[voice_full][music_vol]amix=inputs=2:duration=first[aout]"
        )
        audio_map = "[aout]"
    else:
        parts.append(f"[{voice_index}:a]volume=1.0[aout]")
        audio_map = "[aout]"

    return ";".join(parts), "[vout]", audio_map


# ── FFmpeg runner ──────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: list[str]) -> None:
    """
    Execute an FFmpeg command and stream its stderr log output.

    FFmpeg writes progress and error messages to stderr, not stdout.
    We capture stderr and log it line-by-line so it appears in our log file
    without flooding the console with FFmpeg's verbose frame-by-frame progress.

    Why subprocess over os.system()?
    subprocess gives us:
    - Return code checking (raises on failure)
    - Captured stderr (we can log and display it)
    - No shell injection risk (list args, not string)
    - Timeout support

    Why not subprocess.run(capture_output=True)?
    FFmpeg encodes in real-time. capture_output=True buffers all output in RAM
    until the process ends — for a 60-second HD video this can be hundreds of MB.
    We use Popen with readline() to stream output without buffering.
    """
    log.info(f"FFmpeg command: {' '.join(cmd[:6])} ... [{len(cmd)} args total]")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Stream stderr line by line
    stderr_lines: list[str] = []
    for line in process.stderr:
        line = line.rstrip()
        stderr_lines.append(line)
        # Only log lines that contain useful info (skip frame progress spam)
        if any(kw in line.lower() for kw in ("error", "warning", "invalid", "failed")):
            log.warning(f"ffmpeg: {line}")
        elif line.startswith("Output #"):
            log.info(f"ffmpeg: {line}")

    process.wait()

    if process.returncode != 0:
        # On failure, show last 20 lines of stderr to help diagnose
        tail = "\n".join(stderr_lines[-20:])
        raise RuntimeError(
            f"FFmpeg exited with code {process.returncode}.\n"
            f"Last FFmpeg output:\n{tail}"
        )

    log.info("FFmpeg completed successfully")


# ── Branded intro frame helpers ───────────────────────────────────────────────

def _make_intro_frame(hook_line: str, first_clip_path: Path, out_path: Path) -> Path:
    """
    Create a 1080×1920 branded PNG to use as the baked-in Shorts thumbnail frame.

    Layers:
      1. A real frame from first_clip_path (grabbed at 1 second) — gives the
         intro an authentic cinematic look rather than a flat solid colour.
      2. A 66%-opacity black overlay — ensures text is readable over any footage.
      3. Hook text centred vertically, large Bebas Neue, white with drop shadow,
         word-wrapped to ≤3 lines within 920px of the 1080px frame width.
      4. "CIPHERPULSE" watermark top-left in brand cyan at 50% opacity.

    Args:
        hook_line:       The script's opening hook sentence (upper-cased on draw).
        first_clip_path: Path to the first video clip; a frame is extracted at 1s.
        out_path:        Destination for the finished PNG.

    Returns:
        out_path (always written even if frame extraction fails — falls back to
        solid brand-black background).
    """
    import tempfile
    from PIL import Image, ImageDraw, ImageFont

    # ── Extract a frame from the first (guaranteed-dark) clip ─────────────────
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as _tf:
        frame_tmp = Path(_tf.name)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "1.0",
                "-i", str(first_clip_path),
                "-vframes", "1",
                "-vf", (
                    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
                    ":force_original_aspect_ratio=increase,"
                    f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
                ),
                str(frame_tmp),
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:
        log.warning(f"Intro frame extraction failed: {exc}")

    if frame_tmp.exists() and frame_tmp.stat().st_size > 0:
        bg = Image.open(frame_tmp).convert("RGBA")
    else:
        bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 6, 9, 255))
    frame_tmp.unlink(missing_ok=True)

    # ── Dark overlay ──────────────────────────────────────────────────────────
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, INTRO_OVERLAY_ALPHA))
    bg = Image.alpha_composite(bg, overlay)
    draw = ImageDraw.Draw(bg)

    # ── Load fonts ────────────────────────────────────────────────────────────
    bebas_path = FONTS_DIR / "BebasNeue-Regular.ttf"
    try:
        body_font = ImageFont.truetype(str(bebas_path), INTRO_FONT_SIZE)
        wm_font   = ImageFont.truetype(str(bebas_path), 28)
    except Exception:
        body_font = ImageFont.load_default()
        wm_font   = body_font

    # ── Word-wrap hook_line to fit within 920px (80px margin each side) ───────
    text       = hook_line.upper()
    max_w      = VIDEO_WIDTH - 160  # 920px text area
    words      = text.split()
    lines: list[str] = []
    current    = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        w = body_font.getbbox(candidate)[2] - body_font.getbbox(candidate)[0]
        if w <= max_w:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    lines = lines[:3]  # hard cap — 3 lines max

    # ── Draw text centred vertically ──────────────────────────────────────────
    line_gap  = INTRO_FONT_SIZE + 18
    block_h   = len(lines) * line_gap
    y_start   = (VIDEO_HEIGHT - block_h) // 2

    for i, line in enumerate(lines):
        bbox   = body_font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x      = (VIDEO_WIDTH - text_w) // 2
        y      = y_start + i * line_gap
        # Drop shadow
        draw.text((x + 3, y + 3), line, font=body_font, fill=(0, 0, 0, 200))
        # White main text
        draw.text((x, y), line, font=body_font, fill=(255, 255, 255, 255))

    # ── CIPHERPULSE watermark — top-left, brand cyan at 50% opacity ───────────
    draw.text((24, 24), "CIPHERPULSE", font=wm_font, fill=(0, 242, 234, 128))

    bg.convert("RGB").save(str(out_path), "PNG")
    return out_path


def _prepend_intro_frame(frame_png: Path, main_video: Path) -> Path:
    """
    Prepend a 0.5-second branded intro frame to an already-assembled video.

    Uses a three-input FFmpeg pass:
      Input 0 — frame_png looped for INTRO_DURATION seconds (1.5s intro)
      Input 1 — main_video (voice+music already mixed, subtitles already burned)
      Input 2 — frame_png looped for OUTRO_DURATION seconds (1.0s outro)

    Filter graph:
      Video: [intro_v] + [main_v] + [outro_v] → concat → [vout]
      Audio: 1.5s silence + [main audio] + 1.0s silence → concat → [aout]

    Bookending the video maximises the chance YouTube's thumbnail sampler picks
    the branded frame — it samples from both early and late positions.

    The re-encode is necessary because H.264 concat requires consistent stream
    parameters. For a 30-45s video at CRF 23 this adds ~5-10 seconds of wall time.

    Args:
        frame_png:  Path to the 1080×1920 branded intro PNG.
        main_video: Path to the assembled video — overwritten in-place on success.

    Returns:
        main_video path (same path, now contains the prepended intro).
    """
    tmp_out = main_video.with_suffix(".intro_tmp.mp4")

    # Three-segment concat: [intro 1.5s] + [main video] + [outro 1.0s]
    # Input 0 — frame_png looped for INTRO_DURATION (start)
    # Input 1 — main_video (voice+music+subtitles already assembled)
    # Input 2 — frame_png looped for OUTRO_DURATION (end)
    filter_complex = (
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
        f"format=yuv420p,setpts=PTS-STARTPTS[intro_v];"
        f"[1:v]setpts=PTS-STARTPTS[main_v];"
        f"[2:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
        f"format=yuv420p,setpts=PTS-STARTPTS[outro_v];"
        f"[intro_v][main_v][outro_v]concat=n=3:v=1:a=0[vout];"
        f"anullsrc=cl=stereo:r=44100,atrim=duration={INTRO_DURATION},"
        f"asetpts=PTS-STARTPTS[sil_s];"
        f"anullsrc=cl=stereo:r=44100,atrim=duration={OUTRO_DURATION},"
        f"asetpts=PTS-STARTPTS[sil_e];"
        f"[sil_s][1:a][sil_e]concat=n=3:v=0:a=1[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        # Input 0: intro frame (1.5s)
        "-loop", "1", "-framerate", str(INTRO_FRAME_FPS),
        "-t", str(INTRO_DURATION),
        "-i", str(frame_png),
        # Input 1: main assembled video
        "-i", str(main_video),
        # Input 2: outro frame (1.0s) — same branded PNG
        "-loop", "1", "-framerate", str(INTRO_FRAME_FPS),
        "-t", str(OUTRO_DURATION),
        "-i", str(frame_png),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(tmp_out),
    ]

    _run_ffmpeg(cmd)
    tmp_out.replace(main_video)
    return main_video


# ── Public API ────────────────────────────────────────────────────────────────

def assemble_video(
    clip_paths: list[Path],
    voiceover_path: Path,
    srt_path: Path,          # accepts .ass or .srt (named srt_path for backward compat)
    output_dir: Path,
    music_track: Optional[Path] = None,
    hook_line: str = "",
) -> Path:
    """
    Assemble a finished CipherPulse Short from its component files.

    Args:
        clip_paths:     Ordered list of UNIQUE stock footage MP4 paths from footage_downloader.
                        Every path must be a different clip — no duplicates.
                        footage_downloader.fetch_clips_for_script() guarantees this.
        voiceover_path: Path to voiceover.mp3 from voice_generator
        srt_path:       Path to subtitles.ass from voice_generator (accepts .srt too)
        output_dir:     Directory to write video.mp4 into
        music_track:    Optional path to background music. If None, auto-selects
                        from assets/music/ (or produces voice-only if empty).
        hook_line:      Hook sentence from the script. When provided, a 0.5-second
                        branded intro frame is prepended to the video — giving
                        YouTube Shorts a dark branded thumbnail (the API cannot
                        set Shorts thumbnails; the auto-grab comes from frame 0).

    Returns:
        Path to the finished video.mp4

    Raises:
        ValueError: If clip_paths is empty or required files are missing
        RuntimeError: If FFmpeg fails
    """
    if not clip_paths:
        raise ValueError("clip_paths is empty — need at least one footage clip")
    if not voiceover_path.exists():
        raise ValueError(f"Voiceover not found: {voiceover_path}")
    if not srt_path.exists():
        raise ValueError(f"Subtitle file not found: {srt_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "video.mp4"

    # ── Measure voiceover duration ─────────────────────────────────────────
    from mutagen.mp3 import MP3
    voice_duration = MP3(str(voiceover_path)).info.length
    log.info(f"Voiceover duration: {voice_duration:.2f}s")

    # ── Distribute clips evenly across the voiceover ───────────────────────
    # footage_downloader guarantees every clip is unique — use them all once.
    # +0.5 s buffer prevents the last clip cutting off right at audio tail.
    n_clips = len(clip_paths)
    seg_duration = (voice_duration + 0.5) / n_clips
    log.info(f"Using {n_clips} unique clips — {seg_duration:.2f}s each")

    # ── Resolve music track ────────────────────────────────────────────────
    if music_track is None:
        music_track = _pick_music_track()

    # ── Build FFmpeg input list ────────────────────────────────────────────
    # Input order matters for index references in filter_complex:
    #   0..n-1  : video clip files (stream_loop -1 lets short clips fill their slot)
    #   n       : voiceover MP3
    #   n+1     : music track (if present)
    cmd: list[str] = ["ffmpeg", "-y"]  # -y = overwrite output without asking

    for clip_path in clip_paths:
        cmd.extend(["-stream_loop", "-1", "-i", str(clip_path)])

    # Voiceover (no looping — it defines the total duration)
    cmd.extend(["-i", str(voiceover_path)])

    # Music track (stream_loop so short tracks fill the whole voiceover)
    voice_index = n_clips
    music_index: Optional[int] = None
    if music_track and music_track.exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(music_track)])
        music_index = n_clips + 1

    # ── Build filter complex ───────────────────────────────────────────────
    filter_complex, video_map, audio_map = _build_filter_complex(
        n_clips=n_clips,
        seg_duration=seg_duration,
        subtitle_path=srt_path,
        voice_index=voice_index,
        music_index=music_index,
        voice_duration=voice_duration,
    )

    # ── Add filter_complex and output options ──────────────────────────────
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", video_map])
    cmd.extend(["-map", audio_map])

    # Video encoding: H.264 (libx264) is the universal short-form video codec.
    # CRF 23 gives excellent quality at ~4-8 Mbps for 1080×1920.
    # -preset fast balances encoding speed vs. file size.
    # -pix_fmt yuv420p ensures compatibility (some decoders reject yuv444p).
    cmd.extend([
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-pix_fmt", "yuv420p",
    ])

    # Audio encoding: AAC at 192k is transparent quality for voice + music.
    cmd.extend(["-c:a", "aac", "-b:a", AUDIO_BITRATE])

    # Limit output duration to voiceover length (prevents runaway from looped inputs)
    cmd.extend(["-t", str(voice_duration)])

    # -movflags +faststart moves the MP4 index (moov atom) to the start of the file.
    # This allows streaming/playback to begin before the file is fully downloaded —
    # critical for platform upload APIs and CDN delivery.
    cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(output_path))

    # ── Run FFmpeg ─────────────────────────────────────────────────────────
    log.info(f"Assembling video → {output_path}")
    _run_ffmpeg(cmd)

    # ── Verify output ──────────────────────────────────────────────────────
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg ran but output file is missing or empty: {output_path}")

    size_mb = output_path.stat().st_size / 1024 / 1024
    real_duration = _get_video_duration(output_path)
    log.info(
        f"Video ready: {output_path.name} "
        f"({size_mb:.1f} MB, {real_duration:.1f}s, "
        f"{VIDEO_WIDTH}×{VIDEO_HEIGHT})"
    )

    # ── Prepend branded intro frame (baked-in Shorts thumbnail) ───────────────
    # YouTube Shorts auto-selects the thumbnail from the first frame of the video.
    # We bake a 0.5-second hook-text frame at the start so the auto-grab always
    # captures a dark branded image instead of random footage.
    if hook_line and clip_paths:
        try:
            intro_png = output_dir / "intro_frame.png"
            _make_intro_frame(hook_line, clip_paths[0], intro_png)
            _prepend_intro_frame(intro_png, output_path)
            intro_png.unlink(missing_ok=True)  # clean up temp PNG
            new_duration = _get_video_duration(output_path)
            new_size_mb  = output_path.stat().st_size / 1024 / 1024
            log.info(
                f"Intro frame prepended — final: "
                f"{new_size_mb:.1f} MB, {new_duration:.1f}s"
            )
        except Exception as exc:
            log.warning(f"Intro frame prepend failed (non-fatal): {exc}")

    return output_path


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Assemble a CipherPulse Short from component files"
    )
    parser.add_argument(
        "--output-dir", default="output/test", dest="output_dir",
        help="Directory containing voiceover.mp3 and subtitles.srt (default: output/test)",
    )
    parser.add_argument(
        "--clips", nargs="+", metavar="CLIP",
        help="Explicit clip paths to use (default: auto-select from footage_cache)",
    )
    parser.add_argument(
        "--music", metavar="FILE",
        help="Explicit music file path (default: random from assets/music/)",
    )
    parser.add_argument(
        "--no-music", action="store_true", dest="no_music",
        help="Produce voice-only output (no background music)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    voiceover = out_dir / "voiceover.mp3"
    subtitles = out_dir / "subtitles.srt"

    for required in (voiceover, subtitles):
        if not required.exists():
            print(f"ERROR: Required file not found: {required}")
            print("Run voice_generator first: python3 -m src.voice_generator --output-dir output/test")
            sys.exit(1)

    # Resolve clips
    if args.clips:
        clips = [Path(c) for c in args.clips]
    else:
        # Auto-select: grab a spread of clips from the cache
        from pathlib import Path as P
        all_clips = list(P("assets/footage_cache").rglob("*.mp4"))
        if not all_clips:
            print("ERROR: No clips in footage_cache. Run footage_downloader first.")
            sys.exit(1)
        # Sample up to 5 clips, preferring portrait orientation
        random.shuffle(all_clips)
        clips = all_clips[:5]
        print(f"Auto-selected {len(clips)} clips from footage cache")

    # Resolve music
    music: Optional[Path] = None
    if args.no_music:
        music = None
        log.info("Music disabled via --no-music")
    elif args.music:
        music = Path(args.music)
    # else: assemble_video auto-selects from assets/music/

    print(f"\nAssembling video in {out_dir}/")
    print(f"  Clips:     {len(clips)}")
    print(f"  Voiceover: {voiceover.name}")
    print(f"  Subtitles: {subtitles.name}")
    print(f"  Music:     {'disabled' if args.no_music else (music.name if music else 'auto-select')}")
    print()

    result_path = assemble_video(
        clip_paths=clips,
        voiceover_path=voiceover,
        srt_path=subtitles,
        output_dir=out_dir,
        music_track=music,
    )

    size_mb = result_path.stat().st_size / 1024 / 1024
    print(f"\n{'═' * 55}")
    print(f"✅  Video assembled: {result_path}")
    print(f"    Size: {size_mb:.1f} MB")
    print(f"    Resolution: {VIDEO_WIDTH}×{VIDEO_HEIGHT}")
    print(f"═" * 55)
    print("\nTo preview:")
    print(f"  mpv {result_path}")
    print(f"  vlc {result_path}")
