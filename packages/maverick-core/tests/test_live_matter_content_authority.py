"""Client content reads stop immediately when live matter authority changes."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from maverick.cache import tool as tool_cache
from maverick.matter_context import (
    MatterContext,
    MatterContextError,
    matter_context_scope,
)
from maverick.tools import Tool, ToolRegistry
from maverick.tools.attachments import list_attachments_tool, read_attachment_tool
from maverick.tools.knowledge import knowledge_search_tool


def _context() -> MatterContext:
    return MatterContext(
        matter_id=41,
        client_id=9,
        principal="user:counsel",
        membership_role="attorney",
        domain="legal",
        jurisdiction="Federal-VA",
        purpose="goal-execution",
        source="live-content-authority-test",
        egress_mode="local_only",
    )


class _World:
    def __init__(self):
        self.list_calls = 0

    @staticmethod
    def get_goal(_goal_id):
        return SimpleNamespace(project_id=41, owner="user:counsel")

    def list_attachments(self, _goal_id):
        self.list_calls += 1
        return [
            SimpleNamespace(
                id=1,
                filename="privileged.txt",
                mime="text/plain",
                size_bytes=10,
            )
        ]


class _Knowledge:
    def __init__(self):
        self.calls = 0

    def search_formatted(self, _collections, _query, _k):
        self.calls += 1
        return "privileged result"


def test_attachment_tools_recheck_revocation_before_world_read(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    context = _context()
    revoked = False

    def resolve():
        if revoked:
            raise MatterContextError("membership revoked")
        return context

    world = _World()
    listing = list_attachments_tool(world, 7)
    reader = read_attachment_tool(world, 7)
    with matter_context_scope(context, authority_resolver=resolve):
        assert "privileged.txt" in listing.fn({})
        assert world.list_calls == 1
        revoked = True
        assert "authority unavailable" in listing.fn({})
        assert "access refused" in reader.fn({"attachment_id": 1})
        assert world.list_calls == 1


def test_knowledge_search_rechecks_revocation(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    context = _context()
    revoked = False

    def resolve():
        if revoked:
            raise MatterContextError("membership revoked")
        return context

    kb = _Knowledge()
    tool = knowledge_search_tool(kb, ["matter-records"], matter_id=41)
    with matter_context_scope(context, authority_resolver=resolve):
        assert asyncio.run(tool.fn({"query": "filing"})) == "privileged result"
        assert kb.calls == 1
        revoked = True
        assert "authority unavailable" in asyncio.run(tool.fn({"query": "filing"}))
        assert kb.calls == 1


def test_attachment_content_blocks_reject_mismatched_goal(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    context = _context()
    world = _World()
    world.get_goal = lambda _goal_id: SimpleNamespace(
        project_id=42,
        owner="user:counsel",
    )
    from maverick.attachments import content_blocks_for_goal

    with matter_context_scope(context, authority_resolver=lambda: context):
        assert content_blocks_for_goal(world, 7, model="anthropic:test") == []
    assert world.list_calls == 0


def test_registry_rechecks_live_authority_for_every_matter_tool(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    import maverick.domain as domain

    profile = SimpleNamespace(allow_tools=["read_file"], deny_tools=[])
    monkeypatch.setattr(
        domain,
        "enabled_domains",
        lambda *args, **kwargs: {"legal": profile},
    )
    monkeypatch.setattr(domain, "suite_for", lambda _name: "legal")
    context = _context()
    revoked = False

    def resolve():
        if revoked:
            raise MatterContextError("membership revoked")
        return context

    calls = 0

    def read_file(_args):
        nonlocal calls
        calls += 1
        return "client file"

    with matter_context_scope(context, authority_resolver=resolve):
        registry = ToolRegistry(principal=context.principal)
        registry.register(
            Tool(
                name="read_file",
                description="test read",
                input_schema={"type": "object", "properties": {}},
                fn=read_file,
            )
        )
        assert asyncio.run(registry.run("read_file", {})) == "client file"
        assert calls == 1
        revoked = True
        denied = asyncio.run(registry.run("read_file", {}))
        assert "authority" in denied and "revoked" in denied
        assert calls == 1


def test_tool_cache_isolated_between_same_attorney_matters(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_TOOL_CACHE", "1")
    import maverick.domain as domain
    from maverick.matter_context import current_matter_context

    profile = SimpleNamespace(allow_tools=["read_file"], deny_tools=[])
    monkeypatch.setattr(
        domain,
        "enabled_domains",
        lambda *args, **kwargs: {"legal": profile},
    )
    monkeypatch.setattr(domain, "suite_for", lambda _name: "legal")
    tool_cache.reset()
    calls = 0

    def read_file(_args):
        nonlocal calls
        calls += 1
        return f"matter:{current_matter_context().matter_id}"

    first = _context()
    second = replace(first, matter_id=42, client_id=10)
    results: list[str] = []
    for context in (first, second):
        with matter_context_scope(context, authority_resolver=lambda c=context: c):
            registry = ToolRegistry(principal=context.principal)
            registry.register(
                Tool(
                    name="read_file",
                    description="test read",
                    input_schema={"type": "object", "properties": {}},
                    fn=read_file,
                    parallel_safe=True,
                )
            )
            results.append(asyncio.run(registry.run("read_file", {"path": "memo"})))
    assert results == ["matter:41", "matter:42"]
    assert calls == 2
    tool_cache.reset()
