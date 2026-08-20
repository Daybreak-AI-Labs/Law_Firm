"""Dashboard-owned runtime tool-deny overlay + ACL union."""
from __future__ import annotations

import stat
from pathlib import Path


def _point_overlay(monkeypatch, tmp_path: Path):
    from maverick import runtime_overrides
    monkeypatch.setattr(runtime_overrides, "OVERRIDES_PATH", tmp_path / "runtime-overrides.toml")
    return runtime_overrides


def test_denied_tools_empty_when_no_file(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    assert ro.denied_tools() == set()


def test_disable_then_denied(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    assert ro.denied_tools() == {"shell"}
    ro.disable_tool("browser")
    assert ro.denied_tools() == {"shell", "browser"}


def test_enable_removes(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    ro.disable_tool("browser")
    ro.enable_tool("shell")
    assert ro.denied_tools() == {"browser"}


def test_enable_unknown_is_noop(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    ro.enable_tool("never-added")  # no raise
    assert ro.denied_tools() == {"shell"}


def test_overlay_written_at_0600(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    path = tmp_path / "runtime-overrides.toml"
    assert path.exists()
    import os
    if os.name != "nt":  # NTFS reports 0o666 regardless of chmod
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_overlay_is_valid_toml(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    ro.disable_tool("computer")
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads((tmp_path / "runtime-overrides.toml").read_text())
    assert set(parsed["security"]["denied_tools"]) == {"shell", "computer"}


def test_corrupt_overlay_fails_closed_on_cold_start(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    (tmp_path / "runtime-overrides.toml").write_text("this is not { valid toml")
    with pytest.raises(ro.RuntimeOverridesSecurityError, match="refusing to continue"):
        ro.denied_tools()


def test_corrupt_overlay_retains_last_known_good_policy(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    ro.set_allowed_models(["anthropic:claude-sonnet-4-6"])

    (tmp_path / "runtime-overrides.toml").write_text("this is not { valid toml")

    assert ro.denied_tools() == {"shell"}
    assert ro.allowed_models() == {"anthropic:claude-sonnet-4-6"}


def test_deleted_overlay_retains_live_last_known_good_policy(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    ro.set_allowed_models(["anthropic:claude-sonnet-4-6"])

    (tmp_path / "runtime-overrides.toml").unlink()

    assert ro.denied_tools() == {"shell"}
    assert ro.allowed_models() == {"anthropic:claude-sonnet-4-6"}


def test_deleted_symlink_retains_live_last_known_good_policy(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    target = tmp_path / "operator-policy.toml"
    target.write_text('[security]\ndenied_tools = ["shell"]\n')
    link = tmp_path / "runtime-overrides-link.toml"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    monkeypatch.setattr(ro, "OVERRIDES_PATH", link)

    assert ro.denied_tools() == {"shell"}
    link.unlink()

    # The cache key is the lexical overlay path, not the symlink target. It
    # therefore remains stable after the link itself disappears.
    assert ro.denied_tools() == {"shell"}


def test_unreadable_overlay_fails_closed_on_cold_start(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    (tmp_path / "runtime-overrides.toml").write_text("[security]\n")

    def unreadable(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(ro, "_read_overlay", unreadable)
    with pytest.raises(ro.RuntimeOverridesSecurityError, match="refusing to continue"):
        ro.denied_tools()


def test_unstatable_overlay_fails_closed_instead_of_looking_missing(
    monkeypatch, tmp_path
):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    path = tmp_path / "runtime-overrides.toml"
    path.write_text("[security]\n")
    original_lstat = Path.lstat

    def unstatable(self):
        if self == path:
            raise PermissionError("stat denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", unstatable)
    with pytest.raises(ro.RuntimeOverridesSecurityError, match="refusing to continue"):
        ro.denied_tools()


def test_oversized_overlay_fails_closed(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    path = tmp_path / "runtime-overrides.toml"
    path.write_bytes(b"#" * (ro._MAX_OVERRIDE_BYTES + 1))

    with pytest.raises(ro.RuntimeOverridesSecurityError, match="refusing to continue"):
        ro.denied_tools()


def test_mutator_refuses_to_overwrite_corrupt_cold_start(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    path = tmp_path / "runtime-overrides.toml"
    damaged = "this is not { valid toml"
    path.write_text(damaged)

    with pytest.raises(ro.RuntimeOverridesSecurityError):
        ro.disable_tool("shell")
    assert path.read_text() == damaged


def test_mutator_refuses_to_overwrite_corrupt_warm_policy(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    ro.disable_tool("shell")
    path = tmp_path / "runtime-overrides.toml"
    damaged = "this is not { valid toml"
    path.write_text(damaged)

    with pytest.raises(ro.RuntimeOverridesSecurityError):
        ro.disable_tool("browser")
    assert path.read_text() == damaged
    # Read-only enforcement retains the previous restriction.
    assert ro.denied_tools() == {"shell"}


def test_structurally_invalid_security_policy_fails_closed(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    (tmp_path / "runtime-overrides.toml").write_text(
        '[security]\ndenied_tools = ["shell", "bad tool name"]\n'
    )
    with pytest.raises(ro.RuntimeOverridesSecurityError):
        ro.denied_tools()


def test_unknown_security_or_access_keys_fail_closed(monkeypatch, tmp_path):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    path = tmp_path / "runtime-overrides.toml"
    for body in (
        '[security]\ndenied_tool = ["shell"]\n',
        '[access]\nallow_models = ["anthropic:claude-sonnet-4-6"]\n',
        '[securty]\ndenied_tools = ["shell"]\n',
        '[mcp_servers.retired]\ncommand = "node"\n',
    ):
        path.write_text(body)
        with pytest.raises(ro.RuntimeOverridesSecurityError):
            ro.denied_tools()


def test_security_names_with_surrounding_whitespace_fail_closed(
    monkeypatch, tmp_path
):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    (tmp_path / "runtime-overrides.toml").write_text(
        '[security]\ndenied_tools = ["shell "]\n'
    )
    with pytest.raises(ro.RuntimeOverridesSecurityError):
        ro.denied_tools()


def test_corrupt_policy_propagates_through_every_runtime_consumer(
    monkeypatch, tmp_path
):
    """No integration layer may turn a policy read failure into defaults."""
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    (tmp_path / "runtime-overrides.toml").write_text("not valid TOML {")

    from maverick import budget, llm, styles
    from maverick.safety import tool_acl

    consumers = (
        tool_acl.resolve_lists,
        budget.budget_from_config,
        styles.active_style_name,
        lambda: llm.model_for_role("orchestrator"),
    )
    for consumer in consumers:
        with pytest.raises(
            ro.RuntimeOverridesSecurityError,
            match="refusing to continue",
        ):
            consumer()


def test_disable_rejects_invalid_tool_name(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    try:
        ro.disable_tool('bad"name')
        raise AssertionError("expected ValueError for invalid tool name")
    except ValueError:
        pass


# ---------- ACL union ----------

def test_acl_unions_overlay_into_deny(monkeypatch, tmp_path):
    """resolve_lists must add the overlay's denied tools to the deny-set."""
    from maverick import runtime_overrides
    from maverick.safety import tool_acl
    monkeypatch.setattr(runtime_overrides, "OVERRIDES_PATH", tmp_path / "ro.toml")
    # No config ACL; empty config path.
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    runtime_overrides.disable_tool("shell")
    allowed, denied = tool_acl.resolve_lists()
    assert "shell" in denied


def test_acl_overlay_filters_registry(monkeypatch, tmp_path):
    """End-to-end: a disabled tool is actually dropped from the registry."""
    from maverick import runtime_overrides
    monkeypatch.setattr(runtime_overrides, "OVERRIDES_PATH", tmp_path / "ro.toml")
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    runtime_overrides.disable_tool("shell")

    from unittest.mock import MagicMock

    from maverick.tools import base_registry
    reg = base_registry(world=MagicMock(), sandbox=MagicMock(__class__=type("Local", (), {})))
    names = {t.name for t in reg.all()}
    assert "shell" not in names, "overlay-denied tool still in registry"


def test_corrupt_policy_stops_registry_construction(monkeypatch, tmp_path):
    """The registry boundary must not turn a broken deny-list into all tools."""
    from unittest.mock import MagicMock

    import pytest
    from maverick import runtime_overrides
    from maverick.tools import base_registry

    monkeypatch.setattr(runtime_overrides, "OVERRIDES_PATH", tmp_path / "ro.toml")
    (tmp_path / "ro.toml").write_text("not valid TOML {", encoding="utf-8")

    with pytest.raises(runtime_overrides.RuntimeOverridesSecurityError):
        base_registry(
            world=MagicMock(),
            sandbox=MagicMock(__class__=type("Local", (), {})),
        )


def test_concurrent_disables_do_not_lose_tools(monkeypatch, tmp_path):
    """Every mutator re-reads the whole overlay and rewrites it; without
    serialization two concurrent writes lose one change. N concurrent
    disable_tool calls must all land on the deny-list (a dropped denied_tools
    update would silently re-enable a tool the operator just disabled)."""
    import threading

    ro = _point_overlay(monkeypatch, tmp_path)
    names = [f"tool{i:02d}" for i in range(24)]

    def disable(n: str):
        ro.disable_tool(n)

    threads = [threading.Thread(target=disable, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ro.denied_tools() == set(names)
    # No fixed-temp droppings from concurrent writers.
    assert list(tmp_path.glob("*.tmp")) == []


def test_disable_racing_set_budget_keeps_both(monkeypatch, tmp_path):
    """A disable_tool racing a set_budget (a DIFFERENT surface in the same
    file) must not have either change clobbered by a stale re-read."""
    import threading

    ro = _point_overlay(monkeypatch, tmp_path)
    barrier = threading.Barrier(2)

    def do_disable():
        barrier.wait()
        ro.disable_tool("shell")

    def do_budget():
        barrier.wait()
        ro.set_budget(12.5)

    ts = [threading.Thread(target=do_disable), threading.Thread(target=do_budget)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert ro.denied_tools() == {"shell"}
    assert ro.budget_override() == 12.5


def test_runtime_model_overlay_allows_one_exact_default(monkeypatch, tmp_path):
    ro = _point_overlay(monkeypatch, tmp_path)
    assert ro.set_default_model("anthropic:claude-sonnet-4-6") == (
        "anthropic:claude-sonnet-4-6"
    )
    assert ro.default_model_override() == "anthropic:claude-sonnet-4-6"


def test_runtime_model_overlay_rejects_bare_and_per_role_models(
    monkeypatch, tmp_path
):
    import pytest

    ro = _point_overlay(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="provider:model"):
        ro.set_default_model("claude-sonnet-4-6")

    path = tmp_path / "runtime-overrides.toml"
    path.write_text('[models]\ncoder = "anthropic:claude-sonnet-4-6"\n')
    with pytest.raises(ro.RuntimeOverridesSecurityError):
        ro.default_model_override()
