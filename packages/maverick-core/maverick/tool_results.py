"""One typed interpretation of the string-based tool result protocol.

Tool functions historically return strings, so execution surfaces had grown
independent ``startswith('ERROR')`` checks.  That made a governed refusal or an
indeterminate external effect look successful in agents and workflows.  Keep
the wire-compatible string contract while centralizing its semantic state.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ToolResultState(str, Enum):
    """Execution meaning of a tool result."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    PREVIEW = "preview"
    INDETERMINATE = "indeterminate"


def unframe_tool_result(value: Any) -> str:
    """Return raw content from the optional untrusted-output frame."""
    text = value if isinstance(value, str) else str(value or "")
    if text.startswith("<tool_output "):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
    return text.lstrip()


def classify_tool_result(value: Any) -> ToolResultState:
    """Classify reserved failure/refusal/ambiguity prefixes consistently."""
    text = unframe_tool_result(value)
    if text.startswith("INDETERMINATE"):
        return ToolResultState.INDETERMINATE
    if text.startswith("DRY RUN"):
        return ToolResultState.PREVIEW
    if text.startswith(("REFUSED", "BLOCKED by Shield", "⚠")):
        return ToolResultState.REFUSED
    if text.startswith("ERROR"):
        return ToolResultState.FAILED
    return ToolResultState.SUCCEEDED


def tool_result_failed(value: Any) -> bool:
    """Whether the caller must not treat this result as successful output.

    A dry-run preview is intentionally included: it describes an effect that
    did not happen and must never advance a live workflow or become cached as a
    successful connector response.
    """
    return classify_tool_result(value) is not ToolResultState.SUCCEEDED


__all__ = [
    "ToolResultState",
    "classify_tool_result",
    "tool_result_failed",
    "unframe_tool_result",
]
