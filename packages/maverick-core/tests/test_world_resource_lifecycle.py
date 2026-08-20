"""Firm tool-ceiling introspection is declarative and side-effect free."""
from __future__ import annotations

from maverick import tools as tools_mod


def test_base_tool_names_returns_the_declared_firm_ceiling() -> None:
    names = tools_mod.base_tool_names()
    assert names == set(tools_mod.FIRM_RUNTIME_MAX_TOOL_NAMES)
    assert names
    assert "shell" not in names
    assert "write_file" not in names
