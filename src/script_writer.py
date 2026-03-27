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
WORDS_PER_MINUTE = 110      # Calibrated to AndrewMultilingualNeural at rate=-5%
                            # (measured 110-132 wpm in practice — use 110 for conservative estimate)
MAX_DURATION_SECONDS = 58   # Hard ceiling — YouTube Shorts cap
MIN_DURATION_SECONDS = 42   # Too short feels rushed
MAX_SCRIPT_RETRIES = 2      # Times to re-prompt if duration is out of range

# Edu mode duration limits — shorter videos (60-80 words ≈ 33-44s at 110 wpm)
EDU_MAX_DURATION_SECONDS = 44   # ~80 words at 110 wpm
EDU_MIN_DURATION_SECONDS = 15   # Floor to catch pathologically short outputs

# Format names for prompt context (matches topics.json format IDs)
FORMAT_NAMES: dict[int, str] = {
    1: "Incident Breakdown",
    2: "AI Reveal",
    3: "Myth Buster",
    4: "How It Works",
    5: "List / Ranking",
    6: "News React",
    7: "Text Card",
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
Total script word count: 80-100 words MAXIMUM.
This is non-negotiable. The voice is read at ~110 words/minute.
80 words = ~44 seconds. 100 words = ~55 seconds. Stay in this range.
Count your words before submitting. If you exceed 120 words, cut ruthlessly.

SCRIPT STRUCTURE (strictly follow this)
1. HOOK (2-3 seconds, ~8-10 words): An arresting opening line that stops the scroll.
   Lead with the most shocking, specific detail FIRST — not with context-setting or setup.
   ✅ STRONG: "Russia hacked 18,000 companies through one poisoned software update."
   ✅ STRONG: "A 17-year-old stole $800 million in crypto — from his bedroom."
   ✅ STRONG: "This AI cloned a CEO's voice and wired $35 million to criminals."
   ❌ WEAK: "In 2020, hackers pulled off a devastating attack on thousands of companies."
   ❌ WEAK: "There's a new type of malware you really need to know about."
   ❌ WEAK: "Cybersecurity experts are warning about a dangerous new threat."
   The hook must be a statement or question — never start with "Have you ever" or "Did you know".

2. BODY (35-45 seconds, ~60-78 words): Short punchy sentences. No jargon unless
   immediately explained in plain language. Each sentence moves the story forward.
   No filler, no padding. Use specific facts (company names, dollar amounts, dates).
   For technical concepts, use a one-sentence analogy before explaining the mechanism.

3. CTA (2-3 seconds, ~8-12 words): A single sentence. Must be EITHER:
   - A provocative question that makes the viewer feel exposed or curious:
     ("Could your company survive this? Most don't find out until it's too late.")
     ("Think your password is safe? The hackers already know it isn't.")
   - A cliffhanger teaser that creates anticipation:
     ("And that wasn't even the worst part — follow for Part 2.")
     ("The scariest part? This attack is still happening right now.")
   NEVER end with "Follow CipherPulse for more" or any generic follow/subscribe CTA.
   Make viewers feel something — dread, urgency, curiosity — not just informed.
   Never use multiple CTA sentences.

VISUAL TAGS
Every 5-8 seconds of spoken content, insert a [VISUAL: keyword phrase] tag on its own line.
These tags tell the footage downloader what stock clips to search for.
Good examples: [VISUAL: hacker typing dark room], [VISUAL: server room blue light],
[VISUAL: ransomware lock screen], [VISUAL: corporate building exterior],
[VISUAL: binary code streaming], [VISUAL: phone screen cracked], [VISUAL: data breach alert]
Use 6-8 visual tags total per script — more tags means more clip variety in the final edit.

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

# ── Edu mode system prompt ─────────────────────────────────────────────────────
# Used when --mode edu is passed to the orchestrator. Produces short beginner-friendly
# explainer scripts (~60-80 words) aimed at CS students with zero background.

EDU_SYSTEM_PROMPT = """\
You are the scriptwriter for CipherPulse Edu, a free tech education channel for beginners.
Your job is to write punchy, approachable 30-40 second explainer scripts for YouTube Shorts.

AUDIENCE
- First-semester CS students or curious people with ZERO prior technical background
- Write like a knowledgeable friend explaining to you over lunch — NOT a textbook
- Use real-world analogies to make abstract concepts click instantly

WORD COUNT LIMIT (CRITICAL — enforced strictly)
Total word count: 60-80 words MAXIMUM. Count before you submit. Cut ruthlessly if over.
At ~110 words/minute: 60 words = 33 seconds. 80 words = 44 seconds.
Every word must earn its place. No filler, no preamble, no "In this video..."

SCRIPT STRUCTURE

1. HOOK (1-2 sentences, 10-15 words):
   A relatable question, surprising fact, or direct statement that earns attention.
   ✅ "Every website you visit has an invisible address — you just never see it."
   ✅ "Your terminal is the most powerful tool on your computer. Here's why."
   ✅ "Linux powers 96% of the world's web servers. It's also completely free."
   ❌ "Today we're going to learn about IP addresses."
   ❌ "Have you ever wondered how the internet works?"

2. EXPLANATION (35-50 words): What it is + why it matters + one concrete analogy.
   - Lead with the analogy FIRST, then the technical concept.
   - Keep it to ONE concept per video. No tangents.
   - Example: "Think of an IP address like a postal address — every device on a
     network gets a unique one so data knows where to go."

3. ACTIONABLE TAKEAWAY (1 sentence, 8-12 words):
   Something concrete they can do, try, or look up right now.
   ✅ "Open your terminal and type 'ip addr' to see your own IP address."
   ✅ "Search 'Kali Linux beginner VM' and set it up this weekend."
   ✅ "Run 'ls -la' in any folder to see hidden files and permissions."

FACTUAL ACCURACY RULES (non-negotiable)
- Stick to textbook-level, well-established facts. No speculation.
- Do not exaggerate capabilities of tools or techniques.
- If simplifying a nuanced concept, simplify it accurately — not incorrectly.
- Use correct tool names, command syntax, and terminology.

TONE RULES
- Conversational and engaging — like a cool senior student, not a lecturer
- Never say "In conclusion", "Today we learned", or "Don't forget to like"
- No drama, no fear-mongering — this is education, not a news channel
- Jargon is OK only if you immediately explain it in plain language
- Short punchy sentences. If a sentence exceeds 15 words, split it.

VISUAL TAGS
Insert 4-5 [VISUAL: keyword] tags throughout. Match the content being explained.
If explaining a terminal command, show terminal footage. If explaining networking,
show network/server footage.

OUTPUT FORMAT
Respond with ONLY this block — no preamble, no explanation:

TITLE: <curiosity-driven title ≤50 chars, e.g. "What is SSH? Explained simply">
HOOK: <the opening 1-2 sentence hook line>
---
<full script with [VISUAL: ...] tags embedded inline>
---
VISUAL_TAGS: <comma-separated tag phrases from your [VISUAL:] tags>
EST_WORDS: <integer word count of the full script>
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


# ── Text Card data structure ───────────────────────────────────────────────────

@dataclass
class TextCardContent:
    """
    Content for a format-7 Text Card Short — single-frame news card, no voiceover.

    The video is ONE composed frame (top image + bottom text) that Ken Burns
    zooms very subtly over 20 seconds.  paragraphs drive the on-screen text;
    title drives YouTube SEO (never shown on video).
    """

    topic:        str
    title:        str              # YouTube title / SEO — NOT shown on video
    category:     str              # DATA BREACH / RANSOMWARE / etc. (SEO context)
    hook_line:    str              # ≤10-word opening hook shown for 1.5s before card
    paragraphs:   list[str]        # 3 story paragraphs with *asterisk cyan markup*
    visual_tags:  list[str]        # 2-3 Pexels search terms for the top image
    raw_response: str = field(default="", repr=False)

    def to_file_content(self) -> str:
        """Serialize to script.txt in the output directory."""
        lines = [
            f"TITLE: {self.title}",
            f"TOPIC: {self.topic}",
            f"FORMAT: 7 — Text Card (single frame)",
            f"CATEGORY: {self.category}",
            f"HOOK_LINE: {self.hook_line}",
            f"VISUAL_TAGS: {' | '.join(self.visual_tags)}",
            "",
        ]
        for i, para in enumerate(self.paragraphs, 1):
            lines += [f"PARAGRAPH_{i}: {para}", ""]
        return "\n".join(lines)


# ── Text Card system prompt ───────────────────────────────────────────────────

TEXT_CARD_SYSTEM_PROMPT = """\
You write 3-paragraph cybersecurity story summaries for CipherPulse, a short-form
video channel. Each summary is displayed as a static news card that viewers read
on their phone — like a long-form social media post, not a slide deck.

CHANNEL IDENTITY
- Name: CipherPulse | Tagline: "The Heartbeat of Digital Threats"
- Tone: Authoritative, slightly dramatic, accessible
- Audience: Tech-curious 18-35 year olds, no technical background needed

OUTPUT FORMAT (output ONLY this block, exact field names, one per line):
TITLE: <compelling YouTube SEO title, 50-80 characters — never shown on video>
CATEGORY: <ALL CAPS, e.g. DATA BREACH / RANSOMWARE / AI THREAT / PRIVACY / FRAUD / HACKING>
HOOK_LINE: <under 10 words — curiosity or shock, makes viewer keep watching. Examples: "One mistake. 153 million passwords exposed." or "This hacker returned $611 million.">
VISUAL_TAGS: <Pexels search term 1> | <Pexels search term 2> | <Pexels search term 3>
PARAGRAPH_1: <Hook — what happened, lead with the most dramatic fact>
PARAGRAPH_2: <Context — backstory, how it happened, timeline>
PARAGRAPH_3: <Impact — why this matters to everyday people>

MARKUP RULES
- Wrap key terms in *asterisks* so the renderer colours them cyan:
    company names, numbers, dollar amounts, percentages, dates, key action verbs
- Example: "In *2013*, *Adobe* was breached and *153 million* user records were stolen."
- Multi-word terms work: "*the largest data breach in history*" colours all 6 words cyan
- Keep asterisks tight around the term — no leading/trailing spaces inside them

CONTENT RULES
- Total word count: 45-60 words HARD MAXIMUM across all 3 paragraphs. Count every word. If over 60, delete whole sentences until under.
- Each paragraph: 2 sentences ONLY, 15-20 words per paragraph
- Short punchy sentences (this is read on a phone screen, not an article)
- Include at least one specific number, date, or dollar figure in each paragraph
- No hashtags, no emojis, no "Follow us", no calls-to-action on the card itself
- Write for a curious non-technical 25-year-old
- NEVER fabricate incidents — only documented, publicly known facts

VISUAL_TAGS: use specific Pexels-friendly imagery (dark/tech aesthetic)
  Good: "hacker dark terminal" | "data breach warning screen" | "server room blue glow"
  Bad: "cybersecurity" | "technology" | "computer"
"""


def generate_text_card_content(
    topic: str,
    api_key: Optional[str] = None,
) -> TextCardContent:
    """
    Generate single-frame news card content for a format-7 Short.

    Produces a YouTube SEO title, a category label, 3 story paragraphs with
    *asterisk markup* for cyan key terms, and 2-3 Pexels visual search tags.

    Uses claude-haiku (fast + cheap — the content structure is simple).

    Args:
        topic:   Video topic string from topics.json
        api_key: Override for ANTHROPIC_API_KEY env var (optional)

    Returns:
        TextCardContent dataclass ready for text_card_assembler.assemble_text_card()

    Raises:
        ValueError:   If ANTHROPIC_API_KEY is not set.
        RuntimeError: If Claude fails to produce parseable output after retries.
    """
    resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not resolved_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")

    client       = anthropic.Anthropic(api_key=resolved_key)
    user_message = f"Write a CipherPulse news card for this topic: {topic}"

    log.info(f"Generating text card content for: {topic[:60]}")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=TEXT_CARD_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
    except Exception as exc:
        raise RuntimeError(f"Text card generation failed: {exc}") from exc

    # ── Parse structured fields ───────────────────────────────────────────────
    def _field(key: str) -> str:
        m = re.search(rf"^{key}:\s*(.+)$", raw, re.MULTILINE)
        return m.group(1).strip() if m else ""

    title     = _field("TITLE")     or topic[:80]
    category  = _field("CATEGORY")  or "CYBER THREAT"
    hook_line = _field("HOOK_LINE")  # empty string if missing — handled in assembler

    vt_raw      = _field("VISUAL_TAGS")
    visual_tags = [t.strip() for t in vt_raw.split("|") if t.strip()][:3]
    if not visual_tags:
        visual_tags = ["hacker dark terminal", "server room blue glow", "data breach screen"]

    paragraphs: list[str] = []
    for i in range(1, 4):
        p = _field(f"PARAGRAPH_{i}")
        if p:
            paragraphs.append(p)

    if len(paragraphs) < 3:
        log.warning(f"Only parsed {len(paragraphs)}/3 paragraphs — padding with placeholders")
        while len(paragraphs) < 3:
            paragraphs.append("")

    content = TextCardContent(
        topic=topic,
        title=title,
        category=category,
        hook_line=hook_line,
        paragraphs=paragraphs,
        visual_tags=visual_tags,
        raw_response=raw,
    )
    log.info(
        f"Text card — [{category}] {title!r} | "
        f"hook={hook_line!r} | "
        f"{sum(len(p.split()) for p in paragraphs)} words across 3 paragraphs"
    )
    return content


# ── Duration estimation ────────────────────────────────────────────────────────

def estimate_duration(text: str) -> tuple[int, float]:
    """
    Estimate spoken duration from a script string.

    Strips [VISUAL: ...] tags (they aren't spoken), counts words, divides by
    WORDS_PER_MINUTE. Returns (word_count, duration_in_seconds).
    """
    # Remove [VISUAL: ...] tags — they don't get spoken
    clean = re.sub(r"\[VISUAL:[^\]]+\]", "", text)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    words = len(clean.split())
    duration = (words / WORDS_PER_MINUTE) * 60
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
def _call_claude(
    client: anthropic.Anthropic,
    user_message: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    """
    Send a message to Claude and return the raw text response.

    Args:
        client:        Anthropic client instance
        user_message:  The user-turn message (topic + any retry instructions)
        system_prompt: System prompt to use — defaults to the news SYSTEM_PROMPT;
                       pass EDU_SYSTEM_PROMPT for edu mode

    The @retry decorator handles RateLimitError / APIStatusError with exponential
    backoff (4s → 8s → 16s), giving up after 3 total attempts.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ── Main public function ───────────────────────────────────────────────────────

def generate_script(
    topic: str,
    format_id: int = 1,
    news_context: Optional[str] = None,
    api_key: Optional[str] = None,
    mode: str = "news",
) -> Script:
    """
    Generate a CipherPulse video script for the given topic.

    Args:
        topic:        The video topic string (from topics.json or edu curriculum title)
        format_id:    Content format 1-6 (matches FORMAT_NAMES dict)
        news_context: For format-6 only — formatted headline string from news_fetcher.
                      Never used in edu mode.
        api_key:      Override for ANTHROPIC_API_KEY env var (optional)
        mode:         "news" (default) or "edu". Selects system prompt and duration limits.
                      edu scripts target 60-80 words (~33-44s); news targets 80-100 words (~44-55s).

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

    # ── Select system prompt and duration limits based on mode ─────────────────
    if mode == "edu":
        sys_prompt = EDU_SYSTEM_PROMPT
        min_dur    = EDU_MIN_DURATION_SECONDS
        max_dur    = EDU_MAX_DURATION_SECONDS
    else:
        sys_prompt = SYSTEM_PROMPT
        min_dur    = MIN_DURATION_SECONDS
        max_dur    = MAX_DURATION_SECONDS

    # ── Build the user message ─────────────────────────────────────────────────
    if mode == "edu":
        user_message = f"Write a CipherPulse Edu Short explaining: {topic}"
    elif news_context and format_id == 6:
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
    # If the estimated duration is outside [min_dur, max_dur], re-prompt.
    # Max 2 correction attempts before accepting whatever we have.

    script: Optional[Script] = None

    for attempt in range(MAX_SCRIPT_RETRIES + 1):
        if attempt > 0:
            direction = "shorter" if script.est_duration_seconds > max_dur else "longer"
            target_words = int(WORDS_PER_MINUTE * max_dur / 60) - 5
            log.warning(
                f"Duration {script.est_duration_seconds:.1f}s is out of range "
                f"[{min_dur}-{max_dur}s]. "
                f"Retry {attempt}/{MAX_SCRIPT_RETRIES} — requesting {direction} script."
            )
            user_message += (
                f"\n\nPREVIOUS ATTEMPT WAS TOO {direction.upper()}. "
                f"Rewrite the script to be approximately {target_words} words "
                f"(spoken duration {min_dur}-{max_dur} seconds). "
                f"Keep the same topic and structure but adjust the content volume."
            )

        log.info(f"Calling Claude (attempt {attempt + 1}) — topic: {topic[:60]}")
        try:
            raw = _call_claude(client, user_message, system_prompt=sys_prompt)
        except Exception as e:
            log.error(f"Claude API call failed after retries: {e}")
            raise RuntimeError(f"Script generation failed: {e}") from e

        script = parse_response(raw, topic, format_id, news_context)

        log.info(
            f"Script parsed — title: '{script.title}' | "
            f"{script.est_words} words | {script.est_duration_seconds:.1f}s | "
            f"{len(script.visual_tags)} visual tags"
        )

        in_range = min_dur <= script.est_duration_seconds <= max_dur
        if in_range:
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
        choices=[1, 2, 3, 4, 5, 6, 7],
        help="Content format ID (1=Incident, 2=AI Reveal, 3=Myth, 4=HowItWorks, 5=List, 6=News, 7=TextCard)",
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
