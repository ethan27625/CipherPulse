"""
script_writer.py — AI-powered Short script generator.

Sends a structured prompt to Claude (claude-sonnet-4-20250514) and returns a
fully formatted CipherPulse video script with [VISUAL: keyword] tags, a parsed
title, hook, body, CTA, and a duration estimate.

Called by the orchestrator for every video, regardless of format. For format-6
(News React) topics, the orchestrator passes a `news_context` string from
news_fetcher.py's Headline.to_prompt_context() as additional grounding.

Cost: ~$0.025 per script at claude-sonnet-4-20250514 pricing (~$3/month at 3/day).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "script_writer.log"),
    ],
)
log = logging.getLogger("script_writer")

# ── Constants ─────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
WORDS_PER_MINUTE = 100      # Conservative floor calibrated from GuyNeural at rate=-8%
                            # (3 samples: 128/117/105 wpm — floor 105, use 100 for safety margin)
MAX_DURATION_SECONDS = 58   # Hard ceiling — YouTube Shorts cap
MIN_DURATION_SECONDS = 42   # Too short feels rushed
MAX_SCRIPT_RETRIES = 2      # Times to re-prompt if duration is out of range

# Format names for prompt context (matches topics.json format IDs)
FORMAT_NAMES: dict[int, str] = {
    1: "Incident Breakdown",
    2: "AI Reveal",
    3: "Myth Buster",
    4: "How It Works",
    5: "List / Ranking",
    6: "News React",
}

# ── System prompt ─────────────────────────────────────────────────────────────
# This is Claude's persistent instruction set — it defines everything about
# the CipherPulse voice, format, and rules. It never changes between calls.
# The user message supplies only the specific topic.

SYSTEM_PROMPT = """\
You are the scriptwriter for CipherPulse, a cybersecurity and AI short-form video channel.
Your job is to write punchy, authoritative 60-second scripts for YouTube Shorts, TikTok,
and Instagram Reels.

CHANNEL IDENTITY
- Name: CipherPulse | Tagline: "The Heartbeat of Digital Threats"
- Tone: Authoritative, slightly dramatic, educational but accessible
- Audience: Tech-curious 18-35 year olds with no assumed technical background
- Every sentence must be understandable by a curious 20-year-old with no tech background

WORD COUNT LIMIT (enforce strictly)
Total script word count: 80-95 words MAXIMUM.
This is non-negotiable. The voice is read at ~105 words/minute with pauses.
80 words = ~46 seconds. 95 words = ~54 seconds. Stay in this range.
Count your words before submitting. If you exceed 95 words, cut ruthlessly.

SCRIPT STRUCTURE (strictly follow this)
1. HOOK (2-3 seconds, ~8-10 words): An arresting opening line that stops the scroll.
   Examples:
   - "Your phone was hacked 3 times this week. You didn't notice."
   - "A 16-year-old just broke into NASA. Here's exactly how."
   - "This AI can clone your voice from a 3-second clip."
   The hook must be a statement or question — never start with "Have you ever" or "Did you know".

2. BODY (35-45 seconds, ~60-78 words): Short punchy sentences. No jargon unless
   immediately explained in plain language. Each sentence moves the story forward.
   No filler, no padding. Use specific facts (company names, dollar amounts, dates).
   For technical concepts, use a one-sentence analogy before explaining the mechanism.

3. CTA (2-3 seconds, ~8-12 words): A single sentence. Either "Follow CipherPulse for
   more" or a teaser hook for curiosity ("The scary part? This isn't even the worst one.")
   Never use multiple CTA sentences.

VISUAL TAGS
Every 5-8 seconds of spoken content, insert a [VISUAL: keyword phrase] tag on its own line.
These tags tell the footage downloader what stock clips to search for.
Good examples: [VISUAL: hacker typing dark room], [VISUAL: server room blue light],
[VISUAL: ransomware lock screen], [VISUAL: corporate building exterior],
[VISUAL: binary code streaming], [VISUAL: phone screen cracked], [VISUAL: data breach alert]
Use 4-6 visual tags total per script.

OUTPUT FORMAT
Respond with ONLY the following block — no preamble, no explanation, no markdown headers:

