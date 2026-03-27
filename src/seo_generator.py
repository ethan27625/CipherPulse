"""
seo_generator.py — Platform-specific SEO metadata generator.

Makes one Anthropic API call per video and produces metadata.json containing
optimised titles, descriptions, captions, and hashtags for all three platforms.

Output schema (metadata.json):
{
  "generated_at": "ISO timestamp",
  "topic": "...",
  "format_id": 1,
  "youtube": {
    "title":       "Under 70 chars, curiosity hook",
    "description": "~200 words, keywords in first 2 lines, hashtags at end",
    "tags":        ["tag1", ..., "tag10"]
  },
  "tiktok": {
    "caption": "Under 150 chars, punchy, hashtags woven in"
  },
  "instagram": {
    "caption": "Hook + context + 30 hashtags in block at end"
  }
}

Why one call instead of three?
Generating all platforms in a single prompt costs the same tokens as three
separate calls but runs in one round-trip (~1-2s vs ~4-6s). The shared context
(script, topic, format) also keeps descriptions consistent across platforms —
you don't end up with contradictory angles on the same video.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        logging.FileHandler(LOG_DIR / "seo_generator.log"),
    ],
)
log = logging.getLogger("seo_generator")

# ── Constants ─────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1200

YOUTUBE_TITLE_MAX   = 50    # Shorter titles perform better in Shorts discovery
YOUTUBE_TAGS_MAX    = 10    # Stay within the 500-char total tags limit
TIKTOK_CAPTION_MAX  = 150   # TikTok caption character limit
INSTAGRAM_TAGS_MAX  = 30    # Instagram penalises >30 hashtags

FORMAT_NAMES: dict[int, str] = {
    1: "Incident Breakdown",
    2: "AI Reveal",
    3: "Myth Buster",
    4: "How It Works",
    5: "List / Ranking",
    6: "News React",
}

# ── System prompt ─────────────────────────────────────────────────────────────
SEO_SYSTEM_PROMPT = """\
You are the SEO and social media manager for CipherPulse, a cybersecurity and
AI short-form video channel with the tagline "The Heartbeat of Digital Threats."

Your job is to write platform-optimised metadata for a video that has already
been scripted and produced. You will receive the script and topic details.

PLATFORM REQUIREMENTS

YouTube:
- title: Generate THREE title options (title_candidates array). Select the most
  compelling one as "title". Each must be under 50 characters. Rules:
    • Curiosity or emotion driven — make the viewer NEED to watch
    • No colons or "Title: Subtitle" format
    • No clickbait — must accurately reflect the content
    • Examples: "Adobe forgot to encrypt passwords" / "The hacker who returned $611M"
  If all 3 exceed 50 chars, truncate the best one at the nearest word boundary.
  Do NOT add "| CipherPulse" suffix.
- description: Use this exact structure (no prose intro — jump straight in):
    Line 1: One punchy hook sentence about the topic (can reuse the hook_line or rephrase).
    Line 2: One sentence of context with relevant SEO keywords worked in naturally
            (e.g. "cybersecurity breach", "data leak", "hacking explained", "zero-day exploit").
    Line 3: One sentence on why this matters to everyday people.
    Line 4: (blank)
    Line 5: Follow @CipherPulse for daily cyber threat breakdowns.
    Line 6: (blank)
    Line 7: Hashtags — always include #cybersecurity #hacking #dataleaks #shorts #cipherpulse,
            plus 3-5 topic-specific ones. DO NOT exceed 10 hashtags total. All on one line.
