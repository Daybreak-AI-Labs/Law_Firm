from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick.matter_context import (
    GOAL_EXECUTION_PURPOSE,
    MatterContext,
    matter_context_scope,
)
from maverick.tools import (
    FIRM_RUNTIME_MAX_TOOL_NAMES,
    LEGAL_PROFILE_TOOL_NAMES,
    Tool,
    ToolRegistry,
    base_registry,
)
from maverick.tools.enterprise_connectors import (
    READ_CONNECTOR_NAMES,
    auth_headers_for,
    connector_catalog,
    enterprise_connectors,
)

_LEGAL_CONNECTORS = {
    "carta_read",
    "clio_read",
    "contractbook_read",
    "docusign_read",
    "ironclad_read",
}
_REMOVED_EXPANSION_NAMES = {
    "find_tools",
    "learn_capability",
    "mcp_filesystem__read",
    "plugin_weather",
    "generated_scraper",
    "grpc_crm",
    "shell",
    "write_file",
    "apply_patch",
    "str_edit",
    "str_replace_editor",
}


class _World:
    pass


def _context(matter_id: int = 7, *, principal: str = "user:alice") -> MatterContext:
    return MatterContext(
        matter_id=matter_id,
        client_id=1,
        principal=principal,
        membership_role="attorney",
        domain="legal_contract_review",
        jurisdiction="Tennessee",
        purpose=GOAL_EXECUTION_PURPOSE,
        source="test",
        egress_mode="local_only",
    )


def _install_profile(monkeypatch, allowed: set[str]) -> None:
    import maverick.domain as domain

    profile = SimpleNamespace(allow_tools=sorted(allowed), deny_tools=[])
    monkeypatch.setattr(
        domain,
        "enabled_domains",
        lambda *args, **kwargs: {"legal_contract_review": profile},
    )
    monkeypatch.setattr(domain, "suite_for", lambda name: "legal")


def _tool(name: str, fn=lambda _args: "ran") -> Tool:
    return Tool(name=name, description=name, input_schema={"type": "object"}, fn=fn)


def test_firm_name_ceiling_is_exact_and_has_no_execution_or_loader_tools():
    assert {
        "carta_read",
        "clause_library_read",
        "clio_read",
        "contract_clause_read",
        "contract_read",
        "contract_repository_read",
        "contractbook_read",
        "dependency_manifest_read",
        "docusign_read",
        "incident_record_read",
        "invoice_read",
        "ironclad_read",
        "knowledge_search",
        "list_attachments",
        "read_attachment",
        "read_file",
        "spreadsheet",
        "sql_query",
        "web_search",
    } == LEGAL_PROFILE_TOOL_NAMES
    assert len(FIRM_RUNTIME_MAX_TOOL_NAMES) == 30
    assert not (_REMOVED_EXPANSION_NAMES & FIRM_RUNTIME_MAX_TOOL_NAMES)


def test_secure_registry_without_matter_is_context_free_kernel_only(
    tmp_path, monkeypatch,
):
    from maverick.sandbox.local import LocalBackend

    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    _install_profile(monkeypatch, set(LEGAL_PROFILE_TOOL_NAMES))
    registry = base_registry(
        _World(),
        LocalBackend(workdir=tmp_path),
        goal_id=3,
        user_id="alice",
        enable_web_search=True,
    )
    assert {tool.name for tool in registry.all()} == {
        "budget_status",
        "citation_verifier",
    }
    assert "ask_user" not in {tool.name for tool in registry.all()}


def test_secure_registry_is_exact_profile_intersection(tmp_path, monkeypatch):
    from maverick.sandbox.local import LocalBackend

    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    allowed = {
        "ironclad_read",
        "list_attachments",
        "read_attachment",
        "read_file",
        "spreadsheet",
        "sql_query",
        "web_search",
        # These must remain impossible even if a malformed profile declares them.
        "shell",
        "write_file",
        "apply_patch",
        "str_edit",
    }
    _install_profile(monkeypatch, allowed)
    with matter_context_scope(_context()):
        registry = base_registry(
            _World(),
            LocalBackend(workdir=tmp_path),
            goal_id=3,
            user_id="alice",
            enable_web_search=True,
        )
    assert {tool.name for tool in registry.all()} == {
        "ask_user",
        "budget_status",
        "citation_verifier",
        "ironclad_read",
        "kv_memory",
        "list_attachments",
        "read_attachment",
        "read_file",
        "spreadsheet",
        "sql_query",
        "web_search",
    }


def test_secure_registry_rejects_shadow_and_expansion_names(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    _install_profile(monkeypatch, set(LEGAL_PROFILE_TOOL_NAMES))
    with matter_context_scope(_context()):
        registry = ToolRegistry(principal="user:alice")
        original = _tool("read_file", lambda _args: "original")
        registry.register(original)
        for name in _REMOVED_EXPANSION_NAMES:
            registry.register(_tool(name))
        registry.register(_tool("read_file", lambda _args: "shadow"))
    assert registry.all() == [original]


@pytest.mark.asyncio
async def test_scoped_tool_cannot_run_under_another_matter(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    _install_profile(monkeypatch, {"read_file"})
    calls: list[str] = []
    matter_seven = _context(7)
    with matter_context_scope(matter_seven, authority_resolver=lambda: matter_seven):
        registry = ToolRegistry(principal="user:alice")
        registry.register(_tool("read_file", lambda _args: calls.append("ran") or "ran"))
        assert await registry.run("read_file", {}) == "ran"
    matter_eight = _context(8)
    with matter_context_scope(matter_eight, authority_resolver=lambda: matter_eight):
        denied = await registry.run("read_file", {})
    assert "outside the bound matter/profile authority" in denied
    assert calls == ["ran"]


def test_connector_catalog_is_exactly_five_get_only_tools():
    tools = enterprise_connectors()
    assert set(READ_CONNECTOR_NAMES) == _LEGAL_CONNECTORS
    assert {tool.name for tool in tools} == _LEGAL_CONNECTORS
    assert {entry["name"] for entry in connector_catalog()} == _LEGAL_CONNECTORS
    for tool in tools:
        assert tool.input_schema["properties"]["op"]["enum"] == ["get"]
        assert tool.fn({"op": "post", "path": "/records", "confirm": True}).startswith(
            "ERROR"
        )


def test_retained_connector_auth_header_has_no_ambient_auxiliary_secret():
    assert auth_headers_for("clio_read", "secret") == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer secret",
    }
