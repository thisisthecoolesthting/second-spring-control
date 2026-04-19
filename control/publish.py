"""Write content to the Astro repo, git commit + push, then poll for liveness."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from . import db
from .config import settings

log = logging.getLogger(__name__)


@dataclass
class PublishResult:
    ok: bool
    commit_sha: str | None = None
    file_path: str | None = None
    live_url: str | None = None
    error: str | None = None


def _content_subdir(item_type: str) -> str:
    return {"product": "products", "pillar": "pillars", "article": "articles"}[item_type]


def _swap_status_to_published(md: str) -> str:
    """Swap `status: "draft"` (or in_review) to `status: "published"` in frontmatter."""
    return (
        md.replace('status: "draft"', 'status: "published"')
          .replace('status: "in_review"', 'status: "published"')
    )


def write_content_file(item: dict) -> Path:
    repo = Path(settings.astro_repo_dir)
    content_root = Path(settings.astro_content_dir)
    subdir = content_root / _content_subdir(item["type"])
    subdir.mkdir(parents=True, exist_ok=True)
    target = subdir / f"{item['slug']}.md"

    md = _swap_status_to_published(item["edited_md"] or item["draft_md"] or "")
    target.write_text(md, encoding="utf-8")

    if not str(target).startswith(str(repo)):
        # Should not happen if config is sane, but guard anyway.
        raise RuntimeError(
            f"Refusing to commit: target {target} is outside Astro repo {repo}"
        )
    return target


def _git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def commit_and_push(file_path: Path, commit_msg: str) -> tuple[bool, str | None, str]:
    repo = Path(settings.astro_repo_dir)
    code, _, err = _git(["add", str(file_path.relative_to(repo))], repo)
    if code != 0:
        return False, None, f"git add failed: {err}"

    code, out, err = _git(["commit", "-m", commit_msg], repo)
    if code != 0:
        if "nothing to commit" in (out + err).lower():
            return True, None, "no-op (nothing to commit)"
        return False, None, f"git commit failed: {err}"

    sha_code, sha, _ = _git(["rev-parse", "HEAD"], repo)
    sha_short = sha[:7] if sha_code == 0 else None

    if not settings.auto_push:
        return True, sha_short, "committed (auto-push disabled)"

    code, _, err = _git(["push", "origin", "HEAD"], repo)
    if code != 0:
        return False, sha_short, f"git push failed: {err}"

    return True, sha_short, "pushed"


async def poll_live(slug: str, item_type: str, timeout_s: int = 180) -> Optional[str]:
    """Poll the live URL until it returns 200 or timeout."""
    subdir = _content_subdir(item_type)
    url = f"{settings.site_url.rstrip('/')}/{subdir}/{slug}/"
    elapsed = 0
    interval = 20
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        while elapsed < timeout_s:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return url
            except Exception:
                pass
            await asyncio.sleep(interval)
            elapsed += interval
    return None


async def publish_item(item_id: int) -> PublishResult:
    item = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        return PublishResult(False, error=f"Item {item_id} not found")

    item_dict = dict(item)
    try:
        target = write_content_file(item_dict)
    except Exception as e:
        return PublishResult(False, error=f"Write failed: {e}")

    commit_msg = f"publish: {item['type']} {item['slug']}"
    ok, sha, msg = commit_and_push(target, commit_msg)
    if not ok:
        return PublishResult(False, file_path=str(target), error=msg)

    db.execute(
        "UPDATE items SET stage = 'published', updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    db.execute(
        "INSERT INTO events (item_id, from_stage, to_stage, actor, note) "
        "VALUES (?, 'ready_to_publish', 'published', 'user', ?)",
        (item_id, msg),
    )

    # Poll for liveness in the background-ish style: caller can await.
    live_url = await poll_live(item["slug"], item["type"])
    if live_url:
        # Ping IndexNow so Bing/Yandex know about the new URL immediately.
        # Fire-and-forget: failures here don't fail the publish.
        try:
            from . import indexnow
            result = await indexnow.submit_urls([live_url])
            if result.get("ok"):
                log.info("indexnow: %s submitted for %s", live_url, item["slug"])
        except Exception as e:
            log.warning("indexnow submission failed for %s: %s", live_url, e)

        db.execute(
            "UPDATE items SET stage = 'live', updated_at = datetime('now') WHERE id = ?",
            (item_id,),
        )
        db.execute(
            "INSERT INTO events (item_id, from_stage, to_stage, actor, note) "
            "VALUES (?, 'published', 'live', 'system', ?)",
            (item_id, f"verified at {live_url}"),
        )

    return PublishResult(
        ok=True, commit_sha=sha, file_path=str(target), live_url=live_url
    )
