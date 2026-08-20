"""Matter-accessible legal deliverables grouped by retained workflow.

Where ``/goals`` is one flat list of every accessible run, this groups legal
runs by the deliverable their gated workflow declares.

Pure and presentation-only: the route gathers the pack contracts (specs) and
the recent runs per domain; this shapes them into the view model. A run is
"awaiting sign-off" when its pack declares a gate and the run has finished
(``done``) -- the human review the gate calls for hasn't been recorded yet.
The recorded signoff decision is the authoritative approval linkage.
"""
from __future__ import annotations

from typing import Any

_DONE = "done"


def _run_view(g: Any, gate: str | None, signoff: str | None) -> dict:
    """One run as the inbox shows it. ``awaiting`` = a gated deliverable that
    has finished and has no sign-off recorded yet, so it is sitting for its
    human review; once signed off (approved/rejected) it drops out."""
    status = getattr(g, "status", "") or ""
    return {
        "id": getattr(g, "id", None),
        "title": getattr(g, "title", "") or "",
        "status": status,
        "updated_at": getattr(g, "updated_at", 0) or 0,
        "signoff": signoff,
        "awaiting": bool(gate) and status == _DONE and not signoff,
    }


def build_inbox(
    specs: list[dict],
    runs_by_domain: dict[str, list],
    signoffs: dict[int, str] | None = None,
) -> dict:
    """Shape the legal deliverable inbox.

    ``specs`` are the packs that declare a deliverable (each a dict with
    ``domain``/``deliverable``/``consumers``/``cadence``/``gate``/``shape``/
    ``suite``); ``runs_by_domain`` maps a workflow name to its recent runs
    (newest first). ``signoffs`` maps a goal id to its recorded decision, so a
    reviewed deliverable drops out of the awaiting queue.

    Returns ``items`` (one per deliverable, runs attached, gated-and-finished
    floated to the top) and ``awaiting`` (the flat sign-off queue, newest first).
    """
    signoffs = signoffs or {}
    items: list[dict] = []
    awaiting: list[dict] = []
    for s in specs:
        runs = [_run_view(g, s.get("gate"), signoffs.get(getattr(g, "id", None)))
                for g in runs_by_domain.get(s["domain"], [])]
        awaiting_count = 0
        for r in runs:
            if r["awaiting"]:
                awaiting_count += 1
                awaiting.append({**r, "deliverable": s["deliverable"],
                                 "domain": s["domain"], "gate": s.get("gate")})
        items.append({**s, "runs": runs, "latest": runs[0] if runs else None,
                      "awaiting_count": awaiting_count})

    # Deliverables that need a human float to the top; then alphabetical.
    items.sort(key=lambda it: (it["awaiting_count"] == 0, it["deliverable"].lower()))
    awaiting.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"items": items, "awaiting": awaiting}


__all__ = ["build_inbox"]
