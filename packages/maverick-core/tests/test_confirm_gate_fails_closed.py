"""Any retained confirmation gate must reject string-shaped booleans."""
from __future__ import annotations

import pytest
from maverick.tools import as_bool


# ---- the shared gate's contract ----
@pytest.mark.parametrize("val,expected", [
    (True, True),
    (False, False),
    ("true", False),      # stringy true is NOT authorization (must be real bool)
    ("false", False),     # the bug: `not "false"` was False -> fired the op
    ("0", False),
    ("", False),
    (0, False), (1, False), (None, False),
])
def test_as_bool_only_true_authorizes(val, expected):
    assert as_bool(val) is expected


def test_no_tool_anywhere_uses_unsafe_confirm_gate():
    """Repo-wide guard: the fail-open pattern must not reappear in any tool."""
    import pathlib

    tools_dir = pathlib.Path(__file__).resolve().parents[1] / "maverick" / "tools"
    offenders = [
        f.name for f in tools_dir.glob("*.py")
        if 'if not args.get("confirm"):' in f.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"fail-open confirm gate reintroduced in: {offenders}"


# ---- end-to-end on a dependency-free tool: stringy confirm -> dry run ----
