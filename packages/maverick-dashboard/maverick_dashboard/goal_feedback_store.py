"""Append-only store of human feedback on a goal's result.

The sign-off gate (``/goals/{id}/signoff``) is the *governed* certify/reject, but
it only exists when a pack declares an output gate. Most runs have no gate, so
until now a person looking at a finished result had no way to say "this was
right" / "this was wrong" -- a dead signal the learning loop never saw. This is
that always-available thumbs-up/down: it grounds a real outcome into the
Consequence Engine (via the shared self-outcome helper) AND persists the verdict
here so the UI can show the current rating and the "what your workforce learned"
view can count the human signal accumulated.

Append-only NDJSON (the ``consequence`` / ``trigger_events`` store shape): 0600,
best-effort (never raises), bounded tail, latest row per goal wins. Owner-stamped
for owner-scoped reads.
"""
from __future__ import annotations

import time

from maverick import config
from maverick.ndjson_log import append_row, clamp01, tail_rows

_MAX_ROWS = 5000  # bound the tail we read back; the file itself grows unbounded


def _path():
    return config.dashboard_overrides_path().parent / "goal-feedback.ndjson"


def record(goal_id: int, owner: str, rating: str, *, value: float,
           note: str = "", by: str = "") -> dict:
    """Append one feedback row (latest per goal wins on read) and return it.
    Never raises."""
    row = {
        "ts": time.time(),
        "goal_id": int(goal_id),
        "owner": str(owner or ""),
        "rating": str(rating),          # "up" | "down" (display), value carries the reward
        "value": clamp01(value),
        "note": str(note)[:500],
        "by": str(by or ""),
    }
    append_row(_path(), row)
    return row


def _read_rows() -> list[dict]:
    return tail_rows(_path(), _MAX_ROWS)


def latest_for_goal(goal_id: int) -> dict | None:
    """The most recent feedback row for a goal, or ``None``."""
    gid = int(goal_id)
    hit = None
    for r in _read_rows():  # rows are append-order, so the last match is newest
        if int(r.get("goal_id", -1)) == gid:
            hit = r
    return hit


def count(*, owner: str | None = None) -> int:
    """How many distinct goals have human feedback (accumulated human signal).
    ``owner=None`` counts all; a specific owner counts only its own."""
    seen: set[int] = set()
    for r in _read_rows():
        if owner is not None and r.get("owner", "") != owner:
            continue
        try:
            seen.add(int(r.get("goal_id")))
        except (TypeError, ValueError):
            continue
    return len(seen)


__all__ = ["record", "latest_for_goal", "count"]
