"""
thumbnail_creator.py — Branded CipherPulse thumbnail generator.

Produces a 1280×720 JPEG thumbnail following the CipherPulse formula:
  Layer 1: Void-black (#060609) background + subtle grid pattern at 2% opacity
  Layer 2: Small cyan accent text at top — category tag or hook keyword
  Layer 3: Main title split into 2 lines (Line 1 white, Line 2 cyan), bold, centered
  Layer 4: "CIPHERPULSE" watermark in bottom-right at 30% opacity

Fonts: Bebas Neue (titles) + Montserrat ExtraBold (accent text).
       Downloaded from Google Fonts GitHub mirror on first run.
       Falls back to DejaVu Sans Bold if download fails.

Output: thumbnail.png at 1280×720 (YouTube's recommended thumbnail resolution).
        PNG is lossless during pipeline; uploaders may convert to JPEG at delivery.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "thumbnail_creator.log"),
    ],
)
log = logging.getLogger("thumbnail_creator")

# ── Canvas dimensions ─────────────────────────────────────────────────────────
THUMB_W = 1280
THUMB_H = 720

# ── Brand colours ─────────────────────────────────────────────────────────────
# All as (R, G, B) tuples for Pillow. RGBA variants include alpha channel.
COLOR_BG          = (6,   6,   9)         # Void Black  #060609
COLOR_CYAN        = (0,   242, 234)        # Primary Cyan #00F2EA
COLOR_DEEP_BLUE   = (0,   119, 182)        # Deep Blue   #0077B6
COLOR_WHITE       = (255, 255, 255)
COLOR_LIGHT_TEXT  = (232, 230, 227)        # #E8E6E3
COLOR_GRID        = (0,   242, 234, 5)     # Cyan grid at 2% opacity (RGBA)
COLOR_WATERMARK   = (232, 230, 227, 77)    # 30% opacity (255 * 0.30 ≈ 77)

# ── Font paths & download URLs ────────────────────────────────────────────────
FONTS_DIR = Path(__file__).parent.parent / "assets" / "fonts"

# Google Fonts — raw.githubusercontent.com URLs (stable direct download, no redirect)
# Variable fonts (.ttf with [wght] in name) contain all weights in one file.
# PIL/FreeType loads these exactly like static fonts.
FONT_DOWNLOADS: dict[str, str] = {
    "BebasNeue-Regular.ttf": (
        "https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/BebasNeue-Regular.ttf"
    ),
    # Oswald variable font — used for accent label (bold, condensed, very readable small)
    "Oswald-Variable.ttf": (
        "https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/Oswald%5Bwght%5D.ttf"
    ),
}

# System fallback if download fails — universally available on Ubuntu
SYSTEM_FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


# ── Font loading ───────────────────────────────────────────────────────────────

def _download_fonts() -> None:
    """
    Download CipherPulse brand fonts from Google Fonts GitHub mirror.

    Only downloads files that don't already exist in assets/fonts/.
    GitHub raw content URLs are stable — they point to specific file paths
    in the repository tree, not release artifacts, so they won't break on
    new font versions unless Google renames the file.

    Why not use pip install or apt install?
    Some Google Fonts aren't packaged for apt (Bebas Neue in particular).
    Downloading the TTF directly is the most reliable cross-platform approach.
    """
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    for filename, url in FONT_DOWNLOADS.items():
        dest = FONTS_DIR / filename
        if dest.exists():
            log.debug(f"Font already cached: {filename}")
            continue

        log.info(f"Downloading font: {filename}")
        try:
            # urllib.request.urlretrieve downloads a URL to a local file.
            # It's simpler than requests for single-file downloads with no auth.
            urllib.request.urlretrieve(url, dest)
            log.info(f"Font saved: {dest} ({dest.stat().st_size // 1024} KB)")
        except Exception as e:
            log.warning(f"Font download failed for {filename}: {e}")
            log.warning(f"Will use system fallback font: {SYSTEM_FONT_FALLBACK}")


def _load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    """
    Load a TrueType font at the given pixel size.

    Tries our downloaded font first, falls back to the system DejaVu Bold.
    ImageFont.truetype() raises OSError if the file doesn't exist or is corrupt.

    Args:
        filename: Font filename in assets/fonts/ (e.g. "BebasNeue-Regular.ttf")
        size:     Font size in pixels (not points — Pillow uses pixels)

    Returns:
        A loaded FreeTypeFont object ready for use with ImageDraw.
    """
    primary = FONTS_DIR / filename
    if primary.exists():
        try:
            return ImageFont.truetype(str(primary), size)
        except OSError as e:
            log.warning(f"Could not load {filename}: {e}")

    # Fallback
    try:
        return ImageFont.truetype(SYSTEM_FONT_FALLBACK, size)
    except OSError:
        log.warning("System fallback font not found — using Pillow default bitmap font")
        return ImageFont.load_default()


# ── Drawing helpers ───────────────────────────────────────────────────────────

def _draw_grid_overlay(canvas: Image.Image) -> Image.Image:
    """
    Draw a subtle cyan grid pattern at 2% opacity over the background.

    Why a grid? It's a cyberpunk aesthetic cue — the matrix-like grid
    subconsciously signals "digital" and "technical" without being distracting.
    At 2% opacity it's almost subliminal — present on close inspection but
    not competing with the text.

    Implementation:
    - Create a transparent RGBA overlay image the same size as the canvas
    - Draw horizontal and vertical lines every GRID_SPACING pixels in cyan
    - Alpha-composite onto the canvas using the overlay's alpha channel

    Alpha compositing formula: output = src * src_alpha + dst * (1 - src_alpha)
    At alpha=5/255 ≈ 2%, the grid barely tints the background.
    """
    GRID_SPACING = 60  # pixels between grid lines
    GRID_LINE_WIDTH = 1

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Draw vertical lines
    for x in range(0, THUMB_W, GRID_SPACING):
        draw.line([(x, 0), (x, THUMB_H)], fill=COLOR_GRID, width=GRID_LINE_WIDTH)

    # Draw horizontal lines
    for y in range(0, THUMB_H, GRID_SPACING):
        draw.line([(0, y), (THUMB_W, y)], fill=COLOR_GRID, width=GRID_LINE_WIDTH)

    # Convert canvas to RGBA for compositing, merge, convert back to RGB
    canvas_rgba = canvas.convert("RGBA")
    composited = Image.alpha_composite(canvas_rgba, overlay)
    return composited.convert("RGB")


def _draw_gradient_bar(canvas: Image.Image) -> Image.Image:
    """
    Draw a subtle bottom gradient bar (deep blue → transparent, 15% opacity).

    This visually separates the watermark text area from the main content
    without a hard edge — a technique used by news chyrons and video players.
    """
    bar_height = 120
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Gradient: draw horizontal strips with increasing alpha toward bottom
    for y_offset in range(bar_height):
        alpha = int((y_offset / bar_height) * 38)  # 0 → 15% opacity
        y = THUMB_H - bar_height + y_offset
        draw.line(
            [(0, y), (THUMB_W, y)],
            fill=(0, 77, 112, alpha),  # Deep Blue tint
        )

    canvas_rgba = canvas.convert("RGBA")
    return Image.alpha_composite(canvas_rgba, overlay).convert("RGB")


def _centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    color: tuple,
    y_center: int,
    shadow: bool = True,
    shadow_offset: int = 4,
    shadow_blur_radius: int = 0,
) -> None:
    """
    Draw text centered horizontally at a given vertical position.

    Args:
        draw:          ImageDraw instance
        text:          String to draw
        font:          Loaded FreeTypeFont
        color:         (R, G, B) fill color
        y_center:      Vertical center of the text in pixels
        shadow:        Whether to draw a dark drop shadow behind the text
        shadow_offset: Shadow displacement in pixels (down and right)

    How text measurement works in Pillow:
        font.getbbox(text) returns (left, top, right, bottom) of the bounding box
        relative to the draw origin. We use (right - left) for width and
        (bottom - top) for height to center the text precisely.

    Note: getbbox() is the modern API (Pillow ≥ 9.2). The older getsize()
    is deprecated and removed in Pillow 10+.
    """
    bbox = font.getbbox(text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (THUMB_W - text_w) // 2
    y = y_center - text_h // 2

    if shadow:
        # Dark semi-transparent shadow — drawn first so text renders on top
        draw.text(
            (x + shadow_offset, y + shadow_offset),
            text,
            font=font,
            fill=(0, 0, 0, 180),
        )

    draw.text((x, y), text, font=font, fill=color)


def _split_title(title: str, max_line_chars: int = 22) -> tuple[str, str]:
    """
    Split a title string into two lines for the thumbnail's two-line layout.

    Strategy:
    1. If title fits on one line (≤ max_line_chars), use it as line 1, line 2 empty
    2. Otherwise split at the word boundary closest to the middle of the string
    3. Never split in the middle of a word

    The two-line format is: Line 1 white, Line 2 cyan.
    We want roughly equal visual weight on both lines, so we target the midpoint.

    Args:
        title: Full title string (typically 40-70 chars)
        max_line_chars: Soft maximum chars per line before forcing a split

    Returns:
        (line1, line2) tuple — line2 may be empty string
    """
    title = title.strip()

    # Short titles: use as-is on line 1
    if len(title) <= max_line_chars:
        return title, ""

    words = title.split()
    if len(words) <= 2:
        return title, ""

    # Find split point closest to midpoint of the string
    mid = len(title) // 2
    best_split = 1  # word index to split at
    best_dist = abs(len(" ".join(words[:1])) - mid)

    for i in range(2, len(words)):
        candidate = len(" ".join(words[:i]))
        dist = abs(candidate - mid)
        if dist < best_dist:
            best_dist = dist
            best_split = i

    line1 = " ".join(words[:best_split])
    line2 = " ".join(words[best_split:])
    return line1, line2


def _make_accent_label(topic: str, format_id: int) -> str:
    """
    Generate a short accent label for the top of the thumbnail.

    The accent label is a brief category or stat in cyan — it gives context
    before the viewer reads the main title. Examples: "BREAKING", "TOP 5",
    "HOW IT WORKS", "2024 INCIDENT".

    Args:
        topic:     Topic string from topics.json
        format_id: Content format ID (1-6)

    Returns:
        Short uppercase label string (≤ 20 chars)
    """
    labels: dict[int, str] = {
        1: "INCIDENT BREAKDOWN",
        2: "AI ALERT",
        3: "MYTH BUSTED",
        4: "HOW IT WORKS",
        5: "TOP LIST",
        6: "BREAKING NEWS",
    }
    return labels.get(format_id, "CIPHERPULSE")


def _extract_video_frame(video_path: Path, time_s: float = 2.5) -> Optional[Image.Image]:
    """
    Extract a single frame from a video file at time_s using FFmpeg.

    The frame is returned as a PIL Image cropped and scaled to 1280×720.
    The video is 1080×1920 (portrait 9:16); we scale to 1280 wide and
    center-crop the height to 720, giving a landscape 16:9 thumbnail from
    the middle of the frame where the main footage content sits.

    Returns None if extraction fails (caller will fall back to solid background).
    """
    tmp_path = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        # Scale portrait frame to 1280 wide, then center-crop to 720 tall
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(time_s),
                "-i", str(video_path),
                "-vframes", "1",
                "-vf", f"scale=1280:-1,crop=1280:720:0:(ih-720)/2",
                "-q:v", "2",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
            img = Image.open(tmp_path).convert("RGB")
            img.load()  # force-load before temp file is deleted
            return img
        log.warning(f"FFmpeg frame extraction failed (rc={result.returncode})")
    except Exception as e:
        log.warning(f"Frame extraction error: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return None


def _wrap_hook_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    Word-wrap hook text to fit within max_width pixels using Pillow font metrics.

    Returns a list of line strings, each guaranteed to render within max_width.
    """
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test_line = " ".join(current + [word])
        bbox = font.getbbox(test_line)
        if current and (bbox[2] - bbox[0]) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


