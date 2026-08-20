"""Minimal structural contract shared by the local and Docker backends."""
from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable

SDK_VERSION = 2
_OPTIONAL = ("put_file", "close", "stop", "exec_authenticated_tests")


@runtime_checkable
class SandboxV2(Protocol):
    """The minimal contract every retained execution backend satisfies."""

    workdir: Any

    def exec(self, cmd: str, timeout: float | None = None) -> Any: ...


def capabilities(backend: Any) -> set[str]:
    """Return optional capabilities exposed by a backend instance."""
    caps = {"exec"}
    for name in _OPTIONAL:
        if callable(getattr(backend, name, None)):
            caps.add(name)
    return caps


def conformance(backend: Any) -> list[str]:
    """Statically check the retained v2 contract without executing code."""
    problems: list[str] = []
    exec_fn = getattr(backend, "exec", None)
    if not callable(exec_fn):
        problems.append("missing exec(cmd, timeout=None)")
    else:
        try:
            sig = inspect.signature(exec_fn)
            params = [
                param
                for param in sig.parameters.values()
                if param.name not in ("self", "cls")
            ]
            names = [param.name for param in params]
            if not names:
                problems.append("exec() takes no command argument")
            accepts_timeout = "timeout" in names or any(
                param.kind is inspect.Parameter.VAR_KEYWORD for param in params
            )
            if not accepts_timeout:
                problems.append("exec() must accept timeout=None")
        except (TypeError, ValueError):
            problems.append("exec() signature is not introspectable")
    if not _declares_workdir(backend):
        problems.append("missing workdir attribute")
    return problems


def _declares_workdir(backend: Any) -> bool:
    if hasattr(backend, "workdir"):
        return True
    if "workdir" in getattr(backend, "__annotations__", {}):
        return True
    init = getattr(backend, "__init__", None)
    if callable(init):
        try:
            return "workdir" in inspect.signature(init).parameters
        except (TypeError, ValueError):
            return False
    return False


__all__ = ["SDK_VERSION", "SandboxV2", "capabilities", "conformance"]
