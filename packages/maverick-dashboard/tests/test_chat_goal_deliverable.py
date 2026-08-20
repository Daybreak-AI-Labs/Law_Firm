"""The goal page renders a legal pack's declared deliverable safely."""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app)

_OBLIGATIONS_RESULT = """\
Contract obligations extracted for attorney review.

| Date | Obligation | Notice |
| --- | --- | --- |
| May 1 | Renewal | 30 days |
| June 3 | Security report | Annual |
"""


def _world(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    return world_model.WorldModel(tmp_path / "world.db")


def test_legal_goal_renders_as_a_review_gated_deliverable_grid(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("Review contract obligations", "", domain="legal_obligations")
    w.set_goal_status(gid, "done", result=_OBLIGATIONS_RESULT)

    t = client.get(f"/chat/goal/{gid}").text
    assert "obligations &amp; renewals tracker" in t
    assert "review gate" in t
    assert "legal_counsel" in t
    assert '<table class="deliverable__table">' in t
    assert "<th scope=\"col\">Obligation</th>" in t
    assert "<td>Renewal</td>" in t


def test_generic_goal_keeps_plain_prose(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("Summarize the meeting", "")  # no domain -> no contract
    w.set_goal_status(gid, "done", result="A short prose summary.")

    t = client.get(f"/chat/goal/{gid}").text
    assert '<div class="prose prewrap" id="goal-result">' in t
    assert "A short prose summary." in t
    # No deliverable card is rendered (the .deliverable__table *style* is always
    # present in the page's <style> block; assert on the rendered markup).
    assert '<table class="deliverable__table">' not in t
    assert 'class="deliverable__head"' not in t
    assert "review gate" not in t
