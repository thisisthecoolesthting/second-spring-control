# Second Spring Control

Local content production controller for `secondspringclub.com`. Runs on
your Windows / Mac / Linux machine, orchestrates the pipeline from raw
ideas through ChatGPT drafting through human edit through git push to
your VPS.

## Architecture

```
                    ┌──────────────────────┐
                    │  Local Controller    │
                    │  (this app)          │
                    │  http://localhost:8080│
                    └──────────────────────┘
                       │              │
        reads ┌────────┘              └────────┐ writes + git push
              ▼                                ▼
   amazon-affiliate-scout/             second-spring-club/
       data/products.json                 src/content/...
                                                │
                                                ▼ git push
                                          VPS auto-rebuild
                                                │
                                                ▼
                                       https://secondspringclub.com
```

## Pipeline

```
idea → skeleton → drafting → draft_ready → editing → edited
     → reviewing → ready_to_publish → published → live
```

Each transition is logged. Quality gates run between `edited→reviewing`
and `reviewing→ready_to_publish`. The `published→live` transition fires
automatically after the controller polls your live URL and confirms 200.

## Install

```bash
cd second-spring-control
python -m venv .venv
# Windows
.venv\Scripts\activate
# Mac/Linux
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env: at minimum set OPENAI_API_KEY, ASTRO_REPO_DIR, SCOUT_PRODUCTS_JSON
```

## Run

```bash
python -m control.main
# or with uvicorn directly:
uvicorn control.main:app --host 127.0.0.1 --port 8080
```

Then open http://localhost:8080 in your browser.

## Daily workflow (the path to 150+ pages/month)

1. **Morning (5 min):** Open Dashboard. See "What's next" — usually 3-5
   items in `editing` from yesterday's batch + 5-10 in `draft_ready` from
   the overnight scout import.

2. **Bulk draft (10 min):** Click `Import from scout` to pull any new
   products from `data/products.json`. New items land in `skeleton`.
   Open Queue → filter `skeleton` → select up to 8 → batch draft. Eight
   parallel OpenAI calls finish in ~60-90 seconds and move items to
   `draft_ready`. Cost: ~$0.08 with `gpt-4o-mini`.

3. **Edit pass (75 min):** Open the first `draft_ready` item, click
   `Open editor`, edit (~7-10 min/review), Cmd/Ctrl+Enter to mark
   `edited` and advance. Repeat for 8 items. Quality gate runs
   automatically between `edited→reviewing` — if a check fails, you'll
   see why and can fix or override.

4. **Publish (5 min):** Move reviewed items to `ready_to_publish`, then
   click Publish on each. Controller writes the .md file, git commits,
   git pushes, and waits for the VPS to serve the new URL. When it does,
   the item goes `live`.

5. **Capture next ideas (5 min):** Anything you noticed during editing
   that's worth its own page → Ideas tab → quick form.

That's roughly 100 minutes/day producing 8 published reviews with no
quality compromise. Five days a week = 40 reviews. Add 2 hand-edited
pillar pages on weekends and you're at the 150+ pages/month target.

## Configuration

All via environment variables / `.env` file. See `.env.example` for the
full list. The most important:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key. Required for AI drafting. |
| `OPENAI_MODEL_DRAFT` | Model for product reviews. Default: `gpt-4o-mini` (~$0.01/review). Bump to `gpt-4o` for higher quality (~$0.10/review). |
| `OPENAI_MODEL_PILLAR` | Model for long-form pillars. Default: `gpt-4o`. |
| `OPENAI_MODEL_SAFETY` | Model for safety section pass. Default: `gpt-4o`. |
| `SCOUT_PRODUCTS_JSON` | Absolute path to your scout's `products.json`. |
| `ASTRO_REPO_DIR` | Absolute path to your `second-spring-club` git repo. |
| `ASTRO_CONTENT_DIR` | `<ASTRO_REPO_DIR>/src/content` typically. |
| `SITE_URL` | Live site URL for post-publish health check. |
| `AMAZON_ASSOCIATE_TAG` | Sanity-checked in CTAs before publish. |
| `MAX_PARALLEL_DRAFTS` | Concurrent OpenAI requests. Default 8. |
| `AUTO_PUSH` | If true, push to remote after commit. If false, commits land locally. |

## Cost estimate at default models

For 150 pages/month at `gpt-4o-mini`:
- 150 product drafts × ~3,500 tokens × $0.0006/1k = **~$0.32**
- 30 safety passes (ingestibles only) × ~600 tokens × $0.015/1k = **~$0.27**
- 8 pillar pages × ~7,000 tokens × $0.015/1k = **~$0.84**
- **Total: ~$1.50/month**. Negligible compared to time saved.

If you upgrade product reviews to `gpt-4o` for higher quality:
- 150 × ~3,500 × $0.015/1k = **~$8/month**. Still trivial.

## Troubleshooting

**App won't start, "OPENAI_API_KEY not set"** — the app boots without a
key, you'll see a yellow warning in the topbar. Set the key in `.env`,
restart.

**"Import from scout" returns 0** — check `SCOUT_PRODUCTS_JSON` path
points to a real file. If you see "products.json not found" the path
is wrong.

**Draft generation fails with rate-limit error** — OpenAI throttles new
accounts. Lower `MAX_PARALLEL_DRAFTS` to 2-3 until your tier upgrades
(usually within a few days of first paid usage).

**Publish succeeds but item never moves to `live`** — VPS rebuild may be
slow. Live polling times out at 3 min. Manually check the URL; if it's
serving, update the item stage manually via the Advance dropdown on the
item page.

**Quality gate keeps failing on a specific phrase** — edit
`control/quality.py`, adjust `FORBIDDEN_PHRASES`. Restart.

## Project layout

```
second-spring-control/
├── README.md
├── requirements.txt
├── .env.example
├── control/
│   ├── __init__.py
│   ├── config.py            # env-driven settings
│   ├── db.py                # SQLite schema + helpers
│   ├── pipeline.py          # state machine + transitions
│   ├── chatgpt.py           # OpenAI integration + parallel batch
│   ├── scout_bridge.py      # scout JSON ingest, idea CRUD
│   ├── quality.py           # automated checks between stages
│   ├── publish.py           # write file + git push + live poll
│   ├── main.py              # FastAPI app and routes
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── ideas.html
│   │   ├── queue.html
│   │   ├── item_detail.html
│   │   ├── editor.html
│   │   └── settings.html
│   └── static/
│       └── style.css
└── control.db               # created on first run, do not commit
```

## What's next (Phase 2)

When you've shipped 30+ pages and want more:

- Pillar page builder UI: pick N product slugs, generate the long-form
- Pinterest pin caption batch generator wired to the queue
- Google Trends keyword discovery automatically seeding the Ideas tab
- Auto-internal-linking when new content publishes
- Multi-niche portfolio support if you launch a second site

These are scoped, build whenever you say go.
