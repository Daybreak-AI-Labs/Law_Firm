"""Q1 2026 batch 2: consent, tool ACL, and budget status."""
from __future__ import annotations

import pytest

# ---------- consent ----------

def test_consent_auto_approve_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "auto-approve")
    from maverick.safety.consent import require_consent
    d = require_consent("test-action", risk="high")
    assert d.granted is True
    assert d.source == "auto"


def test_consent_auto_deny_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "auto-deny")
    from maverick.safety.consent import require_consent
    d = require_consent("test-action")
    assert d.granted is False
    assert d.source == "auto"


def test_consent_ask_mode_non_tty_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # 'ask' mode in a non-tty context (pytest captures stdin) denies.
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "ask")
    from maverick.safety.consent import require_consent
    d = require_consent("test-action")
    assert d.granted is False
    assert d.source == "non-tty-deny"


def test_consent_default_is_opt_in_auto_approve(tmp_path, monkeypatch):
    # With no MAVERICK_CONSENT_MODE set, gating is OFF (auto-approve) so wiring
    # require_consent into tools doesn't change out-of-the-box behavior.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONSENT_MODE", raising=False)
    from maverick.safety.consent import require_consent
    d = require_consent("test-action")
    assert d.granted is True
    assert d.source == "auto"


def test_consent_raise_on_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "auto-deny")
    from maverick.safety.consent import ConsentDenied, require_consent
    with pytest.raises(ConsentDenied):
        require_consent("rm-rf", raise_on_deny=True)


def test_consent_ledger_grant_then_check(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONSENT_MODE", raising=False)
    from maverick.safety.consent import grant_persistent, list_grants, require_consent
    grant_persistent("force-push", scope="main")
    grants = list_grants()
    assert ("force-push", "main") in grants
    d = require_consent("force-push", scope="main")
    assert d.granted is True
    assert d.source == "ledger"


def test_consent_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick.safety.consent import grant_persistent, list_grants, revoke
    grant_persistent("rm-rf", scope="/tmp/test")
    assert ("rm-rf", "/tmp/test") in list_grants()
    assert revoke("rm-rf", scope="/tmp/test") is True
    assert ("rm-rf", "/tmp/test") not in list_grants()
    # Idempotent: revoking again returns False.
    assert revoke("rm-rf", scope="/tmp/test") is False


# ---------- tool_acl ----------

def test_tool_acl_no_lists_keeps_all():
    from maverick.safety.tool_acl import filter_tools
    out = filter_tools({"a", "b", "c"}, allowed=set(), denied=set())
    assert out == {"a", "b", "c"}


def test_tool_acl_allow_list_filters():
    from maverick.safety.tool_acl import filter_tools
    out = filter_tools({"a", "b", "c"}, allowed={"a", "b"})
    assert out == {"a", "b"}


def test_tool_acl_deny_list_drops():
    from maverick.safety.tool_acl import filter_tools
    out = filter_tools({"a", "b", "c"}, denied={"b"})
    assert out == {"a", "c"}


def test_tool_acl_allow_then_deny():
    """When both lists set, allow filters first, then deny drops further."""
    from maverick.safety.tool_acl import filter_tools
    out = filter_tools({"a", "b", "c", "d"}, allowed={"a", "b", "c"}, denied={"b"})
    assert out == {"a", "c"}


def test_tool_acl_apply_to_registry(tmp_path, monkeypatch):
    """apply_to_registry mutates the registry in place per config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Write a config with denied_tools.
    config_dir = tmp_path / ".maverick"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[security]\ndenied_tools = ["computer", "browser"]\n'
    )
    # Force reload of the cached default config path.
    import importlib

    import maverick.config as cfg_mod
    importlib.reload(cfg_mod)

    from maverick.tools import base_registry

    class _FakeSandbox:
        pass

    class _FakeWorld:
        pass

    reg = base_registry(
        world=_FakeWorld(), sandbox=_FakeSandbox(),
    )
    names = {t.name for t in reg.all()}
    assert "computer" not in names
    assert "browser" not in names  # retired from the firm catalog entirely
    # Context-free firm kernel tools survived.
    assert "ask_user" in names


def test_tool_acl_blocks_late_registered_tools(tmp_path, monkeypatch):
    """ACL should also apply to tools registered after apply_to_registry."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".maverick"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[security]\nallowed_tools = ["read_file"]\n'
    )
    import importlib

    import maverick.config as cfg_mod
    importlib.reload(cfg_mod)

    from maverick.tools import Tool, base_registry

    class _FakeSandbox:
        pass

    class _FakeWorld:
        pass

    reg = base_registry(world=_FakeWorld(), sandbox=_FakeSandbox())
    reg.register(Tool(name="late_tool", description="x", input_schema={}, fn=lambda _a: "ok"))
    names = {t.name for t in reg.all()}
    assert "read_file" in names
    assert "late_tool" not in names


# ---------- budget_status ----------

def test_budget_status_no_budget():
    from maverick.tools.budget_status import budget_status
    tool = budget_status(budget=None)
    out = tool.fn({})
    assert "no Budget bound" in out


def test_budget_status_with_budget():
    from maverick.budget import Budget
    from maverick.tools.budget_status import budget_status
    b = Budget(max_dollars=10.0)
    b.record_tokens(1000, 500, model="claude-opus-4-7")
    tool = budget_status(budget=b)
    out = tool.fn({})
    assert "dollars:" in out
    assert "input tokens" in out
    assert "output tokens" in out
    assert "tool calls:" in out
    # Cost recorded -> not zero.
    assert "$0.0000" not in out


def test_budget_status_warns_at_90pct():
    from maverick.budget import Budget
    from maverick.tools.budget_status import budget_status
    b = Budget(max_dollars=1.0)
    # Use atomic update to push spend high without tripping check().
    with b._lock:
        b.dollars = 0.95
    out = budget_status(budget=b).fn({})
    assert "WARNING" in out
