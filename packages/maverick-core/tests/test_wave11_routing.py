"""Anthropic cache TTL and thinking-budget switches."""
from __future__ import annotations


class TestCacheTTL:
    def test_default_is_1h(self, monkeypatch):
        from maverick.providers.anthropic_provider import _default_cache_ttl
        monkeypatch.delenv("MAVERICK_ANTHROPIC_CACHE_TTL", raising=False)
        assert _default_cache_ttl() == "1h"

    def test_explicit_env_override_wins(self, monkeypatch):
        from maverick.providers.anthropic_provider import _default_cache_ttl
        monkeypatch.setenv("MAVERICK_ANTHROPIC_CACHE_TTL", "30m")
        assert _default_cache_ttl() == "30m"


class TestThinkingOnOrchestrator:
    def test_orchestrator_role_gets_thinking_budget(self, tmp_path, monkeypatch):
        """Wave 11: orchestrator + revisor get thinking_budget=8000
        (Anthropic effort=medium). Coder/researcher do not."""
        monkeypatch.setenv(
            "MAVERICK_MODEL_OVERRIDE", "anthropic:claude-opus-4-8"
        )
        from maverick.agent import Agent
        from maverick.blackboard import Blackboard
        from maverick.budget import Budget
        from maverick.sandbox import LocalBackend
        from maverick.swarm import SwarmContext
        from maverick.world_model import WorldModel
        world = WorldModel(tmp_path / "w.db")
        gid = world.create_goal("t", "")
        ctx = SwarmContext(
            llm=None, world=world, budget=Budget(),
            blackboard=Blackboard(),
            sandbox=LocalBackend(workdir=tmp_path),
            goal_id=gid, max_depth=1,
        )
        orch = Agent(ctx=ctx, role="orchestrator", brief="x", depth=0)
        revisor = Agent(ctx=ctx, role="revisor", brief="x", depth=0)
        coder = Agent(ctx=ctx, role="coder", brief="x", depth=0)
        assert orch._thinking_budget() == 8000
        assert revisor._thinking_budget() == 8000
        assert coder._thinking_budget() is None


class TestThinkingBudgetWiredThrough:
    def test_anthropic_provider_passes_thinking_to_kwargs(self, monkeypatch):
        from maverick.providers.anthropic_provider import AnthropicClient
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        c = AnthropicClient()
        kwargs = c._build_request(
            system="s", messages=[{"role": "user", "content": "hi"}],
            tools=None, max_tokens=128, thinking_budget=8000,
            model="claude-sonnet-4-6",
        )
        assert kwargs.get("thinking") == {
            "type": "enabled", "budget_tokens": 8000,
        }
        # Max tokens auto-grew to thinking_budget + 1024.
        assert kwargs["max_tokens"] >= 8000 + 1024

    def test_no_thinking_when_unset(self, monkeypatch):
        from maverick.providers.anthropic_provider import AnthropicClient
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        c = AnthropicClient()
        kwargs = c._build_request(
            system="s", messages=[{"role": "user", "content": "hi"}],
            tools=None, max_tokens=128, thinking_budget=None,
            model="claude-sonnet-4-6",
        )
        assert "thinking" not in kwargs
