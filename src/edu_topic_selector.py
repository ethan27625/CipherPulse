"""
edu_topic_selector.py — Select the next uncompleted topic from the edu curriculum.

Reads config/edu_curriculum.json, picks the next uncompleted topic in mini-series
style, marks it completed=true, updates the meta block, and saves the file.

Mini-series ordering (BATCH_SIZE = 4):
  - Pick up to 4 consecutive uncompleted topics from the current category
  - After 4, rotate to the next category (reset batch counter)
  - Skip exhausted categories; wrap around when all are done
  - When ALL topics across all categories are completed, reset every topic to
    completed=false so the curriculum cycles automatically

This creates short thematic series viewers can follow:
  linux-001 → linux-002 → linux-003 → linux-004 → kali-001 → kali-002 → ...
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CURRICULUM_PATH = Path("config/edu_curriculum.json")
BATCH_SIZE = 4  # Consecutive topics per category before rotating to the next


@dataclass
class EduTopic:
    """A single edu curriculum topic ready for the pipeline."""
    id: str
    title: str
    keywords: list[str]
    search_terms: list[str]   # Curated Pexels search terms (use instead of script visual_tags)
    category_name: str


def pick_edu_topic() -> EduTopic:
    """
    Select and mark the next uncompleted edu topic.

    Uses mini-series ordering: works through BATCH_SIZE uncompleted topics
    in one category, then advances to the next, so the channel produces short
    thematic series that viewers can follow.

    Returns:
        EduTopic dataclass with id, title, keywords, search_terms, and category.

    Raises:
        FileNotFoundError: If edu_curriculum.json is missing.
        RuntimeError:      If no uncompleted topics can be found after scanning all categories.
    """
    if not CURRICULUM_PATH.exists():
        raise FileNotFoundError(
            f"{CURRICULUM_PATH} not found. "
            "Ensure config/edu_curriculum.json exists before using --mode edu."
        )

    data: dict = json.loads(CURRICULUM_PATH.read_text())
    categories: list[dict] = data.get("categories", [])

    if not categories:
        raise RuntimeError("edu_curriculum.json has no categories defined.")

    # ── Initialise meta block if absent ───────────────────────────────────────
    if "meta" not in data:
        data["meta"] = {
            "current_category_index": 0,
            "topics_in_current_batch": 0,
            "batch_size": BATCH_SIZE,
        }
    meta: dict = data["meta"]
    cat_idx  = meta.get("current_category_index", 0) % len(categories)
    batch_n  = meta.get("topics_in_current_batch", 0)

    # ── Global reset when every topic is completed ────────────────────────────
    all_topics = [t for cat in categories for t in cat["topics"]]
    if all(t.get("completed", False) for t in all_topics):
        log.warning(
            "All %d edu topics completed — resetting curriculum for next cycle",
            len(all_topics),
        )
        for cat in categories:
            for t in cat["topics"]:
                t["completed"] = False
        cat_idx = 0
        batch_n = 0

    # ── Find the next topic in mini-series order ──────────────────────────────
    # Scan up to len(categories) rotations to find one with uncompleted topics.
    chosen: dict | None = None
    chosen_cat_name: str = ""

    for rotation in range(len(categories)):
        idx = (cat_idx + rotation) % len(categories)
        cat = categories[idx]
        uncompleted = [t for t in cat["topics"] if not t.get("completed", False)]

        if not uncompleted:
            # Category fully done — skip it entirely, reset batch on the way
            if rotation == 0:
                # We were mid-batch in this category; it just ran out — advance
                cat_idx = (cat_idx + 1) % len(categories)
                batch_n = 0
            continue

        if rotation == 0 and batch_n < BATCH_SIZE:
            # Still within the current batch for the current category
            chosen = uncompleted[0]
            chosen_cat_name = cat["name"]
            break
        elif rotation > 0:
            # We've rotated to a new category (batch exhausted or category drained)
            cat_idx = idx
            batch_n = 0
            chosen = uncompleted[0]
            chosen_cat_name = cat["name"]
            break
        else:
            # rotation==0 and batch_n >= BATCH_SIZE → advance to next category
            cat_idx = (cat_idx + 1) % len(categories)
            batch_n = 0
            next_cat = categories[cat_idx]
            next_uncompleted = [t for t in next_cat["topics"] if not t.get("completed", False)]
            if next_uncompleted:
                chosen = next_uncompleted[0]
                chosen_cat_name = next_cat["name"]
            break

    if chosen is None:
        raise RuntimeError(
            "Could not find any uncompleted edu topics after scanning all categories. "
            "This should not happen — the global reset should have prevented it."
        )

    # ── Mark chosen topic as completed ────────────────────────────────────────
    for cat in categories:
        for t in cat["topics"]:
            if t["id"] == chosen["id"]:
                t["completed"] = True
                break

    # ── Update meta ───────────────────────────────────────────────────────────
    batch_n += 1
    meta["current_category_index"] = cat_idx
    meta["topics_in_current_batch"] = batch_n

    # If the batch is now full, pre-advance the index for the next call
    if batch_n >= BATCH_SIZE:
        meta["current_category_index"] = (cat_idx + 1) % len(categories)
        meta["topics_in_current_batch"] = 0

    data["meta"] = meta
    CURRICULUM_PATH.write_text(json.dumps(data, indent=2))

    edu_topic = EduTopic(
        id=chosen["id"],
        title=chosen["title"],
        keywords=chosen.get("keywords", []),
        search_terms=chosen.get("search_terms", []),
        category_name=chosen_cat_name,
    )
    log.info(
        "Edu topic: [%s] %s — %r",
        chosen_cat_name,
        edu_topic.id,
        edu_topic.title,
    )
    return edu_topic


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Pick the next uncompleted topic from the edu curriculum"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show curriculum completion stats without picking a topic",
    )
    args = parser.parse_args()

    if args.stats:
        data = json.loads(CURRICULUM_PATH.read_text())
        categories = data.get("categories", [])
        meta = data.get("meta", {})
        total = sum(len(c["topics"]) for c in categories)
        done  = sum(1 for c in categories for t in c["topics"] if t.get("completed"))
        print(f"\nEdu curriculum: {done}/{total} topics completed\n")
        print(f"Current batch: category #{meta.get('current_category_index', 0)} "
              f"({meta.get('topics_in_current_batch', 0)}/{BATCH_SIZE} in batch)\n")
        for cat in categories:
            cat_done = sum(1 for t in cat["topics"] if t.get("completed"))
            print(f"  [{cat['name']}] {cat_done}/{len(cat['topics'])} done")
            for t in cat["topics"]:
                mark = "✓" if t.get("completed") else "·"
                print(f"    {mark} {t['id']}: {t['title']}")
        print()
    else:
        topic = pick_edu_topic()
        print(f"\nSelected edu topic:")
        print(f"  ID:           {topic.id}")
        print(f"  Category:     {topic.category_name}")
        print(f"  Title:        {topic.title}")
        print(f"  Keywords:     {', '.join(topic.keywords)}")
        print(f"  Search terms: {', '.join(topic.search_terms)}\n")