- tags: 8-10 keyword tags. Mix broad (#cybersecurity) and specific (#pegasusspyware).
  Lowercase, no spaces inside tags, max 10 tags total. Return as JSON array.

TikTok:
- caption: Under 150 characters. Punchy hook + 3-4 hashtags woven in naturally.
  Must include #fyp and #techtok. Conversational tone. No long hashtag blocks.

Instagram:
- caption: Hook line (first 125 chars must be compelling — shown before "more").
  Then 1-2 sentences of context. Then a blank line. Then a hashtag block of
  exactly 30 hashtags mixing broad and niche, one per line.
  Broad: #cybersecurity #hacking #tech #ai #privacy #infosec
  Niche: topic-specific tags that real searchers use.

OUTPUT FORMAT
Respond with ONLY a JSON code block — no prose before or after:

```json
{
  "youtube": {
    "title": "best option ≤50 chars",
    "title_candidates": ["option 1", "option 2", "option 3"],
    "description": "...",
    "tags": ["tag1", "tag2"]
  },
  "tiktok": {
    "caption": "..."
  },
  "instagram": {
    "caption": "..."
  }
}
```

Validate before responding:
- YouTube title ≤ 50 characters
- TikTok caption ≤ 150 characters
- Instagram caption has exactly 30 hashtags
- All text is original — never copy the script verbatim
"""


# ── Edu mode SEO prompt ────────────────────────────────────────────────────────
# Used when mode="edu". Optimises for educational discovery keywords rather than
# breaking-news / incident keywords.

EDU_SEO_SYSTEM_PROMPT = """\
You are the SEO and social media manager for CipherPulse Edu, a free tech education
channel teaching cybersecurity, Linux, networking, AI, and CS fundamentals to beginners.

Your job is to write discovery-optimised metadata for SHORT educational explainer videos
(~30-45 seconds). This is NOT news — it is beginner education content.

PLATFORM REQUIREMENTS

YouTube:
- title: Under 50 characters. Search-optimised, curiosity-driven.
  Format: "What is [topic]? Explained simply" / "[Topic] in 30 seconds" /
          "How [topic] works (beginner)" / "[Tool] explained for beginners"
  Generate THREE title_candidates. Pick the most searchable as "title".
  Prioritise long-tail search terms beginners actually type.
- description:
  Line 1: "Learn [topic] in under 60 seconds — no experience needed."
  Line 2: One sentence on why this topic matters for cybersecurity/AI/tech careers
           (weave in SEO keywords naturally: e.g. "linux command line", "kali tools",
           "network protocols", "ethical hacking basics").
  Line 3: One practical tip or next step for learners.
  Line 4: (blank)
  Line 5: "Follow @CipherPulse — free cyber & AI education every day."
  Line 6: (blank)
  Line 7: 8-10 hashtags on ONE line. Always include #cybersecurity #learnhacking
           #techeducation #shorts #cipherpulse plus 3-5 topic-specific tags.
- tags: 8-10 keyword tags. Mix broad (cybersecurity, linux, networking) and specific
  (dns explained, ssh tutorial, kali linux beginner). Lowercase. Return as JSON array.

TikTok:
- caption: Under 150 characters. Conversational, educational tone.
  Must include #fyp and #learnontiktok.
  Example: "DNS is basically the internet's phone book 📱 here's how it works #fyp #learnontiktok #tech"

Instagram:
- caption: Hook sentence (compelling first 125 chars). Then 1-2 educational sentences.
  Blank line. Then exactly 30 hashtags one per line.
  Include: #cybersecurity #coding #linux #hacking #techeducation #learnhacking
  #ethicalhacking #infosec #computerscience #programming #kalilinux #networking
  #cybersecuritytips #techstudent #hackthebox #cipherpulse #shorts #learntocode
  plus 12 more topic-specific/niche tags relevant to the lesson.

OUTPUT FORMAT
Respond with ONLY a JSON code block — no prose:

```json
{
  "youtube": {
    "title": "best option ≤50 chars",
    "title_candidates": ["option 1", "option 2", "option 3"],
    "description": "...",
    "tags": ["tag1", "tag2"]
  },
  "tiktok": {"caption": "..."},
  "instagram": {"caption": "..."}
}
```

Validate before responding:
- YouTube title ≤ 50 characters
- TikTok caption ≤ 150 characters
- Instagram caption has exactly 30 hashtags
"""


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class YouTubeMetadata:
    title: str
    description: str
    tags: list[str]


@dataclass
class TikTokMetadata:
    caption: str


@dataclass
class InstagramMetadata:
    caption: str


@dataclass
class VideoMetadata:
    """All platform metadata for one CipherPulse video."""
    topic: str
    format_id: int
    generated_at: str
    youtube: YouTubeMetadata
    tiktok: TikTokMetadata
    instagram: InstagramMetadata
    raw_response: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON writing."""
        return {
            "generated_at": self.generated_at,
            "topic": self.topic,
            "format_id": self.format_id,
            "youtube": {
                "title":       self.youtube.title,
                "description": self.youtube.description,
                "tags":        self.youtube.tags,
            },
            "tiktok": {
                "caption": self.tiktok.caption,
            },
            "instagram": {
                "caption": self.instagram.caption,
            },
        }

    def save(self, output_dir: Path) -> Path:
        """Write metadata.json to output_dir and return the path."""
        out = Path(output_dir) / "metadata.json"
        out.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        log.info(f"Metadata saved: {out}")
        return out


# ── Response parsing ───────────────────────────────────────────────────────────

def _extract_json_block(raw: str) -> dict:
    """
    Extract and parse the JSON block from Claude's response.

    Claude wraps JSON in a fenced code block:
        ```json
        { ... }
        ```

    We use a regex to find the block, then json.loads() to parse it.
    If no fenced block is found, we try parsing the whole response as JSON
    (sometimes Claude omits the fence in short responses).

    Why a fenced block instead of plain JSON?
    When you ask an LLM for JSON, it tends to add explanatory prose unless
    you explicitly instruct it otherwise. The fence instruction ("respond with
    ONLY a JSON code block") is the most reliable prompt pattern for this.
    Extracting via regex is more robust than hoping the entire response is valid JSON.

    Raises:
        ValueError: If no valid JSON can be extracted from the response.
    """
    # Try fenced block first
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # Fallback: treat whole response as JSON
        json_str = raw.strip()

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse JSON from Claude response: {e}\n"
            f"Raw response (first 500 chars): {raw[:500]}"
        )


