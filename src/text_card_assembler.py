"""
text_card_assembler.py — Premium news-card Short assembler (v2).

Architecture:
  1. Pillow renders each slide as a complete static 1080×1920 PNG
     (background frame + 65% dark overlay + styled text in one pass).
  2. FFmpeg applies Ken Burns zoom (zoompan z=1.0→1.05) to slides 0 and 1
     to add cinematic motion.  Slide 2 is a static void-black brand card.
  3. Three .mp4 clips are stitched with FFmpeg xfade crossfades + music.

Why Pillow for text composition (not FFmpeg drawtext):
  The premium look requires per-word color switching (numbers/names in cyan),
  letter-spaced category labels, and multi-line wrapping with precise shadows.
  FFmpeg drawtext cannot switch color mid-line. Pillow handles this cleanly.
  The Ken Burns motion is pure FFmpeg — it applies to the entire composited
  frame, giving the cinematic effect without needing text to be a separate layer.

Slide structure:
  Slide 0 (HOOK)  — 4s: [category label] · [separator] · [ALL CAPS headline]
  Slide 1 (DETAIL)— 5s: [detail paragraph, numbers in cyan] · [source attribution]
  Slide 2 (CTA)   — 3s: [decorative lines] · [Follow @CipherPulse] · [tagline]
  Total: 4+5+3 - 2×0.5s crossfade = 11 seconds.

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

# ── Dimensions ────────────────────────────────────────────────────────────────
SLIDE_W = 1080
SLIDE_H = 1920
SAFE_W  = int(SLIDE_W * 0.82)          # 885px — text stays inside this width
SAFE_X  = (SLIDE_W - SAFE_W) // 2      # 97px left/right margin

# ── Timing ────────────────────────────────────────────────────────────────────
SLIDE_DURATIONS    = [4.0, 5.0, 3.0]   # seconds per slide (total = 12s raw)
CROSSFADE_DURATION = 0.5               # seconds for xfade transition
FPS                = 30
ZOOM_START         = 1.00              # Ken Burns start (full frame)
ZOOM_END           = 1.05              # Ken Burns end   (5% zoom in)
MUSIC_VOLUME       = 0.35              # louder than voiceover — no voice to compete

# ── Brand colours ─────────────────────────────────────────────────────────────
VOID_BLACK  = (6,   6,   9)            # #060609
CYAN        = (0,   242, 234)          # #00F2EA
WHITE       = (232, 230, 227)          # #E8E6E3
BLACK       = (0,   0,   0)
OVERLAY_A   = int(0.65 * 255)          # 65% black overlay = 166/255
CYAN_40     = (0,   242, 234, 102)     # cyan at 40% opacity (separator line)
WHITE_50    = (232, 230, 227, 128)     # white at 50% opacity (source text)

# ── Font sizes ────────────────────────────────────────────────────────────────
FONT_CATEGORY  = 24
FONT_HEADLINE  = 68
FONT_DETAIL    = 36
FONT_SOURCE    = 18
FONT_CTA_MAIN  = 62
FONT_CTA_SUB   = 28

# ── Regex: highlight words that should appear in CYAN ─────────────────────────
# Matches: $35M, 153, 18,000, 60%, 2013, $800, 1.5B, etc.
HIGHLIGHT_RE = re.compile(
    r"^\$?[\d][\d,. ]*[%BKMGT]?$"
    r"|^\d+[BKMGT]$"
    r"|^\$[\d][\d,.]*$",
    re.IGNORECASE,
)

# ── Asset paths ───────────────────────────────────────────────────────────────
ASSETS_DIR  = Path(__file__).parent.parent / "assets"
FONTS_DIR   = ASSETS_DIR / "fonts"
MUSIC_DIR   = ASSETS_DIR / "music"
OSWALD_FONT = FONTS_DIR / "Oswald-Variable.ttf"
BEBAS_FONT  = FONTS_DIR / "BebasNeue-Regular.ttf"


# ── Font loader ───────────────────────────────────────────────────────────────

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """
    Load Oswald Variable at the given size.

    For headlines (bold=True) we try to set the wght axis to 700 (Bold).
    For body text (bold=False) the default axis value (~400 Regular) is used.
    Falls back to Bebas Neue, then Pillow's built-in default.
    """
    if OSWALD_FONT.exists():
        font = ImageFont.truetype(str(OSWALD_FONT), size=size)
        if bold:
            for method, arg in [
                ("set_variation_by_axes", {"wght": 700}),
                ("set_variation_by_name", "Bold"),
            ]:
                try:
                    getattr(font, method)(arg)
                    break
                except Exception:
                    continue
        return font
    if BEBAS_FONT.exists():
        return ImageFont.truetype(str(BEBAS_FONT), size=size)
    log.warning("No custom font found — using Pillow default (text will look basic)")
    return ImageFont.load_default()


# ── Pillow helpers ────────────────────────────────────────────────────────────

def _resize_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and center-crop img so it fills w×h with no letterboxing."""
    sw, sh = img.size
    scale  = max(w / sw, h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - w) // 2
    top    = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _make_draw(size: tuple[int, int] = (SLIDE_W, SLIDE_H)) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Create a blank RGBA canvas and its Draw object."""
    img  = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    return img, draw


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Word-wrap text to fit within max_w pixels. Returns list of line strings."""
    tmp  = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(tmp)
    words, lines, cur = text.split(), [], []
    for word in words:
        test = " ".join(cur + [word])
        if draw.textbbox((0, 0), test, font=font)[2] > max_w and cur:
            lines.append(" ".join(cur))
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(" ".join(cur))
    return lines