# ── Frame-based thumbnail builder ────────────────────────────────────────────

def _create_frame_thumbnail(
    frame: Image.Image,
    hook_line: str,
    out_path: Path,
) -> Path:
    """
    Build a thumbnail from a video frame with the hook text overlaid.

    Layout:
      - Background: extracted video frame (1280×720, landscape crop)
      - Dark overlay strip behind text for readability (60% opacity)
      - Hook text: large white BebasNeue, word-wrapped, centered
      - CIPHERPULSE watermark: bottom-right, 30% opacity

    Args:
        frame:     PIL Image at 1280×720 (already scaled/cropped)
        hook_line: Hook sentence to overlay
        out_path:  File path to save the PNG to

    Returns:
        out_path after saving
    """
    canvas = frame.resize((THUMB_W, THUMB_H), Image.LANCZOS)

    # ── Subtle dark vignette so text pops regardless of footage brightness ──
    vignette = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    vdraw.rectangle([(0, 0), (THUMB_W, THUMB_H)], fill=(0, 0, 0, 60))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), vignette).convert("RGB")

    # ── Measure & wrap hook text ───────────────────────────────────────────
    HOOK_FONT_SIZE = 110
    HOOK_MARGIN    = 60   # px left/right margin
    max_text_w     = THUMB_W - 2 * HOOK_MARGIN

    hook_font = _load_font("BebasNeue-Regular.ttf", HOOK_FONT_SIZE)
    lines = _wrap_hook_lines(hook_line, hook_font, max_text_w)

    # Measure total text block height
    line_bboxes = [hook_font.getbbox(line) for line in lines]
    line_heights = [bb[3] - bb[1] for bb in line_bboxes]
    LINE_SPACING  = 8
    block_h = sum(line_heights) + LINE_SPACING * (len(lines) - 1)

    # Center block vertically in the middle half of the frame (y=160..560)
    BLOCK_CENTER_Y = THUMB_H // 2
    block_top = BLOCK_CENTER_Y - block_h // 2

    # ── Dark bar behind text ───────────────────────────────────────────────
    BAR_PAD = 28
    bar_top    = block_top - BAR_PAD
    bar_bottom = block_top + block_h + BAR_PAD
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rectangle([(0, bar_top), (THUMB_W, bar_bottom)], fill=(0, 0, 0, 165))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    # ── Draw hook text lines ───────────────────────────────────────────────
    draw = ImageDraw.Draw(canvas)
    y = block_top
    for line, lh in zip(lines, line_heights):
        bbox = hook_font.getbbox(line)
        lw = bbox[2] - bbox[0]
        x = (THUMB_W - lw) // 2
        # Shadow
        draw.text((x + 3, y + 3), line, font=hook_font, fill=(0, 0, 0, 200))
        # Text
        draw.text((x, y), line, font=hook_font, fill=COLOR_WHITE)
        y += lh + LINE_SPACING

    # ── CIPHERPULSE watermark ──────────────────────────────────────────────
    wm_font = _load_font("Oswald-Variable.ttf", size=28)
    wm_text = "CIPHERPULSE"
    wm_bbox = wm_font.getbbox(wm_text)
    wm_w = wm_bbox[2] - wm_bbox[0]
    wm_h = wm_bbox[3] - wm_bbox[1]
    MARGIN = 24
    wm_overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    wm_draw = ImageDraw.Draw(wm_overlay)
    wm_draw.text(
        (THUMB_W - wm_w - MARGIN, THUMB_H - wm_h - MARGIN),
        wm_text, font=wm_font, fill=(232, 230, 227, 77),
    )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), wm_overlay).convert("RGB")

    canvas.save(str(out_path), format="PNG", optimize=True)
    size_kb = out_path.stat().st_size // 1024
    log.info(f"Thumbnail saved (frame+hook): {out_path} ({THUMB_W}×{THUMB_H}, {size_kb} KB)")
    return out_path


