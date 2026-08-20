"""The five retained legal connectors reject hostile args without raising."""
from __future__ import annotations

from maverick.tools.enterprise_connectors import enterprise_connectors

_HOSTILE_ARGS = (
    {},
    {"op": 123},
    {"op": 3.14},
    {"op": ["get"]},
    {"op": {"x": 1}},
    {"op": None},
    {"op": True},
    {"op": "get", "path": 123},
    {"op": "get", "path": ["/x"]},
    {"op": "get", "path": {}},
    {"op": "get", "path": None},
    {"op": "FROBNICATE", "path": "/x"},
    {"op": "get", "path": "/x", "params": "not-a-dict"},
    {"op": "post", "path": "/x", "body": "not-a-dict", "confirm": "yes"},
    {"op": "query", "query": 42},           # graphql shape
    {"query": ["not", "a", "string"]},
)


def test_every_connector_returns_a_string_on_hostile_args():
    tools = enterprise_connectors()
    assert {tool.name for tool in tools} == {
        "carta_read",
        "clio_read",
        "contractbook_read",
        "docusign_read",
        "ironclad_read",
    }
    failures = []
    for t in tools:
        for args in _HOSTILE_ARGS:
            try:
                out = t.fn(args)
            except Exception as e:  # noqa: BLE001
                failures.append((t.name, args, f"RAISED {type(e).__name__}: {e}"))
                continue
            if not isinstance(out, str):
                failures.append((t.name, args, f"non-str {type(out).__name__}"))
    assert not failures, f"{len(failures)} hostile-arg failures: {failures[:8]}"


def test_non_string_op_yields_error_not_crash():
    # Regression for the specific bug: a non-string op must produce an ERROR
    # string, never AttributeError.
    by_name = {t.name: t for t in enterprise_connectors()}
    for name in ("carta_read", "clio_read", "docusign_read", "ironclad_read"):
        out = by_name[name].fn({"op": 123})
        assert isinstance(out, str) and out.startswith("ERROR"), (name, out)
