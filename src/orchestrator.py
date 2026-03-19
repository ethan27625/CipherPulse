"""
orchestrator.py — CipherPulse master pipeline controller.

Wires together all 11 modules into a single automated run.

Modes:
  python3 -m src.orchestrator                  # produce 1 Short (default)
  python3 -m src.orchestrator --count 3        # produce 3 Shorts sequentially
  python3 -m src.orchestrator --dry-run        # full pipeline, skip real uploads
  python3 -m src.orchestrator --publish-scheduled  # flush TikTok schedule_queue.json
  python3 -m src.orchestrator --retry-failed   # re-upload any failed platform jobs

Pipeline stages per video:
  1. topic_picker    → pick unused topic from topics.json
  2. news_fetcher    → fetch live cybersecurity headlines (optional context)
  3. script_writer   → Claude generates 45-58s script
  4. voice_generator → edge-TTS → MP3 + SRT
  5. footage_downloader → Pexels clips for each visual tag
  6. video_assembler → FFmpeg → 1080×1920 MP4
  7. thumbnail_creator → Pillow → 1280×720 PNG
  8. seo_generator   → Claude → titles/captions/tags for 3 platforms
  9. youtube_uploader → OAuth2 upload (always enabled)
 10. tiktok_uploader → PKCE upload (gated)
 11. instagram_uploader → Graph API upload (gated; uses file_hoster)

All outputs land in output/<YYYYMMDD_HHMMSS>/
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── Output paths ───────────────────────────────────────────────────────────────

OUTPUT_ROOT  = Path("output")
RUN_LOG_PATH = OUTPUT_ROOT / "run_log.json"
QUEUE_PATH   = Path("config/schedule_queue.json")


# ── Run log helpers ────────────────────────────────────────────────────────────

def _load_run_log() -> list[dict]:
    if RUN_LOG_PATH.exists():
        try:
            return json.loads(RUN_LOG_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("run_log.json is corrupt — starting fresh")
    return []


def _save_run_log(entries: list[dict]) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_LOG_PATH.write_text(json.dumps(entries, indent=2, default=str))


def _append_run(entry: dict) -> None:
    log_entries = _load_run_log()
    log_entries.append(entry)
    _save_run_log(log_entries)


# ── Single pipeline run ────────────────────────────────────────────────────────

def run_pipeline(
    dry_run: bool = False,
    publish_at: Optional[str] = None,
) -> dict:
    """
    Execute one complete Short production cycle.

    Args:
        dry_run:    If True, skip real platform uploads.
        publish_at: ISO 8601 UTC string for YouTube scheduled publishing.
                    If None, publishes immediately (or uses default scheduling).

    Returns:
        A run-record dict written to run_log.json.
    """
    run_id   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir  = OUTPUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    record: dict = {
        "run_id":     run_id,
        "out_dir":    str(out_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status":     "running",
        "topic":      None,
        "script":     {},
        "voice":      {},
        "footage":    {},
        "video":      {},
        "thumbnail":  {},
        "seo":        {},
        "uploads":    {"youtube": {}, "tiktok": {}, "instagram": {}},
        "error":      None,
    }

    try:
        # ── Stage 1: Topic ─────────────────────────────────────────────────────
        log.info("Stage 1/11 — Picking topic…")
        from src.topic_picker import pick_topic
        topic = pick_topic()
        record["topic"] = {"id": topic.id, "topic": topic.topic, "format": topic.format}
        log.info(f"  Topic #{topic.id}: {topic.topic!r} (format {topic.format})")

        # ── Stage 2: News ──────────────────────────────────────────────────────
        log.info("Stage 2/11 — Fetching news headlines…")
        news_context: Optional[str] = None
        try:
            from src.news_fetcher import fetch_all_headlines
            headlines = fetch_all_headlines()
            if headlines:
                # Pass the top 3 headlines as context for format F6 (News React)
                news_context = "\n".join(h.to_prompt_context() for h in headlines[:3])
                log.info(f"  Fetched {len(headlines)} fresh headlines")
        except Exception as exc:
            log.warning(f"  News fetch failed (non-fatal): {exc}")

        # ── Stage 3: Script ────────────────────────────────────────────────────
        log.info("Stage 3/11 — Writing script with Claude…")
        from src.script_writer import generate_script
        script = generate_script(
            topic=topic.topic,
            format_id=topic.format,
            news_context=news_context if topic.format == 6 else None,
        )
        # Save script to disk
        script_path = out_dir / "script.txt"
        script_path.write_text(script.to_file_content())
        record["script"] = {
            "title":           script.title,
            "est_words":       script.est_words,
            "est_duration_s":  script.est_duration_seconds,
        }
        log.info(
            f"  Script: {script.est_words} words, "
            f"~{script.est_duration_seconds:.1f}s — {script.title!r}"
        )

        # ── Stage 4: Voiceover ─────────────────────────────────────────────────
        log.info("Stage 4/11 — Generating voiceover…")
        from src.voice_generator import generate_voiceover
        # generate_voiceover takes plain text (no [VISUAL] tags)
        voice = generate_voiceover(
            script_text=script.full_text,
            output_dir=out_dir,
        )
        record["voice"] = {
            "duration_s":    voice.duration_seconds,
            "word_count":    voice.word_count,
            "caption_count": voice.caption_count,
        }
        log.info(
            f"  Voice: {voice.duration_seconds:.1f}s, "
            f"{voice.word_count} words, {voice.caption_count} captions"
        )

        # ── Stage 5: Footage ───────────────────────────────────────────────────
        log.info("Stage 5/11 — Downloading footage clips…")
        from src.footage_downloader import fetch_clips_for_script
        # Clips go to the shared footage_cache; returns list of local Paths
        clips = fetch_clips_for_script(visual_tags=script.visual_tags)
        record["footage"] = {"clip_count": len(clips), "paths": [str(c) for c in clips]}
        log.info(f"  Footage: {len(clips)} clips")

        # ── Stage 6: Video ─────────────────────────────────────────────────────
        log.info("Stage 6/11 — Assembling video with FFmpeg…")
        from src.video_assembler import assemble_video
        video_path = assemble_video(
            clip_paths=clips,
            voiceover_path=voice.mp3_path,
            srt_path=voice.srt_path,
            output_dir=out_dir,
        )
        record["video"] = {"path": str(video_path), "size_mb": video_path.stat().st_size / 1e6}
        log.info(f"  Video: {record['video']['size_mb']:.1f} MB → {video_path}")

        # ── Stage 7: Thumbnail ─────────────────────────────────────────────────
        log.info("Stage 7/11 — Creating thumbnail…")
        from src.thumbnail_creator import create_thumbnail
        thumb_path = create_thumbnail(
            title=script.title,
            output_dir=out_dir,
            format_id=topic.format,
        )
        record["thumbnail"] = {"path": str(thumb_path)}
        log.info(f"  Thumbnail: {thumb_path}")

        # ── Stage 8: SEO metadata ──────────────────────────────────────────────
        log.info("Stage 8/11 — Generating SEO metadata…")
        from src.seo_generator import generate_metadata
        metadata = generate_metadata(
            topic=topic.topic,
            format_id=topic.format,
            script_text=script.full_text,
            video_title=script.title,
            output_dir=out_dir,
        )
        record["seo"] = {"yt_title": metadata.youtube.title}
        log.info(f"  YouTube title: {metadata.youtube.title!r}")

        # ── Stage 9: YouTube upload ────────────────────────────────────────────
        log.info("Stage 9/11 — Uploading to YouTube…")
        yt_result: dict = {}
        try:
            from src.youtube_uploader import upload_short as yt_upload
            yt_result = yt_upload(output_dir=out_dir, dry_run=dry_run, publish_at=publish_at)
            log.info(f"  YouTube: {yt_result.get('status')} — {yt_result.get('url', '')}")
        except Exception as exc:
            log.error(f"  YouTube upload failed: {exc}")
            yt_result = {"status": "error", "error": str(exc)}
        record["uploads"]["youtube"] = yt_result

        # ── Stage 10: TikTok upload ────────────────────────────────────────────
        log.info("Stage 10/11 — Uploading to TikTok…")
        tt_result: dict = {}
        try:
            from src.tiktok_uploader import upload_short as tt_upload
            tt_result = tt_upload(output_dir=out_dir, dry_run=dry_run)
            log.info(f"  TikTok: {tt_result.get('status', '?')}")
        except Exception as exc:
            log.error(f"  TikTok upload failed: {exc}")
            tt_result = {"status": "error", "error": str(exc)}
        record["uploads"]["tiktok"] = tt_result

        # ── Stage 11: Instagram upload ─────────────────────────────────────────
        log.info("Stage 11/11 — Uploading to Instagram…")
        ig_result: dict = {}
        try:
            from src.instagram_uploader import upload_short as ig_upload
            ig_obj = ig_upload(output_dir=out_dir, dry_run=dry_run)
            ig_result = ig_obj.to_dict()
            log.info(f"  Instagram: {ig_result.get('status', '?')}")
        except Exception as exc:
            log.error(f"  Instagram upload failed: {exc}")
            ig_result = {"status": "error", "error": str(exc)}
        record["uploads"]["instagram"] = ig_result

        # ── Determine overall status ───────────────────────────────────────────
        all_statuses = [
            yt_result.get("status", "error"),
            tt_result.get("status", "skipped"),     # skipped = OK (gated)
            ig_result.get("status", "skipped"),
        ]
        if all(s in ("uploaded", "dry_run", "skipped", "queued") for s in all_statuses):
            record["status"] = "success"
        elif any(s == "error" for s in all_statuses):
            record["status"] = "partial"
        else:
            record["status"] = "success"

    except Exception as exc:
        record["status"] = "failed"
        record["error"]  = traceback.format_exc()
        log.error(f"Pipeline failed: {exc}")
        log.debug(record["error"])

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    _append_run(record)

    return record


# ── Publish-scheduled mode ─────────────────────────────────────────────────────

def publish_scheduled() -> None:
    """
    Flush TikTok's schedule_queue.json: upload all pending jobs whose
    publish_at time has passed.
    """
    from src.tiktok_uploader import upload_short as tt_upload, get_valid_token

    if not QUEUE_PATH.exists():
        log.info("No schedule queue found — nothing to publish.")
        return

    queue = json.loads(QUEUE_PATH.read_text())
    now   = datetime.now(timezone.utc).isoformat()
    updated = False

    for job in queue:
        if job.get("status") != "pending":
            continue
        if job.get("publish_at", "9999") > now:
            log.info(f"  Skipping {job['run_id']} — not yet due ({job['publish_at']})")
            continue

        log.info(f"Publishing queued TikTok job: {job['run_id']} (was due {job['publish_at']})")
        try:
            result = tt_upload(output_dir=Path(job["out_dir"]), dry_run=False)
            job["status"]      = result.get("status", "unknown")
            job["published_at"] = datetime.now(timezone.utc).isoformat()
            updated = True
        except Exception as exc:
            log.error(f"  Failed: {exc}")
            job["status"] = "error"
            job["error"]  = str(exc)
            updated = True

    if updated:
        QUEUE_PATH.write_text(json.dumps(queue, indent=2))
        log.info("schedule_queue.json updated.")


# ── Retry-failed mode ──────────────────────────────────────────────────────────

def retry_failed(dry_run: bool = False) -> None:
    """
    Re-attempt platform uploads for any runs marked 'partial' or 'failed'
    in run_log.json.
    """
    entries = _load_run_log()
    if not entries:
        log.info("run_log.json is empty — nothing to retry.")
        return

    retried = 0
    for entry in entries:
        if entry.get("status") not in ("partial", "failed"):
            continue

        out_dir = Path(entry["out_dir"])
        if not out_dir.exists():
            log.warning(f"Output directory missing: {out_dir} — skipping")
            continue

        log.info(f"Retrying uploads for run {entry['run_id']}…")

        # Only retry platforms that errored
        yt = entry["uploads"].get("youtube", {})
        tt = entry["uploads"].get("tiktok", {})
        ig = entry["uploads"].get("instagram", {})

        if yt.get("status") == "error":
            try:
                from src.youtube_uploader import upload_short as yt_upload
                entry["uploads"]["youtube"] = yt_upload(output_dir=out_dir, dry_run=dry_run)
            except Exception as exc:
                entry["uploads"]["youtube"]["error"] = str(exc)

        if tt.get("status") == "error":
            try:
                from src.tiktok_uploader import upload_short as tt_upload
                entry["uploads"]["tiktok"] = tt_upload(output_dir=out_dir, dry_run=dry_run)
            except Exception as exc:
                entry["uploads"]["tiktok"]["error"] = str(exc)

        if ig.get("status") == "error":
            try:
                from src.instagram_uploader import upload_short as ig_upload
                entry["uploads"]["instagram"] = ig_upload(output_dir=out_dir, dry_run=dry_run).to_dict()
            except Exception as exc:
                entry["uploads"]["instagram"]["error"] = str(exc)

        # Re-evaluate status
        statuses = [v.get("status", "error") for v in entry["uploads"].values()]
        if all(s in ("uploaded", "dry_run", "skipped", "queued") for s in statuses):
            entry["status"] = "success"
        else:
            entry["status"] = "partial"

        retried += 1

    if retried:
        _save_run_log(entries)
        log.info(f"Retried {retried} run(s). run_log.json updated.")
    else:
        log.info("No failed runs found to retry.")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="CipherPulse — automated Shorts pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m src.orchestrator                  # produce 1 Short
  python3 -m src.orchestrator --count 3        # produce 3 Shorts
  python3 -m src.orchestrator --dry-run        # full pipeline, skip real uploads
  python3 -m src.orchestrator --publish-scheduled  # flush TikTok queue
  python3 -m src.orchestrator --retry-failed   # re-upload failed platforms
""",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help="Number of Shorts to produce (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline but skip real platform uploads",
    )
    parser.add_argument(
        "--publish-scheduled",
        action="store_true",
        help="Flush pending jobs in TikTok schedule_queue.json",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-attempt uploads for partial/failed runs in run_log.json",
    )
    parser.add_argument(
        "--publish-at",
        metavar="ISO8601",
        default=None,
        help="Schedule YouTube upload at this UTC time (e.g. 2024-03-15T20:00:00Z)",
    )
    args = parser.parse_args()

    # ── Special modes ──────────────────────────────────────────────────────────

    if args.publish_scheduled:
        log.info("═" * 60)
        log.info("MODE: Publish Scheduled TikTok Jobs")
        log.info("═" * 60)
        publish_scheduled()
        sys.exit(0)

    if args.retry_failed:
        log.info("═" * 60)
        log.info("MODE: Retry Failed Uploads")
        log.info("═" * 60)
        retry_failed(dry_run=args.dry_run)
        sys.exit(0)

    # ── Production / dry-run pipeline ─────────────────────────────────────────
    mode_label = "DRY RUN" if args.dry_run else "PRODUCTION"
    log.info("═" * 60)
    log.info(f"CipherPulse — {mode_label} — producing {args.count} Short(s)")
    log.info("═" * 60)

    success_count = 0
    for i in range(args.count):
        if args.count > 1:
            log.info(f"\n── Run {i + 1} of {args.count} ──")
        record = run_pipeline(dry_run=args.dry_run, publish_at=args.publish_at)
        log.info(
            f"\nRun {record['run_id']} → {record['status'].upper()}"
            f"  [{record.get('finished_at', '')}]"
        )
        if record["status"] in ("success", "partial"):
            success_count += 1
        elif record["status"] == "failed":
            log.error(f"Run failed:\n{record.get('error', '')}")
            if args.count == 1:
                sys.exit(1)    # fail fast on single-video runs

    log.info(f"\n{'═' * 60}")
    log.info(f"Done: {success_count}/{args.count} successful")
    log.info(f"Logs: {RUN_LOG_PATH}")
    log.info("═" * 60)
