"""Request-less poll-and-fire for event triggers.

Shared by the HTTP endpoint (``POST /api/v1/event-triggers/poll``) and the
background automation scheduler (app lifespan). Each polls its sources, fires
the bound template per new event -- the event's fields filling the template's
declared params -- and advances the cursor at most once. ``fire(goal_id)``
schedules the actual run: a FastAPI BackgroundTask in the request path, a daemon
thread in the scheduler.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _safe_error(exc: BaseException, *, fallback: str = "operation failed") -> str:
    """Return a bounded, secret-scrubbed error safe for API notes/history."""
    from maverick.secrets import scrub

    message = scrub(str(exc)).strip()[:500]
    return message or fallback


def _fire_template(world, trig, tpl, events, owner, fire, summary) -> None:
    """Render the bound template per event (event fields fill DECLARED params)
    and fire a goal. Mutates ``summary``."""
    from maverick_dashboard import trigger_events_store
    name = trig["name"]
    declared = set(tpl.params)
    for event in events:
        params = dict(trig.get("params") or {})
        for k, v in event.items():      # event fields fill DECLARED params only
            if k in declared:
                params[str(k)] = str(v)[:2000]
        try:
            title, description = tpl.render(**params)
        except ValueError as e:         # a required param the event didn't supply
            detail = _safe_error(e, fallback="template parameters are invalid")
            summary["notes"].append(f"skipped an event: {detail}")
            trigger_events_store.record(
                name,
                owner,
                trigger_events_store.EVENT_SKIPPED,
                note=detail,
            )
            continue
        goal_id = world.create_goal(title[:200], description[:8000], owner=owner)
        world.record_goal_origin(goal_id, "event", name)
        fire(goal_id)
        summary["fired"].append(goal_id)


def _event_key(name: str, event: dict) -> str:
    """A stable idempotency key for an event: the trigger name + the event's own
    id (or a hash of its fields), so a re-delivered event doesn't fire twice."""
    import hashlib
    import json as _json
    eid = event.get("id") or event.get("guid") or event.get("uid")
    if eid is None:
        # A dedup fingerprint, not a security digest -> usedforsecurity=False.
        digest = hashlib.sha1(_json.dumps(event, sort_keys=True, default=str).encode(),
                              usedforsecurity=False)
        eid = digest.hexdigest()[:16]
    return f"{name}:{eid}"


def _fire_flow(trig, events, owner, fire_flow, summary) -> None:
    """Start a flow run per event, with the event's fields as the run's data
    (the trigger's ``params`` are defaults). Each fire carries an idempotency key
    so a re-delivered event is deduplicated. Mutates ``summary``; run ids land in
    ``summary['fired_flows']``."""
    flow_id = trig["flow"]
    for event in events:
        data = dict(trig.get("params") or {})
        for k, v in event.items():   # a flow takes ALL event fields as data (no declared set)
            data[str(k)] = v if isinstance(v, (int, float, bool)) else str(v)[:2000]
        run_id = fire_flow(flow_id, data, owner, _event_key(trig["name"], event),
                           f"event:{trig['name']}",
                           expected_revision=str(trig.get("flow_revision") or ""))
        if run_id:
            summary["fired_flows"].append(run_id)


def _deliver(world, trig, res, owner, fire, fire_flow, summary) -> bool:
    """Dispatch a trigger's new events to its target (bound flow, else template).
    Returns False when nothing was delivered and the caller must NOT advance the
    cursor past these events (missing target, or no flow dispatcher wired);
    otherwise True. The missing-target cases advance the cursor themselves so a
    permanent backlog can't wedge the trigger."""
    from maverick_dashboard import event_triggers_store, trigger_events_store
    name = trig["name"]
    tenant = str(trig.get("tenant") or "")
    if trig.get("flow"):
        from maverick.flow import store as flow_store

        from maverick_dashboard.auth import auth_genuinely_off

        try:
            published = flow_store.load_published_bundle(trig["flow"])
        except flow_store.FlowSnapshotError:
            published = None
        flow = published[0] if published is not None else None
        release = published[1] if published is not None else None
        expected_owner = str(trig.get("flow_owner") or "")
        expected_revision = str(trig.get("flow_revision") or "")
        stale = flow is None or (
            not expected_revision and not auth_genuinely_off()
        )
        if flow is not None and expected_revision:
            stale = stale or (
                flow.owner != expected_owner
                or release is None
                or str(release.get("release_id") or "") != expected_revision
            )
        if stale:
            summary["notes"].append("flow unavailable: binding is stale")
            # A deleted/recreated target is not the target that was approved at
            # trigger creation. Advance past this batch so an ABA replacement
            # cannot accumulate an unbounded replay backlog.
            event_triggers_store.set_cursor(name, res.cursor, tenant=tenant)
            trigger_events_store.record(
                name,
                owner,
                trigger_events_store.TEMPLATE_UNAVAILABLE,
                note="flow binding is unavailable",
            )
            return False
        if fire_flow is None:
            # No flow dispatcher in this context (e.g. a caller that only wires
            # template fires). Leave the cursor so these events retry once a
            # dispatcher is available rather than silently advancing past them.
            summary["notes"].append("flow firing unavailable in this context")
            return False
        _fire_flow(trig, res.events, owner, fire_flow, summary)
        return True
    try:
        from maverick_dashboard.triggers_store import bound_template

        tpl = bound_template(trig)
    except ValueError as e:
        detail = _safe_error(e, fallback="template unavailable")
        summary["notes"].append(f"template unavailable: {detail}")
        event_triggers_store.set_cursor(name, res.cursor, tenant=tenant)
        trigger_events_store.record(
            name,
            owner,
            trigger_events_store.TEMPLATE_UNAVAILABLE,
            note=detail,
        )
        return False
    _fire_template(world, trig, tpl, res.events, owner, fire, summary)
    return True


