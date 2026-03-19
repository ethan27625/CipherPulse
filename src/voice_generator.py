"""
voice_generator.py — Text-to-speech voiceover and subtitle generator.

Converts a CipherPulse script into:
  - voiceover.mp3  : narration audio using Microsoft Edge-TTS (free, no API key)
  - subtitles.srt  : word-timed subtitle file for FFmpeg caption burning

Voice: en-US-GuyNeural — deep, authoritative male voice
Settings: rate=-8% (slightly slower for dramatic delivery), pitch=-3Hz

The SRT file groups words into 5-6 word caption chunks so the final video
displays large, readable text lines rather than one word at a time.

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
VOICE = "en-US-GuyNeural"
RATE = "-8%"               # Slightly slower than default — more dramatic delivery
PITCH = "-3Hz"             # Slightly lower pitch — deeper, authoritative tone
MAX_DURATION_SECONDS = 58  # Hard ceiling for Shorts
WORDS_PER_CAPTION = 5      # Words grouped per subtitle caption line
MIN_CUE_DURATION_MS = 800  # Minimum caption display time in milliseconds


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class WordEvent:
    """A single word with its start time from Edge-TTS word boundary events."""
    word: str
    start_ms: int    # milliseconds from audio start


@dataclass
class VoiceResult:
    """All outputs from a voice generation run."""
    mp3_path: Path
    srt_path: Path
    duration_seconds: float
    word_count: int
    caption_count: int

    def is_valid_duration(self) -> bool:
        return self.duration_seconds <= MAX_DURATION_SECONDS

    def summary(self) -> str:
        status = "✅ VALID" if self.is_valid_duration() else "⚠️  TOO LONG"
        return (
            f"{status} | {self.duration_seconds:.1f}s | "
            f"{self.word_count} words | {self.caption_count} captions"
        )


# ── SRT formatting helpers ────────────────────────────────────────────────────

def _ms_to_srt_time(ms: int) -> str:
    """
    Convert milliseconds to SRT timecode format: HH:MM:SS,mmm

    SRT uses a comma (not a dot) before milliseconds — this is a quirk of the
    format spec that trips up a lot of implementations. FFmpeg is strict about
    this when parsing subtitle files.

    Example: 3750 ms → "00:00:03,750"
    """
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1_000
    millis = ms % 1_000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _build_srt(word_events: list[WordEvent], total_duration_ms: int) -> str:
    """
    Group word events into caption cues and format as an SRT string.

    Grouping strategy:
    - Collect WORDS_PER_CAPTION words per cue
    - Each cue starts at the first word's timestamp
    - Each cue ends at the next cue's start (or total_duration_ms for the last)
    - Enforce MIN_CUE_DURATION_MS so fast-spoken words don't flash imperceptibly

    Why 5 words per caption for Shorts?
    Large bold text in the middle of a vertical 9:16 frame can only fit ~5-6
    words comfortably at a font size readable on a phone screen. Shorter chunks
    also pace better with the speech rhythm, matching what the viewer hears to
    what they read with minimal lag.
    """
    if not word_events:
        return ""

    # Split word_events into chunks of WORDS_PER_CAPTION
    chunks: list[list[WordEvent]] = []
    for i in range(0, len(word_events), WORDS_PER_CAPTION):
        chunk = word_events[i : i + WORDS_PER_CAPTION]
        if chunk:
            chunks.append(chunk)

    srt_blocks: list[str] = []

    for idx, chunk in enumerate(chunks):
        start_ms = chunk[0].start_ms

        # End time = next chunk's start, or total duration for last chunk
        if idx + 1 < len(chunks):
            end_ms = chunks[idx + 1][0].start_ms
        else:
            end_ms = total_duration_ms

        # Enforce minimum display time
        if end_ms - start_ms < MIN_CUE_DURATION_MS:
            end_ms = start_ms + MIN_CUE_DURATION_MS

        caption_text = " ".join(w.word for w in chunk)
        start_tc = _ms_to_srt_time(start_ms)
        end_tc = _ms_to_srt_time(end_ms)

        srt_blocks.append(f"{idx + 1}\n{start_tc} --> {end_tc}\n{caption_text}")

    return "\n\n".join(srt_blocks) + "\n"


# ── Core async generation ──────────────────────────────────────────────────────

async def _synthesize(
    text: str,
    mp3_path: Path,
    srt_path: Path,
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
    Async orchestrator: synthesize audio, build SRT, measure duration.

    Returns a VoiceResult with paths to both output files and metadata.
    """
    mp3_path = output_dir / "voiceover.mp3"
    srt_path = output_dir / "subtitles.srt"

    log.info(f"Starting synthesis — voice={VOICE} rate={RATE} pitch={PITCH}")
    log.info(f"Script length: {len(text.split())} words")

    word_events = await _synthesize(text, mp3_path, srt_path)

    # Measure real audio duration using mutagen
    # mutagen reads the MP3 header metadata — far more accurate than word count
    audio = MP3(str(mp3_path))
    duration_seconds = audio.info.length
    total_duration_ms = int(duration_seconds * 1000)

    log.info(f"Real audio duration: {duration_seconds:.2f}s")
    log.info(f"Word boundary events captured: {len(word_events)}")

    # Build and write SRT
    srt_content = _build_srt(word_events, total_duration_ms)
    srt_path.write_text(srt_content, encoding="utf-8")
    caption_count = srt_content.count("\n\n") + 1 if srt_content.strip() else 0
    log.info(f"SRT written: {srt_path} ({caption_count} captions)")

    return VoiceResult(
        mp3_path=mp3_path,
        srt_path=srt_path,
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
    print(f"  SRT:      {result.srt_path}")
    print(f"  Duration: {result.duration_seconds:.2f}s")
    print(f"  Words:    {result.word_count}")
    print(f"  Captions: {result.caption_count}")

    if not result.is_valid_duration():
        print(f"\n⚠️  Duration {result.duration_seconds:.1f}s exceeds {MAX_DURATION_SECONDS}s ceiling.")
        print("   The orchestrator will re-prompt script_writer for a shorter script.")
    else:
        print(f"\n✅  Duration valid — ready for video_assembler.")

    # Print first 10 caption cues so we can visually verify timing
    print(f"\n── First 10 SRT cues ──────────────────────────────────")
    srt_lines = result.srt_path.read_text().strip().split("\n\n")
    for cue in srt_lines[:10]:
        print(cue)
        print()
