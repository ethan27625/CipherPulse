# CipherPulse — Claude Code Project Memory

## Project Overview
- **Name:** CipherPulse
- **Tagline:** "The Heartbeat of Digital Threats"
- **Description:** Fully automated multi-platform Shorts pipeline for cybersecurity & AI content.
  Generates, assembles, and distributes 60-second vertical videos to YouTube Shorts, TikTok,
  and Instagram Reels with zero human intervention after initial setup.
- **Owner context:** Computer science student building this as a portfolio project + monetizable channel.
  Always explain technical decisions and teach underlying concepts as you build.
- **VM context:** Running inside a sandboxed Ubuntu VM (VMware Fusion on macOS Apple Silicon).
  The VM is isolated from the host machine's files for security.

---

## Project Structure

```
~/CipherPulse/
├── CLAUDE.md                    # This file — Claude Code persistent memory
├── src/
│   ├── __init__.py
│   ├── orchestrator.py          # Master controller — wires all modules together
│   ├── topic_picker.py          # Reads topics.json, selects next unused topic
│   ├── news_fetcher.py          # Pulls live headlines from 6 RSS feeds
│   ├── script_writer.py         # Anthropic API → original 45-58s Short script
│   ├── voice_generator.py       # Edge-TTS → MP3 voiceover + SRT subtitle file
│   ├── footage_downloader.py    # Pexels API → vertical stock video clips
│   ├── video_assembler.py       # FFmpeg → 1080x1920 MP4 (voice + captions + music)
│   ├── thumbnail_creator.py     # Pillow → CipherPulse branded 1280x720 thumbnail
│   ├── seo_generator.py         # Anthropic API → platform-specific metadata JSON
│   ├── youtube_uploader.py      # YouTube Data API v3 (OAuth2)
│   ├── tiktok_uploader.py       # TikTok Content Posting API (gated behind config flag)
│   ├── instagram_uploader.py    # Instagram Reels API via Graph API (gated)
│   └── file_hoster.py           # Uploads MP4 to tmpfiles.org for Instagram's URL requirement
├── assets/
│   ├── music/                   # Pre-downloaded royalty-free background tracks (Pixabay)
│   ├── fonts/                   # Google Fonts: Bebas Neue, Oswald, Montserrat Black
│   └── footage_cache/           # Cached Pexels clips organized by category:
│       └── {hacking,servers,code,AI,city,dark-tech,data,surveillance}/
├── output/                      # Generated Shorts — one folder per video
│   └── YYYY-MM-DD_topic-slug/
│       ├── script.txt
│       ├── voiceover.mp3
│       ├── subtitles.srt
│       ├── video.mp4
│       ├── thumbnail.png
│       └── metadata.json        # SEO metadata for all platforms
├── config/
│   └── platforms.json           # Per-platform enable/disable flags + schedule times
├── .env                         # API keys — NEVER commit. Loaded by python-dotenv.
├── topics.json                  # 500 pre-seeded video topics with used/unused tracking
├── state.json                   # Per-video, per-platform upload status tracking
├── schedule_queue.json          # Queued TikTok/Instagram uploads (no native scheduling)
├── .github/
│   └── workflows/
│       ├── generate-shorts.yml      # 3x daily cron — generates + uploads 1 Short
│       └── publish-scheduled.yml    # Hourly cron — processes queued TikTok/IG uploads
├── .gitignore
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## Brand Identity

### Channel Identity
- **Name:** CipherPulse
- **Tagline:** "The Heartbeat of Digital Threats"
- **Niche:** Cybersecurity incidents, AI developments, digital privacy, cyber threats, social engineering,
  deepfakes, AI tools, and how emerging technology impacts everyday people
- **Tone:** Authoritative, slightly dramatic, educational but accessible. Target: curious 20-year-old
  with no tech background who finds complex tech urgent and fascinating.
- **Audience:** Men and women 18-35, tech-curious

### Brand Colors (exact hex values — use in all visual generation)
```
Primary Cyan:   #00F2EA  — accents, highlights, CTAs
Deep Blue:      #0077B6  — gradients, secondary elements
Teal Mid:       #00BCD4  — gradient midpoint
Void Black:     #060609  — primary background
Dark Surface:   #0D1117  — cards, elevated surfaces
Light Text:     #E8E6E3  — primary text color
```

### Visual Style
- Dark cinematic aesthetic, vertical 9:16 format (1080x1920 for video, 1280x720 for thumbnails)
- Large bold centered captions — white text with dark shadow
- Caption position: centered vertically in the MIDDLE THIRD of the screen (MarginV=400, Alignment=10)
  This is critical — never put captions at the bottom where UI overlays cover them
- Font: Bold 800 weight, max 5-6 words per line
- Stock footage: servers, code terminals, hacking, data centers, cityscapes at night, glitch effects
- Background music: dark ambient / electronic at 15-20% volume
- Thumbnail: subtle grid pattern overlay at 2% opacity; shield+keyhole watermark at low opacity

### Thumbnail Formula (thumbnail_creator.py)
1. Layer 1: Background #060609 + subtle grid at 2% opacity
2. Layer 2: Small cyan (#00F2EA) accent text at top — stat or hook keyword
3. Layer 3: Main 2-line text — Line 1 white (#FFFFFF), Line 2 cyan (#00F2EA). Bold weight.
4. Layer 4: "CIPHERPULSE" watermark bottom-right at 30% opacity
- Fonts: Bebas Neue / Oswald / Montserrat Black (stored in assets/fonts/)

---

## Content Formats (rotate to prevent repetition)
1. **Incident Breakdown** — "In [year], a hacker broke into [company] and stole [data]... here's how"
2. **AI Reveal** — "This AI can now [shocking capability]" — new or alarming AI development
3. **Myth Buster** — "You think [common belief]? Here's the truth..."
4. **How It Works** — "Here's how [phishing/ransomware/deepfakes] actually works in 60 seconds"
5. **List / Ranking** — "3 signs your phone has been hacked" / "5 most dangerous AI tools right now"
6. **News React** — Quick breakdown of a trending cybersecurity or AI story (uses news_fetcher)

---

## Coding Conventions (enforce in ALL modules)
- **Python version:** 3.10+
- **Type hints:** Required on ALL function signatures
- **Docstrings:** Required on ALL modules (module-level) and ALL public functions
- **Error handling:** try/except with `logging` on ALL API calls and file I/O
- **Retries:** Exponential backoff on all network requests (use `tenacity` library)
- **API keys:** NEVER hardcode. Always read from environment variables via `python-dotenv`
- **Logging:** Use Python's `logging` module (not print). Log to both console and `logs/` directory
- **Constants:** Define at module top in SCREAMING_SNAKE_CASE
- **No magic numbers:** Name every constant (e.g., MAX_DURATION_SECONDS = 58, not just 58)
- **File paths:** Use `pathlib.Path` everywhere, not string concatenation

---

## Module Dependency Order

```
orchestrator.py
├── topic_picker.py        (reads topics.json)
│   └── news_fetcher.py    (called only for format=6 News React topics)
├── script_writer.py       (depends on: topic from topic_picker, optional headline from news_fetcher)
├── voice_generator.py     (depends on: script from script_writer)
├── footage_downloader.py  (depends on: [VISUAL: tags] extracted from script)
├── video_assembler.py     (depends on: voiceover.mp3, subtitles.srt, footage clips, music)
├── thumbnail_creator.py   (depends on: script title/hook, output directory)
├── seo_generator.py       (depends on: script content, topic metadata)
├── youtube_uploader.py    (depends on: video.mp4, thumbnail.png, metadata.json)
├── tiktok_uploader.py     (depends on: video.mp4, metadata.json — gated by config)
├── file_hoster.py         (depends on: video.mp4 — only if Instagram enabled)
└── instagram_uploader.py  (depends on: hosted URL from file_hoster, metadata.json — gated)
```

---

## Environment Variables

| Variable | Module(s) | Source |
|---|---|---|
| `ANTHROPIC_API_KEY` | script_writer.py, seo_generator.py | console.anthropic.com |
| `PEXELS_API_KEY` | footage_downloader.py | pexels.com/api |
| `YOUTUBE_CLIENT_ID` | youtube_uploader.py | Google Cloud Console |
| `YOUTUBE_CLIENT_SECRET` | youtube_uploader.py | Google Cloud Console |
| `TIKTOK_CLIENT_KEY` | tiktok_uploader.py | developers.tiktok.com |
| `TIKTOK_CLIENT_SECRET` | tiktok_uploader.py | developers.tiktok.com |
| `INSTAGRAM_ACCESS_TOKEN` | instagram_uploader.py | Facebook Developer app |
| `INSTAGRAM_ACCOUNT_ID` | instagram_uploader.py | Facebook Developer app |

All variables loaded from `.env` file in project root via `python-dotenv`.
The `.env` file is in `.gitignore` and NEVER committed.

---

## Platform Configuration
Config file: `config/platforms.json`
- YouTube: **ENABLED** — OAuth2 token stored at `config/token.json` (gitignored)
- TikTok: **DISABLED** — awaiting developer approval. Flip `enabled: true` to activate.
- Instagram: **DISABLED** — awaiting Facebook app approval. Flip `enabled: true` to activate.

All uploaders have `--dry-run` mode for testing without actually uploading.

---

## Critical Rules (never violate)
1. **No platform watermarks on video** — never burn YouTube/TikTok/Instagram logos into MP4
2. **No article text reproduction** — scripts must be 100% original. Headlines = topic inspiration only.
3. **No fabricated incidents** — never attribute fake attacks to real companies
4. **No API keys in code** — always environment variables
5. **Video must be under 58 seconds** — voice_generator validates; script_writer retries if over
6. **Captions in middle third** — MarginV=400, never at bottom where UI overlays block them

---

## Testing Instructions

### news_fetcher.py
```bash
cd ~/CipherPulse && python -m src.news_fetcher
# Expected: prints 5-10 fresh headlines from each of the 6 RSS feeds
```

### script_writer.py
```bash
cd ~/CipherPulse && python -m src.script_writer --topic "WannaCry ransomware attack" --format 1
cd ~/CipherPulse && python -m src.script_writer --news-headline "New AI worm discovered targeting enterprise networks"
# Expected: 45-58 second script with [VISUAL: tags] printed to stdout
```

### voice_generator.py
```bash
cd ~/CipherPulse && python -m src.voice_generator --script "Your phone was hacked 3 times this week."
# Expected: voiceover.mp3 + subtitles.srt in output/test/; duration printed
```

### footage_downloader.py
```bash
cd ~/CipherPulse && python -m src.footage_downloader --tags "hacker typing" "dark server room"
# Expected: clips downloaded to assets/footage_cache/; file paths printed
```

### video_assembler.py
```bash
cd ~/CipherPulse && python -m src.video_assembler --output-dir output/test/
# Expected: output/test/video.mp4 at 1080x1920
```

### thumbnail_creator.py
```bash
cd ~/CipherPulse && python -m src.thumbnail_creator --title "Your Phone Was Already Hacked" --output-dir output/test/
# Expected: output/test/thumbnail.png at 1280x720
```

### seo_generator.py
```bash
cd ~/CipherPulse && python -m src.seo_generator --topic "WannaCry ransomware" --script-file output/test/script.txt
# Expected: metadata.json with youtube/tiktok/instagram sections
```

### youtube_uploader.py
```bash
cd ~/CipherPulse && python -m src.youtube_uploader --output-dir output/test/ --dry-run
# Expected: prints what would be uploaded without calling YouTube API
```

### orchestrator.py (full pipeline test)
```bash
cd ~/CipherPulse && python -m src.orchestrator --count 1 --dry-run
# Expected: full pipeline runs, all files generated in output/, no actual uploads
```

---

## Build Status

| Module | Status | Notes |
|---|---|---|
| CLAUDE.md | ✅ Done | This file — project memory |
| topics.json | ✅ Done | 500 topics seeded (124 F1, 78 F2, 48 F3, 125 F4, 93 F5, 32 F6) |
| .env.example | ✅ Done | Template for all required env vars |
| .gitignore | ✅ Done | Excludes .env, token.json, output/, logs/, footage_cache/ |
| config/platforms.json | ✅ Done | YT enabled; TikTok + IG gated |
| state.json | ✅ Done | Schema defined, empty |
| schedule_queue.json | ✅ Done | Empty queue ready |
| news_fetcher.py | ✅ Done | 6 RSS feeds; MAX_AGE_DAYS=14; Headline dataclass; to_prompt_context() for Claude |
| script_writer.py | ✅ Done | Anthropic SDK; Script dataclass; duration validation; retry loop; news_context injection |
| voice_generator.py | ✅ Done | edge-tts SentenceBoundary→word timing; mutagen real duration; SRT 5-word chunks |
| footage_downloader.py | ✅ Done | Portrait-first search; .meta.json sidecar; 8-category cache; 3-level fallback |
| video_assembler.py | ✅ Done | 5-stage FFmpeg graph; scale+crop; libass subtitles; amix music; stream_loop |
| thumbnail_creator.py | ✅ Done | 4-layer Pillow composite; Bebas Neue + Oswald fonts; grid overlay; watermark |
| seo_generator.py | ✅ Done | Single API call → 3 platforms; JSON fence extraction; Postel's Law clamping |
| youtube_uploader.py | ✅ Done | OAuth2 flow + auto-refresh; resumable upload; publishAt scheduling; dry-run |
| tiktok_uploader.py | ✅ Done | PKCE OAuth2; two-phase upload (init→chunks→poll); rotating refresh tokens; schedule_queue.json |
| instagram_uploader.py | ✅ Done | Graph API container/publish flow; 60-day token auto-refresh; gated behind platforms.json |
| file_hoster.py | ✅ Done | litterbox.catbox.moe primary (72h TTL); file.io fallback; tenacity retry |
| topic_picker.py | ✅ Done | pick_topic(); reset_all_topics(); auto-reset when exhausted; --stats CLI |
| orchestrator.py | ✅ Done | 11-stage pipeline; --count N; --dry-run; --publish-scheduled; --retry-failed; run_log.json |
| GitHub Actions | ✅ Done | generate-shorts.yml (3× cron); publish-scheduled.yml (hourly); artifact upload; B64 token restore |
| Dockerfile | ✅ Done | Multi-stage build; ffmpeg+libass; non-root user; volume mounts for output/config |
| README.md | ✅ Done | Mermaid architecture diagram; quick start; module reference; cost breakdown |
