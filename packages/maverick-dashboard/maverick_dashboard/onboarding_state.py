"""Read-only state for the dashboard's first-run checklist."""
from __future__ import annotations

from typing import Any


def _has_user_template() -> bool:
    try:
        from maverick.templates import user_templates_dir

        return any(user_templates_dir().glob("*.md"))
    except OSError:
        return False


def _has_user_agent() -> bool:
    try:
        from maverick.domain_edit import list_agents

        return any(agent.get("is_override") for agent in list_agents())
    except Exception:
        return False


def _has_run(world: Any) -> bool:
    try:
        return bool(world.list_goals(limit=1))
    except Exception:
        return False


def _has_automation() -> bool:
    try:
        from maverick.job_queue import JobQueue

        if any(
            (job.payload or {}).get("__cron__")
            for job in JobQueue().list(status="pending")
        ):
            return True
    except Exception:
        pass
    try:
        from . import triggers_store

        return bool(triggers_store.list_triggers())
    except Exception:
        return False


def build(world: Any) -> dict[str, Any]:
    """Build the activation checklist and deterministic install preflight."""

    from maverick.config import any_provider_configured
    from maverick.operator_preflight import collect

    provider_ok = bool(any_provider_configured())
    built = _has_user_template() or _has_user_agent()
    activated = _has_run(world) or _has_automation()
    steps = [
        {
            "title": "Connect a model provider",
            "done": provider_ok,
            "body": (
                "Add a credential or self-hosted endpoint, then run the offline "
                "preflight before the first task."
            ),
            "cta": "/settings",
            "cta_label": "Open Settings",
            "command": "maverick preflight",
        },
        {
            "title": "Build a workflow or agent",
            "done": built,
            "body": (
                "Draft a reusable workflow or a governed specialist agent in "
                "the builder."
            ),
            "cta": "/workflow-builder",
            "cta_label": "Open the builder",
            "command": "",
        },
        {
            "title": "Run it or put it on autopilot",
            "done": activated,
            "body": (
                "Start a goal from chat, or arm a schedule or webhook trigger "
                "to run it."
            ),
            "cta": "/chat",
            "cta_label": "Start a goal",
            "command": "",
        },
    ]
    done_count = sum(step["done"] for step in steps)
    next_step = next((step for step in steps if not step["done"]), None)
    runtime_preflight = collect("run").to_dict()
    assurance_preflight = collect("cockpit").to_dict()
    return {
        "steps": steps,
        "done_count": done_count,
        "total": len(steps),
        "next_step": next_step,
        # Compatibility alias for existing template and API consumers.
        "preflight": runtime_preflight,
        "runtime_preflight": runtime_preflight,
        "assurance_preflight": assurance_preflight,
        "runtime_ready": bool(runtime_preflight["ready"]),
        "assurance_ready": bool(assurance_preflight["ready"]),
        "runtime_all_set": (
            done_count == len(steps) and bool(runtime_preflight["ready"])
        ),
    }


__all__ = ["build"]
