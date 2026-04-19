"""Bridge to the amazon-affiliate-scout project.

We do not import its Python modules directly (different repo). Instead we
read its products.json and build skeletons inline using a duplicated
copy of the generator template. This keeps repos decoupled.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import db
from .config import settings


def _slugify(text: str, max_len: int = 80) -> str:
    if not text:
        return "untitled"
    s = text.lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "untitled")


SKELETON_BODY = """\
# {title} review

<!-- SECTION: INTRO (80-120 words, friendly-expert voice) -->

## Who this is for

<!-- SECTION: AUDIENCE (3-4 specific personas, one sentence each) -->

## What stands out

<!-- SECTION: HIGHLIGHTS (3 concrete standouts with numbers/evidence) -->

## Who should skip it

<!-- SECTION: CAVEATS (2-3 honest limitations) -->

## The price question

<!-- SECTION: PRICE (60-90 words, "around $X" not exact) -->

## Safety and interactions

<!-- SECTION: SAFETY (REQUIRED for ingestibles/topicals; cite drugs.com/NIH;
     for non-ingestibles return the standard "no interactions apply" line) -->

## Our take

<!-- SECTION: VERDICT (50-80 words; Great pick / Good but has caveats / Skip) -->

<AffiliateCTA asin="{asin}" label="Check current price on Amazon" />

## Related picks from our tests

<!-- SECTION: RELATED (filled later from the catalog) -->
"""


def _frontmatter(record: dict, slug: str) -> str:
    def _val(k: str, default=""):
        v = record.get(k)
        return v if v is not None else default

    title_safe = (record.get("title", "") or "").replace('"', "'")
    lines = [
        "---",
        f'asin: "{record.get("asin", "")}"',
        f'slug: "{slug}"',
        f'title: "{title_safe}"',
        'seoTitle: ""',
        'metaDescription: ""',
        f'category: "{record.get("category", "default")}"',
        f'price: {record.get("price") if record.get("price") is not None else "null"}',
        f'rating: {record.get("rating") if record.get("rating") is not None else "null"}',
        f'reviewCount: {record.get("review_count") if record.get("review_count") is not None else "null"}',
        f'bsr: {record.get("bsr_in_category") if record.get("bsr_in_category") is not None else "null"}',
        f'commissionPerSale: {record.get("commission_per_sale", 0)}',
        f'score: {record.get("score", 0)}',
        f'imageUrl: "{record.get("image_url") or ""}"',
        f'affiliateUrl: "{record.get("affiliate_url", "")}"',
        f'isPrime: {str(record.get("is_prime", False)).lower()}',
        'status: "draft"',
        f'firstSeen: "{record.get("first_seen", "")}"',
        f'lastSeen: "{record.get("last_seen", "")}"',
        "tags:",
        "  - menopause",
        "  - midlife",
        "---",
        "",
    ]
    return "\n".join(lines)


def build_skeleton(record: dict, slug: str) -> str:
    return _frontmatter(record, slug) + SKELETON_BODY.format(
        title=record.get("title", "Untitled"),
        asin=record.get("asin", ""),
    )


def import_from_products_json(
    path: Optional[Path] = None,
    min_score: float = 0.0,
    max_items: Optional[int] = None,
) -> dict:
    """Pull products.json from scout, create items in 'skeleton' stage.

    Idempotent: if an ASIN is already in the items table, skip it.
    """
    p = Path(path or settings.scout_products_json)
    if not p.exists():
        return {"error": f"products.json not found at {p}", "imported": 0, "skipped": 0}

    with p.open("r", encoding="utf-8") as f:
        records = json.load(f)

    sorted_records = sorted(
        (r for r in records if (r.get("score") or 0) >= min_score),
        key=lambda r: r.get("score", 0),
        reverse=True,
    )

    imported = 0
    skipped = 0
    for r in sorted_records:
        if max_items is not None and imported >= max_items:
            break

        asin = r.get("asin")
        if not asin:
            continue

        existing = db.fetch_one("SELECT id FROM items WHERE asin = ?", (asin,))
        if existing:
            skipped += 1
            continue

        slug = _slugify(f'{asin}-{r.get("title", "")}')
        skeleton_md = build_skeleton(r, slug)

        item_id = db.execute(
            "INSERT INTO items (type, asin, slug, title, category, score, "
            "skeleton_md, stage, metadata_json, priority) "
            "VALUES ('product', ?, ?, ?, ?, ?, ?, 'skeleton', ?, ?)",
            (
                asin,
                slug,
                r.get("title", "Untitled"),
                r.get("category", "default"),
                r.get("score", 0),
                skeleton_md,
                json.dumps(r),
                int((r.get("score") or 0) * 100),
            ),
        )
        db.execute(
            "INSERT INTO events (item_id, from_stage, to_stage, actor, note) "
            "VALUES (?, 'idea', 'skeleton', 'scout-import', ?)",
            (item_id, f"imported from {p.name}"),
        )
        imported += 1

    return {"imported": imported, "skipped": skipped, "source": str(p)}


def create_idea_item(
    title: str,
    type_: str = "product",
    target_keyword: str = "",
    notes: str = "",
    priority: int = 50,
    source: str = "manual",
) -> int:
    """Create an idea record (not yet promoted).

    `source` is a free-form tag so the Ideas page can show whether an
    idea came from 'manual' input, 'auto-brainstorm', 'brainstorm', etc.
    """
    return db.execute(
        "INSERT INTO ideas (title, type, source, target_keyword, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, type_, source, target_keyword, notes),
    )


def promote_idea(idea_id: int) -> int:
    """Promote an idea row into an actual item in skeleton stage."""
    idea = db.fetch_one("SELECT * FROM ideas WHERE id = ?", (idea_id,))
    if not idea:
        raise ValueError(f"Idea {idea_id} not found")

    slug = _slugify(idea["title"])
    item_id = db.execute(
        "INSERT INTO items (type, slug, title, target_keyword, stage, priority) "
        "VALUES (?, ?, ?, ?, 'skeleton', 50)",
        (idea["type"], slug, idea["title"], idea["target_keyword"] or ""),
    )
    db.execute("UPDATE ideas SET promoted_item_id = ? WHERE id = ?", (item_id, idea_id))
    db.execute(
        "INSERT INTO events (item_id, from_stage, to_stage, actor, note) "
        "VALUES (?, 'idea', 'skeleton', 'user', 'promoted from idea')",
        (item_id,),
    )
    return item_id
