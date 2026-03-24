"""
text_card_assembler.py — Single-frame news card Short assembler (v3).

Layout
------
  TOP SECTION   (0–700 px)    : Pexels clip frame, fill-cropped to 1080×700.
                                 "CP" watermark at top-left (20 px, white, 50 % opacity).
  BOTTOM SECTION (700–1920 px) : Solid #0a0a0f background with 3 story paragraphs.
                                 Left-aligned text (60 px margins), Oswald 34 px,
                                 1.5× line height.  Key terms in *asterisks* are
                                 rendered in cyan (#00F2EA) bold; all other text
                                 is plain white.

Motion / audio
--------------
  A single composed 1080×1920 PNG is fed to FFmpeg with a very subtle Ken Burns
  zoom (1.00 → 1.02 over the full duration) so the card doesn't feel completely
  static.  A dark/ambient licensed track is mixed at 25 % volume with a 1 s
  fade-out at the end.  Only tracks with "dark"/"ambient" in filename
  are used — never upbeat SoundHelix tracks.

Duration  : DEFAULT_DURATION = 16 s (15–18 s recommended)
Output    : 1080×1920 MP4, H.264, 30 fps, AAC audio
"""

from __future__ import annotations

import logging
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

# ── Canvas dimensions ─────────────────────────────────────────────────────────
CANVAS_W     = 1080
CANVAS_H     = 1920

# Top image section
IMAGE_H      = 700          # height of the Pexels clip frame at the top

# Bottom text section
TEXT_Y_START = IMAGE_H + 30  # 30 px gap below image  (= 730 px from top)
TEXT_LEFT    = 60            # left margin
TEXT_RIGHT   = CANVAS_W - 60 # right margin
TEXT_MAX_W   = TEXT_RIGHT - TEXT_LEFT   # 960 px

# ── Timing ────────────────────────────────────────────────────────────────────
DEFAULT_DURATION = 16.0      # 15-18 s — reading time for 80-120 words
FPS              = 30
ZOOM_START       = 1.00
ZOOM_END         = 1.02      # very subtle — 2 % zoom over full clip
MUSIC_VOLUME     = 0.25      # background atmosphere only

# ── Colours (RGB tuples for Pillow) ──────────────────────────────────────────
BG_COLOR    = (10,   10,   15)    # #0a0a0f  — dark background
CYAN        = (0,    242,  234)   # #00F2EA  — key term highlight
WHITE       = (255,  255,  255)   # #FFFFFF  — body text
SHADOW      = (0,    0,    0,    180)  # RGBA — drop shadow

# ── Font sizes ────────────────────────────────────────────────────────────────
FONT_BODY    = 34
FONT_WM      = 20   # "CP" watermark
LINE_SPACING = 1.5  # line height multiplier
PARA_GAP_MUL = 1.8  # extra gap between paragraphs (multiplier of line height)

# ── Asset paths ───────────────────────────────────────────────────────────────
ASSETS_DIR  = Path(__file__).parent.parent / "assets"
FONTS_DIR   = ASSETS_DIR / "fonts"
MUSIC_DIR   = ASSETS_DIR / "music"
OSWALD_FONT = FONTS_DIR / "Oswald-Variable.ttf"
BEBAS_FONT  = FONTS_DIR / "BebasNeue-Regular.ttf"


