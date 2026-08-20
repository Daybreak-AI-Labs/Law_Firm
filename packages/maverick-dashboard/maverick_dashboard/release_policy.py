"""Fail-closed authorization for goal content crossing a release boundary.

Viewing a goal inside the authenticated workspace is not a release.  Public
share links and mechanical deliverable exports are: they can put privileged
client work outside the matter workspace.  Every such path must therefore prove
the same durable facts instead of interpreting a missing policy as permission.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any


class GoalReleaseDenied(RuntimeError):
    """A goal is not currently eligible to cross an external boundary."""

    def __init__(self, code: str, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class AuthorizedGoalRelease:
    """The policy facts proven for one exact release check."""

    project_id: int
    domain: str
    gate: str
    signoff: dict[str, Any]
    deliverable: str
    deliverable_updated_at: float
    deliverable_sha256: str


def deliver_release_audit(
    *,
    actor: str,
    release: AuthorizedGoalRelease,
    goal_id: int,
    action: str,
    destination_class: str,
    event_id: str | None = None,
    share_link_id: int | None = None,
    expires_at: float | None = None,
    world=None,
) -> str:
    """Append one privacy-minimized release event or refuse the release.

    Share-link callers pass the durable outbox ``event_id`` and ``world`` so
    the link remains inert until the append is acknowledged.  Synchronous
    exports need no durable bearer and therefore audit their intent directly
    before constructing the response.
    """
    principal = str(actor or "").strip()
    if not principal:
        raise GoalReleaseDenied(
            "actor_required",
            "a named release actor is required",
            status_code=403,
        )
    stable_event_id = str(event_id or ("release-" + secrets.token_urlsafe(24)))
    try:
        from maverick.audit import EventKind, audit_event

        recorded = audit_event(
            EventKind.LEGAL_RELEASE,
            agent=principal,
            goal_id=int(goal_id),
            event_id=stable_event_id,
            actor=principal,
            matter_id=int(release.project_id),
            action=str(action),
            destination_class=str(destination_class),
            deliverable_updated_at=float(release.deliverable_updated_at),
            deliverable_sha256=str(release.deliverable_sha256),
            share_link_id=share_link_id,
            expires_at=expires_at,
        )
        if not recorded:
            raise RuntimeError("signed release audit append did not succeed")
        if world is not None and not world.mark_release_audit_delivered(stable_event_id):
            raise RuntimeError("release audit delivery was not acknowledged")
    except GoalReleaseDenied:
        raise
    except Exception as exc:
        raise GoalReleaseDenied(
            "release_audit_unavailable",
            "release audit is temporarily unavailable; no content was released",
            status_code=503,
        ) from exc
    return stable_event_id


def deliver_current_signoff_audit(world, goal, signoff: dict[str, Any]):
    """Deliver the exact current decision to the signed chain or fail closed.

    The outbox row was committed with the signoff. A failed append leaves it
    pending and therefore non-releasable; a retry reuses the stable event id.
    No privileged note or result text enters the audit payload.
    """
    try:
        event = world.current_signoff_audit_event(int(goal.id))
    except Exception as exc:
        raise GoalReleaseDenied(
            "audit_unavailable",
            "legal sign-off audit is temporarily unavailable",
            status_code=503,
        ) from exc
    deliverable = str(getattr(goal, "result", "") or "")
    digest = hashlib.sha256(deliverable.encode("utf-8")).hexdigest()
    if not (
        event
        and event.decision == signoff.get("decision")
        and event.decided_by == str(signoff.get("decided_by") or "")
        and event.deliverable_updated_at == float(goal.updated_at)
        and event.deliverable_sha256 == digest
    ):
        raise GoalReleaseDenied(
            "audit_stale",
            "the reviewed deliverable changed before its audit record was finalized",
            status_code=409,
        )
    if event.delivered_at is None:
        try:
            from maverick.audit import EventKind, audit_event

            recorded = audit_event(
                EventKind.LEGAL_SIGNOFF_DECISION,
                goal_id=int(goal.id),
                event_id=event.event_id,
                matter_id=int(goal.project_id),
                decision=event.decision,
                decided_by=event.decided_by,
                deliverable_updated_at=event.deliverable_updated_at,
                deliverable_sha256=event.deliverable_sha256,
            )
            if not recorded:
                raise RuntimeError("signed audit append did not succeed")
            if not world.mark_signoff_audit_delivered(event.event_id):
                raise RuntimeError("sign-off audit delivery was not acknowledged")
        except GoalReleaseDenied:
            raise
        except Exception as exc:
            raise GoalReleaseDenied(
                "audit_pending",
                "legal sign-off is saved but signed-audit delivery is pending; retry",
                status_code=503,
            ) from exc

    # Reconcile after the audit append/ack boundary. A concurrent edit may have
    # invalidated the signoff while the append was in flight; that historical
    # event remains truthful, but it cannot authorize release of new bytes.
    try:
        fresh_goal = world.get_goal(int(goal.id))
        fresh_signoff = world.signoff_for(int(goal.id))
        fresh_event = world.current_signoff_audit_event(int(goal.id))
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc
    if not (
        fresh_goal
        and fresh_goal.status == "done"
        and fresh_goal.updated_at == goal.updated_at
        and fresh_goal.result == goal.result
        and fresh_signoff == signoff
        and fresh_event
        and fresh_event.event_id == event.event_id
        and fresh_event.delivered_at is not None
    ):
        raise GoalReleaseDenied(
            "approval_invalidated",
            "the deliverable or its approval changed before release",
            status_code=409,
        )
    return fresh_goal, fresh_event


def authorize_goal_release(world, goal) -> AuthorizedGoalRelease:
    """Prove that ``goal`` may be shared or exported right now.

    A releasable law-firm deliverable is finished, belongs to an existing
    matter, resolves to a known domain with an executable terminal release gate,
    and has a current named approval.  WorldModel invalidates sign-off in the
    same transaction as result/artifact mutation, so reading the authoritative
    row here also binds release to the exact reviewed version.

    Policy/read failures are unavailable, never an implicit ungated result.
    Callers should map the structured denial to an authenticated response or an
    opaque public 404 as appropriate for their boundary.
    """
    if getattr(goal, "status", "") != "done":
        raise GoalReleaseDenied(
            "unfinished",
            "only a finished deliverable can be released",
            status_code=409,
        )

    raw_project_id = getattr(goal, "project_id", None)
    if (
        isinstance(raw_project_id, bool)
        or not isinstance(raw_project_id, int)
        or raw_project_id <= 0
    ):
        raise GoalReleaseDenied(
            "matter_required",
            "a deliverable must belong to a matter before release",
            status_code=409,
        )
    try:
        project = world.get_project(raw_project_id)
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc
    if project is None:
        raise GoalReleaseDenied(
            "matter_required",
            "the deliverable's matter is no longer available",
            status_code=409,
        )
    # Legacy workspace rows are not releasable client matters.  Keep one
    # indistinguishable denial for all missing intake facts so this boundary
    # never becomes a client-id or matter-metadata oracle.
    client_id = project.get("client_id")
    if (
        isinstance(client_id, bool)
        or not isinstance(client_id, int)
        or client_id <= 0
        or not str(project.get("matter_number") or "").strip()
        or not str(project.get("jurisdiction") or "").strip()
    ):
        raise GoalReleaseDenied(
            "matter_intake_incomplete",
            "the matter intake record is incomplete and cannot authorize release",
            status_code=409,
        )

    raw_domain = str(getattr(goal, "domain", "") or "")
    if not raw_domain:
        raise GoalReleaseDenied(
            "domain_required",
            "a deliverable must use an approved legal profile before release",
            status_code=409,
        )
    if raw_domain != raw_domain.strip():
        raise GoalReleaseDenied(
            "unknown_domain",
            "the goal's domain policy is no longer available",
            status_code=409,
        )
    domain = raw_domain
    try:
        from maverick.domain import available_domains, enforced_gate, suite_for

        profile = available_domains().get(domain)
        if profile is None:
            raise GoalReleaseDenied(
                "unknown_domain",
                "the goal's domain policy is no longer available",
                status_code=409,
            )
        if suite_for(domain) != "legal":
            raise GoalReleaseDenied(
                "nonlegal_domain",
                "only an approved legal profile can authorize client release",
                status_code=409,
            )
        terminal_gate = profile.workflow[-1].gate if profile.workflow else None
        gate = enforced_gate(profile)
    except GoalReleaseDenied:
        raise
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc

    if terminal_gate not in {"review", "approval"} or gate not in {
        "review",
        "approval",
    }:
        raise GoalReleaseDenied(
            "terminal_gate_required",
            "the goal's profile has no enforceable terminal review gate",
            status_code=409,
        )

    try:
        signoff = world.signoff_for(int(goal.id))
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc
    if not (
        signoff
        and signoff.get("decision") == "approved"
        and str(signoff.get("decided_by") or "").strip()
    ):
        raise GoalReleaseDenied(
            "approval_required",
            f"{gate} sign-off is required before releasing this deliverable",
            status_code=403,
        )

    # A historical signature is not a permanent release credential. The named
    # reviewer must still be an active attorney on this exact matter at the
    # instant content crosses the boundary. This independently protects legacy
    # rows and non-SQLite backends even though the current WorldModel also
    # removes sign-offs transactionally on attorney demotion/revocation.
    signer = str(signoff["decided_by"]).strip()
    try:
        from .auth import auth_genuinely_off, is_qualified_attorney_principal

        if not auth_genuinely_off() and not is_qualified_attorney_principal(signer):
            raise GoalReleaseDenied(
                "approval_invalidated",
                "the approving attorney is no longer authorized for this matter",
                status_code=403,
            )
    except GoalReleaseDenied:
        raise
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc
    try:
        signer_role = world.project_member_role(raw_project_id, signer)
    except Exception as exc:
        raise GoalReleaseDenied(
            "policy_unavailable",
            "deliverable release policy is temporarily unavailable",
            status_code=503,
        ) from exc
    if signer_role not in {"responsible_attorney", "attorney"}:
        raise GoalReleaseDenied(
            "approval_invalidated",
            "the approving attorney is no longer authorized for this matter",
            status_code=403,
        )

    fresh_goal, audit_event = deliver_current_signoff_audit(world, goal, signoff)
    return AuthorizedGoalRelease(
        project_id=raw_project_id,
        domain=domain,
        gate=gate,
        signoff=signoff,
        deliverable=fresh_goal.result or "",
        deliverable_updated_at=fresh_goal.updated_at,
        deliverable_sha256=audit_event.deliverable_sha256,
    )


__all__ = [
    "AuthorizedGoalRelease",
    "GoalReleaseDenied",
    "authorize_goal_release",
    "deliver_release_audit",
    "deliver_current_signoff_audit",
]
