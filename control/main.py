"""FastAPI app — the local controller dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Force INFO level for our modules so scheduler messages land in systemd /
# uvicorn log. Without this, the control.scheduler logger inherits
# WARNING-level root and its info() calls are dropped.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from . import chatgpt, db, pipeline, publish, quality, scout_bridge
from .config import settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Second Spring Control")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Init DB at import time so the schema exists for any request path,
# including TestClient and uvicorn workers. Safe to call repeatedly
# (uses CREATE TABLE IF NOT EXISTS).
db.init_db()


# Launch the background scheduler as soon as the event loop is ready.
@app.on_event("startup")
async def _start_scheduler():
    from . import scheduler
    asyncio.create_task(scheduler.scheduler_loop())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    base_ctx = {
        "settings": settings,
        "stage_counts": pipeline.stage_counts(),
    }
    base_ctx.update(ctx)
    # Starlette 0.36+ API: request is the first positional argument.
    return templates.TemplateResponse(request, name, base_ctx)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from . import scheduler
    next_items = pipeline.next_action_items(limit=8)
    recent_events = db.fetch_all(
        "SELECT e.*, i.title, i.slug FROM events e "
        "JOIN items i ON i.id = e.item_id "
        "ORDER BY e.created_at DESC LIMIT 12"
    )
    spend_row = db.fetch_one(
        "SELECT COALESCE(SUM(cost_usd), 0) as total FROM ai_requests"
    )
    spend_today_row = db.fetch_one(
        "SELECT COALESCE(SUM(cost_usd), 0) as total FROM ai_requests "
        "WHERE date(created_at) = date('now')"
    )
    recent_ai_requests = db.fetch_all(
        "SELECT ar.created_at, ar.purpose, ar.status, ar.cost_usd, "
        "  ar.model, i.title "
        "FROM ai_requests ar LEFT JOIN items i ON i.id = ar.item_id "
        "ORDER BY ar.created_at DESC LIMIT 10"
    )
    return _render(
        request, "dashboard.html",
        next_items=next_items,
        recent_events=recent_events,
        total_spend=spend_row["total"] if spend_row else 0,
        today_spend=spend_today_row["total"] if spend_today_row else 0,
        scheduler_runs=scheduler.last_runs,
        recent_ai_requests=recent_ai_requests,
    )


# ---------------------------------------------------------------------------
# Manual scheduler triggers
# ---------------------------------------------------------------------------

@app.post("/scheduler/run/{job}")
async def scheduler_run_now(job: str):
    """Force-trigger a scheduler job immediately instead of waiting for the loop."""
    from . import scheduler as sched
    jobs = {
        "advance":  sched.auto_advance_job,
        "import":   sched.auto_import_job,
        "pipeline": sched.auto_pipeline_job,
        "scout":    sched.auto_scout_job,
        "publish":  sched.auto_publish_job,
        "ideas":    sched.auto_ideas_job,
        "cleanup":  sched.auto_cleanup_job,
        "links":    sched.auto_links_job,
        "images":   sched.auto_images_job,
    }
    if job not in jobs:
        return JSONResponse({"ok": False, "error": f"unknown job: {job}"}, status_code=400)
    try:
        result = await jobs[job]()
        return JSONResponse({"ok": True, "job": job, "result": result})
    except Exception as e:
        return JSONResponse(
            {"ok": False, "job": job, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/ideas", response_class=HTMLResponse)
async def ideas_page(request: Request):
    rows = db.fetch_all(
        "SELECT * FROM ideas WHERE promoted_item_id IS NULL "
        "AND rejected_at IS NULL ORDER BY created_at DESC LIMIT 200"
    )
    return _render(request, "ideas.html", ideas=rows)


@app.post("/ideas")
async def create_idea(
    title: str = Form(...),
    type_: str = Form("product", alias="type"),
    target_keyword: str = Form(""),
    notes: str = Form(""),
):
    scout_bridge.create_idea_item(title=title, type_=type_,
                                  target_keyword=target_keyword, notes=notes)
    return RedirectResponse("/ideas", status_code=303)


@app.post("/ideas/{idea_id}/promote")
async def promote_idea_route(idea_id: int):
    item_id = scout_bridge.promote_idea(idea_id)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/ideas/{idea_id}/reject")
async def reject_idea_route(idea_id: int):
    db.execute("UPDATE ideas SET rejected_at = datetime('now') WHERE id = ?", (idea_id,))
    return RedirectResponse("/ideas", status_code=303)


@app.post("/scout/import")
async def scout_import(min_score: float = Form(0.0), max_items: Optional[int] = Form(None)):
    result = scout_bridge.import_from_products_json(
        min_score=min_score, max_items=max_items,
    )
    return JSONResponse(result)


@app.post("/seed/import")
async def seed_import():
    """Load seed_ideas.json from the project root into the ideas backlog."""
    seed_file = BASE_DIR.parent / "seed_ideas.json"
    if not seed_file.exists():
        return JSONResponse({"ok": False, "error": f"No seed file at {seed_file}"}, status_code=404)
    seeds = json.loads(seed_file.read_text(encoding="utf-8"))
    inserted = 0
    for s in seeds:
        scout_bridge.create_idea_item(
            title=s.get("title", ""),
            type_=s.get("type", "product"),
            target_keyword=s.get("target_keyword", ""),
            notes=s.get("notes", ""),
        )
        inserted += 1
    return JSONResponse({"ok": True, "inserted": inserted})


# ---------------------------------------------------------------------------
# Research / brainstorm
# ---------------------------------------------------------------------------

@app.get("/research", response_class=HTMLResponse)
async def research_page(request: Request):
    recent_brainstorms = db.fetch_all(
        "SELECT * FROM ai_requests WHERE purpose = 'brainstorm' "
        "ORDER BY created_at DESC LIMIT 10"
    )
    return _render(request, "research.html", recent=recent_brainstorms)


@app.post("/research/brainstorm")
async def brainstorm_route(topic: str = Form(...), n: int = Form(20)):
    if not settings.openai_configured:
        return JSONResponse({"ok": False, "error": "OPENAI_API_KEY not configured"}, status_code=400)

    result = await chatgpt.brainstorm_ideas(topic=topic, n=n)
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.error}, status_code=500)

    # Parse JSON, write each as an idea row.
    inserted = 0
    parsed: list = []
    text = result.text.strip()
    # Strip code fences if the model added them despite instructions.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"ok": False, "error": f"AI returned non-JSON: {e}", "raw": result.text[:500]},
            status_code=500,
        )

    for idea in parsed:
        if not idea.get("title"):
            continue
        scout_bridge.create_idea_item(
            title=idea["title"],
            type_=idea.get("type", "product"),
            target_keyword=idea.get("target_keyword", ""),
            notes=f'[brainstorm: {topic}] {idea.get("notes", "")}',
        )
        inserted += 1

    return JSONResponse({
        "ok": True,
        "inserted": inserted,
        "cost": result.cost_usd,
        "tokens": result.completion_tokens,
        "topic": topic,
    })


# ---------------------------------------------------------------------------
# Per-item AI helpers
# ---------------------------------------------------------------------------

@app.post("/items/{item_id}/safety")
async def item_safety(item_id: int):
    item = db.fetch_one("SELECT id, title FROM items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404)
    result = await chatgpt.safety_one(item_id, item["title"])
    return JSONResponse({
        "ok": result.ok,
        "text": result.text,
        "cost": result.cost_usd,
        "error": result.error,
    })


@app.post("/items/{item_id}/meta")
async def item_meta(item_id: int):
    item = db.fetch_one("SELECT id, title FROM items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404)
    meta_result = await chatgpt.generate_meta(item_id, item["title"])
    seo_result = await chatgpt.generate_seo_title(item_id, item["title"])
    return JSONResponse({
        "ok": meta_result.ok and seo_result.ok,
        "meta_description": meta_result.text.strip(),
        "seo_title": seo_result.text.strip(),
        "cost": meta_result.cost_usd + seo_result.cost_usd,
    })


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

@app.post("/bulk/draft")
async def bulk_draft(item_ids: str = Form(...)):
    """Comma-separated item IDs. Fires up to MAX_PARALLEL_DRAFTS at once."""
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return JSONResponse({"ok": False, "error": "no item ids"}, status_code=400)
    placeholders = ",".join("?" * len(ids))
    rows = db.fetch_all(
        f"SELECT id, skeleton_md FROM items WHERE id IN ({placeholders}) "
        "AND skeleton_md IS NOT NULL AND stage = 'skeleton'",
        tuple(ids),
    )
    pairs = [(r["id"], r["skeleton_md"]) for r in rows]
    for iid, _ in pairs:
        pipeline.transition(iid, "drafting", actor="ai", note="bulk draft", skip_gate=True)

    results = await chatgpt.draft_batch(pairs)
    summary = {"ok": 0, "errors": 0, "total_cost": 0.0}
    for (iid, _), result in zip(pairs, results):
        if result.ok:
            db.execute("UPDATE items SET draft_md = ? WHERE id = ?", (result.text, iid))
            pipeline.transition(iid, "draft_ready", actor="ai",
                                note=f"bulk ${result.cost_usd:.4f}", skip_gate=True)
            summary["ok"] += 1
            summary["total_cost"] += result.cost_usd
        else:
            db.execute("UPDATE items SET error = ?, stage = 'skeleton' WHERE id = ?",
                       (result.error, iid))
            summary["errors"] += 1
    return JSONResponse(summary)


@app.post("/bulk/promote")
async def bulk_promote(idea_ids: str = Form(...)):
    ids = [int(x) for x in idea_ids.split(",") if x.strip().isdigit()]
    promoted = 0
    for iid in ids:
        try:
            scout_bridge.promote_idea(iid)
            promoted += 1
        except Exception:
            continue
    return JSONResponse({"ok": True, "promoted": promoted})


@app.post("/bulk/publish")
async def bulk_publish(item_ids: str = Form(...)):
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    summary = {"ok": 0, "errors": 0, "details": []}
    for iid in ids:
        result = await publish.publish_item(iid)
        if result.ok:
            summary["ok"] += 1
        else:
            summary["errors"] += 1
        summary["details"].append({
            "item_id": iid, "ok": result.ok,
            "error": result.error, "live_url": result.live_url,
        })
    return JSONResponse(summary)


# ---------------------------------------------------------------------------
# Auto-pipeline: draft -> critique -> (optional rewrite) -> advance to edited
# No human in the loop unless critique fails after one retry.
# ---------------------------------------------------------------------------

def _parse_critique_json(text: str) -> dict | None:
    """Critique prompt asks for strict JSON. Strip fences just in case."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.startswith("json"):
            t = t[4:].strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