def _validate_and_clamp(data: dict) -> dict:
    """
    Validate platform-specific constraints and clamp values that exceed limits.

    Rather than raising errors on minor limit violations (Claude sometimes
    generates a 72-char YouTube title), we log a warning and truncate at the
    nearest word boundary. Hard failures are reserved for missing required fields.

    This is the "be liberal in what you accept" principle from Postel's Law —
    the robustness principle used in internet protocol design.
    """
    yt = data.get("youtube", {})
    tt = data.get("tiktok", {})
    ig = data.get("instagram", {})

    # YouTube title
    title = yt.get("title", "")
    if len(title) > YOUTUBE_TITLE_MAX:
        truncated = title[:YOUTUBE_TITLE_MAX].rsplit(" ", 1)[0]
        log.warning(f"YouTube title truncated: {len(title)} → {len(truncated)} chars")
        yt["title"] = truncated

    # YouTube tags
    tags = yt.get("tags", [])
    if len(tags) > YOUTUBE_TAGS_MAX:
        log.warning(f"YouTube tags clamped: {len(tags)} → {YOUTUBE_TAGS_MAX}")
        yt["tags"] = tags[:YOUTUBE_TAGS_MAX]

    # TikTok caption
    caption = tt.get("caption", "")
    if len(caption) > TIKTOK_CAPTION_MAX:
        truncated = caption[:TIKTOK_CAPTION_MAX].rsplit(" ", 1)[0]
        log.warning(f"TikTok caption truncated: {len(caption)} → {len(truncated)} chars")
        tt["caption"] = truncated

    # Instagram: count hashtags (any word starting with #)
    ig_caption = ig.get("caption", "")
    hashtag_count = len(re.findall(r"#\w+", ig_caption))
    if hashtag_count != INSTAGRAM_TAGS_MAX:
        log.warning(
            f"Instagram hashtag count: {hashtag_count} (target {INSTAGRAM_TAGS_MAX})"
        )

    data["youtube"] = yt
    data["tiktok"] = tt
    data["instagram"] = ig
    return data


