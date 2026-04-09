r"""
voice_generator.py — Text-to-speech voiceover and subtitle generator.

Converts a CipherPulse script into:
  - voiceover.mp3   : narration audio using Microsoft Edge-TTS (free, no API key)
  - subtitles.ass   : ASS subtitle file with per-word karaoke timing for FFmpeg

Voice: en-US-AndrewMultilingualNeural — warm, confident, authentic delivery
Settings: rate=-5% (natural pace), pitch=-1Hz (slight depth without artificiality)

The ASS file uses {\kf} karaoke tags so each word sweeps from white → cyan
as it is spoken, producing the high-retention caption style used by top Shorts.
Groups of ASS_WORDS_PER_LINE (6) words are shown at a time, positioned in the
lower third of the frame (80% from top) with a semi-transparent dark box.
Font size 72 (down from 84) accommodates the longer phrases without overflow.

Edge-TTS uses asyncio internally. All async functions are wrapped by the
public synchronous API (generate_voiceover) so callers don't need to
manage event loops.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import edge_tts
from mutagen.mp3 import MP3

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "voice_generator.log"),
    ],
)
log = logging.getLogger("voice_generator")

# ── Constants ─────────────────────────────────────────────────────────────────
VOICE = "en-US-AndrewMultilingualNeural"  # Warm, confident, authentic (News/Copilot)
# Alternative: "en-US-ChristopherNeural" — Reliable, Authority (News/Novel)
RATE  = "-5%"              # Natural pace — less robotic than -8%
PITCH = "-1Hz"             # Subtle depth — avoids the artificial "deepened" sound
MAX_DURATION_SECONDS = 58  # Hard ceiling for Shorts
ASS_WORDS_PER_LINE   = 6   # Words shown simultaneously in karaoke captions
MIN_CUE_DURATION_MS  = 800 # Minimum caption display time in milliseconds


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class WordEvent:
    """A single word with its start time from Edge-TTS word boundary events."""
    word: str
    start_ms: int    # milliseconds from audio start


@dataclass
class VoiceResult:
    """All outputs from a voice generation run."""
    mp3_path:        Path
    subtitle_path:   Path   # .ass file with karaoke timing (was srt_path)
    duration_seconds: float
    word_count:      int
    caption_count:   int

    def is_valid_duration(self) -> bool:
        return self.duration_seconds <= MAX_DURATION_SECONDS

    def summary(self) -> str:
        status = "✅ VALID" if self.is_valid_duration() else "⚠️  TOO LONG"
        return (
            f"{status} | {self.duration_seconds:.1f}s | "
            f"{self.word_count} words | {self.caption_count} captions"
        )


# ── ASS subtitle helpers ──────────────────────────────────────────────────────

def _ms_to_ass_time(ms: int) -> str:
    """
    Convert milliseconds to ASS timecode: H:MM:SS.cc  (centiseconds, not millis)

    ASS uses centiseconds (1/100s) not milliseconds before the decimal.
    Example: 3750 ms → "0:00:03.75"
    """
    cs = (ms % 1_000) // 10
    s  = (ms // 1_000) % 60
    m  = (ms // 60_000) % 60
    h  =  ms // 3_600_000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_ass(word_events: list[WordEvent], total_duration_ms: int) -> str:
    """
    Build an ASS subtitle file with per-word karaoke highlighting.

    Each dialogue line shows ASS_WORDS_PER_LINE words. The karaoke tag (backslash-kf)
    causes each word to sweep from SecondaryColour (white = upcoming) to
    PrimaryColour (cyan = spoken) over cs centiseconds as the voice reaches it.

    ASS colour format: &HAABBGGRR  (alpha, blue, green, red — reversed from HTML)
      Cyan  #00F2EA → R=0x00 G=0xF2 B=0xEA → BGR: EA F2 00 → &H00EAF200
      White #FFFFFF → &H00FFFFFF
      Black #000000 → &H00000000
      Box   60% opaque black  → alpha=0x99 → &H99000000

    Positioning:
      Alignment=2 (bottom-center) + MarginV=400 places text 400px from the
      bottom of the 1920px frame → 1520px from top → exactly 79% down.
      This keeps captions in the lower-third where viewers expect them.

    BorderStyle=3 draws an opaque box behind each line using BackColour.
    Outline=0 removes the text outline (the box handles contrast).
    """
    if not word_events:
        return ""

    # ASS colour constants
    CYAN_ASS  = "&H00EAF200"   # Cyan #00F2EA — active/current word
    WHITE_ASS = "&H00FFFFFF"   # White — upcoming words
    BLACK_ASS = "&H00000000"   # Black outline (unused with BorderStyle=3)
    BOX_ASS   = "&H99000000"   # 60% opaque black background box

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Oswald,72,{CYAN_ASS},{WHITE_ASS},{BLACK_ASS},{BOX_ASS},"
        "-1,0,0,0,100,100,3,0,3,0,0,2,80,80,400,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogue_lines: list[str] = []
    n = len(word_events)

    for i in range(0, n, ASS_WORDS_PER_LINE):
        chunk = word_events[i : i + ASS_WORDS_PER_LINE]
        if not chunk:
            continue

        start_ms = chunk[0].start_ms

        # End time: next chunk's first word, or end of audio
        next_idx = i + ASS_WORDS_PER_LINE
        end_ms = word_events[next_idx].start_ms if next_idx < n else total_duration_ms
        end_ms = max(end_ms, start_ms + MIN_CUE_DURATION_MS)

        # Build karaoke text: {\kf<cs>}word for each word in chunk
        # The space between {\kf}word fragments is correct ASS karaoke syntax.
        text_parts: list[str] = []
        for j, we in enumerate(chunk):
            # Duration this word is "active" = until next word starts
            if j + 1 < len(chunk):
                dur_ms = chunk[j + 1].start_ms - we.start_ms
            else:
                dur_ms = end_ms - we.start_ms
            dur_cs = max(5, dur_ms // 10)   # centiseconds, minimum 0.05s
            text_parts.append(f"{{\\kf{dur_cs}}}{we.word}")

        text     = " ".join(text_parts)
        start_tc = _ms_to_ass_time(start_ms)
        end_tc   = _ms_to_ass_time(end_ms)

        dialogue_lines.append(
            f"Dialogue: 0,{start_tc},{end_tc},Default,,0,0,0,,{text}"
        )

    return header + "\n".join(dialogue_lines) + "\n"


# ── Core async generation ──────────────────────────────────────────────────────

async def _synthesize(
    text: str,
    mp3_path: Path,
) -> list[WordEvent]:
    """
    Run Edge-TTS synthesis and derive word-level timing from sentence boundaries.

    Edge-TTS 7.x streams two event types alongside audio chunks:
      - 'audio'            : raw MP3 bytes to concatenate into the output file
      - 'SentenceBoundary' : sentence text + offset + duration (100ns ticks)

    Word-level events (WordBoundary) were removed in edge-tts 7.x by Microsoft.
    We compensate by distributing each sentence's duration proportionally across
    its words. For a sentence starting at T ms lasting D ms with N words:
      word[i].start_ms = T + (i / N) * D

    This gives subtitle timing that's accurate to within ~100-200ms — imperceptible
    to viewers. The captions will read naturally with the audio.

    100ns ticks → milliseconds: divide by 10,000
    (offset=1_000_000 → 100ms, duration=25_125_000 → 2512.5ms)
    """
    communicate = edge_tts.Communicate(text=text, voice=VOICE, rate=RATE, pitch=PITCH)

    sentence_events: list[dict] = []   # {text, offset_ms, duration_ms}
    audio_chunks: list[bytes] = []

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])

        elif chunk["type"] == "SentenceBoundary":
            offset_ms = chunk["offset"] // 10_000
            duration_ms = chunk["duration"] // 10_000
            sentence_text = chunk.get("text", "").strip()
            if sentence_text:
                sentence_events.append({
                    "text": sentence_text,
                    "offset_ms": offset_ms,
                    "duration_ms": duration_ms,
                })

    # Write concatenated audio bytes to MP3 file
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_path.write_bytes(b"".join(audio_chunks))
    log.info(f"Audio written: {mp3_path} ({mp3_path.stat().st_size / 1024:.1f} KB)")

    # Derive per-word timing from sentence boundaries
    word_events: list[WordEvent] = []
    for sent in sentence_events:
        words = sent["text"].split()
        n = len(words)
        if n == 0:
            continue
        ms_per_word = sent["duration_ms"] / n
        for i, word in enumerate(words):
            start_ms = int(sent["offset_ms"] + i * ms_per_word)
            word_events.append(WordEvent(word=word, start_ms=start_ms))

    log.info(
        f"Sentence boundaries: {len(sentence_events)} | "
        f"Derived word events: {len(word_events)}"
    )
    return word_events


async def _generate_async(
    text: str,
    output_dir: Path,
) -> VoiceResult:
    """
    Async orchestrator: synthesize audio, build ASS karaoke subtitles, measure duration.

    Returns a VoiceResult with paths to both output files and metadata.
    """
    mp3_path      = output_dir / "voiceover.mp3"
    subtitle_path = output_dir / "subtitles.ass"

    log.info(f"Starting synthesis — voice={VOICE} rate={RATE} pitch={PITCH}")
    log.info(f"Script length: {len(text.split())} words")

    word_events = await _synthesize(text, mp3_path)

    # Measure real audio duration using mutagen
    audio = MP3(str(mp3_path))
    duration_seconds  = audio.info.length
    total_duration_ms = int(duration_seconds * 1000)

    log.info(f"Real audio duration: {duration_seconds:.2f}s")
    log.info(f"Word boundary events captured: {len(word_events)}")

    # Build and write ASS karaoke subtitle file
    ass_content   = _build_ass(word_events, total_duration_ms)
    subtitle_path.write_text(ass_content, encoding="utf-8")
    # Count Dialogue lines = number of caption groups
    caption_count = ass_content.count("\nDialogue:")
    log.info(f"ASS written: {subtitle_path} ({caption_count} caption groups)")

    return VoiceResult(
        mp3_path=mp3_path,
        subtitle_path=subtitle_path,
        duration_seconds=duration_seconds,
        word_count=len(word_events),
        caption_count=caption_count,
    )


# ── Public synchronous API ────────────────────────────────────────────────────

def generate_voiceover(
    script_text: str,
    output_dir: Path,
) -> VoiceResult:
    """
    Generate voiceover MP3 and SRT subtitle file from a script string.

    This is the primary entry point called by the orchestrator and other modules.
    It wraps the async implementation so callers don't need to manage event loops.

    Args:
        script_text: The speakable script text — no [VISUAL: ...] tags.
                     Use Script.full_text (not full_text_with_visuals) from
                     script_writer.py.
        output_dir:  Directory to write voiceover.mp3 and subtitles.srt.
                     Created if it doesn't exist.

    Returns:
        VoiceResult with paths, real duration, word count, and caption count.
        Call result.is_valid_duration() to check against the 58s ceiling.

    Why asyncio.run() here?
    Edge-TTS is async-native. asyncio.run() creates a fresh event loop, runs
    our coroutine to completion, then tears the loop down. This is the standard
    way to call async code from a synchronous context in Python 3.7+. The
    orchestrator is synchronous, so we handle the async boundary here rather
    than making every caller async-aware.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean script text: strip [VISUAL: ...] tags if caller passed the wrong field
    clean_text = re.sub(r"\[VISUAL:[^\]]+\]\n?", "", script_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()

    if not clean_text:
        raise ValueError("script_text is empty after stripping visual tags")

    try:
        result = asyncio.run(_generate_async(clean_text, output_dir))
    except Exception as e:
        log.error(f"Voice generation failed: {e}")
        raise

    log.info(f"Voice generation complete — {result.summary()}")
    return result


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Edge-TTS voiceover MP3 + SRT from script text"
    )
    parser.add_argument(
        "--script", type=str,
        help="Script text to synthesize (wrap in quotes)",
    )
    parser.add_argument(
        "--script-file", type=str, dest="script_file",
        help="Path to a script.txt file (reads full_text section)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="output/test",
        dest="output_dir",
        help="Directory to write voiceover.mp3 and subtitles.srt (default: output/test)",
    )
    args = parser.parse_args()

    # Resolve script text source
    if args.script_file:
        raw = Path(args.script_file).read_text()
        # Extract the script body from script.txt (written by Script.to_file_content())
        # The section starts after "── SCRIPT (WITH VISUAL TAGS) ──..." and ends before
        # "── VISUAL TAGS ──..." — we strip [VISUAL:] tags to get speakable text.
        match = re.search(
            r"── SCRIPT \(WITH VISUAL TAGS\)[^\n]*\n(.*?)(?=── VISUAL TAGS|\Z)",
            raw, re.DOTALL
        )
        if match:
            script_text = match.group(1).strip()
        else:
            script_text = raw
    elif args.script:
        script_text = args.script
    else:
        # Built-in demo text for quick smoke testing
        script_text = (
            "Your phone was hacked three times this week. You didn't notice.\n\n"
            "In 2017, a piece of ransomware called WannaCry spread to 300,000 computers "
            "across 150 countries in a single day. It locked every file on every machine "
            "it touched and demanded Bitcoin payment to unlock them.\n\n"
            "The weapon? Stolen NSA hacking tools. Code built by the US government to "
            "spy on enemies — now turned against hospitals, banks, and ordinary people.\n\n"
            "A 22-year-old researcher stopped it by accident, registering a domain name "
            "that acted as a kill switch. Pure luck saved the internet.\n\n"
            "Follow CipherPulse for more."
        )
        log.info("No script provided — using built-in demo text")

    out = Path(args.output_dir)
    print(f"\nSynthesizing to: {out}/")
    print(f"Voice: {VOICE} | Rate: {RATE} | Pitch: {PITCH}\n")

    result = generate_voiceover(script_text, out)

    print(f"\n{'═' * 55}")
    print(f"RESULT: {result.summary()}")
    print(f"{'═' * 55}")
    print(f"  MP3:      {result.mp3_path}")
    print(f"  ASS:      {result.subtitle_path}")
    print(f"  Duration: {result.duration_seconds:.2f}s")
    print(f"  Words:    {result.word_count}")
    print(f"  Captions: {result.caption_count}")

    if not result.is_valid_duration():
        print(f"\n⚠️  Duration {result.duration_seconds:.1f}s exceeds {MAX_DURATION_SECONDS}s ceiling.")
        print("   The orchestrator will re-prompt script_writer for a shorter script.")
    else:
        print(f"\n✅  Duration valid — ready for video_assembler.")

    # Print first 10 dialogue lines so we can visually verify timing
    print(f"\n── First 10 ASS dialogue lines ──────────────────────────")
    ass_lines = [l for l in result.subtitle_path.read_text().splitlines() if l.startswith("Dialogue:")]
    for line in ass_lines[:10]:
        print(line)