# ── Font loader ───────────────────────────────────────────────────────────────

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """
    Load Oswald Variable at the requested size.

    Pillow uses FreeType sub-pixel antialiasing automatically for all TrueType
    fonts — no extra configuration needed.  The antialiasing is applied during
    draw.text() calls, producing smooth edges at any size.

    For bold=True we attempt to set the wght axis to 700 so cyan key terms
    look heavier than body text.  Falls back to Bebas Neue, then Pillow's
    built-in default if neither custom font is available.
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
    log.warning("No custom font — using Pillow default")
    return ImageFont.load_default()


# ── Image helpers ─────────────────────────────────────────────────────────────

def _resize_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and center-crop img to exactly w×h with no letterboxing."""
    sw, sh = img.size
    scale  = max(w / sw, h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - w) // 2
    top    = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _extract_frame(clip_path: Path, t: float = 1.0) -> Image.Image:
    """
    Extract one frame from clip_path at timestamp t seconds, return as PIL Image.

    Falls back to a solid dark frame if ffmpeg fails (e.g. very short clip).
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path),
             "-frames:v", "1", "-q:v", "2", str(tmp_path)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
            return Image.open(tmp_path).copy()
        else:
            log.warning(f"Frame extract failed for {clip_path.name}: {r.stderr[-200:]}")
    except Exception as exc:
        log.warning(f"Frame extract error: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)
    # Fallback: CipherPulse dark gradient
    return Image.new("RGB", (CANVAS_W, IMAGE_H), BG_COLOR)


# ── Markup tokeniser ──────────────────────────────────────────────────────────

def _tokenize(paragraph: str) -> list[tuple[str, bool]]:
    """
    Split a paragraph with *asterisk markup* into (word, is_cyan) pairs.

    Example:
        "In *2013*, *Yahoo* leaked *3 billion* accounts."
        → [("In", F), ("2013,", T), ("Yahoo", T), ("leaked", F),
           ("3", T), ("billion", T), ("accounts.", F)]

    Multi-word cyan spans work correctly: "largest *data breach in history*"
    → all four words inside the asterisks get is_cyan=True.
    """
    tokens: list[tuple[str, bool]] = []
    parts = re.split(r"\*([^*]+)\*", paragraph)
    for i, chunk in enumerate(parts):
        is_cyan = bool(i % 2)
        for word in chunk.split():
            tokens.append((word, is_cyan))
    return tokens


# ── Line wrapper ──────────────────────────────────────────────────────────────

def _wrap_tokens(
    tokens: list[tuple[str, bool]],
    normal_font: ImageFont.FreeTypeFont,
    bold_font:   ImageFont.FreeTypeFont,
    max_w: int,
    draw: ImageDraw.ImageDraw,
) -> list[list[tuple[str, bool]]]:
    """
    Greedy word-wrap of mixed-color tokens.

    Returns a list of lines, where each line is a list of (word, is_cyan).
    Each word is measured in its own font (bold for cyan, normal otherwise)
    so the line-break decisions account for font weight differences.
    """
    space_w = draw.textbbox((0, 0), " ", font=normal_font)[2]
    lines:        list[list[tuple[str, bool]]] = []
    current_line: list[tuple[str, bool]]       = []
    current_w = 0

    for word, is_cyan in tokens:
        font   = bold_font if is_cyan else normal_font
        word_w = draw.textbbox((0, 0), word, font=font)[2]

        if current_line and current_w + space_w + word_w > max_w:
            lines.append(current_line)
            current_line = [(word, is_cyan)]
            current_w    = word_w
        else:
            if current_line:
                current_w += space_w
            current_line.append((word, is_cyan))
            current_w += word_w

    if current_line:
        lines.append(current_line)
    return lines


# ── Line renderer ─────────────────────────────────────────────────────────────

def _draw_line(
    draw:        ImageDraw.ImageDraw,
    line:        list[tuple[str, bool]],
    normal_font: ImageFont.FreeTypeFont,
    bold_font:   ImageFont.FreeTypeFont,
    x:           int,
    y:           int,
) -> None:
    """
    Render one wrapped line of mixed-color text with a drop shadow.

    Words with is_cyan=True are drawn in CYAN using bold_font.
    All other words are drawn in WHITE using normal_font.
    A dark shadow is drawn 2 px below-right of each word for readability.
    """
    space_w = draw.textbbox((0, 0), " ", font=normal_font)[2]
    cx = x
    for idx, (word, is_cyan) in enumerate(line):
        font  = bold_font if is_cyan else normal_font
        color = CYAN      if is_cyan else WHITE
        # Drop shadow
        draw.text((cx + 2, y + 2), word, font=font, fill=SHADOW)
        # Actual text
        draw.text((cx,     y    ), word, font=font, fill=color)
        cx += draw.textbbox((0, 0), word, font=font)[2]
        if idx < len(line) - 1:
            cx += space_w


# ── Music picker ──────────────────────────────────────────────────────────────

def _pick_music() -> Optional[Path]:
    """
    Return a dark/ambient licensed track from assets/music/, or None (silence).

    Strict dark-only filter: only tracks whose filename contains one of
    "dark", "ambient", "slow", "atmospheric", or "cinematic" are used.
    Never falls back to upbeat SoundHelix tracks.

    To populate dark tracks:
        python3 -m src.download_safe_music --source dark-ambient
    Or upgrade to real music:
        python3 -m src.download_safe_music --source jamendo
    """
    import random
    from src.download_safe_music import verify_track

    DARK_KEYWORDS = {"dark", "ambient", "slow", "atmospheric", "cinematic"}

    all_tracks = (
        [p for p in MUSIC_DIR.iterdir()
         if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}]
        if MUSIC_DIR.exists() else []
    )
    licensed    = [p for p in all_tracks if verify_track(p.name)]
    dark_tracks = [
        p for p in licensed
        if any(kw in p.stem.lower() for kw in DARK_KEYWORDS)
    ]

    if dark_tracks:
        chosen = random.choice(dark_tracks)
        log.info(f"Music: {chosen.name}")
        return chosen

    log.warning(
        f"No dark/ambient tracks available ({len(licensed)} licensed tracks exist "
        "but none match dark keywords). Video will be silent. "
        "Fix: python3 -m src.download_safe_music --source dark-ambient"
    )
    return None


# ── Frame composer ────────────────────────────────────────────────────────────

def _compose_frame(
    clip_path:  Path,
    paragraphs: list[str],
) -> Image.Image:
    """
    Build the complete 1080×1920 RGB frame.

    Layout:
      • Top 700 px  : Pexels clip frame (fill-cropped to 1080×700) +
                      "CP" watermark top-left (20 px, white, 50 % opacity)
      • Bottom text : #0a0a0f background, 3 paragraphs starting at y=730.
                      Left-aligned (60 px margin), 34 px Oswald, 1.5× line height.
                      *Marked* words are cyan bold; plain words are white.
    """
    # ── 1. Base canvas — exact pixel dimensions, no rounding ─────────────────
    assert CANVAS_W == 1080 and CANVAS_H == 1920, "Canvas must be exactly 1080×1920"
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # ── 2. Top image — LANCZOS resampling for maximum sharpness ───────────────
    # _resize_fill uses Image.LANCZOS (Pillow's highest-quality downscale filter).
    # The crop() at the end of _resize_fill guarantees exactly CANVAS_W×IMAGE_H px.
    frame     = _extract_frame(clip_path)
    top_img   = _resize_fill(frame, CANVAS_W, IMAGE_H)
    assert top_img.size == (CANVAS_W, IMAGE_H), \
        f"Top image is {top_img.size}, expected ({CANVAS_W}, {IMAGE_H})"
    canvas.paste(top_img, (0, 0))

    # ── 3. RGBA overlay for watermark + text (alpha_composite at end) ─────────
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # "CP" watermark — top-left corner, 50 % opacity
    wm_font = _load_font(FONT_WM)
    draw.text((20, 20), "CP", font=wm_font, fill=(255, 255, 255, 128))

    # ── 4. Paragraph text ─────────────────────────────────────────────────────
    normal_font  = _load_font(FONT_BODY, bold=False)
    bold_font    = _load_font(FONT_BODY, bold=True)
    footer_font  = _load_font(20,        bold=False)
    line_h       = int(FONT_BODY * LINE_SPACING)       # 51 px
    para_gap     = int(line_h * PARA_GAP_MUL)          # extra gap between paragraphs

    y = TEXT_Y_START
    for idx, para in enumerate(paragraphs):
        if not para.strip():
            y += line_h
            continue
        tokens = _tokenize(para)
        lines  = _wrap_tokens(tokens, normal_font, bold_font, TEXT_MAX_W, draw)
        for line in lines:
            _draw_line(draw, line, normal_font, bold_font, TEXT_LEFT, y)
            y += line_h
        if idx < len(paragraphs) - 1:
            y += para_gap   # gap between paragraphs, not after the last one

    # ── 4b. CTA footer: separator line + follow text ──────────────────────────
    sep_y = y + 28
    draw.rectangle(
        [(TEXT_LEFT, sep_y), (TEXT_RIGHT, sep_y + 2)],
        fill=(*CYAN, 180),
    )
    draw.text(
        (TEXT_LEFT, sep_y + 10),
        "Follow @CipherPulse  ·  Daily Cyber Threats & AI News",
        font=footer_font,
        fill=(*CYAN, 210),
    )

    # ── 5. Composite overlay onto canvas ─────────────────────────────────────
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(overlay)
    return canvas_rgba.convert("RGB")


# ── Video maker ───────────────────────────────────────────────────────────────

def _make_video(
    frame_path:  Path,
    output_path: Path,
    music_path:  Optional[Path],
    duration:    float,
) -> None:
    """
    Convert a still PNG into a video with Ken Burns zoom and background music.

    Ken Burns: zoom from ZOOM_START (1.00) to ZOOM_END (1.02) over the full
    duration — barely perceptible motion that prevents a completely frozen frame.

    Audio: music at MUSIC_VOLUME (35 %) with a 1 s fade-out at the end.
    If no music is available, produces a silent video.
    """
    n_frames = int(duration * FPS)
    inc      = (ZOOM_END - ZOOM_START) / max(n_frames, 1)
    fade_st  = max(0.0, duration - 1.0)

    cmd = ["ffmpeg", "-y",
           "-loop", "1", "-framerate", str(FPS), "-t", str(duration),
           "-i", str(frame_path)]

    if music_path:
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]

    # Video filter: zoompan → format
    vf = (
        f"zoompan="
        f"z='min(zoom+{inc:.8f},{ZOOM_END})':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d={n_frames}:s={CANVAS_W}x{CANVAS_H}:fps={FPS},"
        f"format=yuv420p"
    )

    if music_path:
        af = (
            f"[1:a]volume={MUSIC_VOLUME},"
            f"atrim=0:{duration:.3f},"
            f"afade=t=out:st={fade_st:.3f}:d=1[aout]"
        )
        fc = f"[0:v]{vf}[vout];{af}"
        cmd += ["-filter_complex", fc,
                "-map", "[vout]", "-map", "[aout]"]
    else:
        fc = f"[0:v]{vf}[vout]"
        cmd += ["-filter_complex", fc,
                "-map", "[vout]"]

    cmd += [
        "-c:v", "libx264", "-profile:v", "high",
        "-crf", "18",          # near-lossless quality (default 23 was too soft)
        "-preset", "slow",     # better compression at same CRF vs fast/medium
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-s", f"{CANVAS_W}x{CANVAS_H}",   # lock output to exactly 1080×1920
        "-t", str(duration),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    log.info(f"FFmpeg: single-frame Ken Burns → {output_path.name}  ({duration:.0f}s)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"FFmpeg stderr:\n{r.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg failed (exit {r.returncode})")
    log.info(f"Video ready: {output_path.name}  ({output_path.stat().st_size / 1e6:.1f} MB)")


# ── Public API ────────────────────────────────────────────────────────────────

def assemble_text_card(
    paragraphs:  list[str],
    clip_paths:  list[Path],
    output_dir:  Path,
    duration:    float = DEFAULT_DURATION,
) -> Path:
    """
    Assemble a single-frame news card Short.

    Steps:
      1. Extract a still frame from the first clip → top 1080×700 image.
      2. Pillow renders the full 1080×1920 composite (image + text).
      3. The composed frame is saved as frame.png (also copied to slide_0.png
         for thumbnail generation by the orchestrator).
      4. FFmpeg applies subtle Ken Burns zoom + background music → video.mp4.

    Args:
        paragraphs:  3 story paragraphs.  Key terms wrapped in *asterisks*
                     are rendered in cyan bold; all other text is plain white.
        clip_paths:  At least 1 Pexels clip — first clip provides the top image.
        output_dir:  Directory to write video.mp4 and slide_0.png into.
        duration:    Video duration in seconds (default 20 s, range 18–22 s).

    Returns:
        Path to video.mp4

    Raises:
        RuntimeError: If no clips are provided or FFmpeg fails.
    """
    if not clip_paths:
        raise RuntimeError("assemble_text_card requires at least one clip path")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    music = _pick_music()

    # ── Compose the full 1080×1920 frame ─────────────────────────────────────
    log.info("Composing 1080×1920 news card frame…")
    frame_img = _compose_frame(clip_paths[0], paragraphs)

    # Save as slide_0.png so orchestrator can build the thumbnail
    slide0_path = output_dir / "slide_0.png"
    frame_img.save(slide0_path, "PNG")
    log.info("slide_0.png saved for thumbnail generation")

    # Also save as frame.png for clarity
    frame_path = output_dir / "frame.png"
    frame_img.save(frame_path, "PNG")

    # ── Produce the video ─────────────────────────────────────────────────────
    video_out = output_dir / "video.mp4"
    _make_video(frame_path, video_out, music, duration)

    return video_out


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from pathlib import Path as P

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Assemble a CipherPulse news card Short"
    )
    parser.add_argument("--output-dir", default="output/test", dest="output_dir")
    parser.add_argument("--duration",   type=float, default=DEFAULT_DURATION)
    args = parser.parse_args()

    out = P(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Demo paragraphs (use real data via orchestrator in production)
    demo = [
        "In *2013*, *Adobe* suffered a *massive data breach* exposing *153 million* user records. Encrypted passwords and credit card data were stolen from one of the world's largest software companies.",
        "The attackers spent *months* inside Adobe's network undetected. They exfiltrated an entire customer database, including *2.9 million* records with payment information.",
        "Many users had *reused their Adobe password* on other sites. Within weeks, attackers used those credentials to break into *email accounts, banks, and social media* across the internet.",
    ]

    # Find a demo clip from the footage cache
    cache = P("assets/footage_cache")
    clips = list(cache.rglob("*.mp4"))
    if not clips:
        print("No clips in footage_cache — run footage_downloader first.")
        import sys; sys.exit(1)

    result = assemble_text_card(
        paragraphs=demo,
        clip_paths=clips[:1],
        output_dir=out,
        duration=args.duration,
    )
    print(f"\nVideo: {result}  ({result.stat().st_size / 1e6:.1f} MB)")
    print(f"Frame: {out / 'slide_0.png'}")
