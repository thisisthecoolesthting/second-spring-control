"""IndexNow integration.

Instant indexing signal to Bing, Yandex, Seznam, Naver, and any other
search engine in the IndexNow consortium. When we publish a new URL, we
ping api.indexnow.org; the engines pick it up within minutes instead of
waiting for their normal crawl cycle (which can be weeks for a new site).

Setup:
  1. A key file (32-char hex) must be served at https://<site>/<key>.txt
     with the key as the file contents.
  2. The Astro public/ folder contains this file.
  3. Every publish event calls submit_urls() with the new URL.
"""
from __future__ import annotations

import logging
from typing import Iterable, List

import httpx

from .config import settings

log = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


def _host_from_url(url: str) -> str:
    """Extract hostname from a URL (e.g., https://foo.com/bar -> foo.com)."""
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0]


async def submit_urls(urls: Iterable[str]) -> dict:
    """Submit one or more URLs to IndexNow. Returns {ok, submitted, error}."""
    if not settings.indexnow_enabled:
        return {"ok": False, "skipped": True, "reason": "indexnow_enabled=false"}
    if not settings.indexnow_key:
        return {"ok": False, "skipped": True, "reason": "no INDEXNOW_KEY"}

    url_list: List[str] = [u for u in urls if u and u.startswith("https://")]
    if not url_list:
        return {"ok": False, "submitted": 0, "reason": "no valid URLs"}

    host = _host_from_url(url_list[0])
    key = settings.indexnow_key
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"https://{host}/{key}.txt",
        "urlList": url_list,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                INDEXNOW_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        # IndexNow returns 200 (success) or 202 (accepted) for valid requests.
        # 422 means one or more URLs rejected but the others were accepted.
        # 400/403 = problem with key or host.
        log.info("indexnow: POST %s URLs -> %s", len(url_list), resp.status_code)
        if resp.status_code in (200, 202):
            return {"ok": True, "submitted": len(url_list), "status": resp.status_code}
        return {
            "ok": False,
            "submitted": 0,
            "status": resp.status_code,
            "body": resp.text[:300],
        }
    except Exception as e:
        log.warning("indexnow: exception: %s", e)
        return {"ok": False, "submitted": 0, "error": str(e)}
