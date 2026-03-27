"""
topic_picker.py — Select the next unused topic from topics.json.

Reads topics.json, picks the next unused topic (lowest id first),
marks it used with today's date, and writes the file back.

Topic schema:
  {"id": 1, "topic": "...", "format": 1, "used": false, "date_used": null}
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

TOPICS_PATH = Path("topics.json")

# 30% of picks are overridden to format 7 (Text Card) for content variety.
# This mixes quick text card Shorts with the standard voiceover Shorts
# without needing separate format-7 entries in topics.json.
TEXT_CARD_PROBABILITY = 0.0   # 0.0 = always use footage+voiceover format; 1.0 = always text card

# High-search-volume cybersecurity keywords that boost a topic's selection
# weight during random_pick mode.  Topics containing any of these terms are
# 3× more likely to be selected than topics without them.
# This is a soft bias — non-matching topics can still be chosen.
TREND_KEYWORDS = [
    "ransomware", "password", "data breach", "social engineering",
    "phishing", "vpn", "dark web", "two factor authentication",
    "zero day", "malware",
]
TREND_WEIGHT_MULTIPLIER = 3  # matching topics get 3× the base weight


@dataclass
class Topic:
    id:     int
    topic:  str
    format: int     # 1-6 matching the F1-F6 content formats


def pick_topic(random_pick: bool = False) -> Topic:
    """
    Select and mark the next unused topic.

    Args:
        random_pick: If True, pick randomly from unused topics instead of
                     lowest-id-first order (useful for variety in manual runs).

    Returns:
        Topic dataclass with id, topic string, and format number.

    Raises:
        FileNotFoundError: If topics.json doesn't exist.
        RuntimeError: If all topics are exhausted (all used=true).
    """
    if not TOPICS_PATH.exists():
        raise FileNotFoundError(
            f"{TOPICS_PATH} not found. Run generate_topics.py first."
        )

    all_topics: list[dict] = json.loads(TOPICS_PATH.read_text())
    unused = [t for t in all_topics if not t.get("used", False)]

    if not unused:
        # Reset all topics so the pipeline never stops
        log.warning("All topics exhausted — resetting used flags for all entries")
        for t in all_topics:
            t["used"]      = False
            t["date_used"] = None
        TOPICS_PATH.write_text(json.dumps(all_topics, indent=2))
        unused = all_topics

    if random_pick:
        # Trend-weighted random: topics matching TREND_KEYWORDS get a higher
        # selection weight.  Non-matching topics still have weight=1 so they
        # can always be chosen — this is a soft bias, not a hard filter.
        topic_lower = [t["topic"].lower() for t in unused]
        weights = [
            TREND_WEIGHT_MULTIPLIER
            if any(kw in tl for kw in TREND_KEYWORDS)
            else 1
            for tl in topic_lower
        ]
        trending_count = sum(1 for w in weights if w > 1)
        log.debug(
            f"Trend-weighted pick: {trending_count}/{len(unused)} topics match "
            f"keywords (weight={TREND_WEIGHT_MULTIPLIER}×)"
        )
        chosen_data = random.choices(unused, weights=weights, k=1)[0]
    else:
        # Lowest id first — deterministic ordering (used by cron/GitHub Actions)
        chosen_data = min(unused, key=lambda t: t["id"])

    # Mark as used
    for t in all_topics:
        if t["id"] == chosen_data["id"]:
            t["used"]      = True
            t["date_used"] = date.today().isoformat()
            break

    TOPICS_PATH.write_text(json.dumps(all_topics, indent=2))

    # 30% chance to override format → 7 (Text Card) for content variety.
    # The topic subject stays the same; only the production path changes.
    assigned_format = chosen_data["format"]
    if random.random() < TEXT_CARD_PROBABILITY:
        assigned_format = 7
        log.info(
            f"Picked topic #{chosen_data['id']} (format overridden → 7/TextCard): "
            f"{chosen_data['topic']!r}"
        )
    else:
        log.info(
            f"Picked topic #{chosen_data['id']} (format {assigned_format}): "
            f"{chosen_data['topic']!r}"
        )

    return Topic(
        id=chosen_data["id"],
        topic=chosen_data["topic"],
        format=assigned_format,
    )


def reset_all_topics() -> int:
    """Reset all topics to unused. Returns the number of topics reset."""
    all_topics = json.loads(TOPICS_PATH.read_text())
    for t in all_topics:
        t["used"]      = False
        t["date_used"] = None
    TOPICS_PATH.write_text(json.dumps(all_topics, indent=2))
    log.info(f"Reset {len(all_topics)} topics to unused.")
    return len(all_topics)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Pick next unused topic from topics.json")
    parser.add_argument("--random", action="store_true", help="Pick randomly instead of by ID")
    parser.add_argument("--reset", action="store_true", help="Reset all topics to unused")
    parser.add_argument(
        "--stats", action="store_true", help="Show usage statistics without picking"
    )
    args = parser.parse_args()

    if args.reset:
        n = reset_all_topics()
        print(f"Reset {n} topics.")
    elif args.stats:
        all_topics = json.loads(TOPICS_PATH.read_text())
        used   = sum(1 for t in all_topics if t.get("used"))
        unused = len(all_topics) - used
        by_format = {}
        for t in all_topics:
            f = t["format"]
            by_format.setdefault(f, {"total": 0, "used": 0})
            by_format[f]["total"] += 1
            if t.get("used"):
                by_format[f]["used"] += 1
        print(f"\nTopics: {used} used / {unused} unused / {len(all_topics)} total")
        print("\nBy format:")
        for f in sorted(by_format):
            d = by_format[f]
            print(f"  F{f}: {d['used']}/{d['total']} used")
    else:
        topic = pick_topic(random_pick=args.random)
        print(f"\nSelected topic:")
        print(f"  ID:     {topic.id}")
        print(f"  Format: F{topic.format}")
        print(f"  Topic:  {topic.topic}\n")