def _parse_response(
    raw: str, topic: str, format_id: int
) -> VideoMetadata:
    """
    Parse Claude's raw text into a VideoMetadata dataclass.

    Args:
        raw:       Full text response from Claude
        topic:     Original topic string (for metadata record-keeping)
        format_id: Content format ID

    Returns:
        Populated VideoMetadata object.

    Raises:
        ValueError: If required fields are missing from the response.
    """
    data = _extract_json_block(raw)
    data = _validate_and_clamp(data)

    yt_data  = data.get("youtube", {})
    tt_data  = data.get("tiktok", {})
    ig_data  = data.get("instagram", {})

    # Validate required fields exist
    for field_name, value in [
        ("youtube.title",      yt_data.get("title")),
        ("youtube.description", yt_data.get("description")),
        ("youtube.tags",       yt_data.get("tags")),
        ("tiktok.caption",     tt_data.get("caption")),
        ("instagram.caption",  ig_data.get("caption")),
    ]:
        if not value:
            raise ValueError(f"Missing required field in SEO response: {field_name}")

    return VideoMetadata(
        topic=topic,
        format_id=format_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        youtube=YouTubeMetadata(
            title=yt_data["title"],
            description=yt_data["description"],
            tags=yt_data["tags"],
        ),
        tiktok=TikTokMetadata(
            caption=tt_data["caption"],
        ),
        instagram=InstagramMetadata(
            caption=ig_data["caption"],
        ),
        raw_response=raw,
    )


# ── API call ───────────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_claude(
    client: anthropic.Anthropic,
    user_message: str,
    system_prompt: str = SEO_SYSTEM_PROMPT,
) -> str:
    """Call Claude and return raw text response, with exponential backoff retry."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ── Public API ────────────────────────────────────────────────────────────────

def generate_metadata(
    topic: str,
    format_id: int,
    script_text: str,
    video_title: str,
    output_dir: Optional[Path] = None,
    api_key: Optional[str] = None,
    mode: str = "news",
) -> VideoMetadata:
    """
    Generate platform-specific SEO metadata for a CipherPulse video.

    Args:
        topic:       Topic string (from topics.json or edu curriculum title)
        format_id:   Content format ID 1-6
        script_text: The speakable script text (Script.full_text, no [VISUAL] tags)
        video_title: The video title from Script.title — used as YouTube title seed
        output_dir:  If provided, saves metadata.json here automatically
        api_key:     Optional ANTHROPIC_API_KEY override (reads from .env by default)
        mode:        "news" (default) or "edu". Selects the SEO prompt template.

    Returns:
        VideoMetadata dataclass with all platform fields populated.
        Call .save(output_dir) to write metadata.json.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set or response parsing fails.
        RuntimeError: If all API retry attempts are exhausted.
    """
    resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not resolved_key:
        raise ValueError("ANTHROPIC_API_KEY not set — add to .env or pass api_key=")

    client = anthropic.Anthropic(api_key=resolved_key)
    format_name = FORMAT_NAMES.get(format_id, "Unknown")

    # Select system prompt based on mode
    sys_prompt = EDU_SEO_SYSTEM_PROMPT if mode == "edu" else SEO_SYSTEM_PROMPT

    user_message = f"""\
Generate platform-optimised SEO metadata for this CipherPulse video.

TOPIC: {topic}
FORMAT: {format_name}
DRAFT TITLE (use as inspiration, improve if possible): {video_title}

SCRIPT:
{script_text}

