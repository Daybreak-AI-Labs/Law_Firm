"""Helpers shared by the dashboard's ``app`` and ``api`` modules.

These four symbols lived as verbatim copies in both ``app`` and ``api``.
The duplication was deliberate -- ``app`` imports ``api`` at module load,
so the reverse import would cycle, and the old ``_world`` docstrings said
as much. A third module that imports neither removes the copies without
reintroducing the cycle; both modules ``from ._shared import`` these.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from maverick.runtime_overrides import RuntimeOverridesSecurityError

# One process-wide WorldModel cache keyed by absolute DB path (was a
# separate dict per module). Tests clear it via ``<module>._world_cache``.
_world_cache: dict[str, Any] = {}


# SSE concurrency cap. Each open event-stream holds a slot; new streams 503
# past the cap. This lived as a verbatim copy in both ``app`` and ``api``,
# which created TWO independent semaphores -- so the real ceiling was the cap
# per module, not process-wide. One shared semaphore makes the limit actually
# bind across all streaming routes. Built lazily on the running loop.
def _max_sse_streams() -> int:
    try:
        return max(1, int(os.environ.get("MAVERICK_DASHBOARD_MAX_SSE", "64")))
    except ValueError:
        return 64


_sse_semaphore: asyncio.Semaphore | None = None


def _get_sse_semaphore() -> asyncio.Semaphore:
    global _sse_semaphore
    if _sse_semaphore is None:
        _sse_semaphore = asyncio.Semaphore(_max_sse_streams())
    return _sse_semaphore

def _any_provider_key_set() -> bool:
    """True iff some LLM provider is configured (env key, base-url env, or
    a ``[providers.<name>]`` config table with api_key/base_url).

    Council UX fix round 1: the dashboard used to hard-fail on a missing
    ANTHROPIC_API_KEY even when the user had OpenAI or Gemini set up.
    Round 2 (platform test): it still rejected keyless self-hosted setups
    (Ollama/vLLM/TGI via config or *_BASE_URL) that the CLI accepted and ran
    fine — three components, three different predicates. Delegate to the one
    shared predicate in maverick.config; the name is kept for call sites.
    """
    try:
        from maverick.config import any_provider_configured
        return any_provider_configured()
    except Exception:  # pragma: no cover -- never block the dashboard on config
        return False


def require_provider_or_400() -> None:
    """Raise HTTP 400 unless the one selected run model is ready."""
    try:
        from maverick.config import load_config
        from maverick.operator_preflight import (
            _format_route_missing,
            _routed_configuration_missing,
        )

        cfg = load_config()
        missing = {
            provider: fields
            for provider, fields in _routed_configuration_missing(cfg).items()
            if fields
        }
    except RuntimeOverridesSecurityError:
        # This is an operator-policy integrity failure, not an incomplete model
        # route. Let the app's explicit handler return the actionable 503.
        raise
    except Exception:
        missing = {"selected": ("configuration",)}
    if not missing:
        return
    from fastapi import HTTPException

    missing_detail = _format_route_missing(missing)
    raise HTTPException(
        status_code=400,
        detail=(
            "The selected AI run model is incomplete "
            f"({missing_detail}). An administrator can run 'maverick init' and "
            "'maverick doctor' before this operation."
        ),
    )


def _world():
    """Return a per-DB-path cached WorldModel (council perf fix).

    Opening a new WorldModel on every request re-runs the PRAGMAs and the
    schema-migration check, leaks the connection (no close()), and
    serialises the asyncio loop because sqlite3 is sync. Cache by absolute
    DB path so test fixtures that monkeypatch ``DEFAULT_DB`` to a fresh
    ``tmp_path`` still get an isolated WorldModel per test.
    """
    from maverick.world_model import default_db_path, open_world
    path = default_db_path()
    key = str(path)
    cached = _world_cache.get(key)
    if cached is None:
        # Preserve DEFAULT_DB override ownership semantics: open_world() knows
        # whether this is Maverick's managed default or a caller-owned path.
        cached = open_world()
        _world_cache[key] = cached
    return cached
