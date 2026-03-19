"""
news_fetcher.py — Live cybersecurity & AI headline fetcher.

Pulls recent articles from 6 free RSS feeds (no API key, no signup) and returns
structured headline data for use by script_writer.py as topic inspiration.

This module is only called by the orchestrator when the selected topic format is
6 (News React). It never copies article text — it only returns headlines and
brief summaries that Claude uses as factual context for an original script.

Feeds:
  - The Hacker News    — major cyber incidents and breaches
  - BleepingComputer   — malware, vulnerabilities, ransomware
  - Krebs on Security  — investigative cybersecurity journalism
  - CISA Alerts        — US government cybersecurity advisories
  - Ars Technica Sec   — tech-focused security reporting
  - MIT Tech Review    — AI developments and implications
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "news_fetcher.log"),
    ],
)
log = logging.getLogger("news_fetcher")

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_AGE_DAYS = 14         # Ignore articles older than this (Krebs posts weekly)
MAX_PER_FEED = 5          # Max headlines to return per feed
FETCH_TIMEOUT = 10        # HTTP timeout in seconds (feedparser uses socket timeout)
MAX_SUMMARY_CHARS = 400   # Truncate summaries beyond this to keep prompts lean

# ── Feed definitions ──────────────────────────────────────────────────────────
# Each tuple: (display_name, rss_url, category_tag)
FEEDS: list[tuple[str, str, str]] = [
    (
        "The Hacker News",
        "https://thehackernews.com/feeds/posts/default",
        "cybersecurity",
    ),
    (
        "BleepingComputer",
        "https://www.bleepingcomputer.com/feed/",
        "cybersecurity",
    ),
    (
        "Krebs on Security",
        "https://krebsonsecurity.com/feed/",
        "cybersecurity",
    ),
    (
        "CISA Alerts",
        "https://www.cisa.gov/news.xml",
        "government-advisory",
    ),
    (
        "Ars Technica Security",
        "https://feeds.arstechnica.com/arstechnica/security",
        "cybersecurity",
    ),
    (
        "MIT Technology Review",
        "https://www.technologyreview.com/feed/",
        "AI",
    ),
]


@dataclass
class Headline:
    """A single normalized news headline from an RSS feed."""

    title: str
    url: str
    source: str
    category: str
    published: datetime
    summary: str
    age_days: float = field(init=False)

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.age_days = (now - self.published).total_seconds() / 86_400

    def is_fresh(self) -> bool:
        """Return True if the article is within MAX_AGE_DAYS."""
        return self.age_days <= MAX_AGE_DAYS

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON logging or passing to other modules."""
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "category": self.category,
            "published": self.published.isoformat(),
            "age_days": round(self.age_days, 1),
            "summary": self.summary,
        }

    def to_prompt_context(self) -> str:
        """
        Format headline for injection into script_writer's Claude prompt.

        Returns a compact string like:
          SOURCE: The Hacker News
          HEADLINE: Hackers Exploit Critical Flaw in...
          SUMMARY: A newly disclosed vulnerability in...
        """
        return (
            f"SOURCE: {self.source}\n"
            f"HEADLINE: {self.title}\n"
            f"SUMMARY: {self.summary}"
        )


def _parse_date(entry: feedparser.FeedParserDict) -> datetime:
    """
    Extract and normalize the publication date from a feedparser entry.

    feedparser provides `published_parsed` as a time.struct_time in UTC.
    We convert to a timezone-aware datetime for correct age calculations.

    Falls back to 'now' if the date field is missing or malformed — this
    prevents a single bad entry from crashing the whole fetch.
    """
    try:
        if entry.get("published_parsed"):
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if entry.get("updated_parsed"):
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    # Fallback: treat as current so it passes the freshness filter
    return datetime.now(timezone.utc)


