"""Secure-default run_goal requires a durable, matching matter authority."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick import audit
from maverick import matter_context as matter_context_module
from maverick import orchestrator as orch
from maverick.matter_context import (
    GOAL_EXECUTION_PURPOSE,
    MatterContext,
    MatterContextError,
    current_matter_context,
    matter_context_scope,
)


def _context(*, matter_id: int = 101, domain: str = "legal") -> MatterContext:
    return MatterContext(
        matter_id=matter_id,
        client_id=11,
        principal="user:attorney@example.test",
        membership_role="attorney",
        domain=domain,
        jurisdiction="Tennessee",
        purpose=GOAL_EXECUTION_PURPOSE,
        source="test",
    )


def _forbid_post_context_work(monkeypatch, calls: list[str]) -> None:
    def settlement(**_kwargs):
        calls.append("settlement")
        raise AssertionError("quota settlement constructed")

    async def implementation(**_kwargs):
        calls.append("provider")
        raise AssertionError("goal implementation entered")

    monkeypatch.setattr(orch, "_QuotaUsageSettlement", settlement)
    monkeypatch.setattr(orch, "_run_goal_impl", implementation)
    monkeypatch.setattr(
        audit,
        "audit_event",
        lambda *_args, **_kwargs: calls.append("audit") or True,
    )


@pytest.mark.asyncio
async def test_secure_run_goal_missing_context_fails_before_world_or_work(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    calls: list[str] = []
    _forbid_post_context_work(monkeypatch, calls)

    class UnreadWorld:
        def get_goal(self, _goal_id):
            calls.append("world")
            raise AssertionError("durable goal read without bound context")

    with pytest.raises(MatterContextError, match="not bound"):
        await orch.run_goal(object(), UnreadWorld(), object(), 7)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "requested_domain", "message"),
    [
        (_context(matter_id=202), None, "durable goal matter"),
        (_context(domain="probate"), None, "durable goal domain"),
        (_context(), "probate", "requested goal domain"),
    ],
)
async def test_secure_run_goal_context_mismatch_fails_before_work(
    monkeypatch, context, requested_domain, message,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    calls: list[str] = []
    _forbid_post_context_work(monkeypatch, calls)

    class GoalWorld:
        def get_goal(self, goal_id):
            calls.append("goal-read")
            return SimpleNamespace(
                id=goal_id,
                project_id=101,
                domain="legal",
                owner="user:attorney@example.test",
            )

    with matter_context_scope(context):
        with pytest.raises(MatterContextError, match=message):
            await orch.run_goal(
                object(), GoalWorld(), object(), 7,
                domain=requested_domain,
            )

    assert calls == ["goal-read"]


@pytest.mark.asyncio
async def test_secure_run_goal_fresh_principal_mismatch_fails_before_work(
    monkeypatch,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    calls: list[str] = []
    _forbid_post_context_work(monkeypatch, calls)
    bound = _context()
    changed = MatterContext(
        matter_id=bound.matter_id,
        client_id=bound.client_id,
        principal="user:different@example.test",
        membership_role=bound.membership_role,
        domain=bound.domain,
        jurisdiction=bound.jurisdiction,
        purpose=bound.purpose,
        source=bound.source,
    )

    class GoalWorld:
        def get_goal(self, goal_id):
            calls.append("goal-read")
            return SimpleNamespace(
                id=goal_id,
                project_id=bound.matter_id,
                domain=bound.domain,
                owner=bound.principal,
            )

    monkeypatch.setattr(
        matter_context_module, "resolve_matter_context",
        lambda *_args, **_kwargs: changed,
    )

    with matter_context_scope(bound):
        with pytest.raises(MatterContextError, match="context changed"):
            await orch.run_goal(object(), GoalWorld(), object(), 7)

    assert calls == ["goal-read"]


@pytest.mark.asyncio
async def test_secure_run_goal_refreshes_and_binds_current_egress_policy(
    monkeypatch,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    seen: dict = {}
    stale = _context()
    fresh = MatterContext(
        matter_id=stale.matter_id,
        client_id=stale.client_id,
        principal=stale.principal,
        membership_role="responsible_attorney",
        domain=stale.domain,
        jurisdiction=stale.jurisdiction,
        purpose=stale.purpose,
        source=stale.source,
        egress_mode="approved_services",
    )
    goal = SimpleNamespace(
        id=7,
        project_id=101,
        domain="legal",
        owner=stale.principal,
    )

    class GoalWorld:
        def get_goal(self, goal_id):
            assert goal_id == goal.id
            return goal

    def resolve(_world, **kwargs):
        seen["resolve"] = kwargs
        return fresh

    class Settlement:
        def __init__(self, *, principal, budget, domain):
            del budget, domain
            seen["quota_principal"] = principal

        def record(self):
            seen["settlement_context"] = current_matter_context()

    async def implementation(**kwargs):
        seen["implementation_context"] = current_matter_context()
        seen["verified_goal"] = kwargs["_verified_goal"]
        return "done"

    monkeypatch.setattr(matter_context_module, "resolve_matter_context", resolve)
    monkeypatch.setattr(orch, "_QuotaUsageSettlement", Settlement)
    monkeypatch.setattr(orch, "_run_goal_impl", implementation)

    with matter_context_scope(stale):
        result = await orch.run_goal(
            object(), GoalWorld(), object(), goal.id, user_id="spoofed",
        )
        assert current_matter_context() is stale

    assert result == "done"
    assert seen["resolve"] == {
        "matter_id": 101,
        "principal": stale.principal,
        "domain": "legal",
        "purpose": GOAL_EXECUTION_PURPOSE,
        "source": "test",
    }
    assert seen["quota_principal"] == fresh.principal
    assert seen["implementation_context"] is fresh
    assert seen["settlement_context"] is fresh
    assert seen["verified_goal"] is goal
