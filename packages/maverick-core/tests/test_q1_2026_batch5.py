"""Q1 2026 batch 5: plugin hooks via entry points, VS Code extension scaffold."""
from __future__ import annotations

from unittest.mock import patch

# ---------- hooks: load_from_entry_points ----------

def test_hooks_load_from_entry_points_empty(monkeypatch, tmp_path):
    """No matching entry points -> 0 hooks registered, no errors."""
    from maverick.hooks import clear, load_from_entry_points
    clear()
    monkeypatch.delenv("MAVERICK_PLUGINS_ALLOW", raising=False)
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "nonexistent.toml"))
    n = load_from_entry_points()
    assert n == 0  # must not raise


def test_hooks_load_from_entry_points_registers_specs(monkeypatch):
    """An entry-point factory returning HookSpec objects gets registered."""
    from maverick.hooks import (
        HookEvent,
        HookSpec,
        clear,
        installed,
        load_from_entry_points,
    )
    clear()
    monkeypatch.setenv("MAVERICK_PLUGINS_ALLOW", "*")

    def _register() -> list[HookSpec]:
        return [
            HookSpec(event=HookEvent.PRE_TOOL_USE, matcher="*", callable=lambda ctx: True),
            HookSpec(event=HookEvent.POST_TOOL_USE, matcher="shell", callable=lambda ctx: None),
        ]

    class _FakeEP:
        name = "fake_plugin"
        def load(self):
            return _register

    with patch("importlib.metadata.entry_points", return_value=[_FakeEP()]):
        n = load_from_entry_points()

    assert n == 2
    names = [(s.event, s.matcher) for s in installed()]
    assert (HookEvent.PRE_TOOL_USE, "*") in names
    assert (HookEvent.POST_TOOL_USE, "shell") in names


def test_hooks_load_from_entry_points_accepts_tuples(monkeypatch):
    """Factory returning (event, callable) tuples works too."""
    from maverick.hooks import HookEvent, clear, installed, load_from_entry_points
    clear()
    monkeypatch.setenv("MAVERICK_PLUGINS_ALLOW", "*")

    def _register():
        return [
            (HookEvent.SESSION_START, lambda ctx: True),
        ]

    class _FakeEP:
        name = "fake_plugin"
        def load(self):
            return _register

    with patch("importlib.metadata.entry_points", return_value=[_FakeEP()]):
        n = load_from_entry_points()

    assert n == 1
    assert any(s.event == HookEvent.SESSION_START for s in installed())


def test_hooks_load_from_entry_points_isolates_broken_plugin(monkeypatch):
    """A plugin that raises on load() doesn't disable other plugins."""
    from maverick.hooks import HookEvent, HookSpec, clear, installed, load_from_entry_points
    clear()
    monkeypatch.setenv("MAVERICK_PLUGINS_ALLOW", "*")

    class _BrokenEP:
        name = "broken"
        def load(self):
            raise RuntimeError("plugin import failed")

    class _GoodEP:
        name = "good"
        def load(self):
            return lambda: [HookSpec(event=HookEvent.PRE_TOOL_USE, matcher="*", callable=lambda ctx: True)]

    with patch("importlib.metadata.entry_points", return_value=[_BrokenEP(), _GoodEP()]):
        n = load_from_entry_points()

    # Broken plugin contributed 0; good plugin contributed 1.
    assert n == 1
    assert len(installed()) == 1


def test_hooks_load_from_entry_points_isolates_broken_factory_call(monkeypatch):
    """A factory that raises when called doesn't crash the loader."""
    from maverick.hooks import HookEvent, HookSpec, clear, installed, load_from_entry_points
    clear()
    monkeypatch.setenv("MAVERICK_PLUGINS_ALLOW", "*")

    def _broken_factory():
        raise ValueError("factory crashed")

    def _good_factory():
        return [HookSpec(event=HookEvent.POST_TOOL_USE, matcher="*", callable=lambda ctx: None)]

    class _BrokenEP:
        name = "broken"
        def load(self):
            return _broken_factory

    class _GoodEP:
        name = "good"
        def load(self):
            return _good_factory

    with patch("importlib.metadata.entry_points", return_value=[_BrokenEP(), _GoodEP()]):
        n = load_from_entry_points()

    assert n == 1
    assert len(installed()) == 1


def test_hooks_load_from_entry_points_ignores_invalid_items(monkeypatch):
    """Items that aren't HookSpec / (event, fn) get warned + dropped."""
    from maverick.hooks import HookEvent, HookSpec, clear, installed, load_from_entry_points
    clear()
    monkeypatch.setenv("MAVERICK_PLUGINS_ALLOW", "*")

    def _register():
        return [
            "garbage string",
            42,
            HookSpec(event=HookEvent.PRE_TOOL_USE, matcher="ok", callable=lambda ctx: True),
            (HookEvent.STOP,),  # malformed tuple
        ]

    class _FakeEP:
        name = "fake"
        def load(self):
            return _register

    with patch("importlib.metadata.entry_points", return_value=[_FakeEP()]):
        n = load_from_entry_points()

    # Only the valid HookSpec entry registers.
    assert n == 1
    assert len(installed()) == 1


# ---------- VS Code extension scaffold ----------

