"""A glob `[security.tool_risk]` override must not silently *lower* a built-in
risk classification: built-in risk is a floor that only an exact override can
drop. This closes the footgun where a broad wildcard (e.g. ``"s*" = "low"``)
declassifies dangerous built-ins like ``shell`` / ``wire_transfer``, defeating
max_risk ceilings and the governance risk gate.
"""
from __future__ import annotations

from maverick.safety.tool_risk import tool_risk


def test_glob_cannot_lower_builtin_high():
    # A wildcard that would declassify a built-in HIGH tool is clamped to the
    # built-in floor.
    assert tool_risk("shell", {"s*": "low"}) == "high"
    assert tool_risk("wire_transfer", {"wire_*": "low"}) == "high"
    assert tool_risk("release_payment", {"*": "low"}) == "high"


def test_exact_override_still_lowers_builtin():
    # An explicit, exact operator decision is honored (precise intent).
    assert tool_risk("shell", {"shell": "low"}) == "low"
    assert tool_risk("wire_transfer", {"wire_transfer": "medium"}) == "medium"


def test_glob_can_still_raise_and_relax_non_builtins():
    # A glob may RAISE a built-in...
    assert tool_risk("read_file", {"read_*": "high"}) == "high"
    # ...and may still classify a non-built-in tool through an explicit glob.
    assert tool_risk("external_other_write", {"external_*": "medium"}) == "medium"
    # An unknown tool is classified freely by a glob.
    assert tool_risk("acme_custom_tool", {"acme_*": "low"}) == "low"


def test_namespaced_action_inherits_its_namespaces_risk():
    # ``salesforce`` is a built-in HIGH connector, but the governed action that
    # actually posts to the system of record is named ``salesforce.write`` --
    # which was absent from the table and fell through to the ``medium``
    # default, slipping under a max_risk="medium" ceiling.
    assert tool_risk("salesforce.write", {}) == "high"
    assert tool_risk("servicenow.write", {}) == "high"
    assert tool_risk("salesforce", {}) == "high"


def test_namespace_inheritance_is_upward_only():
    # A suffix must never launder a namespace DOWN: inheriting ``low`` from
    # read_file would classify an unknown ``read_file.<something>`` below the
    # medium default it is entitled to.
    assert tool_risk("read_file.thing", {}) == "medium"
    assert tool_risk("read_file", {}) == "low"


def test_an_exact_override_still_beats_inherited_risk():
    # Operators keep a precise escape hatch for a namespaced read seat.
    assert tool_risk("salesforce.read", {"salesforce.read": "medium"}) == "medium"


def test_a_dotless_or_empty_namespace_changes_nothing():
    assert tool_risk("totally_unknown_tool", {}) == "medium"
    assert tool_risk(".leading_dot", {}) == "medium"