def _lw(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    """Pixel width of a text string."""
    return draw.textbbox((0, 0), text, font=font)[2]


def _lh(font: ImageFont.FreeTypeFont) -> int:
    """Pixel height of one line for the given font."""
    tmp  = Image.new("RGB", (1, 1))
    d    = ImageDraw.Draw(tmp)
    b    = d.textbbox((0, 0), "Ag", font=font)
    return b[3] - b[1]


def _shadow(draw: ImageDraw.ImageDraw, text: str, x: int, y: int,
            font: ImageFont.FreeTypeFont, fill: tuple, offset: int = 3) -> None:
    """Draw text with a dark drop-shadow at pixel offset."""
    draw.text((x + offset, y + offset), text, font=font, fill=(0, 0, 0, 220))
    draw.text((x,           y          ), text, font=font, fill=fill)


def _block_height(lines: int, font: ImageFont.FreeTypeFont, spacing: float = 1.35) -> int:
    """Total pixel height of a multi-line text block."""
    h = _lh(font)
    return int(h + (lines - 1) * h * spacing)


def _draw_centered_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    center_y: int,
    spacing: float = 1.35,
) -> None:
    """Draw pre-wrapped lines centered horizontally, vertically around center_y."""
    h       = _lh(font)
    step    = int(h * spacing)
    total_h = h + (len(lines) - 1) * step
    y       = center_y - total_h // 2
    for line in lines:
        x = (SLIDE_W - _lw(draw, line, font)) // 2
        _shadow(draw, line, x, y, font, fill)
        y += step


def _draw_highlighted_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    center_y: int,
    spacing: float = 1.35,
) -> None:
    """
    Draw a text block with per-word cyan highlighting for numbers.

    Each word is checked against HIGHLIGHT_RE.  Numbers, dollar amounts,
    and percentages render in CYAN; all other words render in WHITE.
    Words are drawn individually to track x-position; the line is
    pre-measured to compute the centering start_x.
    """
    h       = _lh(font)
    step    = int(h * spacing)
    total_h = h + (len(lines) - 1) * step
    start_y = center_y - total_h // 2
    space_w = _lw(draw, " ", font)

    for i, line in enumerate(lines):
        y       = start_y + i * step
        line_w  = _lw(draw, line, font)
        x       = (SLIDE_W - line_w) // 2
        for j, word in enumerate(line.split()):
            is_number = bool(HIGHLIGHT_RE.match(word.strip(".,!?:;\"'-")))
            color     = (*CYAN, 255) if is_number else (*WHITE, 255)
            _shadow(draw, word, x, y, font, color)
            x += _lw(draw, word, font) + space_w


