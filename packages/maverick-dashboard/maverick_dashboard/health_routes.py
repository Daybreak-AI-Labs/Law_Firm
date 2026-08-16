"""Operational health, readiness, and Prometheus response builders.

The FastAPI route declarations remain in :mod:`maverick_dashboard.app` so the
public application contract and test monkeypatch points stay stable.  This
module owns the probe policy and metric rendering, keeping those operational
concerns out of the dashboard's page controller.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import auth_metrics

MAX_USER_SPEND_METRIC_SERIES = 100
USER_SPEND_OVERFLOW_LABEL = "__other__"


class _World(Protocol):
    """The small world-model surface operational probes consume."""

    def ping(self) -> Any: ...

    def goal_status_counts(self) -> dict[str, int]: ...

    def total_spend(self) -> dict[str, Any]: ...


WorldProvider = Callable[[], _World]
ProviderKeyCheck = Callable[[], bool]
OwnerFilter = Callable[[Request], str | None]


def readiness_deep_checks() -> tuple[bool, dict[str, str]]:
    """Return fail-closed readiness checks for governed runtime posture."""
    checks: dict[str, str] = {}
    ok = True
    try:
        from maverick.client import client_binding_enforced, client_id

        if client_binding_enforced() and not client_id():
            checks["client_binding"] = "fail: enforced but no valid client id"
            ok = False
        else:
            checks["client_binding"] = "ok"
    except Exception as exc:  # pragma: no cover - probe must return, not raise
        checks["client_binding"] = f"unknown: {type(exc).__name__}"
        ok = False

    try:
        from maverick.shield_policy import shield_available, shield_required

        if shield_required() and not shield_available():
            checks["shield"] = "fail: required but not installed/available"
            ok = False
        else:
            checks["shield"] = "ok"
    except Exception as exc:  # pragma: no cover - probe must return, not raise
        checks["shield"] = f"unknown: {type(exc).__name__}"
        ok = False

    # Replica posture is evidence for operators, not a traffic gate.  A process
    # that does not own the lease may be intentionally configured as a reader.
    try:
        from maverick.control_plane_lease import posture

        state = posture()
        checks["replica_safety"] = (
            f"ok: single writer (token {state['fencing_token']})"
            if state["state"] == "owned"
            else f"{state['state']}: {state.get('detail', '')}".strip()
        )
    except Exception as exc:  # pragma: no cover - evidence remains best effort
        checks["replica_safety"] = f"unknown: {type(exc).__name__}"
    return ok, checks


def health_should_redact() -> bool:
    """Whether auth-exempt probe responses must omit operational details."""
    if os.environ.get("MAVERICK_DASHBOARD_TOKEN"):
        return True
    try:
        from maverick.oidc import oidc_enabled

        if oidc_enabled():
            return True
    except Exception:
        # An unreadable auth posture is not permission to disclose internals.
        return True
    try:
        from maverick.proxy_auth import proxy_auth_enabled

        if proxy_auth_enabled():
            return True
    except Exception:
        # An unreadable auth posture is not permission to disclose internals.
        return True
    return False


async def health_response(
    *, world_provider: WorldProvider, provider_key_set: ProviderKeyCheck
) -> JSONResponse:
    """Build the deep health response without coupling to the app singleton."""
    from maverick.runner import MAX_CONCURRENT_GOALS, inflight_goals

    checks: dict[str, str] = {}
    overall_ok = True
    try:
        world_provider().ping()
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = (
            f"fail: {type(exc).__name__}"
            if health_should_redact()
            else f"fail: {type(exc).__name__}: {exc}"
        )
        overall_ok = False

    if provider_key_set():
        checks["llm_key"] = "ok"
    else:
        checks["llm_key"] = "missing"
        overall_ok = False

    checks["runner"] = f"in_flight={inflight_goals()}/{MAX_CONCURRENT_GOALS}"
    status = "ok" if overall_ok else "degraded"
    payload: dict[str, Any] = (
        {"status": status}
        if health_should_redact()
        else {"status": status, "checks": checks}
    )
    return JSONResponse(payload, status_code=200 if overall_ok else 503)


async def readiness_response(
    *, world_provider: WorldProvider, provider_key_set: ProviderKeyCheck
) -> JSONResponse:
    """Combine health with fail-closed trust and client-binding readiness."""
    health = await health_response(
        world_provider=world_provider, provider_key_set=provider_key_set
    )
    health_ok = health.status_code == 200
    deep_ok, deep_checks = await run_in_threadpool(readiness_deep_checks)
    overall_ok = health_ok and deep_ok
    if health_should_redact():
        payload: dict[str, Any] = {"status": "ok" if overall_ok else "not_ready"}
    else:
        health_body = json.loads(bytes(health.body).decode("utf-8"))
        checks = dict(health_body.get("checks", {}))
        checks.update(deep_checks)
        payload = {
            "status": "ok" if overall_ok else "not_ready",
            "checks": checks,
        }
    return JSONResponse(payload, status_code=200 if overall_ok else 503)


def bounded_user_spend_series(
    spend_by_principal: dict[str, float],
) -> list[tuple[str, float]]:
    """Bound principal-labelled series while preserving total spend."""
    ranked = sorted(
        spend_by_principal.items(),
        key=lambda item: (-float(item[1]), str(item[0])),
    )
    if len(ranked) <= MAX_USER_SPEND_METRIC_SERIES:
        return ranked
    keep = dict(ranked[: MAX_USER_SPEND_METRIC_SERIES - 1])
    overflow = sum(
        float(value)
        for _, value in ranked[MAX_USER_SPEND_METRIC_SERIES - 1 :]
    )
    keep[USER_SPEND_OVERFLOW_LABEL] = (
        float(keep.get(USER_SPEND_OVERFLOW_LABEL, 0.0)) + overflow
    )
    return sorted(keep.items())


async def metrics_response(
    request: Request,
    *,
    world_provider: WorldProvider,
    owner_filter: OwnerFilter,
) -> PlainTextResponse:
    """Render bounded Prometheus text for the configured runtime backend."""
    from maverick.runner import MAX_CONCURRENT_GOALS, inflight_goals

    try:
        world = world_provider()
        status_counts = world.goal_status_counts()
        spend = world.total_spend()
    except Exception:
        status_counts = {}
        spend = {
            "dollars": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "runs": 0,
        }

    lines = [
        "# HELP maverick_goals_total Current goals by status",
        "# TYPE maverick_goals_total gauge",
    ]
    for status, count in status_counts.items():
        lines.append(f'maverick_goals_total{{status="{status}"}} {count}')
    lines += [
        "# HELP maverick_cost_dollars_total Total LLM spend",
        "# TYPE maverick_cost_dollars_total counter",
        f"maverick_cost_dollars_total {spend['dollars']:.4f}",
        "# HELP maverick_tokens_total Total input/output tokens",
        "# TYPE maverick_tokens_total counter",
        f'maverick_tokens_total{{direction="input"}} {spend["input_tokens"]}',
        f'maverick_tokens_total{{direction="output"}} {spend["output_tokens"]}',
        "# HELP maverick_concurrent_goals Goals running right now",
        "# TYPE maverick_concurrent_goals gauge",
        f"maverick_concurrent_goals {inflight_goals()}",
        "# HELP maverick_max_concurrent_goals Concurrency cap",
        "# TYPE maverick_max_concurrent_goals gauge",
        f"maverick_max_concurrent_goals {MAX_CONCURRENT_GOALS}",
        "# HELP maverick_auth_failures_total Rejected dashboard/SCIM auth attempts by reason",
        "# TYPE maverick_auth_failures_total counter",
    ]

    failures = auth_metrics.auth_failure_counts()
    if not failures:
        lines.append('maverick_auth_failures_total{reason="none"} 0')
    for reason, count in sorted(failures.items()):
        safe = _prometheus_label(reason)
        lines.append(f'maverick_auth_failures_total{{reason="{safe}"}} {count}')

    try:
        from maverick.job_queue import JobQueue

        queue_counts = JobQueue().counts()
    except Exception:
        queue_counts = {}
    lines += [
        "# HELP maverick_queue_jobs Job-queue jobs by status (incl. pending backlog and failed dead-letter)",
        "# TYPE maverick_queue_jobs gauge",
    ]
    for status, count in sorted(queue_counts.items()):
        lines.append(f'maverick_queue_jobs{{status="{status}"}} {count}')

    user_spend: dict[str, float] = {}
    if owner_filter(request) is None:
        try:
            from maverick.quotas import UsageLedger

            user_spend = UsageLedger().spend_by_principal()
        except Exception:
            user_spend = {}
    if user_spend:
        lines += [
            "# HELP maverick_user_spend_dollars_today Per-principal spend for the current UTC day",
            "# TYPE maverick_user_spend_dollars_today gauge",
        ]
        for principal, dollars in bounded_user_spend_series(user_spend):
            lines.append(
                "maverick_user_spend_dollars_today"
                f'{{principal="{_prometheus_label(principal)}"}} {dollars:.4f}'
            )

    lines.extend(_storage_metrics())
    return PlainTextResponse("\n".join(lines) + "\n")


def _prometheus_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _storage_metrics() -> list[str]:
    """Return local data-volume metrics, or no series when unavailable."""
    try:
        import shutil

        from maverick.world_model import default_db_path
        from maverick.world_model_backends import is_postgres_configured

        db_path = default_db_path()
        probe = db_path.parent if db_path.parent.exists() else None
        usage = shutil.disk_usage(str(probe) if probe else ".")
        lines: list[str] = []
        if not is_postgres_configured():
            db_bytes = db_path.stat().st_size if db_path.exists() else 0
            lines += [
                "# HELP maverick_world_db_bytes Size of the world.db file on disk",
                "# TYPE maverick_world_db_bytes gauge",
                f"maverick_world_db_bytes {db_bytes}",
            ]
        lines += [
            "# HELP maverick_data_disk_free_bytes Free space on the data volume",
            "# TYPE maverick_data_disk_free_bytes gauge",
            f"maverick_data_disk_free_bytes {usage.free}",
        ]
        return lines
    except Exception:
        return []
