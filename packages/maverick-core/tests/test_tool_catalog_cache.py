"""ToolRegistry.to_anthropic() is memoized but stays correct across mutations."""
from __future__ import annotations

from maverick.tools import Tool, ToolRegistry


def _tool(name: str) -> Tool:
    return Tool(name=name, description=f"d {name}", input_schema={"type": "object"}, fn=lambda a: "")


def test_cache_returns_stable_payload_and_memoizes():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))
    first = reg.to_anthropic()
    second = reg.to_anthropic()
    assert first is second  # memoized (same object)
    assert {t["name"] for t in first} == {"a", "b"}


def test_register_invalidates_cache():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    before = reg.to_anthropic()
    reg.register(_tool("c"))
    after = reg.to_anthropic()
    assert after is not before
    assert {t["name"] for t in after} == {"a", "c"}


def test_acl_and_registration_invalidate_cache():
    reg = ToolRegistry()
    reg.register(_tool("x"))
    reg.register(_tool("y"))
    before = reg.to_anthropic()
    reg.set_acl(denied={"y"})
    reg.register(_tool("z"))  # ACL applies on registration
    names = {t["name"] for t in reg.to_anthropic()}
    assert reg.to_anthropic() is not before
    assert "z" in names
