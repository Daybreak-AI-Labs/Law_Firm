"""Negative contracts for physically retired non-firm runtime surfaces."""

import importlib.util

_RETIRED_MODULES = (
    "maverick.governed_connectors",
    "maverick.governed_rest",
    "maverick.governed_tools",
    "maverick.tools.github_issues",
    "maverick.tools.http_fetch",
    "maverick.tools.memory",
)


def test_retired_modules_are_physically_absent():
    for name in _RETIRED_MODULES:
        assert importlib.util.find_spec(name) is None, name


def test_firm_catalog_has_no_generic_network_write_or_global_memory_names():
    from maverick.tools import FIRM_RUNTIME_MAX_TOOL_NAMES

    assert FIRM_RUNTIME_MAX_TOOL_NAMES.isdisjoint(
        {"github_issues", "http_fetch", "memory"}
    )


def test_fixed_legal_connectors_are_get_only():
    from maverick.tools.enterprise_connectors import enterprise_connectors

    tools = enterprise_connectors()
    assert tools
    for tool in tools:
        assert tool.input_schema["properties"]["op"]["enum"] == ["get"]
        assert tool.fn({"op": "post", "path": "/records"}).startswith("ERROR")


def test_kv_memory_does_not_cross_goal_or_matter(tmp_path):
    from maverick.tools.kv_memory import kv_memory
    from maverick.world_model import WorldModel

    world = WorldModel(tmp_path / "world.db")
    try:
        first = world.create_goal(
            "first", owner="user:alice", domain="legal", project_id=101
        )
        second = world.create_goal(
            "second", owner="user:alice", domain="legal", project_id=202
        )
        first_memory = kv_memory(world, first)
        second_memory = kv_memory(world, second)
        assert "set" in first_memory.fn(
            {"op": "set", "key": "strategy", "value": "FIRST_MATTER_ONLY"}
        )
        assert second_memory.fn({"op": "get", "key": "strategy"}).startswith(
            "(no fact stored"
        )
        assert second_memory.fn(
            {"op": "search", "query": "FIRST_MATTER_ONLY"}
        ).startswith("no matches")
    finally:
        world.close()


def test_kv_memory_without_an_active_goal_fails_closed():
    from maverick.tools.kv_memory import kv_memory

    out = kv_memory(None, None).fn({"op": "list"})
    assert out.startswith("ERROR: kv_memory requires an active goal")