async def _pipeline_one(item_id: int) -> dict:
    """Drive a single item from skeleton through draft + critique + advance.

    Catches every exception so one item's failure never blows up the batch
    or returns 500 to the browser. Errors land in the returned dict.
    """
    try:
        return await _pipeline_one_inner(item_id)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # Persist the error on the item so it shows in the UI.
        try:
            db.execute(
                "UPDATE items SET error = ? WHERE id = ?",
                (f"auto-pipeline exception: {e}", item_id),
            )
        except Exception:
            pass
        return {
            "item_id": item_id,
            "ok": False,
            "error": f"exception: {type(e).__name__}: {e}",
            "traceback": tb.splitlines()[-5:],
        }


async def _pipeline_one_inner(item_id: int) -> dict:
    row = db.fetch_one("SELECT id, skeleton_md, stage FROM items WHERE id = ?", (item_id,))
    if not row:
        return {"item_id": item_id, "ok": False, "error": "not found"}

    # 1. Draft (only if we're starting from skeleton)
    if row["stage"] == "skeleton" and row["skeleton_md"]:
        pipeline.transition(item_id, "drafting", actor="ai", note="auto-pipeline", skip_gate=True)
        # Run draft + meta + seo_title IN PARALLEL since they don't depend
        # on each other. Cuts ~2s off every item by overlapping API calls.
        title_row = db.fetch_one("SELECT title FROM items WHERE id = ?", (item_id,))
        title = title_row["title"] if title_row else ""

        draft_task = asyncio.create_task(chatgpt.draft_one(item_id, row["skeleton_md"]))
        meta_task = asyncio.create_task(chatgpt.generate_meta(item_id, title))
        seo_task = asyncio.create_task(chatgpt.generate_seo_title(item_id, title))
        draft, meta_result, seo_result = await asyncio.gather(
            draft_task, meta_task, seo_task, return_exceptions=False,
        )

        if not draft.ok:
            db.execute("UPDATE items SET error = ?, stage = 'skeleton' WHERE id = ?",
                       (draft.error, item_id))
            return {"item_id": item_id, "ok": False, "error": f"draft failed: {draft.error}"}

        # Splice the meta description and SEO title into the frontmatter.
        draft_text = draft.text
        if meta_result.ok and meta_result.text.strip():
            draft_text = draft_text.replace(
                'metaDescription: ""',
                f'metaDescription: "{meta_result.text.strip().replace(chr(34), chr(39))}"',
                1,
            )
        if seo_result.ok and seo_result.text.strip():
            draft_text = draft_text.replace(
                'seoTitle: ""',
                f'seoTitle: "{seo_result.text.strip().replace(chr(34), chr(39))}"',
                1,
            )

        db.execute("UPDATE items SET draft_md = ? WHERE id = ?", (draft_text, item_id))
        total_cost = draft.cost_usd + (meta_result.cost_usd if meta_result.ok else 0) + (seo_result.cost_usd if seo_result.ok else 0)
        pipeline.transition(item_id, "draft_ready", actor="ai",
                            note=f"auto+seo ${total_cost:.4f}", skip_gate=True)
    elif not row["skeleton_md"]:
        return {"item_id": item_id, "ok": False, "error": "no skeleton"}

    # 2. Critique
    cur = db.fetch_one("SELECT draft_md FROM items WHERE id = ?", (item_id,))
    draft_md = cur["draft_md"] or ""
    if not draft_md:
        return {"item_id": item_id, "ok": False, "error": "no draft to critique"}

    crit = await chatgpt.critique_draft(item_id, draft_md)
    if not crit.ok:
        return {"item_id": item_id, "ok": False, "error": f"critique failed: {crit.error}"}

    parsed = _parse_critique_json(crit.text)
    if not parsed:
        return {"item_id": item_id, "ok": False,
                "error": "critique returned unparseable JSON",
                "raw_preview": crit.text[:300]}

    score = parsed.get("score", 0)
    passed = parsed.get("pass", False)
    rewrite = parsed.get("rewrite") or ""

    # 3. If fail and rewrite provided, swap the draft for the rewrite.
    if not passed and rewrite:
        db.execute("UPDATE items SET draft_md = ? WHERE id = ?", (rewrite, item_id))
        draft_md = rewrite
        # Run critique ONE more time on the rewrite.
        crit2 = await chatgpt.critique_draft(item_id, rewrite)
        parsed2 = _parse_critique_json(crit2.text) or {}
        score = parsed2.get("score", score)
        passed = parsed2.get("pass", False)

    # 4. Save critique note + advance if passed.
    critique_note = f"auto-qa score={score} pass={passed}"
    if passed:
        # Move to edited so the normal reviewing gate picks it up next.
        db.execute("UPDATE items SET edited_md = ? WHERE id = ?", (draft_md, item_id))
        pipeline.transition(item_id, "editing", actor="ai", note=critique_note, skip_gate=True)
        pipeline.transition(item_id, "edited", actor="ai", note=critique_note, skip_gate=True)
    else:
        # Leave it for human pickup with the critique notes saved.
        issues = parsed.get("issues", [])
        issue_summary = "; ".join(f'{i.get("severity")}: {i.get("why")}' for i in issues[:3])
        db.execute("UPDATE items SET error = ? WHERE id = ?",
                   (f"auto-qa failed score={score}: {issue_summary}", item_id))

    return {
        "item_id": item_id,
        "ok": True,
        "score": score,
        "passed": passed,
        "advanced_to": "edited" if passed else "draft_ready",
    }


