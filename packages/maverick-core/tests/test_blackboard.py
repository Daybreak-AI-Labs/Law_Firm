"""Blackboard tests."""
from __future__ import annotations

from maverick.blackboard import Blackboard


def test_post_and_render():
    bb = Blackboard()
    bb.post("agent1", "plan", "do thing")
    bb.post("agent2", "finding", "got result")
    rendered = bb.render()
    assert "do thing" in rendered
    assert "got result" in rendered
    assert "agent1" in rendered


def test_by_kind_filter():
    bb = Blackboard()
    bb.post("a", "plan", "x")
    bb.post("b", "finding", "y")
    bb.post("c", "plan", "z")
    plans = bb.by_kind("plan")
    findings = bb.by_kind("finding")
    assert len(plans) == 2
    assert len(findings) == 1


def test_by_agent_filter():
    bb = Blackboard()
    bb.post("a", "plan", "x")
    bb.post("b", "plan", "y")
    bb.post("a", "finding", "z")
    a_entries = bb.by_agent("a")
    assert len(a_entries) == 2


def test_render_respects_max_entries():
    bb = Blackboard()
    for i in range(100):
        bb.post("x", "observation", f"item-{i}")
    short = bb.render(max_entries=5)
    # Last 5 items, should contain item-99 but not item-0.
    assert "item-99" in short
    assert "item-0" not in short
