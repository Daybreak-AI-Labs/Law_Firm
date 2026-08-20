"""Plain-language run explanation remains after retiring the orphan UX store."""
from __future__ import annotations

import types

from maverick.plain_language import explain


def _goal(**kw):
    base = {"title": "Migrate the billing DB", "status": "done", "result": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _ev(kind, content, agent="coder"):
    return types.SimpleNamespace(kind=kind, content=content, agent=agent)


def test_explain_full_story():
    events = [
        _ev("plan", "1. snapshot 2. migrate 3. verify"),
        _ev("finding", "the staging DB had drift"),
        _ev("error", "first migration attempt timed out"),
    ]
    out = explain(_goal(), events)
    assert "Migrate the billing DB" in out
    assert "finished successfully" in out
    assert "plan" in out and "snapshot" in out
    assert "drift" in out
    assert "timed out" in out and "recovered" in out


def test_explain_failure_and_empty():
    out = explain(_goal(status="failed"), [])
    assert "couldn't get past" in out
    assert "No detailed activity" in out


def test_explain_strips_markdown_and_clamps():
    long_note = "**bold** `code` " + "x" * 400
    out = explain(_goal(), [_ev("finding", long_note)])
    assert "**" not in out and "`" not in out
    assert "…" in out


def test_explain_dict_events_and_result():
    out = explain(
        {"title": "T", "status": "running", "result": "half done"},
        [{
            "kind": "observation",
            "content": "API is rate limited",
            "agent": "researcher",
        }],
    )
    assert "still in progress" in out
    assert "rate limited" in out
    assert "half done" in out