@app.post("/items/{item_id}/auto-pipeline")
async def auto_pipeline_one(item_id: int):
    return JSONResponse(await _pipeline_one(item_id))


@app.post("/bulk/auto-pipeline")
async def bulk_auto_pipeline(item_ids: str = Form(...)):
    """One-click: draft + critique + advance for every selected skeleton."""
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return JSONResponse({"ok": False, "error": "no item ids"}, status_code=400)

    sem = asyncio.Semaphore(settings.max_parallel_drafts)

    async def _bounded(iid: int):
        async with sem:
            return await _pipeline_one(iid)

    # return_exceptions=True: one item's failure won't crash the whole batch.
    raw_results = await asyncio.gather(
        *(_bounded(i) for i in ids), return_exceptions=True
    )

    # Normalize any stray exceptions into dicts so the response is always JSON.
    results = []
    for iid, r in zip(ids, raw_results):
        if isinstance(r, Exception):
            results.append({
                "item_id": iid, "ok": False,
                "error": f"unhandled: {type(r).__name__}: {r}",
            })
        else:
            results.append(r)

    summary = {"total": len(results), "advanced": 0, "needs_human": 0, "errors": 0}
    for r in results:
        if not r.get("ok"):
            summary["errors"] += 1
        elif r.get("passed"):
            summary["advanced"] += 1
        else:
            summary["needs_human"] += 1
    summary["details"] = results
    return JSONResponse(summary)


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, stage: Optional[str] = None):
    if stage:
        items = pipeline.items_in_stage(stage, limit=200)
    else:
        items = db.fetch_all(
            "SELECT * FROM items WHERE stage NOT IN ('live', 'rejected') "
            "ORDER BY priority DESC, score DESC LIMIT 200"
        )
    return _render(request, "queue.html", items=items, current_stage=stage,
                   stages=pipeline.STAGES)


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    item = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404)
    events = db.fetch_all(
        "SELECT * FROM events WHERE item_id = ? ORDER BY created_at DESC",
        (item_id,),
    )
    requests_log = db.fetch_all(
        "SELECT * FROM ai_requests WHERE item_id = ? ORDER BY created_at DESC",
        (item_id,),
    )
    return _render(request, "item_detail.html", item=item, events=events,
                   ai_requests=requests_log,
                   valid_next=pipeline.TRANSITIONS.get(item["stage"], []))


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
async def editor(request: Request, item_id: int):
    item = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404)
    # Auto-bump to editing if currently draft_ready.
    if item["stage"] == "draft_ready":
        pipeline.transition(item_id, "editing", actor="user", note="opened editor")
        item = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    return _render(request, "editor.html", item=item)


