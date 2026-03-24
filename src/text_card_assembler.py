"""
text_card_assembler.py — Animated news card Short assembler (v4).

Layout
------
  TOP SECTION   (0–700 px)    : 2-3 Pexels images that slowly crossfade into
                                 each other throughout the video.  Each image
                                 has a subtle Ken Burns zoom (1.00 → 1.03).
                                 20 px dark margin at top prevents flush-edge
                                 cropping.  "CP" watermark at (30, 30).
  BOTTOM SECTION (700–1920 px) : Solid #0a0a0f background — completely static.
                                 2 punchy paragraphs, vertically centred in the
                                 section.  Left-aligned (60 px margin), Oswald
                                 38 px, 1.5× line height.  *Marked* key terms
                                 render in cyan (#00F2EA) bold.

Motion / audio
--------------
  Top section: FFmpeg zoompan (1.00→1.03) applied to each still frame, then
  xfade crossfades (1.5 s each) stitch the clips into one 1080×700 video.
  Bottom section: a still PNG that never moves.
  Both halves are combined with FFmpeg pad + RGBA overlay (transparent top /
  opaque bottom) — the overlay is the final paint layer so xfade edge artifacts
  in the top video can never bleed into the text zone.
  A licensed background track is mixed at 25 % volume with a 1 s fade-out.
  Dark/ambient tracks are preferred; SoundHelix is used as fallback.

Duration  : DEFAULT_DURATION = 20 s
Output    : 1080×1920 MP4, H.264 CRF 18, 30 fps, AAC 192k
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
CANVAS_W  = 1080
CANVAS_H  = 1920
IMAGE_H   = 700                       # top image section height
BOTTOM_H  = CANVAS_H - IMAGE_H        # 1220 px — bottom text section height

# Text layout (relative to bottom panel, i.e. y=0 = start of bottom section)
TEXT_LEFT  = 60
TEXT_RIGHT = CANVAS_W - 60
TEXT_MAX_W = TEXT_RIGHT - TEXT_LEFT   # 960 px

# ── Timing ────────────────────────────────────────────────────────────────────
DEFAULT_DURATION = 20.0   # seconds
FPS              = 30
ZOOM_START       = 1.00
ZOOM_END         = 1.03   # 3 % zoom on top images — more noticeable than full-canvas
TOP_XFADE        = 1.5    # crossfade duration between top images (seconds)
MUSIC_VOLUME     = 0.25   # background atmosphere — not foreground

# ── Typography ────────────────────────────────────────────────────────────────
FONT_BODY    = 38          # px — larger font for shorter/punchier 2-para format
FONT_WM      = 20          # px — "CP" watermark
LINE_SPACING = 1.5         # line height multiplier
PARA_GAP_MUL = 1.6         # extra gap between paragraphs (× line height)

# ── Colours ───────────────────────────────────────────────────────────────────
BG_COLOR  = (10,  10,  15)           # #0a0a0f  — bottom panel background
CYAN      = (0,   242, 234)          # #00F2EA  — key term highlight
WHITE     = (255, 255, 255)          # #FFFFFF  — body text
SHADOW    = (0,   0,   0,   180)     # RGBA drop shadow

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

    Pillow uses FreeType sub-pixel antialiasing automatically for TrueType
    fonts — no extra configuration is needed.

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
    """Scale and center-crop img to exactly w×h using LANCZOS — no letterboxing."""
    sw, sh = img.size
    scale  = max(w / sw, h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - w) // 2
    top    = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _extract_frame(clip_path: Path, t: float = 1.0) -> Image.Image:
    """
    Extract one frame from clip_path at timestamp t seconds.

    Falls back to a solid dark frame if ffmpeg fails.
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
        log.warning(f"Frame extract failed for {clip_path.name}: {r.stderr[-200:]}")
    except Exception as exc:
        log.warning(f"Frame extract error: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return Image.new("RGB", (CANVAS_W, IMAGE_H), BG_COLOR)


# ── Markup tokeniser ──────────────────────────────────────────────────────────

def _tokenize(paragraph: str) -> list[tuple[str, bool]]:
    """
    Split a paragraph with *asterisk markup* into (word, is_cyan) pairs.

    Multi-word spans work: "largest *data breach in history*" → all 4 words cyan.
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
    tokens:      list[tuple[str, bool]],
    normal_font: ImageFont.FreeTypeFont,
    bold_font:   ImageFont.FreeTypeFont,
    max_w:       int,
    draw:        ImageDraw.ImageDraw,
) -> list[list[tuple[str, bool]]]:
    """
    Greedy word-wrap of mixed-color tokens into lines that fit within max_w px.

    Each word is measured in its own font (bold for cyan, normal otherwise)
    so line-break decisions account for the weight difference.
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
    """Render one wrapped line with per-word colour and drop shadow."""
    space_w = draw.textbbox((0, 0), " ", font=normal_font)[2]
    cx = x
    for idx, (word, is_cyan) in enumerate(line):
        font  = bold_font if is_cyan else normal_font
        color = CYAN      if is_cyan else WHITE
        draw.text((cx + 2, y + 2), word, font=font, fill=SHADOW)
        draw.text((cx,     y    ), word, font=font, fill=color)
        cx += draw.textbbox((0, 0), word, font=font)[2]
        if idx < len(line) - 1:
            cx += space_w


# ── Music picker ──────────────────────────────────────────────────────────────

def _pick_music() -> Optional[Path]:
    """
    Return a licensed track from assets/music/, preferring dark/ambient tones.

    Selection priority:
      1. Any track whose filename contains "dark", "ambient", "atmospheric", or
         "cinematic" (case-insensitive) — good for Jamendo dark ambient upgrades.
      2. If none match, fall back to a random track from the full licensed pool.

    Volume is always set to MUSIC_VOLUME (0.25) — background atmosphere only.
    """
    import random
    from src.download_safe_music import verify_track

    all_tracks = (
        [p for p in MUSIC_DIR.iterdir()
         if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}]
        if MUSIC_DIR.exists() else []
    )
    licensed = [p for p in all_tracks if verify_track(p.name)]
    if not licensed:
        log.warning("No licensed tracks — producing silent video")
        return None

    PREFERRED_KEYWORDS = {"dark", "ambient", "atmospheric", "cinematic"}
    preferred = [
        p for p in licensed
        if any(kw in p.stem.lower() for kw in PREFERRED_KEYWORDS)
    ]
    if preferred:
        chosen = random.choice(preferred)
        log.debug(f"Picked preferred dark/ambient track: {chosen.name}")
        return chosen

    # No keyword match — use any licensed track at the reduced volume
    log.debug("No dark/ambient tracks found — using random licensed track")
    return random.choice(licensed)