TITLE: <compelling title under 70 characters, written as a curiosity hook>
HOOK: <the opening 2-3 second line>
---
<full script body — hook through CTA, with [VISUAL: ...] tags embedded inline>
---
VISUAL_TAGS: <comma-separated list of just the keyword phrases from your [VISUAL:] tags>
EST_WORDS: <integer word count of the full script including hook and CTA>

FACTUAL ACCURACY RULES (non-negotiable)
- Use correct company names, dates, and incident details for real events
- Never fabricate cyberattacks or attribute fake incidents to real companies
- Never invent statistics — only cite figures that are publicly documented
- For real incidents: use publicly known facts (company, date, type, general impact)
- Do not speculate about unreported details of real incidents

COPYRIGHT RULES (non-negotiable)
- Write 100% original scripts — never reproduce text from articles or blogs
- For News React format: the provided headline is TOPIC INSPIRATION ONLY
  Write your own original analysis and commentary around the public facts
- Headlines state public facts (not copyrightable); your script is your own work
- Never quote articles even paraphrastically — rewrite everything in your own voice

CONTENT PROHIBITIONS
- Never include platform watermarks, branding, or calls to action for specific platforms
- Never use fear-mongering or exaggerated claims beyond documented facts
- Never recommend illegal activities
- Keep all language appropriate for a general audience (no graphic details of violence)
"""


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Script:
    """A fully parsed CipherPulse video script ready for downstream processing."""

    topic: str
    format_id: int
    title: str
    hook: str
    full_text: str          # Complete script: hook + body + CTA (no [VISUAL] tags)
    full_text_with_visuals: str  # Complete script including [VISUAL] tags
    visual_tags: list[str]  # Extracted tag keyword phrases for footage_downloader
    est_words: int
    est_duration_seconds: float
    news_context: Optional[str] = None  # Set for format-6 News React scripts
    raw_response: str = field(default="", repr=False)  # Full Claude response

    def is_valid_duration(self) -> bool:
        """Return True if estimated duration is within the acceptable window."""
        return MIN_DURATION_SECONDS <= self.est_duration_seconds <= MAX_DURATION_SECONDS

    def to_file_content(self) -> str:
        """Format for writing to script.txt in the output directory."""
        lines = [
            f"TITLE: {self.title}",
            f"TOPIC: {self.topic}",
            f"FORMAT: {self.format_id} — {FORMAT_NAMES.get(self.format_id, 'Unknown')}",
            f"EST_DURATION: {self.est_duration_seconds:.1f}s ({self.est_words} words)",
            "",
            "── SCRIPT (WITH VISUAL TAGS) ──────────────────────────────────────",
            self.full_text_with_visuals,
            "",
            "── VISUAL TAGS ────────────────────────────────────────────────────",
            ", ".join(self.visual_tags),
        ]
        if self.news_context:
            lines = [
                "── NEWS CONTEXT ───────────────────────────────────────────────────",
                self.news_context,
                "",
            ] + lines
        return "\n".join(lines)


# ── Duration estimation ────────────────────────────────────────────────────────

def estimate_duration(text: str) -> tuple[int, float]:
    """
    Estimate spoken duration from a script string.

    Strips [VISUAL: ...] tags (they aren't spoken), counts words, divides by
    WORDS_PER_MINUTE. Returns (word_count, duration_in_seconds).

    Real-world note: Edge-TTS at rate=-8% is slightly slower than the average
    speaker, so we use 145 wpm instead of 150 for a conservative estimate.
    """
    # Remove [VISUAL: ...] tags — they don't get spoken
    clean = re.sub(r"\[VISUAL:[^\]]+\]", "", text)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    words = len(clean.split())
    # 100 wpm — conservative floor from 3 GuyNeural samples (105/117/128 wpm observed)
    # Erring slow avoids the orchestrator having to retry for over-length audio.
    duration = (words / 100) * 60
    return words, duration


# ── Response parser ────────────────────────────────────────────────────────────

def parse_response(raw: str, topic: str, format_id: int,
                   news_context: Optional[str]) -> Script:
    """
    Parse Claude's raw text response into a Script dataclass.

    Expected format:
        TITLE: ...
        HOOK: ...
        ---
        <full script with [VISUAL:] tags>
        ---
        VISUAL_TAGS: tag1, tag2, ...
        EST_WORDS: 123

    We extract each field with regex. If a field is missing, we fall back
    to reasonable defaults rather than crashing — a partial script is better
    than a pipeline halt.
    """
    # Extract TITLE
    title_match = re.search(r"^TITLE:\s*(.+)$", raw, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else topic[:70]

    # Extract HOOK
    hook_match = re.search(r"^HOOK:\s*(.+)$", raw, re.MULTILINE)
    hook = hook_match.group(1).strip() if hook_match else ""

    # Extract the body between the two --- delimiters
    body_match = re.search(r"---\n(.*?)\n---", raw, re.DOTALL)
    full_text_with_visuals = body_match.group(1).strip() if body_match else raw

    # Remove [VISUAL:] tags to get speakable text
    full_text = re.sub(r"\[VISUAL:[^\]]+\]\n?", "", full_text_with_visuals)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

    # Extract VISUAL_TAGS line
    tags_match = re.search(r"^VISUAL_TAGS:\s*(.+)$", raw, re.MULTILINE)
    if tags_match:
        visual_tags = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]
    else:
        # Fallback: extract from inline [VISUAL: ...] tags
        visual_tags = re.findall(r"\[VISUAL:\s*([^\]]+)\]", full_text_with_visuals)

    # Use our own word count rather than trusting Claude's EST_WORDS
    est_words, est_duration = estimate_duration(full_text_with_visuals)

    return Script(
        topic=topic,
        format_id=format_id,
        title=title,
        hook=hook,
        full_text=full_text,
        full_text_with_visuals=full_text_with_visuals,
        visual_tags=visual_tags,
        est_words=est_words,
        est_duration_seconds=est_duration,
        news_context=news_context,
        raw_response=raw,
    )


# ── API call with retry ────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_claude(client: anthropic.Anthropic, user_message: str) -> str:
    """
    Send a message to Claude and return the raw text response.

    The @retry decorator from tenacity handles:
    - RateLimitError (429): wait 4s, 8s, 16s between retries
    - APIStatusError (5xx server errors): same backoff
    - Gives up after 3 attempts and re-raises the exception

    Exponential backoff explanation: instead of retrying immediately (which
    hammers a rate-limited API), we wait progressively longer — 4s, then 8s,
    then 16s. This is standard practice for any networked API call.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ── Main public function ───────────────────────────────────────────────────────

def generate_script(
    topic: str,
    format_id: int = 1,
    news_context: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Script:
    """
    Generate a CipherPulse video script for the given topic.

    Args:
        topic:        The video topic string (from topics.json or a live news headline)
        format_id:    Content format 1-6 (matches FORMAT_NAMES dict)
        news_context: For format-6 only — the formatted string from
                      Headline.to_prompt_context() (source + headline + summary).
                      Never used to copy text; only grounds Claude in public facts.
        api_key:      Override for ANTHROPIC_API_KEY env var (optional, for testing)

    Returns:
        Script dataclass with all parsed fields populated.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set and no api_key provided.
        RuntimeError: If Claude fails to produce a valid script after all retries.
    """
    resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not resolved_key:
        raise ValueError(
            "ANTHROPIC_API_KEY not set. Add it to .env or pass api_key= directly."
        )

    client = anthropic.Anthropic(api_key=resolved_key)
    format_name = FORMAT_NAMES.get(format_id, "Incident Breakdown")

    # ── Build the user message ─────────────────────────────────────────────────
    # The user message tells Claude the specific topic and format.
    # For News React, we inject the live headline as grounding context.

    if news_context and format_id == 6:
        user_message = f"""\
Write a CipherPulse Short in the "{format_name}" format.

LIVE NEWS CONTEXT (use these public facts as inspiration — write an original script):
{news_context}

The script should react to this story with your own original analysis and commentary.
Do not reproduce any text from the article. The headline is a public fact you can reference.
Explain the significance to a non-technical audience in an engaging, authoritative way.
"""
    else:
        user_message = f"""\
Write a CipherPulse Short in the "{format_name}" format.

TOPIC: {topic}

Create an original, engaging script about this topic following all format rules.
"""

    # ── Script generation loop with duration validation ────────────────────────
    # If the estimated duration is outside [MIN, MAX], re-prompt with guidance.
    # Max 2 correction attempts before accepting whatever we have.

    script: Optional[Script] = None

    for attempt in range(MAX_SCRIPT_RETRIES + 1):
        if attempt > 0:
            # Tell Claude exactly how to fix the duration on retry
            direction = "shorter" if script.est_duration_seconds > MAX_DURATION_SECONDS else "longer"
            target_words = int(WORDS_PER_MINUTE * MAX_DURATION_SECONDS / 60) - 5
            log.warning(
                f"Duration {script.est_duration_seconds:.1f}s is out of range "
                f"[{MIN_DURATION_SECONDS}-{MAX_DURATION_SECONDS}s]. "
                f"Retry {attempt}/{MAX_SCRIPT_RETRIES} — requesting {direction} script."
            )
            user_message += (
                f"\n\nPREVIOUS ATTEMPT WAS TOO {direction.upper()}. "
                f"Rewrite the script to be approximately {target_words} words "
                f"(spoken duration {MIN_DURATION_SECONDS}-{MAX_DURATION_SECONDS} seconds). "
                f"Keep the same topic and structure but adjust the content volume."
            )

        log.info(f"Calling Claude (attempt {attempt + 1}) — topic: {topic[:60]}")
        try:
            raw = _call_claude(client, user_message)
        except Exception as e:
            log.error(f"Claude API call failed after retries: {e}")
            raise RuntimeError(f"Script generation failed: {e}") from e

        script = parse_response(raw, topic, format_id, news_context)

        log.info(
            f"Script parsed — title: '{script.title}' | "
            f"{script.est_words} words | {script.est_duration_seconds:.1f}s | "
            f"{len(script.visual_tags)} visual tags"
        )

        if script.is_valid_duration():
            log.info("Duration valid — script accepted.")
            break

    # Accept the last attempt even if duration is slightly off —
    # voice_generator will measure the real audio duration as a final check.
    return script


# ── CLI entrypoint for manual testing ─────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a CipherPulse video script via Claude API"
    )
    parser.add_argument("--topic", type=str, help="Topic string for the video")
    parser.add_argument(
        "--format", type=int, default=1, dest="format_id",
        choices=[1, 2, 3, 4, 5, 6],
        help="Content format ID (1=Incident, 2=AI Reveal, 3=Myth, 4=HowItWorks, 5=List, 6=News)",
    )
    parser.add_argument(
        "--news-headline", type=str, dest="news_headline",
        help="For format 6: raw headline string (fetched by news_fetcher in production)",
    )
    parser.add_argument(
        "--save", type=str, metavar="DIR",
        help="Save script.txt to this directory",
    )
    args = parser.parse_args()

    # Defaults for quick interactive testing
    topic = args.topic or "WannaCry ransomware attack 2017"
    format_id = args.format_id

    news_context: Optional[str] = None
    if args.news_headline:
        format_id = 6
        # Simulate what news_fetcher.Headline.to_prompt_context() returns
        news_context = f"SOURCE: CLI test\nHEADLINE: {args.news_headline}\nSUMMARY: (provided via --news-headline flag)"
        topic = args.news_headline

    print(f"\nGenerating script for: {topic}")
    print(f"Format: {format_id} — {FORMAT_NAMES[format_id]}\n")

    script = generate_script(topic=topic, format_id=format_id, news_context=news_context)

    # Pretty-print results
    divider = "═" * 60
    print(divider)
    print(f"TITLE:    {script.title}")
    print(f"DURATION: {script.est_duration_seconds:.1f}s ({script.est_words} words) — {'✅ VALID' if script.is_valid_duration() else '⚠️  OUT OF RANGE'}")
    print(f"VISUALS:  {', '.join(script.visual_tags)}")
    print(divider)
    print("\n── FULL SCRIPT (with visual tags) ─────────────────────────────")
    print(script.full_text_with_visuals)
    print("\n── SPEAKABLE TEXT ONLY ─────────────────────────────────────────")
    print(script.full_text)

    if args.save:
        out_dir = Path(args.save)
        out_dir.mkdir(parents=True, exist_ok=True)
        script_path = out_dir / "script.txt"
        script_path.write_text(script.to_file_content())
        print(f"\n✅ Script saved to {script_path}")