@app.post("/items/{item_id}/save")
async def save_edit(item_id: int, edited_md: str = Form(...)):
    db.execute(
        "UPDATE items SET edited_md = ?, updated_at = datetime('now') WHERE id = ?",
        (edited_md, item_id),
    )
    return JSONResponse({"ok": True})


@app.post("/items/{item_id}/transition")
async def transition_route(
    item_id: int,
    to_stage: str = Form(...),
    note: str = Form(""),
    skip_gate: bool = Form(False),
):
    result = pipeline.transition(item_id, to_stage, actor="user", note=note,
                                 skip_gate=skip_gate)
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.error}, status_code=400)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/draft")
async def request_draft(item_id: int):
    item = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404)
    if not item["skeleton_md"]:
        return JSONResponse({"ok": False, "error": "No skeleton to draft from"}, status_code=400)
    if not settings.openai_configured:
        return JSONResponse({"ok": False, "error": "OPENAI_API_KEY not configured"}, status_code=400)

    pipeline.transition(item_id, "drafting", actor="ai", note="OpenAI request started", skip_gate=True)
    result = await chatgpt.draft_one(item_id, item["skeleton_md"])

    if result.ok:
        db.execute(
            "UPDATE items SET draft_md = ?, updated_at = datetime('now') WHERE id = ?",
            (result.text, item_id),
        )
        pipeline.transition(item_id, "draft_ready", actor="ai",
                            note=f"draft ${result.cost_usd:.4f}, {result.completion_tokens}t",
                            skip_gate=True)
        return JSONResponse({"ok": True, "cost": result.cost_usd, "tokens": result.completion_tokens})
    else:
        db.execute("UPDATE items SET error = ?, stage = 'skeleton' WHERE id = ?",
                   (result.error, item_id))
        return JSONResponse({"ok": False, "error": result.error}, status_code=500)


