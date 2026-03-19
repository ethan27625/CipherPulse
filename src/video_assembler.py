"""
video_assembler.py — FFmpeg-based video assembly pipeline.

Combines stock footage clips, voiceover audio, SRT subtitles, and optional
background music into a finished 1080×1920 MP4 ready for platform upload.

Pipeline stages (all handled in a single FFmpeg invocation):
  1. NORMALIZE  — scale/crop each clip to exactly 1080×1920, 30fps, yuv420p
  2. TRIM       — cut each clip to its time slot (voiceover_duration / n_clips)
  3. CONCAT     — join all clips into one continuous video stream
  4. SUBTITLES  — burn SRT captions via libass (large bold text, middle of frame)
  5. AUDIO MIX  — voiceover at full volume + music track at 17% volume

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
MUSIC_VOLUME = 0.17     # Background music at 17% of voiceover volume

MUSIC_DIR = Path(__file__).parent.parent / "assets" / "music"

# ── ASS subtitle style ────────────────────────────────────────────────────────
# force_style overrides the default SRT→ASS conversion styling.
# ASS color format: &HAABBGGRR  (alpha, blue, green, red — reversed from HTML)
#   White:  &H00FFFFFF
#   Black:  &H00000000
#
# Alignment uses numpad layout (ASS v4+ spec):
#   7 8 9       top-left, top-center, top-right
#   4 5 6       mid-left, mid-center, mid-right   ← we use 5 (middle center)
#   1 2 3       bot-left, bot-center, bot-right
#
# FontSize=72 at 1080px width is large and readable on a phone screen.
# Outline=3 gives a thick black border that keeps white text legible on any bg.
# Shadow=2 adds a drop shadow for depth on dark backgrounds.

SUBTITLE_STYLE = (
    "FontName=Arial,"
    "FontSize=72,"
    "Bold=1,"
    "PrimaryColour=&H00FFFFFF,"   # white text
    "OutlineColour=&H00000000,"   # black outline
    "BackColour=&H80000000,"      # 50% transparent black shadow background
    "BorderStyle=1,"              # 1=outline+shadow (not opaque box)
    "Outline=3,"
    "Shadow=2,"
    "Alignment=5,"                # middle center of frame
    "MarginV=0"                   # no vertical offset from true center
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_music_track() -> Optional[Path]:
    """
    Randomly select a music track from assets/music/.

    Accepts .mp3, .wav, .m4a, .flac files.
    Returns None if the directory is empty — caller produces voice-only audio.
    """
    if not MUSIC_DIR.exists():
        return None
    tracks = [
        p for p in MUSIC_DIR.iterdir()
        if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}
    ]
    if not tracks:
        log.warning("assets/music/ is empty — producing voice-only audio (no background music)")
        return None
    chosen = random.choice(tracks)
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
    srt_path: Path,
    voice_index: int,
    music_index: Optional[int],
) -> tuple[str, str, str]:
    """
    Build the FFmpeg -filter_complex string for the full pipeline.

    Args:
        n_clips:      Number of video clip inputs (indices 0..n_clips-1)
        seg_duration: Duration in seconds each clip should fill
        srt_path:     Path to the SRT subtitle file
        voice_index:  FFmpeg input index for the voiceover MP3
        music_index:  FFmpeg input index for the music track (None if no music)

    Returns:
        Tuple of (filter_complex_string, video_map_label, audio_map_label)
        The map labels are used in the -map flags of the final FFmpeg command.

    Filter graph breakdown:
    ┌─────────────────────────────────────────────────────────────────────┐
    │ For each clip i:                                                    │
    │   [i:v] → trim → setpts → scale → crop → setsar → fps → fmt [vi]  │
    │                                                                     │
    │ [v0][v1]...[vN] → concat → [vcat]                                  │
    │                                                                     │
    │ [vcat] → subtitles → [vout]                                         │
    │                                                                     │
    │ [voice_idx:a] ─────────────────────────────────┐                   │
    │                                                 ├→ amix → [aout]   │
    │ [music_idx:a] → aloop → volume ─────────────────┘                  │
    └─────────────────────────────────────────────────────────────────────┘
    """
    parts: list[str] = []
    seg = f"{seg_duration:.3f}"

    # ── Stage 1 & 2: Normalize + trim each clip ────────────────────────────
    # Explanation of each filter:
    #   trim=0:SEC        → take only the first SEC seconds (clips loop via stream_loop)
    #   setpts=PTS-STARTPTS → reset timestamps to start at 0 (required after trim)
    #   scale=W:H:force_original_aspect_ratio=increase
    #                     → scale up until BOTH dimensions meet or exceed W×H,
    #                       preserving aspect ratio. Portrait clips scale exactly;
    #                       landscape clips overshoot in width → then we crop.
    #   crop=W:H          → center-crop to exactly W×H (removes excess from landscape)
    #   setsar=1          → set Sample Aspect Ratio to 1:1 (square pixels)
    #                       Some cameras record non-square pixels; this normalizes.
    #   fps=FPS           → enforce consistent frame rate across all clips
    #   format=yuv420p    → convert to standard 4:2:0 chroma subsampling —
    #                       required for maximum compatibility with H.264 decoders
    #                       and all platforms (YouTube, TikTok, Instagram).

    for i in range(n_clips):
        parts.append(
            f"[{i}:v]"
            f"trim=0:{seg},setpts=PTS-STARTPTS,"
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            f"setsar=1,"
            f"fps={VIDEO_FPS},"
            f"format=yuv420p"
            f"[v{i}]"
        )

    # ── Stage 3: Concatenate all normalized clips ──────────────────────────
    # concat filter: n=number of segments, v=1 video stream, a=0 audio streams
    # We handle audio separately, so a=0. The filter joins [v0][v1]...[vN]
    # into one continuous stream in order, producing [vcat].
    concat_inputs = "".join(f"[v{i}]" for i in range(n_clips))
    parts.append(f"{concat_inputs}concat=n={n_clips}:v=1:a=0[vcat]")

    # ── Stage 4: Burn subtitles ────────────────────────────────────────────
    # subtitles filter uses libass to render SRT/ASS captions onto the video.
    # force_style= overrides the default styling with our CipherPulse style.
    # The path must be escaped because : is a filter option separator.
    srt_escaped = _escape_filter_path(srt_path)
    parts.append(
        f"[vcat]subtitles='{srt_escaped}':force_style='{SUBTITLE_STYLE}'[vout]"
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
    srt_path: Path,
    output_dir: Path,
    music_track: Optional[Path] = None,
) -> Path:
    """
    Assemble a finished CipherPulse Short from its component files.

    Args:
        clip_paths:     Ordered list of stock footage MP4 paths from footage_downloader
        voiceover_path: Path to voiceover.mp3 from voice_generator
        srt_path:       Path to subtitles.srt from voice_generator
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
    log.info(f"Clips to use: {len(clip_paths)}")

    # ── Calculate per-clip segment duration ───────────────────────────────
    # Distribute voiceover time evenly across clips.
    # Add a small buffer (0.5s) so the last clip doesn't cut off early.
    seg_duration = (voice_duration + 0.5) / len(clip_paths)
    log.info(f"Segment duration per clip: {seg_duration:.2f}s")

    # ── Resolve music track ────────────────────────────────────────────────
    if music_track is None:
        music_track = _pick_music_track()

    # ── Build FFmpeg input list ────────────────────────────────────────────
    # Input order matters for index references in filter_complex:
    #   0..n-1  : video clip files (stream_loop -1 for looping)
    #   n       : voiceover MP3
    #   n+1     : music track (if present)
    cmd: list[str] = ["ffmpeg", "-y"]  # -y = overwrite output without asking

    # Add all clip inputs with stream_loop so short clips can fill longer slots
    for clip_path in clip_paths:
        cmd.extend(["-stream_loop", "-1", "-i", str(clip_path)])

    # Voiceover (no looping — it defines the total duration)
    cmd.extend(["-i", str(voiceover_path)])

    # Music track (stream_loop so short tracks fill the whole voiceover)
    voice_index = len(clip_paths)
    music_index: Optional[int] = None
    if music_track and music_track.exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(music_track)])
        music_index = len(clip_paths) + 1

    # ── Build filter complex ───────────────────────────────────────────────
    filter_complex, video_map, audio_map = _build_filter_complex(
        n_clips=len(clip_paths),
        seg_duration=seg_duration,
        srt_path=srt_path,
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