Write metadata for YouTube, TikTok, and Instagram following all platform rules.
Return only the JSON block, nothing else.
"""

    log.info(f"Generating SEO metadata for: {topic[:60]}")

    try:
        raw = _call_claude(client, user_message, system_prompt=sys_prompt)
    except Exception as e:
        log.error(f"SEO API call failed: {e}")
        raise RuntimeError(f"SEO generation failed: {e}") from e

    metadata = _parse_response(raw, topic, format_id)

    log.info(f"YouTube title ({len(metadata.youtube.title)} chars): {metadata.youtube.title}")
    # Log all three title candidates if present in raw response
    try:
        raw_data = _extract_json_block(metadata.raw_response)
        candidates = raw_data.get("youtube", {}).get("title_candidates", [])
        if candidates:
            log.info(f"Title candidates: {candidates}")
    except Exception:
        pass
    log.info(f"YouTube tags: {metadata.youtube.tags}")
    log.info(f"TikTok caption ({len(metadata.tiktok.caption)} chars)")
    ig_tag_count = len(re.findall(r"#\w+", metadata.instagram.caption))
    log.info(f"Instagram hashtags: {ig_tag_count}")

    if output_dir:
        metadata.save(output_dir)

    return metadata


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Generate platform-specific SEO metadata for a CipherPulse video"
    )
    parser.add_argument(
        "--script-file", type=str, dest="script_file",
        help="Path to script.txt from script_writer (reads topic, title, and script body)",
    )
    parser.add_argument(
        "--topic", type=str,
        help="Topic string override (if not reading from script file)",
    )
    parser.add_argument(
        "--title", type=str,
        help="Video title override",
    )
    parser.add_argument(
        "--format", type=int, default=1, dest="format_id",
        choices=[1, 2, 3, 4, 5, 6],
    )
    parser.add_argument(
        "--output-dir", type=str, default="output/test", dest="output_dir",
        help="Directory to write metadata.json (default: output/test)",
    )
    args = parser.parse_args()

    # ── Resolve inputs ─────────────────────────────────────────────────────────
    topic = args.topic or "Pegasus spyware targeting journalists"
    title = args.title or "How Governments Spy on Journalists Using Your Phone"
    format_id = args.format_id
    script_text = ""

    if args.script_file:
        raw_file = Path(args.script_file).read_text()

        # Extract topic
        tm = re.search(r"^TOPIC:\s*(.+)$", raw_file, re.MULTILINE)
        if tm:
            topic = tm.group(1).strip()

        # Extract title
        ttm = re.search(r"^TITLE:\s*(.+)$", raw_file, re.MULTILINE)
        if ttm:
            title = ttm.group(1).strip()

        # Extract format
        fm = re.search(r"^FORMAT:\s*(\d+)", raw_file, re.MULTILINE)
        if fm:
            format_id = int(fm.group(1))

        # Extract script body (strip visual tags for clean text)
        body_match = re.search(
            r"── SCRIPT \(WITH VISUAL TAGS\)[^\n]*\n(.*?)(?=── VISUAL TAGS|\Z)",
            raw_file, re.DOTALL
        )
        if body_match:
            script_text = re.sub(r"\[VISUAL:[^\]]+\]\n?", "", body_match.group(1)).strip()

    if not script_text:
        # Fallback demo script for testing without a script file
        script_text = (
            "Governments are reading journalists' texts right now. "
            "Meet Pegasus, the most dangerous spyware on Earth. "
            "Created by Israeli company NSO Group, Pegasus infects phones without a single click. "
            "Once installed it reads every message, records calls, and activates your camera secretly. "
            "Over 50,000 journalists and activists worldwide have been targeted. "
            "Your phone could be next. Follow CipherPulse for more."
        )

    out_dir = Path(args.output_dir)

    print(f"\nGenerating SEO metadata for: {topic}")
    print(f"Title: {title}")
    print(f"Format: {format_id} — {FORMAT_NAMES[format_id]}\n")

    metadata = generate_metadata(
        topic=topic,
        format_id=format_id,
        script_text=script_text,
        video_title=title,
        output_dir=out_dir,
    )

    divider = "═" * 60
    print(divider)
    print("YOUTUBE")
    print(divider)
    print(f"Title ({len(metadata.youtube.title)} chars):")
    print(f"  {metadata.youtube.title}")
    print(f"\nTags ({len(metadata.youtube.tags)}):")
    print(f"  {', '.join(metadata.youtube.tags)}")
    print(f"\nDescription:")
    for line in metadata.youtube.description.split("\n"):
        print(f"  {line}")

    print(f"\n{divider}")
    print("TIKTOK")
    print(divider)
    print(f"Caption ({len(metadata.tiktok.caption)} chars):")
    print(f"  {metadata.tiktok.caption}")

    print(f"\n{divider}")
    print("INSTAGRAM")
    print(divider)
    ig_hashtags = len(re.findall(r"#\w+", metadata.instagram.caption))
    print(f"Caption ({ig_hashtags} hashtags):")
    for line in metadata.instagram.caption.split("\n"):
        print(f"  {line}")

    print(f"\n{divider}")
    print(f"✅  metadata.json saved to {out_dir}/metadata.json")
