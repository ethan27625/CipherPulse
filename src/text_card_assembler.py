"""
text_card_assembler.py — Pillow + FFmpeg text card Short assembler.

Produces 8-15 second vertical videos (1080×1920) from 3 slides:
  Slide 0 (HOOK):   ALL CAPS headline — most shocking specific fact
  Slide 1 (DETAIL): 2-3 sentence summary — white text, numbers in cyan
  Slide 2 (CTA):    Provocative question or follow prompt — cyan text

Each slide uses a background frame extracted from a Pexels video clip (via
footage_downloader), overlaid with a semi-transparent black mask so text
stays readable over any footage.

Slides are stitched with 0.5s crossfade transitions and background music at
30% volume (louder than voiceover videos since there is no voice to compete with).

Called by orchestrator.py when topic.format == 7 (Text Card).
"""

from __future__ import annotations

import logging
import random
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "text_card_assembler.log"),
    ],
)
log = logging.getLogger("text_card_assembler")

# ── Constants ─────────────────────────────────────────────────────────────────

SLIDE_W = 1080
SLIDE_H = 1920

# Safe zone: text stays in the center 80% of the frame width
SAFE_W   = int(SLIDE_W * 0.80)   # 864px
SAFE_X   = (SLIDE_W - SAFE_W) // 2  # 108px left margin

# How long each slide is displayed (seconds)
SLIDE_DURATIONS: list[float] = [4.0, 5.0, 4.0]
CROSSFADE_DURATION = 0.5
MUSIC_VOLUME       = 0.30   # louder than voiceover videos (0.22)

# Brand colours (exact hex from CLAUDE.md)
VOID_BLACK   = (6,   6,   9)
CYAN         = (0,   242, 234)   # #00F2EA
WHITE        = (232, 230, 227)   # #E8E6E3
OVERLAY_RGBA = (0,   0,   0,   153)  # 60% opacity black

# Font sizes per slide role
FONT_SIZE_HEADLINE = 66   # Slide 0 — large bold headline
FONT_SIZE_DETAIL   = 40   # Slide 1 — detail paragraph
FONT_SIZE_CTA      = 50   # Slide 2 — CTA / brand line

# Line height multiplier
LINE_SPACING_FACTOR = 1.35

# Regex: words that should render in CYAN on the detail slide
# Matches numbers, dollar amounts, percentages, large-number abbreviations
HIGHLIGHT_RE = re.compile(
    r"^\$?[\d][\d,. ]*[%BKMGT]?$"
    r"|^\d+[BKMGT]$"
    r"|^\$[\d][\d,.]*$",
    re.IGNORECASE,
)

ASSETS_DIR = Path(__file__).parent.parent / "assets"
FONTS_DIR  = ASSETS_DIR / "fonts"
MUSIC_DIR  = ASSETS_DIR / "music"

OSWALD_FONT    = FONTS_DIR / "Oswald-Variable.ttf"
BEBAS_FONT     = FONTS_DIR / "BebasNeue-Regular.ttf"


