"""Model-facing tool outputs must serialize JSON compactly.

Connector tools return JSON that re-enters the context window as a
tool_result, usually truncated to a char budget (``[:3000]``-style).
``indent=2`` there wastes tokens on whitespace AND spends the truncation
budget on indentation instead of data — a 3000-char pretty dump can carry
~40% less payload than the compact form. These tests pin the compact
behavior of the shared helpers and ratchet ``indent=`` out of the model-
facing serialization sites under ``maverick/tools/``.
"""
from __future__ import annotations

import re
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "maverick" / "tools"

# Files whose json.dumps(indent=...) never reaches the model: oauth_helper
# pretty-prints a credentials file on disk for humans to inspect.
_INDENT_OK = {"oauth_helper.py"}

_INDENT_RE = re.compile(r"\bjson\.dumps\([^)]*\bindent\s*=", re.DOTALL)


def test_no_pretty_printed_json_in_tool_modules():
    offenders = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name in _INDENT_OK:
            continue
        if _INDENT_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, (
        "json.dumps(indent=...) in model-facing tool modules wastes context "
        "tokens and truncation budget; use maverick.fastjson.dumps_compact "
        f"instead (or allowlist a genuinely human-facing file): {offenders}"
    )
