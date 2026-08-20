"""Firm-mode Agent dispatch is matter-current, Shielded, and content-free in audit."""
from __future__ import annotations

import hashlib
import json

import pytest
from maverick.agent import Agent
from maverick.blackboard import Blackboard
from maverick.budget import Budget
from maverick.matter_context import (
    GOAL_EXECUTION_PURPOSE,
    MatterContext,
    MatterContextError,
    matter_context_scope,
)
from maverick.sandbox import LocalBackend
from maverick.swarm import SwarmContext
from maverick.tools import Tool, ToolRegistry
from maverick.world_model import WorldModel


def _matter_context() -> MatterContext:
    return MatterContext(
        matter_id=101,
        client_id=11,
        principal="user:attorney@example.test",
        membership_role="attorney",
        domain="legal",
        jurisdiction="Tennessee",
        purpose=GOAL_EXECUTION_PURPOSE,
        source="test",
    )


def _agent(tmp_path, monkeypatch, *, shield):
    # Agent construction exercises a large legacy registry.  The boundary
    # itself is switched to firm mode immediately before dispatch below.
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    world = WorldModel(tmp_path / "world.db")
    goal_id = world.create_goal("boundary test", "")
    ctx = SwarmContext(
        llm=None,
        world=world,
        budget=Budget(),
        blackboard=Blackboard(),
        sandbox=LocalBackend(workdir=tmp_path),
        goal_id=goal_id,
        max_depth=1,
        shield=shield,
    )
    agent = Agent(ctx=ctx, role="researcher", brief="test")
    events: list[tuple[str, dict]] = []
    agent._audit_tool_event = lambda kind, **payload: events.append((kind, payload))
    return agent, events


def _register(agent: Agent, fn) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="matter_read",
            description="read exact matter data",
            input_schema={"type": "object"},
            fn=fn,
        )
    )
    agent.tools = registry


class _AllowShield:
    def scan_tool_call(self, _name, _args):
        return type("Verdict", (), {"allowed": True, "severity": "low", "reasons": []})()

    def scan_output(self, _text):
        return type("Verdict", (), {"allowed": True, "severity": "low", "reasons": []})()


@pytest.mark.asyncio
async def test_revoked_matter_member_never_reaches_read_tool(tmp_path, monkeypatch):
    from maverick import matter_context as matter_context_module

    agent, events = _agent(tmp_path, monkeypatch, shield=_AllowShield())
    calls: list[str] = []
    _register(agent, lambda _args: calls.append("executed") or "client data")
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")

    def revoked(*_args, **_kwargs):
        raise MatterContextError("membership revoked")

    monkeypatch.setattr(matter_context_module, "resolve_goal_matter_context", revoked)
    with matter_context_scope(_matter_context()):
        result = await agent._run_tool("matter_read", {})

    assert calls == []
    assert "authority could not be verified" in result
    assert events == [
        (
            "tool_denied",
            {"name": "matter_read", "status": "matter_authority_unavailable"},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shield", [None, RuntimeError("input scanner failed")])
async def test_missing_or_broken_input_shield_denies_before_tool(
    tmp_path, monkeypatch, shield,
):
    from maverick import matter_context as matter_context_module

    class _BrokenInputShield(_AllowShield):
        def scan_tool_call(self, _name, _args):
            raise shield

    active_shield = None if shield is None else _BrokenInputShield()
    agent, _events = _agent(tmp_path, monkeypatch, shield=active_shield)
    calls: list[str] = []
    _register(agent, lambda _args: calls.append("executed") or "client data")
    bound = _matter_context()
    monkeypatch.setattr(
        matter_context_module,
        "resolve_goal_matter_context",
        lambda *_args, **_kwargs: bound,
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")

    with matter_context_scope(bound):
        result = await agent._run_tool("matter_read", {})

    assert calls == []
    assert "denied" in result.lower()


@pytest.mark.asyncio
async def test_output_scanner_error_withholds_client_text(tmp_path, monkeypatch):
    from maverick import matter_context as matter_context_module

    client_text = "DISTINCTIVE CLIENT PRIVILEGE 9c21"

    class _BrokenOutputShield(_AllowShield):
        def scan_output(self, _text):
            raise RuntimeError("scanner failed")

    agent, events = _agent(tmp_path, monkeypatch, shield=_BrokenOutputShield())
    _register(agent, lambda _args: client_text)
    bound = _matter_context()
    monkeypatch.setattr(
        matter_context_module,
        "resolve_goal_matter_context",
        lambda *_args, **_kwargs: bound,
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")

    with matter_context_scope(bound):
        result = await agent._run_tool("matter_read", {"query": client_text})

    assert "withheld" in result.lower()
    assert client_text not in result
    result_event = next(payload for kind, payload in events if kind == "tool_result")
    assert result_event == {
        "name": "matter_read",
        "status": "shield_scan_error_withheld",
        "output_bytes": len(client_text.encode("utf-8")),
        "output_sha256": hashlib.sha256(client_text.encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_tool_audit_file_contains_only_lengths_and_digests(tmp_path, monkeypatch):
    from maverick import matter_context as matter_context_module

    input_text = "DISTINCTIVE CLIENT INPUT 5f72"
    output_text = "DISTINCTIVE CLIENT RESULT c813"
    agent, events = _agent(tmp_path, monkeypatch, shield=_AllowShield())
    _register(agent, lambda _args: output_text)
    bound = _matter_context()
    monkeypatch.setattr(
        matter_context_module,
        "resolve_goal_matter_context",
        lambda *_args, **_kwargs: bound,
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")

    with matter_context_scope(bound):
        result = await agent._run_tool("matter_read", {"query": input_text})

    assert output_text in result
    audit_path = tmp_path / "today-audit.jsonl"
    audit_path.write_text(
        "\n".join(json.dumps({"kind": kind, **payload}, sort_keys=True) for kind, payload in events),
        encoding="utf-8",
    )
    persisted = audit_path.read_text(encoding="utf-8")
    assert input_text not in persisted
    assert output_text not in persisted
    assert "input_summary" not in persisted
    assert "output_summary" not in persisted
    assert "input_sha256" in persisted
    assert "output_sha256" in persisted
