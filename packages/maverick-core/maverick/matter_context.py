"""Immutable, principal-bound context for law-firm goal execution.

The durable goal row is the authority for a run's matter and specialist domain;
callers may not supply either as ambient metadata.  The authenticated principal
must also have an active, execution-capable membership in that exact matter.
This module deliberately knows nothing about dashboard/global administrator
roles: an administrator who is not a matter member receives no ethical-wall
bypass.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

GOAL_EXECUTION_PURPOSE = "goal-execution"
_EXECUTION_ROLES = frozenset({"responsible_attorney", "attorney", "staff"})
_TERMINAL_GATES = frozenset({"review", "approval"})
_EGRESS_MODES = frozenset({"local_only", "approved_services"})
_NON_HUMAN_EXECUTION_PRINCIPALS = frozenset({"user:dashboard-static-bearer"})


class MatterContextError(RuntimeError):
    """A goal cannot be proven safe to execute in its claimed matter."""


def _positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MatterContextError(f"{field} must be a positive integer")
    return value


def _exact_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise MatterContextError(f"{field} must be a non-empty canonical string")
    return value


@dataclass(frozen=True)
class MatterContext:
    """The exact execution authority bound around one governed goal run."""

    matter_id: int
    client_id: int
    principal: str
    membership_role: str
    domain: str
    jurisdiction: str
    purpose: str
    source: str
    egress_mode: str = "local_only"

    def __post_init__(self) -> None:
        _positive_id(self.matter_id, "matter_id")
        _positive_id(self.client_id, "client_id")
        _exact_text(self.principal, "principal")
        role = _exact_text(self.membership_role, "membership_role")
        if role not in _EXECUTION_ROLES:
            raise MatterContextError(
                f"membership role {role!r} is not permitted to execute matter work"
            )
        _exact_text(self.domain, "domain")
        _exact_text(self.jurisdiction, "jurisdiction")
        _exact_text(self.purpose, "purpose")
        _exact_text(self.source, "source")
        mode = _exact_text(self.egress_mode, "egress_mode")
        if mode not in _EGRESS_MODES:
            raise MatterContextError("matter egress mode is invalid")


_CURRENT_MATTER_CONTEXT: ContextVar[MatterContext | None] = ContextVar(
    "maverick_matter_context",
    default=None,
)
_CURRENT_MATTER_AUTHORITY_RESOLVER: ContextVar[
    Callable[[], MatterContext] | None
] = ContextVar(
    "maverick_matter_authority_resolver",
    default=None,
)


def current_matter_context() -> MatterContext | None:
    """Return the execution context bound to this thread/task, if any."""
    return _CURRENT_MATTER_CONTEXT.get()


def require_matter_context() -> MatterContext:
    """Return the active context or fail closed outside governed execution."""
    context = current_matter_context()
    if context is None:
        raise MatterContextError("matter execution context is not bound")
    return context


@contextmanager
def matter_context_scope(
    context: MatterContext,
    *,
    authority_resolver: Callable[[], MatterContext] | None = None,
) -> Iterator[MatterContext]:
    """Bind context plus its live durable-authority resolver for one scope.

    A nested scope that omits ``authority_resolver`` inherits the parent's
    resolver. This lets Agent rebind a freshly resolved policy without
    accidentally disabling the provider/HTTP revocation choke point.
    """
    if not isinstance(context, MatterContext):
        raise MatterContextError("a validated MatterContext is required")
    if authority_resolver is not None and not callable(authority_resolver):
        raise MatterContextError("matter authority resolver must be callable")
    inherited_resolver = _CURRENT_MATTER_AUTHORITY_RESOLVER.get()
    resolver = authority_resolver or inherited_resolver
    context_token = _CURRENT_MATTER_CONTEXT.set(context)
    resolver_token = _CURRENT_MATTER_AUTHORITY_RESOLVER.set(resolver)
    try:
        yield context
    finally:
        _CURRENT_MATTER_AUTHORITY_RESOLVER.reset(resolver_token)
        _CURRENT_MATTER_CONTEXT.reset(context_token)


def refresh_matter_context() -> MatterContext:
    """Re-resolve current matter authority immediately before external work.

    Firm mode has no snapshot fallback: a missing resolver, revoked membership,
    unreadable policy, or changed matter identity denies the dispatch. Explicit
    legacy mode retains the historical snapshot behavior.
    """
    bound = require_matter_context()
    resolver = _CURRENT_MATTER_AUTHORITY_RESOLVER.get()
    if resolver is None:
        try:
            from .security_defaults import secure_by_default

            secure = bool(secure_by_default())
        except Exception:
            secure = True
        if secure:
            raise MatterContextError("live matter authority resolver is not bound")
        return bound
    try:
        fresh = resolver()
    except MatterContextError:
        raise
    except Exception as exc:
        raise MatterContextError("live matter authority could not be resolved") from exc
    if not isinstance(fresh, MatterContext):
        raise MatterContextError("live matter authority resolver returned invalid data")
    expected = (
        bound.matter_id,
        bound.client_id,
        bound.principal,
        bound.domain,
        bound.jurisdiction,
        bound.purpose,
    )
    actual = (
        fresh.matter_id,
        fresh.client_id,
        fresh.principal,
        fresh.domain,
        fresh.jurisdiction,
        fresh.purpose,
    )
    if actual != expected:
        raise MatterContextError("durable matter execution authority changed")
    return fresh


def resolve_matter_context(
    world: Any,
    *,
    matter_id: int,
    principal: str,
    domain: str,
    purpose: str = GOAL_EXECUTION_PURPOSE,
    source: str,
) -> MatterContext:
    """Resolve current matter membership and legal-domain governance.

    Every value is checked against durable state or the enabled domain catalog.
    In particular, ``principal`` is matched directly against active
    ``matter_memberships``; no owner/global-admin fallback exists.
    """
    durable_matter_id = _positive_id(matter_id, "matter_id")
    exact_principal = _exact_text(principal, "principal")
    if exact_principal in _NON_HUMAN_EXECUTION_PRINCIPALS:
        raise MatterContextError("a shared bearer cannot execute client matter work")
    exact_domain = _exact_text(domain, "domain")
    exact_purpose = _exact_text(purpose, "purpose")
    exact_source = _exact_text(source, "source")

    matter = world.get_project(durable_matter_id)
    if matter is None:
        raise MatterContextError("goal matter does not exist")
    client_id = _positive_id(matter.get("client_id"), "client_id")
    _exact_text(matter.get("matter_number"), "matter_number")
    jurisdiction = _exact_text(matter.get("jurisdiction"), "jurisdiction")
    egress_mode = matter.get("egress_mode")
    if egress_mode not in _EGRESS_MODES:
        raise MatterContextError("matter egress policy is missing or invalid")

    role = world.project_member_role(durable_matter_id, exact_principal)
    if role is None:
        raise MatterContextError("principal is not an active member of the goal matter")
    if role == "viewer":
        raise MatterContextError("viewer membership cannot execute matter work")
    if role not in _EXECUTION_ROLES:
        raise MatterContextError("matter membership role is not execution-capable")

    from .domain import enabled_domains, suite_for

    profile = enabled_domains().get(exact_domain)
    if profile is None:
        raise MatterContextError("goal domain is unknown or disabled")
    if suite_for(exact_domain) != "legal":
        raise MatterContextError("goal domain is not in the legal suite")
    terminal_gate = profile.workflow[-1].gate if profile.workflow else None
    if terminal_gate not in _TERMINAL_GATES:
        raise MatterContextError(
            "legal goal domain must end with a review or approval gate"
        )

    return MatterContext(
        matter_id=durable_matter_id,
        client_id=client_id,
        principal=exact_principal,
        membership_role=role,
        domain=exact_domain,
        jurisdiction=jurisdiction,
        purpose=exact_purpose,
        source=exact_source,
        egress_mode=egress_mode,
    )


def resolve_goal_matter_context(
    world: Any,
    goal_id: int,
    *,
    principal: str,
    purpose: str = GOAL_EXECUTION_PURPOSE,
    source: str,
) -> MatterContext:
    """Resolve a run context exclusively from the durable goal row."""
    durable_goal_id = _positive_id(goal_id, "goal_id")
    goal = world.get_goal(durable_goal_id)
    if goal is None:
        raise MatterContextError("goal does not exist")
    matter_id = getattr(goal, "project_id", None)
    if matter_id is None:
        raise MatterContextError("goal is not bound to a matter")
    context = resolve_matter_context(
        world,
        matter_id=matter_id,
        principal=principal,
        domain=getattr(goal, "domain", None),
        purpose=purpose,
        source=source,
    )
    if getattr(goal, "owner", None) != principal:
        raise MatterContextError("goal owner does not match authenticated principal")
    return context


def verify_context_snapshot(
    context: MatterContext,
    *,
    matter_id: Any,
    principal: Any,
    domain: Any,
) -> None:
    """Fail when a freshly resolved context differs from a signed snapshot."""
    expected = (
        _positive_id(matter_id, "matter_id"),
        _exact_text(principal, "principal"),
        _exact_text(domain, "domain"),
    )
    actual = (context.matter_id, context.principal, context.domain)
    if actual != expected:
        raise MatterContextError("durable matter execution context changed")


__all__ = [
    "GOAL_EXECUTION_PURPOSE",
    "MatterContext",
    "MatterContextError",
    "current_matter_context",
    "matter_context_scope",
    "refresh_matter_context",
    "require_matter_context",
    "resolve_goal_matter_context",
    "resolve_matter_context",
    "verify_context_snapshot",
]