def _draw_letter_spaced(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    center_x: int,
    y: int,
    spacing: int = 5,
) -> None:
    """
    Draw text with extra letter spacing, centered around center_x.

    Pillow has no native letter-spacing.  We measure each character,
    compute the total width, then draw char-by-char with `spacing` px gap.
    """
    char_widths = [_lw(draw, c, font) for c in text]
    total_w     = sum(char_widths) + spacing * (len(text) - 1)
    x           = center_x - total_w // 2
    for char, cw in zip(text, char_widths):
        _shadow(draw, char, x, y, font, fill, offset=2)
        x += cw + spacing


# ── Background preparation ────────────────────────────────────────────────────

def _extract_frame(clip_path: Path, out_path: Path, t: float = 1.0) -> None:
    """Extract one frame from clip_path at timestamp t into out_path (PNG)."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path),
         "-frames:v", "1", "-q:v", "2", str(out_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Frame extract failed ({clip_path.name}): {r.stderr[-300:]}")


def _composited_background(frame_path: Path) -> Image.Image:
    """Load a clip frame, fill-crop to 1080×1920, apply the 65% dark overlay."""
    bg  = _resize_fill(Image.open(frame_path).convert("RGBA"), SLIDE_W, SLIDE_H)
    ovl = Image.new("RGBA", (SLIDE_W, SLIDE_H), (0, 0, 0, OVERLAY_A))
    return Image.alpha_composite(bg, ovl)


# ── Per-slide PNG renderers ───────────────────────────────────────────────────

def _render_slide0(bg_frame: Path, headline: str, category: str) -> Image.Image:
    """
    Slide 0 — HOOK.

    Layout (top → bottom):
      • Category label  — cyan, 24 px, letter-spaced, ALL CAPS
      • Thin separator  — 200 px wide cyan line, 40 % opacity
      • Headline        — 68 px Oswald Bold, ALL CAPS, white/cyan on numbers
                          centred vertically in the frame

    The whole three-part block (category + sep + headline) is vertically
    centred together so the composition feels balanced.
    """
    base      = _composited_background(bg_frame)
    overlay, draw = _make_draw()

    cat_font  = _load_font(FONT_CATEGORY)
    hed_font  = _load_font(FONT_HEADLINE, bold=True)

    cat_text  = category.upper()
    hed_lines = _wrap(headline.upper(), hed_font, SAFE_W)

    # Measure heights
    cat_h     = _lh(cat_font)
    sep_gap   = 18          # px between category and separator
    sep_hed   = 24          # px between separator and headline
    hed_h     = _block_height(len(hed_lines), hed_font)

    total_h   = cat_h + sep_gap + 2 + sep_hed + hed_h
    block_top = SLIDE_H // 2 - total_h // 2  # vertically centred

    # ─ Category label ─────────────────────────────────────────────────────────
    _draw_letter_spaced(draw, cat_text, cat_font, (*CYAN, 255),
                        SLIDE_W // 2, block_top, spacing=5)

    # ─ Separator line ─────────────────────────────────────────────────────────
    sep_y  = block_top + cat_h + sep_gap
    sep_w  = 200
    sep_x  = (SLIDE_W - sep_w) // 2
    draw.rectangle([sep_x, sep_y, sep_x + sep_w, sep_y + 2], fill=CYAN_40)

    # ─ Headline ───────────────────────────────────────────────────────────────
    hed_top    = sep_y + 2 + sep_hed
    hed_center = hed_top + hed_h // 2
    _draw_highlighted_block(draw, hed_lines, hed_font, hed_center)

    base.alpha_composite(overlay)
    return base.convert("RGB")


def _render_slide1(bg_frame: Path, detail: str, source: str) -> Image.Image:
    """
    Slide 1 — DETAIL.

    Layout:
      • Detail paragraph — 36 px Oswald, white with numbers in cyan,
                           vertically centred slightly above midpoint
      • Source credit    — 18 px, 50 % opacity white, near bottom
    """
    base          = _composited_background(bg_frame)
    overlay, draw = _make_draw()

    det_font = _load_font(FONT_DETAIL)
    src_font = _load_font(FONT_SOURCE)

    det_lines = _wrap(detail, det_font, SAFE_W)
    det_center = int(SLIDE_H * 0.47)   # slightly above centre for visual balance

    _draw_highlighted_block(draw, det_lines, det_font, det_center)

    # Source attribution
    src_text = f"SOURCE: {source.upper()}"
    src_x    = (SLIDE_W - _lw(draw, src_text, src_font)) // 2
    src_y    = SLIDE_H - 140
    draw.text((src_x + 2, src_y + 2), src_text, font=src_font, fill=(0, 0, 0, 160))
    draw.text((src_x,     src_y    ), src_text, font=src_font, fill=WHITE_50)

    base.alpha_composite(overlay)
    return base.convert("RGB")


def _render_slide2() -> Image.Image:
    """
    Slide 2 — BRAND CTA.

    Fixed content on void-black background:
      • Decorative cyan line (top)
      • "Follow @CipherPulse"  — large cyan text
      • "Daily Cyber Threats & AI News"  — small white text
      • Decorative cyan line (bottom)

    No background image; no Ken Burns (brand slide is deliberately static).
    """
    img, draw = _make_draw()

    # Void-black fill
    draw.rectangle([0, 0, SLIDE_W, SLIDE_H], fill=(*VOID_BLACK, 255))

    main_font = _load_font(FONT_CTA_MAIN, bold=True)
    sub_font  = _load_font(FONT_CTA_SUB)

    main_text = "Follow @CipherPulse"
    sub_text  = "Daily Cyber Threats & AI News"

    main_h = _lh(main_font)
    sub_h  = _lh(sub_font)
    gap    = 28
    line_h = 3
    line_w = 280
    line_gap_outer = 55   # gap between outer lines and text block

    total_h  = line_h + line_gap_outer + main_h + gap + sub_h + line_gap_outer + line_h
    block_y  = SLIDE_H // 2 - total_h // 2

    lx = (SLIDE_W - line_w) // 2

    # Top line
    draw.rectangle([lx, block_y, lx + line_w, block_y + line_h], fill=(*CYAN, 255))

    # "Follow @CipherPulse"
    my = block_y + line_h + line_gap_outer
    mx = (SLIDE_W - _lw(draw, main_text, main_font)) // 2
    _shadow(draw, main_text, mx, my, main_font, (*CYAN, 255), offset=3)

    # Tagline
    sy = my + main_h + gap
    sx = (SLIDE_W - _lw(draw, sub_text, sub_font)) // 2
    _shadow(draw, sub_text, sx, sy, sub_font, (*WHITE, 255), offset=2)

    # Bottom line
    by2 = sy + sub_h + line_gap_outer
    draw.rectangle([lx, by2, lx + line_w, by2 + line_h], fill=(*CYAN, 255))

    return img.convert("RGB")


# ── FFmpeg video generators ───────────────────────────────────────────────────

def _make_motion_clip(slide_img: Image.Image, duration: float, out: Path) -> None:
    """
    Produce a video clip from a still image using FFmpeg zoompan (Ken Burns).

    Zoompan explanation:
      The filter takes a still (or video) input and produces N output frames,
      where each frame shows a progressively smaller viewport of the source —
      effectively zooming in.  The key parameters:

        z  = zoom level per frame.  We use min(zoom + inc, ZOOM_END) so zoom
             increases from 1.0 to 1.05 linearly over the clip duration, then
             stays capped at 1.05.  `zoom` refers to the PREVIOUS frame's value.
        x  = top-left x of the crop window in the SOURCE frame.
             iw/2 - iw/zoom/2  centres the crop as zoom grows.
        y  = same vertically.
        d  = total output frames (must match input length exactly).
        s  = output frame size.
        fps= output framerate.

    With a 1080×1920 source at z=1.0: x=0, y=0 (full frame shown).
    At z=1.05: crop window is 1029×1829, centred at (25.7, 45.7) — 5% zoomed in.

    The source image is pre-rendered at exactly 1080×1920 by Pillow, so no
    additional scaling step is needed inside the filter graph.
    """
    n   = int(duration * FPS)
    inc = (ZOOM_END - ZOOM_START) / max(n, 1)

    # Write the slide PNG to a temp file so FFmpeg can read it
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        slide_img.save(tmp_path, "PNG")

        vf = (
            f"zoompan="
            f"z='min(zoom+{inc:.8f},{ZOOM_END})':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={n}:s={SLIDE_W}x{SLIDE_H}:fps={FPS},"
            f"format=yuv420p"
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FPS), "-t", str(duration),
            "-i", str(tmp_path),
            "-vf", vf,
            "-c:v", "libx264", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", str(duration),
            str(out),
        ]
        log.debug(f"zoompan cmd: {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log.error(f"zoompan stderr:\n{r.stderr[-800:]}")
            raise RuntimeError(f"Ken Burns clip failed (exit {r.returncode})")
    finally:
        tmp_path.unlink(missing_ok=True)


def _make_static_clip(slide_img: Image.Image, duration: float, out: Path) -> None:
    """
    Produce a static video clip (no motion) from a still image.
    Used for Slide 2 (brand CTA) which intentionally has no Ken Burns.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        slide_img.save(tmp_path, "PNG")
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FPS), "-t", str(duration),
            "-i", str(tmp_path),
            "-vf", f"scale={SLIDE_W}:{SLIDE_H},format=yuv420p",
            "-c:v", "libx264", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", str(duration),
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Static clip failed (exit {r.returncode}): {r.stderr[-300:]}")
    finally:
        tmp_path.unlink(missing_ok=True)