# ── Top image preparation ─────────────────────────────────────────────────────

TOP_IMAGE_MARGIN = 20   # px of dark space above the image — prevents flush-top cropping

def _prepare_top_frames(clip_paths: list[Path], tmp: Path) -> list[Path]:
    """
    Extract one still frame from each clip, composite it into a 1080×700
    canvas with a 20 px top margin, add the "CP" watermark, and save as PNG.

    Layout of each output frame (1080×700):
      y=0  … y=20  — solid BG_COLOR strip (prevents content flush against top)
      y=20 … y=700 — source image, center-cropped to 1080×680

    Resizing to 1080×680 (not 1080×700) means less of the portrait is discarded
    vertically, so faces/heads that sit in the upper portion of the image are
    no longer cropped off at the frame boundary.

    Returns a list of 1080×700 PNG paths suitable for zoompan input.
    """
    wm_font = _load_font(FONT_WM)
    result: list[Path] = []
    image_area_h = IMAGE_H - TOP_IMAGE_MARGIN   # 680 px

    for i, clip in enumerate(clip_paths):
        frame = _extract_frame(clip)

        # Resize source to fill 1080×680, center-cropped
        img_cropped = _resize_fill(frame, CANVAS_W, image_area_h)
        assert img_cropped.size == (CANVAS_W, image_area_h), \
            f"Cropped frame {i} is {img_cropped.size}, expected ({CANVAS_W}, {image_area_h})"

        # Composite onto full 1080×700 canvas — dark strip at top, image below
        canvas = Image.new("RGB", (CANVAS_W, IMAGE_H), BG_COLOR)
        canvas.paste(img_cropped, (0, TOP_IMAGE_MARGIN))
        assert canvas.size == (CANVAS_W, IMAGE_H), \
            f"Canvas {i} is {canvas.size}, expected ({CANVAS_W}, {IMAGE_H})"

        # Draw "CP" watermark at (30, 30) — 30px from top-left, not on the edge
        rgba = canvas.convert("RGBA")
        ovl  = Image.new("RGBA", (CANVAS_W, IMAGE_H), (0, 0, 0, 0))
        d    = ImageDraw.Draw(ovl)
        d.text((30, 30), "CP", font=wm_font, fill=(255, 255, 255, 128))
        rgba.alpha_composite(ovl)

        out = tmp / f"top_{i}.png"
        rgba.convert("RGB").save(out, "PNG")
        result.append(out)
        log.debug(f"Top frame {i} prepared: {out.name}")

    return result