# ── Main thumbnail builder ────────────────────────────────────────────────────

def create_thumbnail(
    title: str,
    output_dir: Path,
    accent_label: Optional[str] = None,
    format_id: int = 1,
    video_path: Optional[Path] = None,
    hook_line: str = "",
) -> Path:
    """
    Generate a CipherPulse branded thumbnail and save it to output_dir.

    When video_path and hook_line are both provided, the thumbnail uses a frame
    extracted from the video as its background and overlays the hook_line text
    in large white type — making the thumbnail feel native to the content.
    Otherwise falls back to the branded void-black + title-text layout.

    Args:
        title:        Video title — used for the text-only fallback layout
        output_dir:   Directory to write thumbnail.png into
        accent_label: Small top text override (text-only layout only)
        format_id:    Content format ID (1-6) for accent label auto-generation
        video_path:   Path to the assembled video.mp4 (for frame extraction)
        hook_line:    Opening hook sentence to overlay on the extracted frame

    Returns:
        Path to the saved thumbnail.png file
    """
    _download_fonts()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "thumbnail.png"

    # ── Decide layout: video-frame or branded solid-bg ─────────────────────
    use_frame_layout = bool(video_path and hook_line.strip())
    frame_img: Optional[Image.Image] = None
    if use_frame_layout:
        frame_img = _extract_video_frame(video_path, time_s=2.5)
        if frame_img is None:
            log.warning("Frame extraction failed — falling back to solid-bg layout")
            use_frame_layout = False

    if use_frame_layout:
        return _create_frame_thumbnail(
            frame=frame_img,
            hook_line=hook_line.strip(),
            out_path=out_path,
        )

    # ── Layer 1: Background ────────────────────────────────────────────────
    canvas = Image.new("RGB", (THUMB_W, THUMB_H), COLOR_BG)

    # ── Layer 1b: Grid overlay ─────────────────────────────────────────────
    canvas = _draw_grid_overlay(canvas)

    # ── Layer 1c: Bottom gradient bar ─────────────────────────────────────
    canvas = _draw_gradient_bar(canvas)

    draw = ImageDraw.Draw(canvas)

    # ── Layer 2: Accent label (top, small cyan) ────────────────────────────
    # Small uppercase label in cyan at the top — gives instant category context.
    # Positioned at y=80 (top 11% of the frame).
    label = accent_label or _make_accent_label("", format_id)
    accent_font = _load_font("Oswald-Variable.ttf", size=36)

    _centered_text(
        draw, label.upper(), accent_font,
        color=COLOR_CYAN,
        y_center=72,
        shadow=True,
        shadow_offset=2,
    )

    # Draw a thin cyan underline beneath the accent label
    label_bbox = accent_font.getbbox(label.upper())
    label_w = label_bbox[2] - label_bbox[0]
    underline_x1 = (THUMB_W - label_w) // 2
    underline_x2 = underline_x1 + label_w
    draw.line(
        [(underline_x1, 94), (underline_x2, 94)],
        fill=COLOR_CYAN + (180,) if len(COLOR_CYAN) == 3 else COLOR_CYAN,
        width=2,
    )

    # ── Layer 3: Main title — two lines ───────────────────────────────────
    # Line 1: white (#FFFFFF), large Bebas Neue
    # Line 2: cyan (#00F2EA), slightly smaller Bebas Neue
    # The vertical center of the title block sits at y=360 (canvas center).
    # We measure both lines first, then position them with appropriate gap.
    line1, line2 = _split_title(title)

    title_font_l1 = _load_font("BebasNeue-Regular.ttf", size=130)
    title_font_l2 = _load_font("BebasNeue-Regular.ttf", size=118)

    # Measure line heights
    l1_bbox = title_font_l1.getbbox(line1) if line1 else (0, 0, 0, 0)
    l1_h = l1_bbox[3] - l1_bbox[1]

    l2_bbox = title_font_l2.getbbox(line2) if line2 else (0, 0, 0, 0)
    l2_h = l2_bbox[3] - l2_bbox[1]

    LINE_GAP = 16
    total_title_h = l1_h + (LINE_GAP + l2_h if line2 else 0)

    # Center the title block vertically in the middle of the canvas (y=360)
    title_block_top = (THUMB_H - total_title_h) // 2
    l1_center = title_block_top + l1_h // 2
    l2_center = title_block_top + l1_h + LINE_GAP + l2_h // 2

    if line1:
        _centered_text(
            draw, line1, title_font_l1,
            color=COLOR_WHITE,
            y_center=l1_center,
            shadow=True,
            shadow_offset=5,
        )

    if line2:
        _centered_text(
            draw, line2, title_font_l2,
            color=COLOR_CYAN,
            y_center=l2_center,
            shadow=True,
            shadow_offset=5,
        )

    # ── Layer 4: Watermark ─────────────────────────────────────────────────
    # "CIPHERPULSE" in bottom-right at 30% opacity.
    # We use an RGBA overlay so we can set per-pixel alpha precisely.
    # The watermark establishes brand recognition even if the thumbnail is
    # shared or embedded without the video player chrome.
    watermark_font = _load_font("Oswald-Variable.ttf", size=28)

    wm_text = "CIPHERPULSE"
    wm_bbox = watermark_font.getbbox(wm_text)
    wm_w = wm_bbox[2] - wm_bbox[0]
    wm_h = wm_bbox[3] - wm_bbox[1]

    MARGIN = 24
    wm_x = THUMB_W - wm_w - MARGIN
    wm_y = THUMB_H - wm_h - MARGIN

    # Draw watermark onto a transparent overlay, then composite
    wm_overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    wm_draw = ImageDraw.Draw(wm_overlay)
    wm_draw.text((wm_x, wm_y), wm_text, font=watermark_font, fill=(232, 230, 227, 77))

    canvas_rgba = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas_rgba, wm_overlay).convert("RGB")

    # ── Save ───────────────────────────────────────────────────────────────
    canvas.save(str(out_path), format="PNG", optimize=True)
    size_kb = out_path.stat().st_size // 1024
    log.info(f"Thumbnail saved: {out_path} ({THUMB_W}×{THUMB_H}, {size_kb} KB)")
    return out_path


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a CipherPulse branded thumbnail"
    )
    parser.add_argument(
        "--title", type=str,
        default="This Spyware Can Secretly Record Everything On Your Phone",
        help="Main title text for the thumbnail",
    )
    parser.add_argument(
        "--format", type=int, default=1, dest="format_id",
        choices=[1, 2, 3, 4, 5, 6],
        help="Content format ID for accent label auto-generation",
    )
    parser.add_argument(
        "--accent", type=str, default=None,
        help="Override the accent label text (e.g. 'BREAKING NEWS')",
    )
    parser.add_argument(
        "--output-dir", type=str, default="output/test", dest="output_dir",
        help="Directory to write thumbnail.png (default: output/test)",
    )
    parser.add_argument(
        "--download-fonts", action="store_true", dest="download_fonts",
        help="Force re-download of Google Fonts even if already cached",
    )
    args = parser.parse_args()

    if args.download_fonts:
        for f in FONTS_DIR.glob("*.ttf"):
            f.unlink()
        log.info("Cleared font cache — will re-download")

    out_dir = Path(args.output_dir)
    path = create_thumbnail(
        title=args.title,
        output_dir=out_dir,
        accent_label=args.accent,
        format_id=args.format_id,
    )

    size_kb = path.stat().st_size // 1024
    print(f"\n{'═' * 55}")
    print(f"✅  Thumbnail saved: {path}")
    print(f"    Size: {THUMB_W}×{THUMB_H} | {size_kb} KB")
    print(f"    Title: {args.title}")
    print(f"    Format: {args.format_id}")
    print("═" * 55)
    print(f"\nTo view:")
    print(f"  eog {path}    # GNOME image viewer")
    print(f"  feh {path}    # lightweight viewer")
    print(f"  xdg-open {path}")
