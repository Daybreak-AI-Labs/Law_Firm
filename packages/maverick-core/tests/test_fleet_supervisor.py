"""Least-privilege role capabilities are preserved in the firm runner."""
from __future__ import annotations

from types import SimpleNamespace


def _cfg(monkeypatch, cfg):
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: cfg)


# --- capability_for_role ---------------------------------------------------

def test_capability_for_role_narrows_by_role(monkeypatch):
    _cfg(monkeypatch, {"roles": {"analyst": {"allow_tools": ["read_file", "search"]}}})
    from maverick.capability import capability_for_role
    cap = capability_for_role("analyst", principal="agent:acme.bob")
    assert cap.principal == "agent:acme.bob"
    assert cap.permits("read_file") is True
    assert cap.permits("shell") is False  # not in the role's allow-list


def test_capability_for_role_cannot_escalate_past_acl(monkeypatch):
    # [security] denies shell; a role "allowing" shell still can't grant it.
    _cfg(monkeypatch, {
        "security": {"denied_tools": ["shell"]},
        "roles": {"power": {"allow_tools": ["shell", "read_file"]}},
    })
    from maverick.capability import capability_for_role
    cap = capability_for_role("power", principal="agent:acme.p")
    assert cap.permits("shell") is False
    assert cap.permits("read_file") is True


def test_capability_for_role_unknown_role_is_rejected(monkeypatch):
    _cfg(monkeypatch, {"roles": {"analyst": {"allow_tools": ["read_file"]}}})
    import pytest
    from maverick.capability import UnknownRoleError, capability_for_role

    with pytest.raises(UnknownRoleError, match="undefined RBAC role"):
        capability_for_role("ghost", principal="agent:acme.g")


def test_capability_for_role_empty_role_is_rejected(monkeypatch):
    _cfg(monkeypatch, {"roles": {"analyst": {"allow_tools": ["read_file"]}}})
    import pytest
    from maverick.capability import UnknownRoleError, capability_for_role

    with pytest.raises(UnknownRoleError, match="undefined RBAC role"):
        capability_for_role("", principal="agent:acme.g")


def test_capability_for_role_default_principal(monkeypatch):
    _cfg(monkeypatch, {"roles": {"analyst": {"allow_tools": ["read_file"]}}})
    from maverick.capability import capability_for_role
    assert capability_for_role("analyst").principal == "agent"


# --- runner threads capability into the SwarmContext -----------------------

def _allow_test_matter(monkeypatch):
    from maverick.matter_context import MatterContext

    monkeypatch.setattr(
        "maverick.matter_context.resolve_goal_matter_context",
        lambda _world, _goal_id, *, principal, source: MatterContext(
            matter_id=1,
            client_id=1,
            principal=principal,
            membership_role="attorney",
            domain="legal",
            jurisdiction="US",
            purpose="goal-execution",
            source=source,
        ),
    )


def test_run_goal_in_thread_threads_capability_into_ctx(monkeypatch):
    """The capability handed to the runner reaches SwarmContext.capability,
    so the root agent runs least-privileged under it (no real LLM runs)."""
    from maverick import budget as budget_mod
    from maverick import llm as llm_mod
    from maverick import orchestrator, runner, world_model
    from maverick import sandbox as sandbox_mod
    from maverick.capability import Capability
    from maverick.swarm import SwarmContext

    captured: dict = {}

    class FakeWorld:
        def get_goal(self, goal_id):
            return SimpleNamespace(id=goal_id, status="done")

        def close(self):
            pass

    def fake_run_goal_sync(*args, **kwargs):
        # Build the ctx exactly as run_goal would and assert the seam carries
        # the capability through. Keeps the test offline (no agent.run()).
        ctx = SwarmContext(
            llm=kwargs.get("llm") or args[0],
            world=FakeWorld(),
            budget=object(),
            blackboard=object(),
            sandbox=object(),
            goal_id=kwargs["goal_id"] if "goal_id" in kwargs else args[3],
            capability=kwargs.get("capability"),
            user_id=kwargs.get("user_id"),
        )
        captured["cap"] = ctx.capability
        captured["user_id"] = ctx.user_id
        return "DONE."

    monkeypatch.setattr(world_model, "open_world", lambda *a, **k: FakeWorld())
    monkeypatch.setattr(llm_mod, "LLM", lambda: object())
    monkeypatch.setattr(sandbox_mod, "build_sandbox", lambda: object())
    monkeypatch.setattr(budget_mod, "budget_from_config", lambda **_kwargs: object())
    monkeypatch.setattr(orchestrator, "run_goal_sync", fake_run_goal_sync)
    _allow_test_matter(monkeypatch)

    cap = Capability(principal="agent:acme.bob", allow_tools=frozenset({"read_file"}))
    status = runner.run_goal_in_thread(
        7,
        capability=cap,
        user_id="agent:acme.bob",
        concurrency_principal="user:alice",
    )
    assert status == "done"
    assert captured["cap"] is cap
    assert captured["cap"].principal == "agent:acme.bob"
    assert captured["user_id"] == "agent:acme.bob"



def test_run_goal_in_thread_closes_sandbox(monkeypatch):
    from maverick import budget as budget_mod
    from maverick import llm as llm_mod
    from maverick import orchestrator, runner, world_model
    from maverick import sandbox as sandbox_mod

    closed = {"sandbox": False, "world": False}

    class FakeWorld:
        def get_goal(self, goal_id):
            return SimpleNamespace(id=goal_id, status="done")

        def close(self):
            closed["world"] = True

    class FakeSandbox:
        def close(self):
            closed["sandbox"] = True

    monkeypatch.setattr(world_model, "open_world", lambda *a, **k: FakeWorld())
    monkeypatch.setattr(llm_mod, "LLM", lambda: object())
    monkeypatch.setattr(sandbox_mod, "build_sandbox", lambda: FakeSandbox())
    monkeypatch.setattr(budget_mod, "budget_from_config", lambda **_kwargs: object())
    monkeypatch.setattr(orchestrator, "run_goal_sync", lambda *args, **kwargs: None)
    _allow_test_matter(monkeypatch)

    assert runner.run_goal_in_thread(
        7,
        concurrency_principal="user:alice",
    ) == "done"
    assert closed == {"sandbox": True, "world": True}

def test_run_goal_in_thread_default_capability_is_none(monkeypatch):
    """Default None == zero behaviour change: no capability reaches the run."""
    from maverick import budget as budget_mod
    from maverick import llm as llm_mod
    from maverick import orchestrator, runner, world_model
    from maverick import sandbox as sandbox_mod

    captured: dict = {}

    class FakeWorld:
        def get_goal(self, goal_id):
            return SimpleNamespace(id=goal_id, status="done")

        def close(self):
            pass

    def fake_run_goal_sync(*args, **kwargs):
        captured["cap"] = kwargs.get("capability", "MISSING")
        return "DONE."

    monkeypatch.setattr(world_model, "open_world", lambda *a, **k: FakeWorld())
    monkeypatch.setattr(llm_mod, "LLM", lambda: object())
    monkeypatch.setattr(sandbox_mod, "build_sandbox", lambda: object())
    monkeypatch.setattr(budget_mod, "budget_from_config", lambda **_kwargs: object())
    monkeypatch.setattr(orchestrator, "run_goal_sync", fake_run_goal_sync)
    _allow_test_matter(monkeypatch)

    runner.run_goal_in_thread(7, concurrency_principal="user:alice")
    assert captured["cap"] is None

