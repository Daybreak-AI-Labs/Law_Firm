"""Deferred tool loading is wired on by default for the connector long tail.

Platform-test finding: every model turn carried 601 tool schemas at consumer
defaults (capabilities off, tools denied) -- the deferred-loading mechanism
(``ToolRegistry.enable_deferred`` + ``find_tools``) existed and was tested,
but nothing in the production path called it. ``base_registry`` now marks the
``enterprise_connectors()`` long tail deferred-eligible and
``Agent._build_tools`` enables deferral (``[capabilities] deferred_tools``,
default on; ``MAVERICK_DEFERRED_TOOLS`` overrides), so the model sees the
core kernel tools + ``find_tools`` while connectors stay discoverable on
demand and executable regardless of exposure.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from maverick.blackboard import Blackboard
from maverick.budget import Budget
from maverick.sandbox import LocalBackend
from maverick.swarm import SwarmContext
from maverick.world_model import WorldModel


@pytest.fixture
def ctx(tmp_path: Path, fake_llm):
    world = WorldModel(tmp_path / "world.db")
    goal_id = world.create_goal("test goal", "")
    return SwarmContext(
        llm=fake_llm,
        world=world,
        budget=Budget(max_dollars=1.0),
        blackboard=Blackboard(),
        sandbox=LocalBackend(workdir=tmp_path),
        goal_id=goal_id,
        max_depth=2,
        use_skills=False,
    )


def _agent(ctx):
    from maverick.agent import Agent
    return Agent(ctx, role="orchestrator", brief="b")


def test_connector_long_tail_marked_deferrable(ctx):
    agent = _agent(ctx)
    deferrable = agent.tools.deferrable_names
    # Floor, not a headcount: the connector roster is scoped to a law firm,
    # so this guards against the deferrable set collapsing to empty (which
    # would silently put every schema back on the wire), not against a
    # particular catalogue size.
    assert len(deferrable) > 100, len(deferrable)
    assert "read_file" not in deferrable
    assert "spawn_swarm" not in deferrable


def test_default_offer_is_lean_but_execution_unrestricted(ctx, monkeypatch):
    monkeypatch.delenv("MAVERICK_DEFERRED_TOOLS", raising=False)
    agent = _agent(ctx)
    offered = {t["name"] for t in agent.tools.to_anthropic()}
    registered = {t.name for t in agent.tools.all()}
    # The long tail is registered (executable) but not offered. Ratchet: 233
    # at introduction (was 601 on the wire) -- lower it as more of the
    # catalog earns deferral, never raise it.
    assert len(offered) < 250, len(offered)
    # Floor guarding against an empty registry, not a catalogue size.
    assert len(registered) > 300, len(registered)
    assert "find_tools" in offered
    # Kernel tools the swarm depends on stay visible.
    for core in ("read_file", "write_file", "list_dir", "spawn_swarm",
                 "spawn_subagent", "ask_user"):
        assert core in offered, core
    # A known long-tail connector: hidden from the catalog, still registered.
    assert "clio" not in offered
    assert "clio" in registered


def test_find_tools_activates_hidden_connector(ctx, monkeypatch):
    monkeypatch.delenv("MAVERICK_DEFERRED_TOOLS", raising=False)
    agent = _agent(ctx)
    ft = agent.tools.get("find_tools")
    out = ft.fn({"query": "clio practice management"})
    assert "clio" in out
    assert "clio" in {t["name"] for t in agent.tools.to_anthropic()}


def test_knob_off_restores_identity_catalog(ctx, monkeypatch):
    monkeypatch.setenv("MAVERICK_DEFERRED_TOOLS", "0")
    agent = _agent(ctx)
    offered = {t["name"] for t in agent.tools.to_anthropic()}
    assert "clio" in offered
    assert "find_tools" not in offered