# ── Music picker ──────────────────────────────────────────────────────────────

def _pick_music() -> Optional[Path]:
    """Return a random track from assets/music/, or None if the dir is empty."""
    tracks = list(MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        log.warning("No music in assets/music/ — producing silent video")
        return None
    return random.choice(tracks)


# ── FFmpeg xfade stitcher ─────────────────────────────────────────────────────

def _stitch(clips: list[Path], music: Optional[Path], output: Path) -> None:
    """
    Concatenate clip videos with crossfade transitions and background music.

    xfade offset calculation:
      Each xfade starts just before the current clip ends.  For clips with
      durations [4, 5, 3] and crossfade=0.5:

        xfade #0→#1: offset = 4.0 − 0.5 = 3.5 s
        xfade #1→#2: offset = (4.0 + 5.0 − 0.5) − 0.5 = 8.0 s
        total output = 4 + 5 + 3 − 2×0.5 = 11.0 s

    Audio: music is trimmed to the total duration with a 1s fade-out at the end.
    """
    n     = len(clips)
    total = sum(SLIDE_DURATIONS) - CROSSFADE_DURATION * (n - 1)
    fade  = max(0.0, total - 1.0)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    if music:
        inputs += ["-i", str(music)]

    # Build xfade chain
    xfade_parts: list[str] = []
    cumulative = SLIDE_DURATIONS[0]
    for k in range(1, n):
        offset = cumulative - CROSSFADE_DURATION
        left   = "[0:v]"  if k == 1 else f"[x{k - 1}]"
        right  = f"[{k}:v]"
        out_   = "[vout]" if k == n - 1 else f"[x{k}]"
        xfade_parts.append(
            f"{left}{right}xfade=transition=fade:"
            f"duration={CROSSFADE_DURATION}:offset={offset:.3f}{out_}"
        )
        cumulative += SLIDE_DURATIONS[k] - CROSSFADE_DURATION

    parts = list(xfade_parts)
    if music:
        parts.append(
            f"[{n}:a]volume={MUSIC_VOLUME},"
            f"afade=t=out:st={fade:.3f}:d=1.0[aout]"
        )

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", "; ".join(parts),
        "-map", "[vout]",
    ]
    if music:
        cmd += ["-map", "[aout]"]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac",     "-b:a", "128k",
        "-shortest", str(output),
    ]

    log.info(f"Stitching {n} clips → {output.name}  ({total:.1f}s total)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"Stitch stderr:\n{r.stderr[-800:]}")
        raise RuntimeError(f"Stitch failed (exit {r.returncode})")

    log.info(f"Final video: {output.name}  ({output.stat().st_size / 1e6:.1f} MB)")


# ── Public entrypoint ─────────────────────────────────────────────────────────

def assemble_text_card(
    headline:   str,
    detail:     str,
    cta:        str,
    category:   str,
    source:     str,
    clip_paths: list[Path],
    output_dir: Path,
) -> Path:
    """
    Assemble a premium news-card Short from text content + stock footage.

    Steps:
      1. Extract a still frame from each of the first 2 clips (slide 0, 1 bgs).
      2. Render each slide as a 1080×1920 RGB PNG using Pillow.
      3. Apply Ken Burns zoompan to slides 0 and 1 → .mp4 clips.
         Slide 2 is a static brand card — no Ken Burns.
      4. Save slide_0.png to output_dir for thumbnail generation.
      5. Stitch 3 clips with FFmpeg xfade + background music.

    Args:
        headline:   Slide 0 — ALL CAPS hook text (under 12 words)
        detail:     Slide 1 — 2-3 sentence summary
        cta:        Kept for engagement comment; not rendered on any slide
        category:   Slide 0 label — e.g. "DATA BREACH", "AI NEWS"
        source:     Slide 1 attribution — e.g. "Adobe Official Reports"
        clip_paths: At least 2 stock video clips for slide backgrounds
        output_dir: Where to write video.mp4 and slide_0.png

    Returns:
        Path to video.mp4

    Raises:
        RuntimeError: If FFmpeg fails or no clips are provided.
    """
    if not clip_paths:
        raise RuntimeError("assemble_text_card requires at least one clip path")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Ensure we have 2 distinct bg clips (cycle if needed)
    bg_clips = list(clip_paths[:2])
    while len(bg_clips) < 2:
        bg_clips.append(bg_clips[0])

    music = _pick_music()

    with tempfile.TemporaryDirectory(prefix="cp_tc_") as tmp_str:
        tmp = Path(tmp_str)

        # ── Extract background frames ──────────────────────────────────────────
        frames: list[Path] = []
        for i, clip in enumerate(bg_clips):
            fp = tmp / f"bg_{i}.png"
            try:
                _extract_frame(clip, fp)
            except RuntimeError as e:
                log.warning(f"Frame extract failed for clip {i}: {e} — using black bg")
                Image.new("RGB", (SLIDE_W, SLIDE_H), VOID_BLACK).save(fp, "PNG")
            frames.append(fp)

        # ── Render slide images (Pillow) ───────────────────────────────────────
        log.info("Rendering slide 0 (HOOK)…")
        s0_img = _render_slide0(frames[0], headline, category)

        log.info("Rendering slide 1 (DETAIL)…")
        s1_img = _render_slide1(frames[1], detail, source)

        log.info("Rendering slide 2 (BRAND CTA)…")
        s2_img = _render_slide2()

        # ── Save slide_0.png for thumbnail ────────────────────────────────────
        s0_img.save(output_dir / "slide_0.png", "PNG")
        log.info("slide_0.png saved for thumbnail generation")

        # ── Produce per-slide video clips (FFmpeg) ────────────────────────────
        clip_paths_out: list[Path] = []

        log.info("Encoding slide 0 with Ken Burns zoom…")
        c0 = tmp / "clip_0.mp4"
        _make_motion_clip(s0_img, SLIDE_DURATIONS[0], c0)
        clip_paths_out.append(c0)

        log.info("Encoding slide 1 with Ken Burns zoom…")
        c1 = tmp / "clip_1.mp4"
        _make_motion_clip(s1_img, SLIDE_DURATIONS[1], c1)
        clip_paths_out.append(c1)

        log.info("Encoding slide 2 (static brand card)…")
        c2 = tmp / "clip_2.mp4"
        _make_static_clip(s2_img, SLIDE_DURATIONS[2], c2)
        clip_paths_out.append(c2)

        # ── Stitch with xfade + music ─────────────────────────────────────────
        video_out = output_dir / "video.mp4"
        _stitch(clip_paths_out, music, video_out)

    return video_out