def poll_and_fire(world: Any, triggers: list[dict], *, principal: str = "",
                  fire: Callable[[int], None],
                  fire_flow: Callable[..., Any] | None = None) -> list[dict]:
    """Poll each trigger and fire its target (template->goal, or flow) per new
    event. Returns a summary per trigger: ``{name, new_events, fired: [goal_id...],
    fired_flows: [run_id...], notes, poll_error}``. Never raises for a single bad
    trigger -- it's recorded in that trigger's notes + ``poll_error`` and the batch
    continues. ``fire_flow(flow_id, data, owner) -> run_id`` starts a flow run."""
    from maverick.automation_events import EventSourceError, poll_source
    from maverick.paths import reset_tenant, set_tenant
    from maverick.tenant.registry import assert_tenant_active

    from maverick_dashboard import event_triggers_store, trigger_events_store
    from maverick_dashboard.auth import (
        AutomationAuthorizationError,
        auth_genuinely_off,
        stored_automation_identity,
    )

    results: list[dict] = []
    for trig in triggers:
        # Attribute the fired goal to the trigger's OWN owner -- so the
        # background tick (which has no request principal and drains every
        # owner's triggers) doesn't create unowned goals. On the on-demand path
        # the trigger's owner is the caller, so this matches ``principal``.
        owner = str(trig.get("owner") or "").strip()
        if not owner and auth_genuinely_off():
            owner = principal
        name = trig["name"]
        summary: dict = {"name": name, "new_events": 0, "fired": [], "fired_flows": [],
                         "notes": [], "poll_error": False}
        results.append(summary)   # append the ref now; it's mutated in place below
        # Bind the poll (incl. any OAuth vault read) to the tenant that created
        # the trigger, so a polled source never reads a cross-tenant token.
        # Single-tenant deployments store "" and run under the active tenant.
        tenant = str(trig.get("tenant") or "")
        tok = set_tenant(tenant) if tenant else None
        try:
            try:
                if tenant:
                    assert_tenant_active(tenant)
                stored_automation_identity(owner)
                # A world/model store is tenant-scoped. Resolve a factory only
                # after the durable trigger tenant has been restored.
                active_world = world() if callable(world) else world
                res = poll_source(trig["source"], trig["config"], trig["cursor"])
            except EventSourceError as e:
                detail = _safe_error(e, fallback="event source unavailable")
                summary["poll_error"] = True
                summary["notes"].append(f"poll failed: {detail}")
                trigger_events_store.record(
                    name,
                    owner,
                    trigger_events_store.POLL_ERROR,
                    note=detail,
                )
                continue
            except AutomationAuthorizationError:
                summary["poll_error"] = True
                summary["notes"].append("trigger authorization unavailable")
                trigger_events_store.record(
                    name,
                    owner,
                    trigger_events_store.POLL_ERROR,
                    note="trigger authorization unavailable",
                )
                continue
            summary["new_events"] = len(res.events)
            if not _deliver(
                active_world, trig, res, owner, fire, fire_flow, summary
            ):
                continue
            # Only touch the store when the cursor actually moved -- an idle poll
            # (no new items) shouldn't rewrite the whole trigger file every tick.
            if res.cursor != trig["cursor"]:
                event_triggers_store.set_cursor(name, res.cursor, tenant=tenant)
            if summary["fired"] or summary["fired_flows"]:
                trigger_events_store.record(name, owner, trigger_events_store.FIRED,
                                            new_events=summary["new_events"],
                                            fired=summary["fired"],
                                            fired_flows=summary["fired_flows"])
        except Exception as e:  # one trigger's unexpected error must not abort the batch
            detail = _safe_error(e)
            summary["poll_error"] = True
            summary["notes"].append(
                f"trigger error: {type(e).__name__}: {detail}"
            )
            trigger_events_store.record(
                name,
                owner,
                trigger_events_store.POLL_ERROR,
                note=f"{type(e).__name__}: {detail}"[:500],
            )
        finally:
            if tok is not None:
                reset_tenant(tok)
    return results