# ── Font loader ───────────────────────────────────────────────────────────────

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load Oswald Variable at the given size, fall back to Bebas Neue."""
    for font_path in (OSWALD_FONT, BEBAS_FONT):
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except Exception:
                continue
    log.warning("No custom font found — using Pillow default")
    return ImageFont.load_default()


# ── Background helpers ────────────────────────────────────────────────────────

def _extract_frame(clip_path: Path, output_path: Path, timestamp: float = 1.0) -> None:
    """
    Extract a single frame from a video clip at the given timestamp.

    Uses FFmpeg -ss for fast seeking. The frame is written as a PNG to
    output_path. This gives us a high-quality still to use as a slide
    background without any additional downloads.

    Args:
        clip_path:   Path to the source MP4 clip
        output_path: Where to write the extracted PNG frame
        timestamp:   Seek position in seconds (default 1.0 avoids black frames)
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(timestamp),
            "-i", str(clip_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Frame extraction failed for {clip_path.name}: {result.stderr[-300:]}")


def _resize_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    """
    Scale an image so it fills w×h exactly, center-cropping any excess.

    'Fill' scaling (as opposed to 'fit' scaling) ensures the entire canvas
    is covered — no letterboxing or pillarboxing. Used so our dark overlay
    and text always have a full-bleed background image underneath.
    """
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # Center crop
    left = (new_w - w) // 2
    top  = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


# ── Text rendering helpers ────────────────────────────────────────────────────

def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    Word-wrap text so each line fits within max_width pixels.

    Uses a throwaway 1×1 ImageDraw object to measure text widths without
    rendering anything. Returns a list of wrapped line strings.
    """
    tmp  = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(tmp)

    words   = text.split()
    lines:  list[str] = []
    current: list[str] = []

    for word in words:
        test = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def _line_width(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.FreeTypeFont) -> int:
    """Return the pixel width of a single text line."""
    bbox = draw.textbbox((0, 0), line, font=font)
    return bbox[2] - bbox[0]


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    """Return the pixel height of one line for the given font."""
    tmp  = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(tmp)
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


def _draw_shadow(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    shadow_offset: int = 3,
) -> None:
    """Draw text with a dark drop-shadow for readability over any background."""
    shadow = (0, 0, 0)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow)
    draw.text((x,                 y                ), text, font=font, fill=fill)


def _draw_centered_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    center_y: int,
) -> None:
    """
    Draw a block of pre-wrapped lines, centered horizontally and vertically.

    center_y is the vertical midpoint of the entire block. Lines are spaced
    at LINE_SPACING_FACTOR × font height.
    """
    lh     = _line_height(font)
    spacing = int(lh * LINE_SPACING_FACTOR)
    total_h = spacing * (len(lines) - 1) + lh
    start_y = center_y - total_h // 2

    for i, line in enumerate(lines):
        lw = _line_width(draw, line, font)
        x  = (SLIDE_W - lw) // 2
        y  = start_y + i * spacing
        _draw_shadow(draw, line, x, y, font, fill)


def _draw_detail_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    center_y: int,
) -> None:
    """
    Draw the detail slide text with per-word cyan highlighting for numbers.

    Renders word-by-word: each word is checked against HIGHLIGHT_RE and
    drawn in CYAN if it looks like a number/amount/percentage, WHITE otherwise.
    This makes key facts visually pop without any manual markup.
    """
    lh      = _line_height(font)
    spacing = int(lh * LINE_SPACING_FACTOR)
    total_h = spacing * (len(lines) - 1) + lh
    start_y = center_y - total_h // 2

    # We need a temporary draw to measure word widths
    for i, line in enumerate(lines):
        y = start_y + i * spacing

        # Measure the full line to compute centered start_x
        lw      = _line_width(draw, line, font)
        x       = (SLIDE_W - lw) // 2
        words   = line.split()

        for j, word in enumerate(words):
            is_highlight = bool(HIGHLIGHT_RE.match(word.strip(".,!?:;\"'-")))
            color = CYAN if is_highlight else WHITE
            _draw_shadow(draw, word, x, y, font, color)
            word_bbox = draw.textbbox((0, 0), word, font=font)
            space_bbox = draw.textbbox((0, 0), " ", font=font)
            x += (word_bbox[2] - word_bbox[0]) + (space_bbox[2] - space_bbox[0])


# ── Slide builders ────────────────────────────────────────────────────────────

def _make_slide_0(
    bg_frame: Path,
    headline: str,
    tmp_dir: Path,
) -> Path:
    """
    Slide 0 — HOOK: large ALL CAPS headline, centered on dark background.

    Layout:
      - Full-bleed background image (from footage clip)
      - 60% black overlay for text readability
      - Small cyan accent line at top (5px, 40% frame width)
      - Headline text: FONT_SIZE_HEADLINE, white, centered vertically
      - Small "CIPHERPULSE" watermark at bottom right (30% opacity)
    """
    img = _resize_fill(Image.open(bg_frame).convert("RGB"), SLIDE_W, SLIDE_H)

    # Dark overlay
    overlay = Image.new("RGBA", img.size, OVERLAY_RGBA)
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, overlay)
    img = img_rgba.convert("RGB")

    draw = ImageDraw.Draw(img)

    # Cyan accent bar at top
    bar_w  = int(SLIDE_W * 0.40)
    bar_h  = 5
    bar_x  = (SLIDE_W - bar_w) // 2
    bar_y  = 80
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=CYAN)

    # Headline text
    font  = _load_font(FONT_SIZE_HEADLINE)
    lines = _wrap_text(headline.upper(), font, SAFE_W)
    _draw_centered_block(draw, lines, font, WHITE, SLIDE_H // 2)

    # Watermark — "CIPHERPULSE" small at bottom right, low opacity
    wm_font = _load_font(28)
    wm_text = "CIPHERPULSE"
    wm_bbox = draw.textbbox((0, 0), wm_text, font=wm_font)
    wm_w    = wm_bbox[2] - wm_bbox[0]
    wm_x    = SLIDE_W - wm_w - 40
    wm_y    = SLIDE_H - 80
    # Semi-transparent watermark via a separate RGBA layer
    wm_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    wm_draw  = ImageDraw.Draw(wm_layer)
    wm_draw.text((wm_x, wm_y), wm_text, font=wm_font, fill=(*CYAN, 77))  # 30% opacity
    img = Image.alpha_composite(img.convert("RGBA"), wm_layer).convert("RGB")

    out = tmp_dir / "slide_0.png"
    img.save(out, "PNG")
    log.info(f"Slide 0 rendered: {out.name}")
    return out


def _make_slide_1(
    bg_frame: Path,
    detail: str,
    tmp_dir: Path,
) -> Path:
    """
    Slide 1 — DETAIL: 2-3 sentence paragraph, numbers highlighted in cyan.

    Layout:
      - Full-bleed background + 60% overlay
      - Detail text centered, FONT_SIZE_DETAIL, word-level cyan highlights
    """
    img = _resize_fill(Image.open(bg_frame).convert("RGB"), SLIDE_W, SLIDE_H)
    overlay = Image.new("RGBA", img.size, OVERLAY_RGBA)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    font  = _load_font(FONT_SIZE_DETAIL)
    lines = _wrap_text(detail, font, SAFE_W)
    _draw_detail_block(draw, lines, font, SLIDE_H // 2)

    out = tmp_dir / "slide_1.png"
    img.save(out, "PNG")
    log.info(f"Slide 1 rendered: {out.name}")
    return out


def _make_slide_2(
    bg_frame: Path,
    cta: str,
    tmp_dir: Path,
) -> Path:
    """
    Slide 2 — CTA: provocative question or brand prompt in CipherPulse cyan.

    Layout:
      - Full-bleed background + 60% overlay
      - CTA text: FONT_SIZE_CTA, CYAN color, centered vertically
      - Cyan accent bar at bottom (mirrors slide 0 top accent)
    """
    img = _resize_fill(Image.open(bg_frame).convert("RGB"), SLIDE_W, SLIDE_H)
    overlay = Image.new("RGBA", img.size, OVERLAY_RGBA)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    font  = _load_font(FONT_SIZE_CTA)
    lines = _wrap_text(cta, font, SAFE_W)
    _draw_centered_block(draw, lines, font, CYAN, SLIDE_H // 2)

    # Cyan accent bar at bottom
    bar_w = int(SLIDE_W * 0.40)
    bar_h = 5
    bar_x = (SLIDE_W - bar_w) // 2
    bar_y = SLIDE_H - 100
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=CYAN)

    out = tmp_dir / "slide_2.png"
    img.save(out, "PNG")
    log.info(f"Slide 2 rendered: {out.name}")
    return out


# ── Music selection ───────────────────────────────────────────────────────────

def _pick_music() -> Optional[Path]:
    """Return a random music track from assets/music/, or None if none exist."""
    tracks = list(MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        log.warning("No music tracks found in assets/music/ — building silent video")
        return None
    return random.choice(tracks)


# ── FFmpeg stitcher ───────────────────────────────────────────────────────────

def _stitch_slides(
    slide_pngs: list[Path],
    music_path: Optional[Path],
    output_path: Path,
    durations: list[float] = SLIDE_DURATIONS,
    crossfade: float = CROSSFADE_DURATION,
) -> None:
    """
    Combine slide PNGs into a video with crossfade transitions and music.

    FFmpeg approach:
      - Each PNG is fed as a still image input with -loop 1 -t <duration>
      - filter_complex chains pairs of slides through the xfade filter
      - xfade transition=fade, offset = cumulative duration minus crossfade
      - Music is trimmed to video length with a 1s fade-out at the end

    The xfade 'offset' parameter is the time in the OUTPUT stream where the
    transition starts — it must equal the sum of all previous slide durations
    minus any prior crossfade durations, minus the crossfade duration itself.

    Example for 3 slides [4.0, 5.0, 4.0] with 0.5s crossfade:
      xfade #1: offset = 4.0 - 0.5 = 3.5  (start fading at t=3.5)
      xfade #2: offset = (4.0 + 5.0 - 0.5) - 0.5 = 8.0
      Total duration = 4.0 + 5.0 + 4.0 - 2×0.5 = 12.0s
    """
    n = len(slide_pngs)
    assert n == len(durations), "slide_pngs and durations must have equal length"

    # Build ffmpeg input arguments: -loop 1 -t <dur> -i <path> for each slide
    input_args: list[str] = []
    for i, (png, dur) in enumerate(zip(slide_pngs, durations)):
        input_args += ["-loop", "1", "-t", str(dur), "-i", str(png)]

    # Music input (optional)
    has_music = music_path is not None
    if has_music:
        input_args += ["-i", str(music_path)]

    # Compute total video duration
    total_dur = sum(durations) - crossfade * (n - 1)
    fade_out_start = max(0.0, total_dur - 1.0)

    # Build filter_complex
    # Step 1: scale + fps each slide stream
    scale_filters = []
    for i in range(n):
        scale_filters.append(
            f"[{i}:v]scale={SLIDE_W}:{SLIDE_H}:force_original_aspect_ratio=increase,"
            f"crop={SLIDE_W}:{SLIDE_H},setsar=1,fps=30[v{i}]"
        )

    # Step 2: chain xfade filters
    # xfade_filters[k] connects [left][right] → [xk]
    xfade_filters = []
    offsets: list[float] = []
    cumulative = durations[0]
    for k in range(1, n):
        offset = cumulative - crossfade
        offsets.append(offset)
        left  = f"[x{k - 1}]" if k > 1 else f"[v0]"
        right = f"[v{k}]"
        out   = f"[vout]" if k == n - 1 else f"[x{k}]"
        xfade_filters.append(
            f"{left}{right}xfade=transition=fade:duration={crossfade}:offset={offset:.3f}{out}"
        )
        cumulative += durations[k] - crossfade

    # Step 3: audio filter (trim + fade out)
    audio_filters = []
    if has_music:
        music_idx = n  # music is the last input
        audio_filters.append(
            f"[{music_idx}:a]volume={MUSIC_VOLUME},"
            f"afade=t=out:st={fade_out_start:.3f}:d=1.0[aout]"
        )

    # Assemble full filter_complex string
    all_filters = scale_filters + xfade_filters + audio_filters
    filter_complex = "; ".join(all_filters)

    # Build the full ffmpeg command
    cmd = ["ffmpeg", "-y"] + input_args + [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if has_music:
        cmd += ["-map", "[aout]"]
    cmd += [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-r", "30",
    ]
    if has_music:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-shortest", str(output_path)]

    log.info(f"FFmpeg stitching {n} slides → {output_path.name} ({total_dur:.1f}s)")
    log.debug(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"FFmpeg stderr:\n{result.stderr[-1000:]}")
        raise RuntimeError(f"FFmpeg text card stitch failed (exit {result.returncode})")

    log.info(f"Text card assembled: {output_path.name} ({output_path.stat().st_size / 1e6:.1f} MB)")


# ── Public entrypoint ─────────────────────────────────────────────────────────

def assemble_text_card(
    headline: str,
    detail: str,
    cta: str,
    clip_paths: list[Path],
    output_dir: Path,
) -> Path:
    """
    Assemble a text card Short from 3 slide backgrounds and text content.

    This function:
      1. Extracts a still frame from each clip (one per slide)
      2. Renders each slide as a 1080×1920 PNG using Pillow
      3. Stitches the slides with FFmpeg xfade + background music
      4. Saves slide_0.png to output_dir (used as thumbnail by orchestrator)

    Args:
        headline:   Slide 0 text — ALL CAPS headline (under 12 words)
        detail:     Slide 1 text — 2-3 sentences with numbers highlighted cyan
        cta:        Slide 2 text — provocative question or follow prompt
        clip_paths: At least 3 video clip paths for slide backgrounds.
                    If fewer than 3, available clips are cycled.
        output_dir: Where to write video.mp4 and slide_0.png

    Returns:
        Path to the assembled video.mp4

    Raises:
        RuntimeError: If FFmpeg fails or no clips are available.
    """
    if not clip_paths:
        raise RuntimeError("assemble_text_card requires at least one clip path")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Cycle clips if we have fewer than 3
    while len(clip_paths) < 3:
        clip_paths = clip_paths * 2
    bg_clips = clip_paths[:3]

    music_path = _pick_music()

    with tempfile.TemporaryDirectory(prefix="cipherpulse_tc_") as tmp_str:
        tmp_dir = Path(tmp_str)

        # Extract one frame per clip
        frame_paths: list[Path] = []
        for i, clip in enumerate(bg_clips):
            frame_path = tmp_dir / f"bg_frame_{i}.png"
            try:
                _extract_frame(clip, frame_path)
            except RuntimeError as e:
                log.warning(f"Frame extraction failed for clip {i} ({clip.name}): {e} — using fallback")
                # Fallback: solid void-black background
                fallback = Image.new("RGB", (SLIDE_W, SLIDE_H), VOID_BLACK)
                fallback.save(frame_path, "PNG")
            frame_paths.append(frame_path)

        # Render slides
        slides = [
            _make_slide_0(frame_paths[0], headline, tmp_dir),
            _make_slide_1(frame_paths[1], detail,   tmp_dir),
            _make_slide_2(frame_paths[2], cta,       tmp_dir),
        ]

        # Copy slide 0 to output_dir as thumbnail source
        import shutil
        shutil.copy2(slides[0], output_dir / "slide_0.png")
        log.info(f"Slide 0 saved to output_dir for thumbnail use")

        # Stitch into video
        video_path = output_dir / "video.mp4"
        _stitch_slides(slides, music_path, video_path)

    return video_path
