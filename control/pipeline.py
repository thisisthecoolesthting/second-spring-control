"""Pipeline state machine.

Stage progression for every item:

    idea
      ↓
    skeleton          (frontmatter + section-marker scaffold ready)
      ↓
    drafting          (OpenAI request in flight)
      ↓
    draft_ready       (AI draft returned, ready for human edit)
      ↓
    editing           (human is actively working on it)
      ↓
    edited            (human declared done)
      ↓
    reviewing         (final automated checks + safety pass)
      ↓
    ready_to_publish  (passed all gates, awaiting git push)
      ↓
    published         (git push fired, awaiting VPS rebuild)
      ↓
    live              (VPS confirmed serving the URL)

Plus terminal: rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

from . import db


STAGES = (
    "idea",
    "skeleton",
    "drafting",
    "draft_ready",
    "editing",
    "edited",
    "reviewing",
    "ready_to_publish",
    "published",
    "live",
    "rejected",
)


# Allowed forward transitions. We allow some back-edges for human override.
TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "idea":             frozenset({"skeleton", "rejected"}),
    "skeleton":         frozenset({"drafting", "editing", "rejected"}),
    "drafting":         frozenset({"draft_ready", "skeleton", "rejected"}),
    "draft_ready":      frozenset({"editing", "skeleton", "rejected"}),
    "editing":          frozenset({"edited", "draft_ready", "rejected"}),
    "edited":           frozenset({"reviewing", "editing", "rejected"}),
    "reviewing":        frozenset({"ready_to_publish", "editing", "rejected"}),
    "ready_to_publish": frozenset({"published", "editing", "rejected"}),
    "published":        frozenset({"live", "ready_to_publish"}),
    "live":             frozenset({"editing"}),
    "rejected":         frozenset({"idea"}),  # un-reject if we change our mind
}


# Stages that require automated quality checks to pass before transitioning.
# Maps from-stage -> to-stage that needs gating.
GATED_TRANSITIONS = {
    ("draft_ready", "editing"): None,   # no gate, just informational
    ("edited", "reviewing"): "edited",  # check no SECTION markers, length, etc.
    ("reviewing", "ready_to_publish"): "review",  # safety + link + frontmatter checks
}


@dataclass
class TransitionResult:
    ok: bool
    item_id: int
    from_stage: str
    to_stage: str
    error: str | None = None


def can_transition(from_stage: str, to_stage: str) -> bool:
    return to_stage in TRANSITIONS.get(from_stage, frozenset())


def transition(
    item_id: int,
    to_stage: str,
    actor: str = "user",
    note: str | None = None,
    skip_gate: bool = False,
) -> TransitionResult:
    row = db.fetch_one("SELECT id, stage FROM items WHERE id = ?", (item_id,))
    if not row:
        return TransitionResult(False, item_id, "?", to_stage, error=f"Item {item_id} not found")

    from_stage = row["stage"]

    if from_stage == to_stage:
        return TransitionResult(True, item_id, from_stage, to_stage, error="No-op")

    if not can_transition(from_stage, to_stage):
        return TransitionResult(
            False, item_id, from_stage, to_stage,
            error=f"Cannot transition {from_stage} → {to_stage}",
        )

    # Gate: run automated checks if applicable.
    if not skip_gate:
        gate_kind = GATED_TRANSITIONS.get((from_stage, to_stage))
        if gate_kind:
            from . import quality
            check_result = quality.run_gate(item_id, gate_kind)
            if not check_result.passed:
                return TransitionResult(
                    False, item_id, from_stage, to_stage,
                    error=f"Quality gate failed: {check_result.reason}",
                )

    db.execute(
        "UPDATE items SET stage = ?, updated_at = datetime('now') WHERE id = ?",
        (to_stage, item_id),
    )
    db.execute(
        "INSERT INTO events (item_id, from_stage, to_stage, actor, note) VALUES (?, ?, ?, ?, ?)",
        (item_id, from_stage, to_stage, actor, note),
    )
    return TransitionResult(True, item_id, from_stage, to_stage)


def stage_counts() -> dict[str, int]:
    rows = db.fetch_all("SELECT stage, COUNT(*) as n FROM items GROUP BY stage")
    counts = {s: 0 for s in STAGES}
    for r in rows:
        counts[r["stage"]] = r["n"]
    return counts


def items_in_stage(stage: str, limit: int = 50) -> list:
    return db.fetch_all(
        "SELECT * FROM items WHERE stage = ? ORDER BY priority DESC, score DESC LIMIT ?",
        (stage, limit),
    )


def next_action_items(limit: int = 5) -> list:
    """Items most likely to need attention right now: anything past skeleton
    that's not live or rejected, sorted by stage urgency."""
    urgency = {
        "ready_to_publish": 0,
        "reviewing": 1,
        "edited": 2,
        "editing": 3,
        "draft_ready": 4,
        "drafting": 5,
        "skeleton": 6,
        "published": 7,
    }
    rows = db.fetch_all(
        "SELECT * FROM items WHERE stage NOT IN ('live', 'rejected', 'idea') "
        "ORDER BY priority DESC, score DESC LIMIT 100"
    )
    sorted_rows = sorted(rows, key=lambda r: (urgency.get(r["stage"], 99), -r["score"]))
    return sorted_rows[:limit]
