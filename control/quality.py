"""Automated quality checks between pipeline stages.

Two gate kinds:
  'edited'  — run when transitioning edited → reviewing
  'review'  — run when transitioning reviewing → ready_to_publish
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from . import db


# Phrases we won't ship under any circumstances. Affiliate-content tells.
FORBIDDEN_PHRASES = [
    "game-changer", "game changer",
    "revolutionary",
    "must-have", "must have",
    "life-changing", "life changing",
    "transformative",
    "ultimate guide",
    "look no further",
    "dive into",
    "unlock", "unleash",
    "in today's fast-paced world",
    "cutting-edge",
    "world-class",
    "breathtaking",
]


# Markers from the skeleton template that must be removed before publish.
SKELETON_MARKERS = [
    "<!-- SECTION:",
    "[PASTE",
    "[FILL",
    "[YOUR",
]


@dataclass
class CheckResult:
    passed: bool
    reason: str = ""
    warnings: List[str] | None = None


def run_gate(item_id: int, kind: str) -> CheckResult:
    row = db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not row:
        return CheckResult(False, reason=f"Item {item_id} not found")

    md = row["edited_md"] or row["draft_md"] or ""
    if not md.strip():
        return CheckResult(False, reason="No draft content to check")

    if kind == "edited":
        return _check_edited(md)
    if kind == "review":
        return _check_review(md, row)
    return CheckResult(True)


def _check_edited(md: str) -> CheckResult:
    warnings: List[str] = []

    # No leftover skeleton markers.
    for marker in SKELETON_MARKERS:
        if marker in md:
            return CheckResult(False, reason=f"Leftover skeleton marker: {marker!r}")

    # Length sanity. Product reviews should be 500-1800 words.
    word_count = len(re.findall(r"\b\w+\b", md))
    if word_count < 350:
        return CheckResult(False, reason=f"Too short: {word_count} words (min 350)")
    if word_count > 2500:
        warnings.append(f"Long: {word_count} words")

    # Must contain a CTA.
    if "AffiliateCTA" not in md and "amazon.com" not in md.lower():
        return CheckResult(False, reason="No affiliate CTA or Amazon link found")

    # Cliche check.
    md_lower = md.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in md_lower:
            return CheckResult(False, reason=f"Forbidden affiliate cliche: {phrase!r}")

    # Frontmatter integrity.
    if not md.startswith("---"):
        return CheckResult(False, reason="Missing frontmatter (must start with ---)")

    if 'seoTitle: ""' in md:
        warnings.append("seoTitle is empty")
    if 'metaDescription: ""' in md:
        warnings.append("metaDescription is empty")

    return CheckResult(True, warnings=warnings)


def _check_review(md: str, row) -> CheckResult:
    # Re-run all edited checks plus link validity sketch.
    base = _check_edited(md)
    if not base.passed:
        return base

    warnings: List[str] = list(base.warnings or [])

    # Affiliate URL must include our tag.
    expected_tag_substrings = ["tag=secondspring-20", "secondspring-20"]
    if row["asin"] and not any(s in md for s in expected_tag_substrings):
        warnings.append("Could not find Associate tag in body — verify CTA")

    # Safety section presence for ingestibles. Heuristic: title mentions
    # supplement-ish words.
    title_lower = (row["title"] or "").lower()
    ingestible_signals = [
        "supplement", "magnesium", "ashwagandha", "vitamin", "extract",
        "tea", "cream", "serum", "lotion", "lubricant",
    ]
    is_ingestible = any(s in title_lower for s in ingestible_signals)
    if is_ingestible and "safety" not in md.lower():
        return CheckResult(False, reason="Ingestible product missing Safety section")

    return CheckResult(True, warnings=warnings)
