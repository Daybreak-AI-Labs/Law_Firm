"""Retained flow helpers cannot cross the mandatory matter boundary."""
from __future__ import annotations


def _unexpected(*_args, **_kwargs):
    raise AssertionError("retired producer performed goal/model/network work")


def test_default_flow_agent_runner_never_creates_or_runs_goal(monkeypatch):
    from maverick.flow import execution

    class HostileWorld:
        create_goal = _unexpected
        get_goal = _unexpected

    monkeypatch.setattr("maverick.runner.run_goal_in_thread", _unexpected)
    runner = execution.default_agent_runner(
        HostileWorld(),
        owner="user:alice",
        concurrency_principal="user:alice",
    )

    result, outcome = runner("external brief", {"untrusted": True})

    assert "pre-bound durable matter goal is required" in result
    assert outcome == 0.0
