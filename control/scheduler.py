"""Background scheduler.

Runs two jobs on independent cadences:
  - auto_advance: every ~60 seconds, push edited/reviewing items one stage
                  closer to publish (gated by quality checks).
  - auto_scout:   every ~6 hours, subprocess out to the scout CLI, then
                  import products + run auto-pipeline on new skeletons.

Launched as an asyncio task from FastAPI's lifespan. Env vars in config.py
control whether each job runs and on what cadence.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db, pipeline, scout_bridge
from .config import settings

log = logging.getLogger(__name__)

# Module-level state so the dashboard can read last-run timestamps.
last_runs: dict[str, dict] = {
    "auto_advance":  {"at": None, "moved": 0, "error": None},
    "auto_scout":    {"at": None, "imported": 0, "drafted": 0, "error": None},
    "auto_import":   {"at": None, "imported": 0, "skipped": 0, "last_mtime": 0, "error": None},
    "auto_pipeline": {"at": None, "drafted": 0, "needs_human": 0, "error": None},
    "auto_publish":  {"at": None, "published": 0, "error": None},
    "auto_ideas":    {"at": None, "inserted": 0, "topic": None, "error": None},
    "auto_cleanup":  {"at": None, "reset": 0, "error": None},
    "auto_links":    {"at": None, "linked": 0, "error": None},
    "auto_images":   {"at": None, "found": 0, "skipped": 0, "error": None},
}


# ---------------------------------------------------------------------------
# auto_advance: push items through the publish-ready gate
# ---------------------------------------------------------------------------

def _advance_one_stage(item_id: int, from_stage: str, to_stage: str) -> bool:
    result = pipeline.transition(item_id, to_stage, actor="scheduler",
                                 note=f"auto-advance {from_stage}->{to_stage}")
    if result.ok:
        # Clear any stale error once an item actually moves forward.
        db.execute("UPDATE items SET error = NULL WHERE id = ?", (item_id,))
        return True
    # Surface the real gate reason so debugging is obvious.
    db.execute(
        "UPDATE items SET error = ? WHERE id = ?",
        (f"gate ({from_stage}->{to_stage}): {result.error}", item_id),
    )
    return False


async def auto_advance_job() -> dict:
    """Advance every `edited` item toward `ready_to_publish` via reviewing.
    Quality gates enforce a stop if something's wrong with the content.
    """
    moved = 0
    # Phase 1: edited -> reviewing
    rows = db.fetch_all("SELECT id FROM items WHERE stage = 'edited'")
    for r in rows:
        if _advance_one_stage(r["id"], "edited", "reviewing"):
            moved += 1

    # Phase 2: reviewing -> ready_to_publish
    rows = db.fetch_all("SELECT id FROM items WHERE stage = 'reviewing'")
    for r in rows:
        if _advance_one_stage(r["id"], "reviewing", "ready_to_publish"):
            moved += 1

    last_runs["auto_advance"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "moved": moved,
        "error": None,
    }
    return {"moved": moved}


# ---------------------------------------------------------------------------
# auto_scout: run scout CLI, then import + auto-pipeline new items
# ---------------------------------------------------------------------------

def _run_scout_cli() -> tuple[bool, str]:
    """Subprocess out to the scout. Returns (ok, combined_output)."""
    scout_dir = Path(settings.scout_project_dir).resolve()
    if not scout_dir.exists():
        return False, f"scout project dir not found: {scout_dir}"

    # Try the venv python first; fall back to system python.
    if sys.platform == "win32":
        venv_py = scout_dir / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = scout_dir / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable

    cmd = [
        py, "-m", "scout.cli", "scout",
        "--source", "keepa",
        "--max", str(settings.auto_scout_max_items),
        "--allow-default-tag",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=str(scout_dir),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "scout timed out after 10 minutes"
    except Exception as e:
        return False, f"scout subprocess failed: {e}"

    out = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode == 0, out[-2000:]


async def auto_scout_job() -> dict:
    """Run scout -> import -> auto-pipeline new skeletons."""
    from . import chatgpt  # avoid circular on import

    log.info("auto_scout: running scout CLI")
    ok, out = _run_scout_cli()
    if not ok:
        last_runs["auto_scout"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "imported": 0, "drafted": 0,
            "error": out[-500:],
        }
        return {"ok": False, "error": out[-500:]}

    log.info("auto_scout: importing products.json")
    result = scout_bridge.import_from_products_json(min_score=0.0)
    imported = result.get("imported", 0)

    drafted = 0
    if imported > 0:
        log.info("auto_scout: drafting %d new skeletons", imported)
        # Import happens in the same process, so _pipeline_one is reachable
        # via main. Import lazily to avoid circular dep at module load.
        from .main import _pipeline_one
        rows = db.fetch_all(
            "SELECT id FROM items WHERE stage = 'skeleton' "
            "ORDER BY score DESC LIMIT ?",
            (settings.auto_scout_max_items,),
        )
        sem = asyncio.Semaphore(settings.max_parallel_drafts)

        async def _bounded(iid):
            async with sem:
                return await _pipeline_one(iid)

        await asyncio.gather(
            *(_bounded(r["id"]) for r in rows),
            return_exceptions=True,
        )
        drafted = len(rows)

    last_runs["auto_scout"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "imported": imported, "drafted": drafted, "error": None,
    }
    return {"imported": imported, "drafted": drafted}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def auto_import_job() -> dict:
    """Auto-import from products.json when its mtime changes."""
    path = Path(settings.scout_products_json)
    if not path.exists():
        last_runs["auto_import"]["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        last_runs["auto_import"]["error"] = f"missing: {path}"
        return {"skipped": True, "reason": "file not found"}

    mtime = path.stat().st_mtime
    prev_mtime = last_runs["auto_import"].get("last_mtime", 0) or 0
    if mtime <= prev_mtime:
        return {"skipped": True, "reason": "unchanged"}

    result = scout_bridge.import_from_products_json(min_score=0.0)
    last_runs["auto_import"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "imported": result.get("imported", 0),
        "skipped": result.get("skipped", 0),
        "last_mtime": mtime,
        "error": result.get("error"),
    }
    log.info("auto_import: imported=%s skipped=%s",
             result.get("imported"), result.get("skipped"))
    return result


async def auto_cleanup_job() -> dict:
    """Reset items stuck in transient stages for longer than threshold."""
    hours = settings.auto_cleanup_stuck_hours
    cutoff_sql = f"datetime('now', '-{hours} hour')"

    # drafting: OpenAI request crashed mid-flight — bounce back to skeleton.
    rows = db.fetch_all(
        f"SELECT id FROM items WHERE stage = 'drafting' "
        f"AND datetime(updated_at) < {cutoff_sql}"
    )
    reset = 0
    for r in rows:
        db.execute(
            "UPDATE items SET stage = 'skeleton', "
            "error = 'auto-cleanup: stuck in drafting' WHERE id = ?",
            (r["id"],),
        )
        reset += 1

    # editing/reviewing for > 24h likely means we forgot about them.
    # Don't auto-advance those — just tag with note so they show in dashboard.
    stale_rows = db.fetch_all(
        "SELECT id FROM items WHERE stage IN ('editing', 'reviewing') "
        "AND datetime(updated_at) < datetime('now', '-24 hour') "
        "AND (error IS NULL OR error NOT LIKE 'auto-cleanup%')"
    )
    for r in stale_rows:
        db.execute(
            "UPDATE items SET error = 'auto-cleanup: stale >24h in editing/reviewing' "
            "WHERE id = ?", (r["id"],)
        )

    last_runs["auto_cleanup"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reset": reset,
        "error": None,
    }
    if reset or stale_rows:
        log.info("auto_cleanup: reset=%s stale_tagged=%s", reset, len(stale_rows))
    return {"reset": reset, "stale_tagged": len(stale_rows)}


async def auto_images_job() -> dict:
    """Find heroImages for pillars and articles that lack one.

    Runs for items in any stage past `draft_ready` that have content but no
    heroImage in their markdown. Queries Unsplash once per item, injects the
    URL into the frontmatter, and saves back to the item's edited_md so the
    next publish cycle writes it to disk.
    """
    from .images import find_hero_image, inject_hero_image

    if not settings.unsplash_access_key:
        last_runs["auto_images"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "found": 0, "skipped": 0,
            "error": "UNSPLASH_ACCESS_KEY not configured",
        }
        return {"skipped": True, "reason": "no unsplash key"}

    # Only target articles and pillars (products already get Amazon images).
    rows = db.fetch_all(
        "SELECT id, title, type, edited_md, draft_md, category "
        "FROM items WHERE type IN ('pillar', 'article') "
        "AND stage IN ('edited', 'reviewing', 'ready_to_publish', 'published', 'live') "
        "AND (edited_md IS NOT NULL OR draft_md IS NOT NULL) "
        "LIMIT 10"
    )
    found = 0
    skipped = 0
    for r in rows:
        md = r["edited_md"] or r["draft_md"] or ""
        # Skip items that already have a populated heroImage.
        if re.search(r'heroImage:\s*"https?://', md):
            skipped += 1
            continue

        image = await find_hero_image(
            title=r["title"] or "",
            target_keyword=r["category"] or "",
        )
        if not image:
            skipped += 1
            continue

        new_md = inject_hero_image(md, image)
        db.execute(
            "UPDATE items SET edited_md = ? WHERE id = ?",
            (new_md, r["id"]),
        )
        found += 1
        log.info("auto_images: %s -> %s", r["title"][:40], image[:80])

    last_runs["auto_images"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "found": found, "skipped": skipped, "error": None,
    }
    return {"found": found, "skipped": skipped}


async def auto_links_job() -> dict:
    """For every item that went 'live' but doesn't yet have a Related
    picks block in its edited_md, pick 2-3 same-category siblings, append
    a Markdown section, rewrite the content file, git commit.
    """
    from .publish import _content_subdir, write_content_file, commit_and_push
    from .config import settings as cfg
    from pathlib import Path

    # Candidate pool: live products without a "You may also like" block.
    rows = db.fetch_all(
        "SELECT id, asin, slug, title, category, edited_md, type "
        "FROM items WHERE type = 'product' AND stage IN ('published', 'live') "
        "AND (edited_md IS NULL OR edited_md NOT LIKE '%You may also like%') "
        "LIMIT 10"
    )
    if not rows:
        last_runs["auto_links"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "linked": 0, "error": None,
        }
        return {"linked": 0, "reason": "nothing to link"}

    linked_count = 0
    per_item = settings.auto_links_per_item

    for r in rows:
        category = r["category"]
        item_id = r["id"]
        title_lower = (r["title"] or "").lower()

        # Find same-category siblings that are also live/published.
        siblings = db.fetch_all(
            "SELECT slug, title FROM items "
            "WHERE id != ? AND category = ? AND type = 'product' "
            "AND stage IN ('published', 'live') "
            "ORDER BY score DESC LIMIT ?",
            (item_id, category, per_item * 3),  # over-fetch for dedup below
        )
        # Pick the top `per_item` that don't share suspicious duplicate
        # substrings with the current title (avoid linking LEVOIT to LEVOIT).
        picked = []
        for s in siblings:
            s_title = (s["title"] or "").lower()
            # Skip if first 3 words overlap heavily — likely same brand/product line.
            if len(s_title) > 0 and s_title[:20] == title_lower[:20]:
                continue
            picked.append(s)
            if len(picked) >= per_item:
                break
        if not picked:
            continue

        # Build the Related block.
        lines = ["", "## You may also like", ""]
        for s in picked:
            lines.append(f"- [{s['title']}](/products/{s['slug']}/)")
        lines.append("")
        block = "\n".join(lines)

        new_md = (r["edited_md"] or "") + block
        db.execute("UPDATE items SET edited_md = ? WHERE id = ?", (new_md, item_id))

        # Rewrite the .md file on disk and commit.
        try:
            target = write_content_file(dict(r) | {"edited_md": new_md, "type": r["type"]})
            ok, _sha, _msg = commit_and_push(
                target, f"auto-links: related picks for {r['slug']}"
            )
            if ok:
                linked_count += 1
                db.execute(
                    "INSERT INTO events (item_id, from_stage, to_stage, actor, note) "
                    "VALUES (?, 'published', 'published', 'auto-links', "
                    "'added related picks block')",
                    (item_id,),
                )
        except Exception as e:
            log.warning("auto-links write failed for id=%s: %s", item_id, e)
            continue

    last_runs["auto_links"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "linked": linked_count,
        "error": None,
    }
    if linked_count:
        log.info("auto_links: added Related blocks to %d items", linked_count)
    return {"linked": linked_count}


async def auto_pipeline_job() -> dict:
    """Draft + critique + advance any skeleton items waiting in the queue.
    Runs the same path as the manual 'Bulk auto-pipeline' button, just
    batched and on a timer.
    """
    rows = db.fetch_all(
        "SELECT id FROM items WHERE stage = 'skeleton' "
        "ORDER BY priority DESC, score DESC LIMIT ?",
        (settings.auto_pipeline_batch,),
    )
    if not rows:
        last_runs["auto_pipeline"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "drafted": 0, "needs_human": 0, "error": None,
        }
        return {"drafted": 0, "reason": "no skeletons"}

    from .main import _pipeline_one
    sem = asyncio.Semaphore(min(settings.max_parallel_drafts, len(rows)))

    async def _bounded(iid):
        async with sem:
            return await _pipeline_one(iid)

    results = await asyncio.gather(
        *(_bounded(r["id"]) for r in rows), return_exceptions=True
    )
    drafted = 0
    needs_human = 0
    errors: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            errors.append(f"{type(r).__name__}: {r}")
        elif not r.get("ok"):
            errors.append(str(r.get("error", "unknown")))
        elif r.get("passed"):
            drafted += 1
        else:
            needs_human += 1

    last_runs["auto_pipeline"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "drafted": drafted,
        "needs_human": needs_human,
        "error": "; ".join(errors[:3]) if errors else None,
    }
    return {"drafted": drafted, "needs_human": needs_human, "errors": errors}


async def auto_publish_job() -> dict:
    """Drip-publish N items per run from ready_to_publish."""
    from . import publish
    rows = db.fetch_all(
        "SELECT id FROM items WHERE stage = 'ready_to_publish' "
        "ORDER BY priority DESC, score DESC LIMIT ?",
        (settings.auto_publish_batch,),
    )
    published = 0
    errors: list[str] = []
    for r in rows:
        try:
            result = await publish.publish_item(r["id"])
            if result.ok:
                published += 1
            else:
                errors.append(f"id={r['id']}: {result.error}")
        except Exception as e:
            errors.append(f"id={r['id']}: {type(e).__name__}: {e}")
    last_runs["auto_publish"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "published": published,
        "error": "; ".join(errors[:3]) if errors else None,
    }
    return {"published": published, "errors": errors}


async def auto_ideas_job() -> dict:
    """If the ideas backlog drops below threshold, brainstorm more."""
    from . import chatgpt
    cnt_row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM ideas "
        "WHERE promoted_item_id IS NULL AND rejected_at IS NULL"
    )
    cnt = cnt_row["n"] if cnt_row else 0
    if cnt >= settings.auto_ideas_min_backlog:
        return {"skipped": True, "backlog": cnt}

    topics = [t.strip() for t in settings.auto_ideas_topics.split("|") if t.strip()]
    if not topics:
        return {"skipped": True, "reason": "no topics configured"}

    # Rotate: pick a topic we haven't used recently.
    used = db.fetch_all(
        "SELECT user_prompt FROM ai_requests WHERE purpose='brainstorm' "
        "ORDER BY created_at DESC LIMIT 5"
    )
    recent_topic_hash = set()
    for u in used:
        for t in topics:
            if t in (u["user_prompt"] or ""):
                recent_topic_hash.add(t)
    fresh = [t for t in topics if t not in recent_topic_hash] or topics
    topic = fresh[0]

    result = await chatgpt.brainstorm_ideas(topic=topic, n=20)
    if not result.ok:
        last_runs["auto_ideas"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inserted": 0, "topic": topic,
            "error": result.error,
        }
        return {"ok": False, "error": result.error}

    import json as _json
    text = result.text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = _json.loads(text)
    except Exception as e:
        last_runs["auto_ideas"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inserted": 0, "topic": topic,
            "error": f"unparseable: {e}",
        }
        return {"ok": False, "error": f"unparseable: {e}"}

    inserted = 0
    for idea in parsed:
        if not idea.get("title"):
            continue
        scout_bridge.create_idea_item(
            title=idea["title"],
            type_=idea.get("type", "product"),
            target_keyword=idea.get("target_keyword", ""),
            notes=f"[auto-backfill: {topic}] {idea.get('notes', '')}",
            source="auto-brainstorm",
        )
        inserted += 1

    last_runs["auto_ideas"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inserted": inserted, "topic": topic, "error": None,
    }
    return {"inserted": inserted, "topic": topic}


async def scheduler_loop() -> None:
    """Long-lived asyncio task. Exits only when the app shuts down."""
    log.info(
        "scheduler: starting (advance=%s, scout=%s, import=%s, pipeline=%s, "
        "publish=%s, ideas=%s, cleanup=%s, links=%s, images=%s)",
        settings.auto_advance_enabled,
        settings.auto_scout_enabled,
        settings.auto_import_enabled,
        settings.auto_pipeline_enabled,
        settings.auto_publish_enabled,
        settings.auto_ideas_enabled,
        settings.auto_cleanup_enabled,
        settings.auto_links_enabled,
        bool(settings.unsplash_access_key) and settings.auto_images_enabled,
    )

    last_advance = 0.0
    last_scout = 0.0
    last_import = 0.0
    last_pipeline = 0.0
    last_publish = 0.0
    last_ideas = 0.0
    last_cleanup = 0.0
    last_links = 0.0
    last_images = 0.0
    # Delay the network-heavy jobs so startup isn't noisy.
    scout_warmup = time.monotonic() + 120
    import_warmup = time.monotonic() + 20
    pipeline_warmup = time.monotonic() + 60
    publish_warmup = time.monotonic() + 180
    ideas_warmup = time.monotonic() + 240
    cleanup_warmup = time.monotonic() + 300
    links_warmup = time.monotonic() + 360
    images_warmup = time.monotonic() + 420

    while True:
        now = time.monotonic()

        if settings.auto_advance_enabled and (now - last_advance) >= settings.auto_advance_interval_sec:
            try:
                await auto_advance_job()
            except Exception as e:
                log.exception("auto_advance failed")
                last_runs["auto_advance"]["error"] = str(e)
            last_advance = now

        if (
            settings.auto_scout_enabled
            and now >= scout_warmup
            and (now - last_scout) >= settings.auto_scout_interval_hours * 3600
        ):
            try:
                await auto_scout_job()
            except Exception as e:
                log.exception("auto_scout failed")
                last_runs["auto_scout"]["error"] = str(e)
            last_scout = now

        if (
            settings.auto_import_enabled
            and now >= import_warmup
            and (now - last_import) >= settings.auto_import_interval_min * 60
        ):
            try:
                await auto_import_job()
            except Exception as e:
                log.exception("auto_import failed")
                last_runs["auto_import"]["error"] = str(e)
            last_import = now

        if (
            settings.auto_cleanup_enabled
            and now >= cleanup_warmup
            and (now - last_cleanup) >= settings.auto_cleanup_interval_min * 60
        ):
            try:
                await auto_cleanup_job()
            except Exception as e:
                log.exception("auto_cleanup failed")
                last_runs["auto_cleanup"]["error"] = str(e)
            last_cleanup = now

        if (
            settings.auto_pipeline_enabled
            and now >= pipeline_warmup
            and (now - last_pipeline) >= settings.auto_pipeline_interval_min * 60
        ):
            try:
                await auto_pipeline_job()
            except Exception as e:
                log.exception("auto_pipeline failed")
                last_runs["auto_pipeline"]["error"] = str(e)
            last_pipeline = now

        if (
            settings.auto_publish_enabled
            and now >= publish_warmup
            and (now - last_publish) >= settings.auto_publish_interval_min * 60
        ):
            try:
                await auto_publish_job()
            except Exception as e:
                log.exception("auto_publish failed")
                last_runs["auto_publish"]["error"] = str(e)
            last_publish = now

        if (
            settings.auto_images_enabled
            and now >= images_warmup
            and (now - last_images) >= settings.auto_images_interval_min * 60
        ):
            try:
                await auto_images_job()
            except Exception as e:
                log.exception("auto_images failed")
                last_runs["auto_images"]["error"] = str(e)
            last_images = now

        if (
            settings.auto_links_enabled
            and now >= links_warmup
            and (now - last_links) >= settings.auto_links_interval_min * 60
        ):
            try:
                await auto_links_job()
            except Exception as e:
                log.exception("auto_links failed")
                last_runs["auto_links"]["error"] = str(e)
            last_links = now

        if (
            settings.auto_ideas_enabled
            and now >= ideas_warmup
            and (now - last_ideas) >= 1800  # at most every 30 min
        ):
            try:
                await auto_ideas_job()
            except Exception as e:
                log.exception("auto_ideas failed")
                last_runs["auto_ideas"]["error"] = str(e)
            last_ideas = now

        await asyncio.sleep(5)  # poll heartbeat
