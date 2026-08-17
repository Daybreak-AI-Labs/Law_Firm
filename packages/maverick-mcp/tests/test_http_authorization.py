"""End-to-end authorization tests for network-reachable MCP tools.

These tests intentionally cross the HTTP bearer boundary.  A unit test of
``Capability.permits`` would not catch the original defect: authentication
resolved the right caller, then HTTP dispatch discarded its authority.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from maverick_mcp.http_transport import build_app  # noqa: E402
from maverick_mcp.server import MCPServer  # noqa: E402
from maverick_mcp.tasks import TaskStore  # noqa: E402


def _client(
    monkeypatch,
    *,
    token: str,
    shared: bool = False,
    tasks: bool = False,
) -> tuple[TestClient, MCPServer, dict[str, str]]:
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "0")
    if shared:
        monkeypatch.setenv("MAVERICK_MCP_TOKEN", token)
    else:
        monkeypatch.delenv("MAVERICK_MCP_TOKEN", raising=False)
    if tasks:
        monkeypatch.setenv("MAVERICK_MCP_HTTP_TASKS", "1")
    else:
        monkeypatch.delenv("MAVERICK_MCP_HTTP_TASKS", raising=False)
    server = MCPServer()
    server._shield = None
    client = TestClient(build_app(server))
    return client, server, {"Authorization": f"Bearer {token}"}


def _rpc(client: TestClient, headers: dict[str, str], method: str, params=None):
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        },
    ).json()


def _tool_names(body: dict) -> set[str]:
    return {tool["name"] for tool in body["result"]["tools"]}


def test_disengaged_shared_bearer_remains_explicitly_unscoped(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="legacy-shared",
        shared=True,
    )

    listed = _tool_names(_rpc(client, headers, "tools/list"))
    assert "maverick_status" in listed
    assert "maverick_start" in listed
    assert "maverick_fact_set" in listed
    invoked = []
    server._dispatch_tool = lambda name, _arguments: invoked.append(name) or "ok"
    server._structured_result = lambda _name: None
    allowed = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert allowed["result"]["isError"] is False
    assert invoked == ["maverick_status"]


def test_disengaged_shared_bearer_honors_mcp_principal_revocation(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="legacy-shared",
        shared=True,
        tasks=True,
    )
    from maverick import revocation

    monkeypatch.setattr(
        revocation,
        "revoked_principal",
        lambda principals: "mcp" if "mcp" in principals else None,
    )
    invoked = []
    server._dispatch_tool = lambda name, _arguments: invoked.append(name) or "ok"

    assert _rpc(client, headers, "tools/list")["result"]["tools"] == []
    denied = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert denied["error"] == {
        "code": -32602,
        "message": "unknown tool: 'maverick_status'",
    }
    denied_task = _rpc(
        client,
        headers,
        "tools/call",
        {
            "name": "maverick_start",
            "arguments": {"title": "must-not-run"},
            "task": {"ttl": 60_000},
        },
    )
    assert denied_task["error"] == {
        "code": -32602,
        "message": "unknown tool: 'maverick_start'",
    }
    assert invoked == []
    assert server._tasks is None


def test_transport_agent_id_cannot_alias_a_user_principal():
    assert MCPServer._connector_principal(
        "user:alice",
        "user:alice",
        SimpleNamespace(principal="agent:user:alice"),
    ) == "agent:user:alice"
    # A trusted in-process caller can still deliberately carry a user grant;
    # only Agent Trust transport ids are forced into the agent namespace.
    assert MCPServer._connector_principal(
        None,
        None,
        SimpleNamespace(principal="user:alice"),
    ) == "user:alice"


def test_queued_shared_task_rechecks_mcp_principal_revocation(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="legacy-shared",
        shared=True,
        tasks=True,
    )
    from maverick import revocation

    revoked = False
    monkeypatch.setattr(
        revocation,
        "revoked_principal",
        lambda principals: "mcp" if revoked and "mcp" in principals else None,
    )
    started = threading.Event()
    release = threading.Event()
    executed = []

    def _tool_start(_worker, arguments, *, capability=None):
        title = arguments["title"]
        executed.append(title)
        if title == "blocker":
            started.set()
            assert release.wait(timeout=5)
        return title

    monkeypatch.setattr(MCPServer, "_tool_start", _tool_start)
    server._tasks = TaskStore(
        lambda name, arguments: server._task_runner(name, arguments),
        context_runner=server._task_runner,
        max_workers=1,
        result_block_ms=5_000,
    )
    try:
        blocker = _rpc(
            client,
            headers,
            "tools/call",
            {
                "name": "maverick_start",
                "arguments": {"title": "blocker"},
                "task": {"ttl": 60_000},
            },
        )
        assert "task" in blocker["result"]
        assert started.wait(timeout=2)

        victim = _rpc(
            client,
            headers,
            "tools/call",
            {
                "name": "maverick_start",
                "arguments": {"title": "must-not-run"},
                "task": {"ttl": 60_000},
            },
        )
        victim_id = victim["result"]["task"]["taskId"]

        revoked = True
        release.set()
        victim_task = server._task_store()._tasks[victim_id]
        assert victim_task.done.wait(timeout=2)
        assert victim_task.status == "failed"
        assert executed == ["blocker"]
    finally:
        release.set()
        server._task_store().shutdown()


def test_unscoped_shared_task_and_resources_honor_mcp_principal_revocation(
    monkeypatch,
):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
        tasks=True,
    )
    server._task_runner = lambda _name, _arguments, context=None: {
        "isError": False,
        "content": [{"type": "text", "text": "shared-result"}],
    }
    created = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "maverick_start",
                "arguments": {"title": "shared"},
                "task": {"ttl": 60_000},
            },
        },
    )
    task_id = created.json()["result"]["task"]["taskId"]
    task_headers = {
        **headers,
        "Mcp-Session-Id": created.headers["Mcp-Session-Id"],
    }
    from maverick import revocation

    monkeypatch.setattr(
        revocation,
        "revoked_principal",
        lambda principals: "mcp" if "mcp" in principals else None,
    )
    assert _rpc(client, task_headers, "tasks/list")["result"]["tasks"] == []
    hidden = _rpc(client, task_headers, "tasks/get", {"taskId": task_id})
    assert hidden["error"] == {"code": -32602, "message": "task not found"}
    assert _rpc(client, headers, "resources/list")["result"]["resources"] == []
    denied_read = _rpc(
        client,
        headers,
        "resources/read",
        {"uri": "maverick://goals"},
    )
    assert denied_read["error"]["code"] == -32602
    server._task_store().shutdown()


def test_enterprise_shield_floor_ignores_disable_override(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    from maverick import enterprise

    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "0")
    monkeypatch.setattr(enterprise, "enterprise_enabled", lambda: True)
    server._shield = None

    def _must_not_dispatch(_name, _arguments):
        raise AssertionError("enterprise MCP dispatched without Shield")

    server._dispatch_tool = _must_not_dispatch
    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert body["result"]["isError"] is True
    assert "required but unavailable" in body["result"]["content"][0]["text"]


def test_required_shield_scrubs_and_scans_resource_output(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "1")
    from maverick import world_model

    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret

    class _World:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_goals(self, *, owner=None):
            assert owner == "agent:mcp"
            return [
                SimpleNamespace(
                    id=7,
                    status="active",
                    title=f"sensitive {raw_secret}",
                )
            ]

    monkeypatch.setattr(world_model, "WorldModel", _World)
    scanned = []

    class _AllowingShield:
        def scan_output(self, text):
            scanned.append(text)
            return SimpleNamespace(allowed=True, reasons=[])

    server._shield = _AllowingShield()
    allowed = _rpc(
        client,
        headers,
        "resources/read",
        {"uri": "maverick://goals"},
    )
    text = allowed["result"]["contents"][0]["text"]
    assert raw_secret not in text
    assert "[REDACTED:openai_key]" in text
    assert scanned == [text]

    class _RejectingShield:
        def scan_output(self, _text):
            return SimpleNamespace(
                allowed=False,
                reasons=[f"sensitive span: {raw_secret}"],
            )

    server._shield = _RejectingShield()
    denied = _rpc(
        client,
        headers,
        "resources/read",
        {"uri": "maverick://goals"},
    )
    assert denied["error"] == {
        "code": -32603,
        "message": "resource output blocked by safety policy",
    }
    assert raw_secret not in str(denied)


def test_tool_exception_is_scrubbed_bounded_and_shield_scanned(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "1")
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
    scanned = []

    class _Shield:
        def scan_input(self, _text):
            return SimpleNamespace(allowed=True, reasons=[])

        def scan_output(self, text):
            scanned.append(text)
            return SimpleNamespace(allowed=True, reasons=[])

    server._shield = _Shield()

    def _crash(_name, _arguments):
        raise RuntimeError(raw_secret + ("x" * 2_000))

    server._dispatch_tool = _crash
    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    detail = body["result"]["content"][0]["text"]
    assert body["result"]["isError"] is True
    assert raw_secret not in detail
    assert "[REDACTED:openai_key]" in detail
    assert len(detail) <= 500
    assert scanned == [detail]

    class _RejectingShield(_Shield):
        def scan_output(self, _text):
            return SimpleNamespace(
                allowed=False,
                reasons=[f"sensitive span: {raw_secret}"],
            )

    server._shield = _RejectingShield()
    denied = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    denied_text = denied["result"]["content"][0]["text"]
    assert denied_text == "Output blocked by safety policy"
    assert raw_secret not in denied_text


def test_required_shield_scan_exception_blocks_remote_output(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "1")

    class _BrokenShield:
        def scan_input(self, _text):
            return SimpleNamespace(allowed=True, reasons=[])

        def scan_output(self, _text):
            raise RuntimeError("scanner unavailable")

    server._shield = _BrokenShield()
    server._dispatch_tool = lambda _name, _arguments: "must-not-cross-boundary"
    server._structured_result = lambda _name: None

    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"]
    assert "safety shield scan error" in text
    assert "must-not-cross-boundary" not in text


def test_required_shield_input_exception_prevents_goal_start(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "1")

    class _BrokenInputShield:
        def scan_input(self, _text):
            raise RuntimeError("scanner unavailable")

        def scan_output(self, _text):
            return SimpleNamespace(allowed=True, reasons=[])

    server._shield = _BrokenInputShield()

    from maverick import world_model

    def _must_not_create_world(*_args, **_kwargs):
        raise AssertionError("goal creation reached after failed required scan")

    monkeypatch.setattr(world_model, "WorldModel", _must_not_create_world)
    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_start", "arguments": {"title": "unsafe"}},
    )
    assert body["result"]["isError"] is True
    assert "safety shield scan error" in body["result"]["content"][0]["text"]


def test_required_missing_shield_blocks_before_tool_dispatch(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )
    monkeypatch.setenv("MAVERICK_REQUIRE_SHIELD", "1")
    server._shield = None

    def _must_not_dispatch(_name, _arguments):
        raise AssertionError("tool dispatched without required Shield")

    server._dispatch_tool = _must_not_dispatch
    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert body["result"]["isError"] is True
    assert "required but unavailable" in body["result"]["content"][0]["text"]


def test_local_dev_shield_exception_remains_fail_open(monkeypatch):
    client, server, headers = _client(
        monkeypatch,
        token="shared-token",
        shared=True,
    )

    class _BrokenShield:
        def scan_output(self, _text):
            raise RuntimeError("scanner unavailable")

    server._shield = _BrokenShield()
    server._dispatch_tool = lambda _name, _arguments: "local-result"
    server._structured_result = lambda _name: None

    body = _rpc(
        client,
        headers,
        "tools/call",
        {"name": "maverick_status", "arguments": {}},
    )
    assert body["result"]["isError"] is False
    assert body["result"]["content"][0]["text"] == "local-result"


