"""Support desk — turn a posted support bundle into a triaged ticket.

Reuses :func:`maverick.support_bundle.ticket_summary` to derive the redacted,
routable summary (SKU, build, failing readiness checks, recent failure modes)
from a bundle the customer's ``maverick support`` already secret-scrubbed. Intake
de-dupes on ``correlation_id`` — a re-sent bundle for an open ticket appends a
note instead of spawning a duplicate.
"""
from __future__ import annotations

import sqlite3

from . import audit, store
from .models import Ticket


def _summary_of(bundle: dict) -> dict:
    if not isinstance(bundle, dict):
        return {}
    from maverick.support_bundle import ticket_summary
    return ticket_summary(bundle)   # pure, .get-based — safe on a redacted bundle


def intake_bundle(conn: sqlite3.Connection, *, customer_id: int | None, bundle: dict,
                  subject: str = "", actor: str = "deployment") -> Ticket:
    """Create (or update) a ticket from a support bundle. Priority is raised to
    ``high`` when the bundle reports failing readiness checks."""
    summary = _summary_of(bundle)
    corr = str(summary.get("correlation_id")
               or (bundle.get("correlation_id") if isinstance(bundle, dict) else "") or "")
    tier = str(summary.get("tier") or "")
    ver = str(summary.get("agent_version") or "")
    failing = summary.get("readiness_failing") or []
    if not subject:
        subject = f"{'; '.join(failing) if failing else 'diagnostics'} — {ver or 'unknown build'}"

    existing = store.ticket_by_correlation(conn, corr) if corr else None
    if existing is not None and existing.open:
        store.add_comment(
            conn, ticket_id=existing.id, author=actor, kind="note",
            body=f"New support bundle received (same correlation id). "
                 f"failing checks: {', '.join(failing) if failing else 'none'}.")
        audit.record(conn, actor=actor, action="ticket.bundle_appended",
                     target=corr, detail={"ticket": existing.id})
        t = store.get_ticket(conn, existing.id)
        assert t is not None
        return t

    tid = store.create_ticket(
        conn, customer_id=customer_id, correlation_id=corr, subject=subject[:200],
        priority="high" if failing else "normal", tier=tier, agent_version=ver,
        summary=summary, bundle=bundle if isinstance(bundle, dict) else {})
    audit.record(conn, actor=actor, action="ticket.create", target=corr or str(tid),
                 detail={"customer_id": customer_id, "failing": failing})
    t = store.get_ticket(conn, tid)
    assert t is not None
    return t


def set_status(conn: sqlite3.Connection, ticket_id: int, status: str, *,
               actor: str, note: str = "") -> None:
    """Change a ticket's status and record it on the ticket timeline + audit."""
    store.update_ticket(conn, ticket_id, status=status)
    body = f"status → {status}" + (f": {note}" if note else "")
    store.add_comment(conn, ticket_id=ticket_id, author=actor, kind="status", body=body)
    audit.record(conn, actor=actor, action="ticket.status",
                 target=str(ticket_id), detail={"status": status})