@app.post("/items/{item_id}/draft-batch")
async def request_draft_batch(item_ids: str = Form(...)):
    """Fire parallel drafts for multiple skeletons. item_ids is comma-separated."""
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    rows = db.fetch_all(
        f"SELECT id, skeleton_md FROM items WHERE id IN ({','.join('?' * len(ids))}) "
        "AND skeleton_md IS NOT NULL",
        tuple(ids),
    )
    pairs = [(r["id"], r["skeleton_md"]) for r in rows]
    for iid, _ in pairs:
        pipeline.transition(iid, "drafting", actor="ai", note="batch draft", skip_gate=True)

    results = await chatgpt.draft_batch(pairs)
    summary = {"ok": 0, "errors": 0, "total_cost": 0.0}
    for (iid, _), result in zip(pairs, results):
        if result.ok:
            db.execute("UPDATE items SET draft_md = ? WHERE id = ?", (result.text, iid))
            pipeline.transition(iid, "draft_ready", actor="ai",
                                note=f"batch ${result.cost_usd:.4f}", skip_gate=True)
            summary["ok"] += 1
            summary["total_cost"] += result.cost_usd
        else:
            db.execute("UPDATE items SET error = ?, stage = 'skeleton' WHERE id = ?",
                       (result.error, iid))
            summary["errors"] += 1
    return JSONResponse(summary)


@app.post("/items/{item_id}/publish")
async def publish_route(item_id: int):
    result = await publish.publish_item(item_id)
    return JSONResponse(result.__dict__)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return _render(request, "settings.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "openai_configured": settings.openai_configured}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def run() -> None:
    import uvicorn
    uvicorn.run("control.main:app", host="127.0.0.1", port=8080, reload=False)


if __name__ == "__main__":
    run()
