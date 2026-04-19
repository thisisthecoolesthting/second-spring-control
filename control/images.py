"""Unsplash image picker.

When an article or pillar item doesn't have a heroImage in its
frontmatter, this module queries Unsplash for a landscape photo matching
the item's topic and writes the URL into the markdown frontmatter.

Free Unsplash demo keys allow 50 requests/hour. Production keys (after
their review) give 5000/hour. We cache recent queries in memory so a
batch run doesn't re-query the same term.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Tiny in-process LRU cache: {query_lowercase -> url}
_cache: dict[str, str] = {}
_CACHE_MAX = 500


def _derive_query(title: str, tags: list[str] | None = None, target_keyword: str = "") -> str:
    """Turn an item's metadata into a reasonable Unsplash search term."""
    if target_keyword:
        return target_keyword.strip()
    if tags:
        # First 2-3 tags, joined — gives focused search without over-constraining.
        return " ".join(tags[:3])
    # Fall back to the first 6 meaningful words of the title.
    words = re.findall(r"\b\w+\b", title.lower())
    stop = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "is", "are"}
    meaningful = [w for w in words if w not in stop][:6]
    return " ".join(meaningful) if meaningful else title[:40]


async def find_hero_image(
    title: str,
    tags: list[str] | None = None,
    target_keyword: str = "",
) -> Optional[str]:
    """Search Unsplash, return a 'regular' (~1080px) image URL or None."""
    if not settings.unsplash_access_key:
        log.info("unsplash: UNSPLASH_ACCESS_KEY not set, skipping")
        return None

    query = _derive_query(title, tags, target_keyword)
    cache_key = query.lower()
    if cache_key in _cache:
        return _cache[cache_key]

    url = "https://api.unsplash.com/search/photos"
    headers = {"Authorization": f"Client-ID {settings.unsplash_access_key}"}
    params = {
        "query": query,
        "per_page": 3,
        "orientation": "landscape",
        "content_filter": "high",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("unsplash query failed for %r: %s", query, e)
        return None

    results = data.get("results", [])
    if not results:
        log.info("unsplash: no results for %r", query)
        return None

    # Pick the first result that has a caption/description (higher editorial
    # quality usually), else the first result.
    chosen = next((r for r in results if r.get("description")), results[0])
    image_url = chosen["urls"].get("regular") or chosen["urls"].get("full")
    if not image_url:
        return None

    # Append optimization params so Astro doesn't pull a 2MB original.
    if "?" not in image_url:
        image_url = f"{image_url}?auto=format&fit=crop&w=1600&q=85"

    # Cache it.
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[cache_key] = image_url
    return image_url


HERO_FRONTMATTER_RE = re.compile(r'^heroImage:\s*"[^"]*"\s*$', re.MULTILINE)


def inject_hero_image(md: str, image_url: str) -> str:
    """Add or replace the heroImage frontmatter field in a Markdown doc.

    Handles three cases:
      1. heroImage: "existing" -> replace value
      2. heroImage: ""         -> replace value
      3. no heroImage line     -> insert after title line
    """
    if not md or not image_url:
        return md

    # Escape quotes in URL just in case.
    safe_url = image_url.replace('"', "")

    # Case 1 & 2: existing heroImage line (value may be empty).
    if HERO_FRONTMATTER_RE.search(md):
        return HERO_FRONTMATTER_RE.sub(f'heroImage: "{safe_url}"', md, count=1)

    # Case 3: no heroImage line — insert after the title line within frontmatter.
    lines = md.splitlines(keepends=True)
    in_frontmatter = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            else:
                # End of frontmatter without finding title — insert before closing ---
                lines.insert(i, f'heroImage: "{safe_url}"\n')
                return "".join(lines)
        if in_frontmatter and stripped.startswith("title:"):
            lines.insert(i + 1, f'heroImage: "{safe_url}"\n')
            return "".join(lines)

    # No frontmatter found at all — prepend a minimal one.
    return f'---\nheroImage: "{safe_url}"\n---\n{md}'