def _clean_summary(raw: str) -> str:
    """
    Strip HTML tags and collapse whitespace from an RSS summary field.

    feedparser often includes raw HTML in summary (e.g. <p>, <a href=...>).
    We do a minimal strip: remove anything inside < > brackets, then
    normalize whitespace. We don't import a full HTML parser to keep
    dependencies minimal.
    """
    import re
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    # Collapse multiple spaces/newlines
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def fetch_feed(name: str, url: str, category: str) -> list[Headline]:
    """
    Fetch and parse a single RSS feed, returning fresh Headline objects.

    Args:
        name:     Human-readable feed name (e.g. "The Hacker News")
        url:      RSS feed URL
        category: Tag string for downstream filtering ("cybersecurity" or "AI")

    Returns:
        List of Headline objects that pass the freshness filter, capped at
        MAX_PER_FEED entries.

    feedparser is intentionally simple — it does one HTTP GET with no auth
    and returns a parsed structure. We set socket timeout via the standard
    library because feedparser doesn't expose a timeout parameter directly.
    """
    import socket
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)

    headlines: list[Headline] = []
    try:
        log.info(f"Fetching feed: {name}")
        feed = feedparser.parse(url)

        if feed.bozo and feed.bozo_exception:
            # bozo=True means feedparser detected a malformed feed.
            # We log a warning but still attempt to use whatever was parsed —
            # many feeds are technically malformed but still readable.
            log.warning(f"Malformed feed from {name}: {feed.bozo_exception}")

        entries = feed.entries[:MAX_PER_FEED * 2]  # Over-fetch, then filter by age
        accepted = 0

        for entry in entries:
            if accepted >= MAX_PER_FEED:
                break

            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()

            if not title or not link:
                continue

            # Prefer full summary, fall back to title-only
            raw_summary = entry.get("summary", entry.get("content", [{}])[0].get("value", ""))
            summary = _clean_summary(raw_summary) if raw_summary else title

            published = _parse_date(entry)

            headline = Headline(
                title=title,
                url=link,
                source=name,
                category=category,
                published=published,
                summary=summary,
            )

            if headline.is_fresh():
                headlines.append(headline)
                accepted += 1
            else:
                log.debug(f"Skipping stale article ({headline.age_days:.1f}d old): {title[:60]}")

        log.info(f"  {name}: {len(headlines)} fresh headlines")

    except Exception as e:
        # Network failures are expected and should never crash the pipeline.
        # Caller will use whatever headlines other feeds returned.
        log.error(f"Failed to fetch {name} ({url}): {e}")

    finally:
        socket.setdefaulttimeout(old_timeout)

    return headlines


def fetch_all_headlines(
    categories: Optional[list[str]] = None,
) -> list[Headline]:
    """
    Fetch headlines from all configured RSS feeds and return a merged,
    deduplicated, recency-sorted list.

    Args:
        categories: Optional filter list. If provided, only return headlines
                    matching one of the given category tags. Pass None for all.
                    Example: ["cybersecurity"] or ["AI"] or None for both.

    Returns:
        List of Headline objects sorted newest-first.

    Deduplication strategy: we track seen URLs. If two feeds happen to
    aggregate the same story, only the first occurrence is kept.
    """
    all_headlines: list[Headline] = []
    seen_urls: set[str] = set()

    for name, url, category in FEEDS:
        if categories and category not in categories:
            log.debug(f"Skipping feed '{name}' (category '{category}' not in filter)")
            continue

        feed_headlines = fetch_feed(name, url, category)

        for h in feed_headlines:
            if h.url not in seen_urls:
                all_headlines.append(h)
                seen_urls.add(h.url)

        # Be polite to servers — brief pause between requests
        time.sleep(0.5)

    # Sort by publication date, newest first
    all_headlines.sort(key=lambda h: h.published, reverse=True)

    log.info(
        f"Total: {len(all_headlines)} unique fresh headlines "
        f"from {len(FEEDS)} feeds"
    )
    return all_headlines


def pick_top_headline(
    categories: Optional[list[str]] = None,
) -> Optional[Headline]:
    """
    Fetch all headlines and return the single most recent one.

    This is the primary entry point called by the orchestrator when a
    format-6 (News React) topic is selected. The returned Headline's
    `to_prompt_context()` method gives script_writer.py exactly the
    string it needs to inject into the Claude prompt.

    Returns None if all feeds fail or return no fresh content.
    """
    headlines = fetch_all_headlines(categories=categories)
    if not headlines:
        log.warning("No fresh headlines found across all feeds")
        return None
    top = headlines[0]
    log.info(f"Top headline: [{top.source}] {top.title}")
    return top


# ── CLI entrypoint for manual testing ─────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Fetch live cybersecurity & AI headlines from RSS feeds"
    )
    parser.add_argument(
        "--category",
        choices=["cybersecurity", "AI"],
        default=None,
        help="Filter to a specific category (default: all)",
    )
    parser.add_argument(
        "--top",
        action="store_true",
        help="Print only the single top headline (as it would be passed to script_writer)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON instead of human-readable text",
    )
    args = parser.parse_args()

    categories = [args.category] if args.category else None

    if args.top:
        headline = pick_top_headline(categories=categories)
        if headline:
            if args.as_json:
                print(json.dumps(headline.to_dict(), indent=2))
            else:
                print("\n" + "═" * 60)
                print("TOP HEADLINE (passed to script_writer)")
                print("═" * 60)
                print(headline.to_prompt_context())
                print(f"\nURL: {headline.url}")
                print(f"Age: {headline.age_days:.1f} days old")
        else:
            print("No fresh headlines found.")
    else:
        headlines = fetch_all_headlines(categories=categories)
        if args.as_json:
            print(json.dumps([h.to_dict() for h in headlines], indent=2))
        else:
            print(f"\n{'═' * 60}")
            print(f"CIPHERPULSE NEWS FETCHER — {len(headlines)} headlines")
            print("═" * 60)
            for i, h in enumerate(headlines, 1):
                age_str = f"{h.age_days:.1f}d ago"
                print(f"\n[{i:02d}] [{h.source}] ({age_str})")
                print(f"     {h.title}")
                if h.summary and h.summary != h.title:
                    # Show first 120 chars of summary
                    preview = h.summary[:120] + ("…" if len(h.summary) > 120 else "")
                    print(f"     → {preview}")
