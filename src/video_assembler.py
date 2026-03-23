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
MUSIC_VOLUME = 0.22     # Background music at 22% of voiceover volume

MUSIC_DIR = Path(__file__).parent.parent / "assets" / "music"
FONTS_DIR = Path(__file__).parent.parent / "assets" / "fonts"

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
    Randomly select a licensed music track from assets/music/.

    Only tracks registered in music_licenses.json are eligible.  Any file
    present on disk but absent from the registry is skipped and logged as an
    error — this prevents unverified tracks from creating Content-ID claims.

    Returns None if no licensed tracks are available; caller produces voice-only audio.
    """
    if not MUSIC_DIR.exists():
        return None

    from src.download_safe_music import verify_track

    all_tracks = [
        p for p in MUSIC_DIR.iterdir()
        if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}
    ]

    licensed: list[Path] = []
    for p in all_tracks:
        if verify_track(p.name):
            licensed.append(p)
        else:
            log.error(
                f"Rejecting unregistered music file: {p.name} — "
                "run 'python3 -m src.download_safe_music' to populate the registry"
            )

    if not licensed:
        log.warning(
            "No licensed tracks in assets/music/ — producing voice-only audio. "
            "Run: python3 -m src.download_safe_music"
        )
        return None

    chosen = random.choice(licensed)
    log.info(f"Background music: {chosen.name}")
    return chosen


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
) -> tuple[str, str, str]:
    """
    Build the FFmpeg -filter_complex string for the full pipeline.

    Args:
        n_clips:       Number of video clip inputs (indices 0..n_clips-1)
        seg_duration:  Duration in seconds each clip should fill
        subtitle_path: Path to the ASS subtitle file (karaoke style)
        voice_index:   FFmpeg input index for the voiceover MP3
        music_index:   FFmpeg input index for the music track (None if no music)

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

    Then: concat → subtitles (ASS karaoke, lower third) → [vout]
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
        f"[vcat]subtitles='{sub_escaped}':fontsdir='{fonts_escaped}'[vout]"
    )

    # ── Stage 5: Audio mixing ──────────────────────────────────────────────
    if music_index is not None:
        # aloop=loop=-1:size=2147483647 → loop music indefinitely
        #   loop=-1 means infinite loops
        #   size=2147483647 is INT_MAX frames — effectively unlimited loop buffer
        # volume=MUSIC_VOLUME → reduce music to 17% of its original level
        # amix=inputs=2:duration=first → mix voice + music, stop when voice ends
        #   duration=first means the output length = length of the first input
        #   (we always put voiceover first, so music is cut off when voice ends)
        parts.append(
            f"[{voice_index}:a]volume=1.0[voice_full];"
            f"[{music_index}:a]aloop=loop=-1:size=2147483647[music_looped];"
            f"[music_looped]volume={MUSIC_VOLUME}[music_vol];"
            f"[voice_full][music_vol]amix=inputs=2:duration=first[aout]"
        )
        audio_map = "[aout]"
    else:
        # No music — pass voiceover through as-is
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


# ── Public API ────────────────────────────────────────────────────────────────

def assemble_video(
    clip_paths: list[Path],
    voiceover_path: Path,
    srt_path: Path,          # accepts .ass or .srt (named srt_path for backward compat)
    output_dir: Path,
    music_track: Optional[Path] = None,
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
    # Each clip gets an equal share of the total duration.
    # (voice_duration + 0.5) adds a 0.5s buffer so the last clip doesn't
    # cut off a syllable right at the tail of the audio.
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
        subtitle_path=srt_path,   # srt_path is now the .ass file
        voice_index=voice_index,
        music_index=music_index,
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