# ── Bottom panel composer ─────────────────────────────────────────────────────

def _compose_bottom_panel(paragraphs: list[str]) -> Image.Image:
    """
    Render the 1080×1220 static text section.

    Text is vertically centred within the panel (minimum 50 px top margin).
    *Marked* key terms render in cyan bold; all other words are plain white.

    Returns a 1080×1220 RGB Image.
    """
    panel   = Image.new("RGB",  (CANVAS_W, BOTTOM_H), BG_COLOR)
    overlay = Image.new("RGBA", (CANVAS_W, BOTTOM_H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    normal_font = _load_font(FONT_BODY, bold=False)
    bold_font   = _load_font(FONT_BODY, bold=True)
    line_h      = int(FONT_BODY * LINE_SPACING)     # 57 px at 38pt
    para_gap    = int(line_h * PARA_GAP_MUL)        # extra space between paragraphs

    # ── Pre-wrap all paragraphs to measure total text height ──────────────────
    # Use a throw-away draw surface for measurement so we don't need the
    # real overlay draw object before we know the y_start position.
    dummy_img  = Image.new("RGB", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    all_wrapped: list[list[list[tuple[str, bool]]]] = []
    for para in paragraphs:
        if para.strip():
            tokens  = _tokenize(para)
            wrapped = _wrap_tokens(tokens, normal_font, bold_font, TEXT_MAX_W, dummy_draw)
            all_wrapped.append(wrapped)
        else:
            all_wrapped.append([])

    n_nonempty  = sum(1 for w in all_wrapped if w)
    total_lines = sum(len(w) for w in all_wrapped)
    total_h     = total_lines * line_h + max(0, n_nonempty - 1) * para_gap

    # Vertically centre with a minimum top margin of 50 px
    y_start = max(50, (BOTTOM_H - total_h) // 2)

    # ── Draw ──────────────────────────────────────────────────────────────────
    y = y_start
    for wrapped in all_wrapped:
        if not wrapped:
            y += line_h
            continue
        for line in wrapped:
            _draw_line(draw, line, normal_font, bold_font, TEXT_LEFT, y)
            y += line_h
        y += para_gap

    panel_rgba = panel.convert("RGBA")
    panel_rgba.alpha_composite(overlay)
    return panel_rgba.convert("RGB")


# ── Top video (Ken Burns + crossfade) ─────────────────────────────────────────

def _make_top_video(
    frame_paths: list[Path],
    duration:    float,
    output:      Path,
) -> None:
    """
    Create an animated 1080×700 video from 2-3 still frame PNGs.

    Each frame gets a Ken Burns zoompan (1.00 → 1.03).  FFmpeg xfade
    crossfades (TOP_XFADE seconds each) stitch the clips into one continuous
    video of exactly `duration` seconds.

    Timing math (n clips, total duration T, crossfade X):
        d_each = (T + X × (n-1)) / n
        xfade offset_k = Σ d_each[0..k] − k × X
    """
    n = len(frame_paths)
    assert n >= 2, "Need at least 2 frames for the crossfade top section"

    d_each    = (duration + TOP_XFADE * (n - 1)) / n
    n_f       = int(d_each * FPS)           # frames per clip for zoompan d=
    inc       = (ZOOM_END - ZOOM_START) / max(n_f, 1)
    # Add half a second buffer on input so zoompan always has enough frames
    input_dur = d_each + 0.5

    cmd = ["ffmpeg", "-y"]
    for fp in frame_paths:
        cmd += ["-loop", "1", "-framerate", str(FPS),
                "-t", f"{input_dur:.3f}", "-i", str(fp)]

    vf = (
        f"zoompan="
        f"z='min(zoom+{inc:.8f},{ZOOM_END})':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d={n_f}:s={CANVAS_W}x{IMAGE_H}:fps={FPS},"
        # Hard-clip to exactly the top zone — prevents any sub-pixel edge
        # artifacts from zoompan bleeding into the bottom panel area.
        f"crop={CANVAS_W}:{IMAGE_H}:0:0,"
        f"setsar=1,"
        f"format=yuv420p"
    )

    parts: list[str] = []
    # Zoompan each input
    for i in range(n):
        parts.append(f"[{i}:v]{vf}[zoom{i}]")

    # Xfade chain
    cumulative = d_each
    for k in range(1, n):
        offset = cumulative - TOP_XFADE
        left   = "[zoom0]"   if k == 1 else f"[xf{k-1}]"
        right  = f"[zoom{k}]"
        out    = "[topout]"  if k == n - 1 else f"[xf{k}]"
        parts.append(
            f"{left}{right}xfade=transition=fade:"
            f"duration={TOP_XFADE}:offset={offset:.3f}{out}"
        )
        cumulative += d_each - TOP_XFADE

    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[topout]",
        "-c:v", "libx264", "-profile:v", "high",
        "-crf", "18", "-preset", "slow",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-t", f"{duration:.3f}",
        str(output),
    ]

    log.info(f"Building animated top section: {n} images, {d_each:.2f}s each, "
             f"{TOP_XFADE}s xfade → {duration:.0f}s")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"Top video stderr:\n{r.stderr[-800:]}")
        raise RuntimeError(f"Top video encode failed (exit {r.returncode})")
    log.info(f"Top video: {output.name}  ({output.stat().st_size / 1e6:.1f} MB)")


# ── Final video composer ──────────────────────────────────────────────────────

def _make_final_video(
    top_video:      Path,
    overlay_png:    Path,
    music:          Optional[Path],
    duration:       float,
    output:         Path,
) -> None:
    """
    Combine the animated 1080×700 top video with a full-canvas RGBA overlay
    and background music into the final 1080×1920 MP4.

    overlay_png is a 1080×1920 RGBA image where:
      • top 700 px   — fully transparent (lets the animated top video show through)
      • bottom 1220 px — solid BG_COLOR + rendered text (fully opaque)

    Compositing is done AFTER padding so the bottom panel is the final paint
    layer: any xfade edge artifacts in the top video are covered by the opaque
    bottom section, completely preventing bleed into the text zone.

    FFmpeg filter graph:
      • pad:     extend top video from 1080×700 to 1080×1920 (dark fill below)
      • overlay: paint the RGBA overlay at 0:0 using alpha — top is transparent
                 (animated video shows through), bottom is opaque (text visible)
      • (optional) audio: music at 25% volume with 1s fade-out
    """
    fade_st = max(0.0, duration - 1.0)
    bg_hex  = f"0x{BG_COLOR[0]:02x}{BG_COLOR[1]:02x}{BG_COLOR[2]:02x}"  # 0x0a0a0f

    cmd = ["ffmpeg", "-y",
           "-i", str(top_video),
           # Full-canvas RGBA overlay: transparent top, opaque bottom text section
           "-loop", "1", "-framerate", str(FPS),
           "-t", f"{duration:.3f}", "-i", str(overlay_png)]

    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    # Pad top video to full canvas, then paint the RGBA overlay on top.
    # format=auto tells FFmpeg to respect the PNG alpha channel, so only the
    # opaque bottom section is painted — the transparent top passes through.
    fc_parts = [
        f"[0:v]pad={CANVAS_W}:{CANVAS_H}:0:0:color={bg_hex}[padded]",
        f"[padded][1:v]overlay=0:0:format=auto[vout]",
    ]
    if music:
        fc_parts.append(
            f"[2:a]volume={MUSIC_VOLUME},"
            f"atrim=0:{duration:.3f},"
            f"afade=t=out:st={fade_st:.3f}:d=1[aout]"
        )

    cmd += ["-filter_complex", ";".join(fc_parts),
            "-map", "[vout]"]
    if music:
        cmd += ["-map", "[aout]"]

    cmd += [
        "-c:v", "libx264", "-profile:v", "high",
        "-crf", "18", "-preset", "slow",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-s", f"{CANVAS_W}x{CANVAS_H}",
        "-t", f"{duration:.3f}",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]

    log.info(f"Compositing final 1080×1920 video → {output.name}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"Final video stderr:\n{r.stderr[-800:]}")
        raise RuntimeError(f"Final video encode failed (exit {r.returncode})")
    log.info(f"Video ready: {output.name}  ({output.stat().st_size / 1e6:.1f} MB)")


# ── Public API ────────────────────────────────────────────────────────────────

def assemble_text_card(
    paragraphs:  list[str],
    clip_paths:  list[Path],
    output_dir:  Path,
    duration:    float = DEFAULT_DURATION,
) -> Path:
    """
    Assemble an animated news card Short.

    Top section  : 2-3 Pexels clip frames crossfade with Ken Burns zoom.
    Bottom section: static text panel — vertically centred, never moves.

    Steps:
      1. Extract still frames from the first 2-3 clips → 1080×700 PNGs.
      2. Render the bottom text panel as a 1080×1220 PNG.
      3. Save slide_0.png (frame0 + bottom panel) for thumbnail generation.
      4. FFmpeg: zoompan + xfade the top frames → 1080×700 top.mp4.
      5. FFmpeg: pad top video + overlay bottom panel + mix music → video.mp4.

    Args:
        paragraphs:  2 story paragraphs with *asterisk markup* for cyan terms.
        clip_paths:  2-3 Pexels clips — first 2-3 provide the top images.
        output_dir:  Directory to write video.mp4 and slide_0.png into.
        duration:    Video duration in seconds (default 20 s).

    Returns:
        Path to video.mp4

    Raises:
        RuntimeError: If fewer than 2 clips are provided or FFmpeg fails.
    """
    if len(clip_paths) < 2:
        raise RuntimeError(
            "assemble_text_card requires at least 2 clip paths "
            "(2-3 images for the crossfade top section)"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use up to 3 clips for top images (2 is the minimum for a crossfade)
    n_imgs     = min(len(clip_paths), 3)
    top_clips  = clip_paths[:n_imgs]
    music      = _pick_music()

    with tempfile.TemporaryDirectory(prefix="cp_tc_") as tmp_str:
        tmp = Path(tmp_str)

        # 1. Prepare top image frames (1080×700 PNGs with CP watermark)
        log.info(f"Preparing {n_imgs} top image frames…")
        top_frames = _prepare_top_frames(top_clips, tmp)

        # 2. Compose static bottom text panel (1080×1220)
        log.info("Composing bottom text panel…")
        bottom_img = _compose_bottom_panel(paragraphs)

        # 3. Build the full-canvas RGBA overlay (1080×1920):
        #    • top 700 px    → fully transparent (animated top video shows through)
        #    • bottom 1220 px → solid BG + text (fully opaque)
        #    This overlay is the LAST paint layer in the FFmpeg composite,
        #    guaranteeing any xfade edge artifacts never bleed into the text zone.
        full_overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        full_overlay.paste(bottom_img.convert("RGBA"), (0, IMAGE_H))
        overlay_path = tmp / "overlay.png"
        full_overlay.save(overlay_path, "PNG")

        # 4. Build slide_0.png for the orchestrator's thumbnail generator
        #    = first top frame pasted above the bottom panel → full 1080×1920 RGB
        frame0  = Image.open(top_frames[0]).convert("RGB")
        canvas  = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
        canvas.paste(frame0,     (0, 0))
        canvas.paste(bottom_img, (0, IMAGE_H))
        slide0_path = output_dir / "slide_0.png"
        canvas.save(slide0_path, "PNG")
        log.info("slide_0.png saved for thumbnail generation")

        # 5. Build animated top video (1080×700) — Ken Burns + xfade
        top_video_path = tmp / "top.mp4"
        _make_top_video(top_frames, duration, top_video_path)

        # 6. Combine: animated top + RGBA overlay + music → final video
        video_out = output_dir / "video.mp4"
        _make_final_video(top_video_path, overlay_path, music, duration, video_out)

    return video_out


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path as P

    parser = argparse.ArgumentParser(
        description="Assemble a CipherPulse animated news card Short"
    )
    parser.add_argument("--output-dir", default="output/test", dest="output_dir")
    parser.add_argument("--duration",   type=float, default=DEFAULT_DURATION)
    args = parser.parse_args()

    out = P(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    demo = [
        "In *2011*, hackers broke into *Sony's PlayStation Network* and stole data from *77 million* accounts — including names, addresses, and *encrypted credit card* numbers. Sony took *23 days* to even notice.",
        "The attackers exploited an *unpatched vulnerability* and moved through Sony's network undetected for weeks. The breach cost Sony over *$171 million* and forced a complete shutdown of the gaming network.",
    ]

    cache = P("assets/footage_cache")
    clips = list(cache.rglob("*.mp4"))
    if len(clips) < 2:
        print("Need at least 2 clips in footage_cache — run footage_downloader first.")
        sys.exit(1)

    result = assemble_text_card(
        paragraphs=demo,
        clip_paths=clips[:3],
        output_dir=out,
        duration=args.duration,
    )
    print(f"\nVideo: {result}  ({result.stat().st_size / 1e6:.1f} MB)")
    print(f"Frame: {out / 'slide_0.png'}")
