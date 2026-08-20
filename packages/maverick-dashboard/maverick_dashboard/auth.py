"""Dashboard authentication and authorization boundary.

The global FastAPI dependency composes OIDC bearer/browser sessions and local
invite sessions. The legacy static dashboard bearer exists only for explicit
localhost auth-off development. Once named authentication is configured,
missing or invalid identity fails closed. Principal-less local-operator
compatibility is available only when auth is genuinely off and client binding
does not forbid ambient loopback trust.

Importing this module never hard-requires PyJWT: ``maverick.oidc`` lazy-imports
it only when an OIDC token is actually verified.
"""
from __future__ import annotations

import ipaddress
import os

from fastapi import HTTPException, Request
from maverick.oidc import (
    OIDCError,
    VerifiedPrincipal,
    oidc_enabled,
    verify_oidc_token,
)

# Probe/discovery endpoints that must answer without a bearer even when OIDC is
# on (load balancers and k8s liveness/readiness probes, plus the OpenAPI docs,
# can't present an ID token). Mirrors the dashboard's existing bearer-auth
# exemptions in ``app._AUTH_EXEMPT``.
_OIDC_EXEMPT_PATHS = frozenset(
    {
        "/healthz",
        "/livez",
        "/readyz",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
        # Built-in browser-login endpoints must answer without an existing
        # session/bearer -- they ARE the way a browser gets one. They self-gate
        # on login_enabled() (404 when the login flow is off).
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/auth/error",
    }
)

_SELF_AUTH_EXEMPT_PATHS = _OIDC_EXEMPT_PATHS

# The static dashboard bearer authenticates one deployment credential, not an
# anonymous local operator and not an individual human.  Give that credential a
# stable, non-secret subject so ownership, attribution, RBAC, and
# audit records all use the same real principal.  Operators can explicitly grant
# this principal a role as ``user:dashboard-static-bearer``; absent a grant it is
# governed by the deny-by-default authenticated-user role.
_STATIC_DASHBOARD_SUBJECT = "dashboard-static-bearer"
_STATIC_DASHBOARD_ISSUER = "maverick:dashboard-static-bearer"
_STATIC_DASHBOARD_AUDIENCE = "maverick-dashboard"


def _self_authenticated_path(path: str) -> bool:
    """Whether a route authenticates itself or bootstraps authentication."""
    return path in _SELF_AUTH_EXEMPT_PATHS or path.startswith(
        ("/share/", "/auth/invite/")
    )


def _bearer_token(request: Request) -> str:
    """Extract the raw JWT from an ``Authorization: Bearer <jwt>`` header."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _dashboard_require_auth_enabled() -> bool:
    """Whether the dashboard's fail-closed require-auth guard is enabled."""
    env = os.environ.get("MAVERICK_DASHBOARD_REQUIRE_AUTH", "").strip().lower()
    try:
        from maverick.config import config_source_errors, load_config

        config = load_config() or {}
        if config_source_errors():
            return True
    except Exception:
        # An unreadable policy can hide ``require_auth = true``. Treat it as
        # enabled so ambiguity can never re-enable principal-less local admin.
        return True
    if not isinstance(config, dict):
        return True
    if env:
        if env in {"1", "true", "yes", "on"}:
            return True
        if env in {"0", "false", "no", "off"}:
            return False
        # A typo in an authentication-policy override is uncertainty, not an
        # instruction to restore anonymous administrator access.
        return True
    if "dashboard" not in config:
        dashboard = {}
    else:
        dashboard = config["dashboard"]
    if not isinstance(dashboard, dict):
        return True
    configured = dashboard.get("require_auth", False)
    return configured if isinstance(configured, bool) else True


def non_static_auth_configured() -> bool:
    """Whether an identity mechanism besides the static bearer is configured.

    The static operator token and named browser identity mechanisms are
    alternatives, not mutually exclusive deployment modes. Configuration
    uncertainty reports identity policy as configured so the request reaches
    the global dependency and fails closed unless an identity verifies.
    """
    try:
        from .invites import invites_enabled
        return any((
            oidc_enabled(),
            invites_enabled(),
        ))
    except Exception:
        return True


def _invite_session_principal(request: Request) -> VerifiedPrincipal | None:
    """Locally-minted email-invite session identity (no-IdP mode).

    Returns a principal only when ``[dashboard] invites`` is enabled AND the
    ``mvk_session`` cookie verifies against the local session secret; any
    error means None — invites must never break the auth path. Imported
    lazily like :func:`_session_principal` so the gate takes no hard
    dependency on the invites module."""
    try:
        from .invites import local_session_principal
        return local_session_principal(request)
    except Exception:  # pragma: no cover - invites must never break auth
        return None


def _session_principal(request: Request) -> VerifiedPrincipal | None:
    """Built-in browser-login identity: a valid ``mvk_session`` cookie.

    Returns a principal only when the login flow is configured AND the cookie
    verifies (correct HMAC + unexpired). Absent/invalid -> ``None`` so the
    caller falls through to the OIDC bearer path unchanged. Imported lazily so
    the dashboard (and the existing bearer/proxy paths) don't take a hard
    dependency on the login module just to import this gate.
    """
    try:
        from .oidc_login import _principal_from_request_session
    except Exception:  # pragma: no cover - defensive; module should import
        return None
    return _principal_from_request_session(request)


def execution_user_id_from_request(request: Request) -> str | None:
    """Return the Maverick ``user_id`` for the authenticated HTTP principal.

    ``run_goal`` derives authorization principals as ``user:<user_id>``. The
    verified dashboard principal stores the raw subject on ``sub`` and exposes
    the full role-assignment key as ``principal``; pass only the subject so
    downstream checks evaluate the same ``user:<id>`` identity instead of
    falling back to ``user:local``.
    """
    principal = getattr(getattr(request, "state", None), "principal", None)
    sub = getattr(principal, "sub", "")
    if isinstance(sub, str) and sub:
        return sub
    principal_name = getattr(principal, "principal", "")
    if not isinstance(principal_name, str):
        return None
    if principal_name.startswith("user:") and len(principal_name) > len("user:"):
        return principal_name[len("user:"):]
    return None


# ---------------------------------------------------------------------------
# Matter ACL and legacy owner authorization
#
# The verified principal is the unit of ownership. Goals created by a
# caller are stamped with that caller's ``user:<sub>`` string; every read/mutate
# of an owned resource checks the caller against the owner.
#
# Load-bearing invariant: when auth is OFF there is no principal, so
# ``caller_principal`` returns None and EVERY check below is a no-op. The
# dashboard then behaves exactly as it did before this layer existed
# (single-user mode -- one operator owns everything).
# ---------------------------------------------------------------------------


def caller_principal(request: Request) -> str | None:
    """The full ``"user:<sub>"`` identity established on this request.

    ``None`` only means no principal was established; it is not proof that auth
    is off because self-authenticated routes also carry no dashboard identity.
    A verified static bearer is bound to its credential principal, while
    authorization helpers prove the remaining local exception through
    :func:`_authorization_principal` before granting operator semantics.
    """
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None:
        return None
    name = getattr(principal, "principal", "")
    return name if isinstance(name, str) and name else None


def _dashboard_token_authenticated(request) -> bool:
    """Whether a local auth-off development request has the legacy bearer.

    A shared token is not a human ethical-wall identity.  Secure firm mode,
    named-auth mode, remote peers, forwarded requests, and non-loopback Host
    headers all reject it even when the environment variable remains set.
    """
    import hmac

    if not legacy_static_bearer_allowed(request):
        return False
    expected = os.environ.get("MAVERICK_DASHBOARD_TOKEN", "")
    if not expected:
        return False
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def legacy_static_bearer_allowed(request) -> bool:
    """Static bearer compatibility is localhost + auth-off + insecure-dev only."""
    try:
        from maverick.security_defaults import secure_by_default

        if secure_by_default() or non_static_auth_configured():
            return False
    except Exception:
        return False
    if not os.environ.get("MAVERICK_DASHBOARD_TOKEN"):
        return False
    return _is_direct_loopback_request(request)


def _is_direct_loopback_request(request: Request) -> bool:
    """Accept only a direct loopback peer and loopback Host (plus TestClient)."""
    if any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")
    ):
        return False
    peer = request.client.host if getattr(request, "client", None) else ""
    host = str(getattr(getattr(request, "url", None), "hostname", "") or "")
    try:
        peer_ok = peer in {"localhost", "testclient"} or ipaddress.ip_address(peer).is_loopback
        host_ok = host in {"localhost", "testserver"} or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
    return peer_ok and host_ok


def _dashboard_token_principal(request) -> VerifiedPrincipal | None:
    """Return the stable identity represented by a valid static bearer.

    The token bytes are deliberately absent from both the subject and claims:
    they are authentication material, not identity data, and must never appear
    in ownership rows or logs.  Token rotation preserves the credential slot's
    identity while revoking possession of the old secret.
    """
    if not _dashboard_token_authenticated(request):
        return None
    return VerifiedPrincipal(
        sub=_STATIC_DASHBOARD_SUBJECT,
        issuer=_STATIC_DASHBOARD_ISSUER,
        audience=_STATIC_DASHBOARD_AUDIENCE,
        claims={"sub": _STATIC_DASHBOARD_SUBJECT, "via": "static-bearer"},
    )


def _establish_dashboard_token_principal(request) -> VerifiedPrincipal | None:
    """Authenticate and bind the static-bearer principal to request state."""
    principal = _dashboard_token_principal(request)
    if principal is None:
        return None
    request.state.principal = principal
    return principal


def auth_genuinely_off() -> bool:
    """Whether the dashboard has no configured authentication mechanism.

    This is deliberately stricter than ``caller_principal(...) is None``:
    Self-authenticated public links are exempt from browser/OIDC authentication,
    while a static dashboard token establishes a non-human credential principal.
    Legacy ownerless local operations are safe only on a truly unauthenticated local
    deployment. Any unreadable auth configuration denies that legacy exception.
    """
    if os.environ.get("MAVERICK_DASHBOARD_TOKEN") or _dashboard_require_auth_enabled():
        return False
    return not non_static_auth_configured()


def anonymous_local_access_allowed(request: Request) -> bool:
    """Whether principal-less loopback requests may act as the local operator.

    Having no configured identity mechanism is necessary but not sufficient:
    client-bound firm deployments explicitly disable ambient loopback
    trust. An unreadable binding policy denies the compatibility path.
    """
    if not auth_genuinely_off() or not _is_direct_loopback_request(request):
        return False
    try:
        from maverick.client import client_binding_enforced

        return not client_binding_enforced()
    except Exception:
        return False


def _authorization_principal(request: Request) -> str | None:
    """Return the caller or prove that principal-less local access is safe.

    ``None`` is an authorization grant only in genuine auth-off local mode. A
    valid static dashboard bearer is a named principal; a missing OIDC/session
    identity must never inherit the legacy local-admin path.
    """
    principal = caller_principal(request)
    if principal is not None:
        return principal
    static_principal = _establish_dashboard_token_principal(request)
    if static_principal is not None:
        return static_principal.principal
    if anonymous_local_access_allowed(request):
        return None
    raise HTTPException(status_code=401, detail="authenticated identity required")


def is_dashboard_admin(principal: str) -> bool:
    """True only for an exact principal in the bootstrap-admin roster."""
    if not principal:
        return False
    configured = (os.environ.get("MAVERICK_DASHBOARD_ADMINS") or "").strip()
    if configured:
        admins = {item.strip() for item in configured.split(",") if item.strip()}
        return principal in admins
    try:
        from maverick.config import config_source_errors, load_config

        if config_source_errors():
            return False
        dashboard = (load_config() or {}).get("dashboard", {}) or {}
        if not isinstance(dashboard, dict):
            return False
        raw = dashboard.get("admins", []) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return False
        return principal in {str(item).strip() for item in raw if str(item).strip()}
    except Exception:
        return False


def global_role_for_principal(principal: str | None) -> str | None:
    """Resolve the firm's explicit role, then its deny-by-default role."""
    if principal is None:
        return None
    if is_dashboard_admin(principal):
        return "admin"
    from . import rbac
    try:
        stored = rbac.get_stored_role(principal)
        if stored:
            return stored
        return rbac.default_role()
    except Exception:
        # A damaged role roster, group-membership source, or config default is
        # deny-all. Falling through to the historical operator default would
        # turn a policy outage into a privilege escalation.
        return None


def role_for_principal(principal: str | None) -> str | None:
    """The dashboard RBAC role for ``principal``.

    None when auth is off (no principal) -- the signal for every gate to
    disable itself (single-user mode). A config-pinned bootstrap admin
    (:func:`is_dashboard_admin`) is always ``"admin"`` and cannot be demoted via
    the store, so you can't lock yourself out. Otherwise the stored role, or the
    configured default (``viewer`` when omitted) for an authenticated user with
    no explicit assignment.
    """
    if principal is None:
        return None
    if is_dashboard_admin(principal):
        return "admin"
    return global_role_for_principal(principal)


def caller_role(request: Request) -> str | None:
    """The RBAC role of the current caller (None when auth is off)."""
    return role_for_principal(_authorization_principal(request))


def has_permission(request: Request, permission: str) -> bool:
    """Whether the caller may perform ``permission`` ("admin"/"operate"/"view").

    Auth off -> True (legacy single-user; the local operator owns everything).
    Otherwise gated by the caller's role.
    """
    principal = _authorization_principal(request)
    if principal is None:
        return True
    from . import rbac
    return permission in rbac.permissions_for(role_for_principal(principal))


def require_permission(request: Request, permission: str) -> None:
    """Raise ``HTTPException(403)`` unless the caller's role grants ``permission``."""
    if not has_permission(request, permission):
        raise HTTPException(status_code=403, detail="insufficient role for this action")


def qualified_attorney_policy() -> tuple[bool, frozenset[str]]:
    """Load the exact human counsel roster from ``[firm] qualified_attorneys``.

    RBAC administrators manage software; that role is not evidence of a law
    license.  Missing, malformed, shared-credential, or non-user entries make
    the policy invalid so authenticated firm startup and every legal decision
    fail closed.
    """
    try:
        from maverick.config import config_source_errors, load_global_config

        if config_source_errors(include_tenant=False):
            return False, frozenset()
        config = load_global_config() or {}
    except Exception:
        return False, frozenset()
    if not isinstance(config, dict):
        return False, frozenset()
    firm = config.get("firm")
    if not isinstance(firm, dict):
        return False, frozenset()
    raw = firm.get("qualified_attorneys")
    if not isinstance(raw, (list, tuple)) or not raw:
        return False, frozenset()
    members: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            return False, frozenset()
        principal = value.strip()
        if (
            not principal.startswith("user:")
            or len(principal) <= len("user:")
            or len(principal) > 256
            or principal == "user:dashboard-static-bearer"
        ):
            return False, frozenset()
        members.add(principal)
    return bool(members), frozenset(members)


def is_qualified_attorney_principal(principal: str | None) -> bool:
    valid, members = qualified_attorney_policy()
    return valid and isinstance(principal, str) and principal in members


def require_qualified_attorney(request: Request) -> str:
    """Require named verified identity, counsel roster, and signoff RBAC."""
    require_permission(request, "legal_signoff")
    verified = getattr(getattr(request, "state", None), "principal", None)
    principal = caller_principal(request)
    if (
        not isinstance(verified, VerifiedPrincipal)
        or not principal
        or getattr(verified, "issuer", "") == _STATIC_DASHBOARD_ISSUER
        or not is_qualified_attorney_principal(principal)
    ):
        raise HTTPException(
            status_code=403,
            detail="qualified counsel authorization required",
        )
    return principal


def has_global_permission(request: Request, permission: str) -> bool:
    """Whether the caller may perform a dashboard-wide control-plane action.

    Matter memberships are intentionally ignored; settings require the firm's
    explicit global role.
    """
    principal = _authorization_principal(request)
    if principal is None:
        return True
    from . import rbac

    return permission in rbac.permissions_for(global_role_for_principal(principal))


def require_global_permission(request: Request, permission: str) -> None:
    """Raise ``HTTPException(403)`` unless the global role grants permission."""
    if not has_global_permission(request, permission):
        raise HTTPException(status_code=403, detail="insufficient role for this action")


def caller_suites(request: Request) -> frozenset[str] | None:
    """The firm-only dashboard has one specialist suite: legal."""
    return frozenset({"legal"}) if _authorization_principal(request) else None


def suite_allowed(request: Request, suite: str | None) -> bool:
    """Whether the caller may use the retained legal ``suite``.

    ``suite`` is a suite key (``maverick.domain.suite_for``); ``None`` means a
    generic/legacy pack and is never admitted for named firm work."""
    allowed = caller_suites(request)
    if suite is None:
        return True
    return allowed is None or suite in allowed


def require_suite(request: Request, suite: str | None) -> None:
    """Raise ``HTTPException(403)`` unless the caller may use ``suite``."""
    if not suite_allowed(request, suite):
        raise HTTPException(
            status_code=403, detail="legal workflow access required")


def goal_owner_filter(request: Request) -> str | None:
    """The ``owner`` value to pass to ``WorldModel.list_goals``.

    Returns None for auth-off and for the historical administrator view of
    non-matter resources. This helper must never authorize a client-matter
    listing: goal/matter surfaces use :func:`list_accessible_goals` and
    :func:`list_accessible_projects`, where global RBAC is not an ethical-wall
    bypass.
    """
    principal = _authorization_principal(request)
    if principal is None or is_dashboard_admin(principal):
        return None
    return principal


def can_access_project_principal(
    principal: str | None, project_id: int, *, world=None,
) -> bool:
    """Whether an exact principal is an active member of a client matter.

    ``None`` preserves auth-off local operation.  No dashboard role, including
    admin, implies matter membership.  Policy/backend failures deny access.
    """
    if principal is None:
        return True
    try:
        if world is None:
            from ._shared import _world
            world = _world()
        return world.project_member_role(int(project_id), principal) is not None
    except Exception:
        return False


def can_access_project(request: Request, project_id: int, *, world=None) -> bool:
    """Request-bound wrapper for :func:`can_access_project_principal`."""
    return can_access_project_principal(
        _authorization_principal(request), project_id, world=world,
    )


def assert_project_access(request: Request, project_id: int, *, world=None) -> None:
    """Hide a matter from callers outside its ethical wall."""
    if not can_access_project(request, project_id, world=world):
        raise HTTPException(status_code=404, detail="no such project")


def list_accessible_projects(request: Request, world):
    """List exactly the matters visible to this request."""
    principal = _authorization_principal(request)
    if principal is None:
        return world.list_projects()
    return world.list_projects(principal=principal)


def list_accessible_goals(request: Request, world, **filters):
    """List goals with the same ethical-wall rule as direct object access."""
    principal = _authorization_principal(request)
    if principal is None:
        return world.list_goals(**filters)
    return world.list_goals(
        accessible_by=principal,
        include_all_unfiled=is_dashboard_admin(principal),
        **filters,
    )


def search_accessible_goals(request: Request, world, query: str, **filters):
    """Search only goal rows the caller could fetch directly."""
    principal = _authorization_principal(request)
    if principal is None:
        return world.search_goals(query, **filters)
    return world.search_goals(
        query,
        accessible_by=principal,
        include_all_unfiled=is_dashboard_admin(principal),
        **filters,
    )


def list_accessible_episodes(request: Request, world, **filters):
    """List run-cost rows without crossing a matter membership boundary."""
    principal = _authorization_principal(request)
    if principal is None:
        return world.list_episodes(**filters)
    return world.list_episodes(
        accessible_by=principal,
        include_all_unfiled=is_dashboard_admin(principal),
        **filters,
    )


def total_accessible_spend(request: Request, world) -> dict[str, float]:
    """Aggregate spend only over runs visible to this request."""
    principal = _authorization_principal(request)
    if principal is None:
        return world.total_spend()
    return world.total_spend(
        accessible_by=principal,
        include_all_unfiled=is_dashboard_admin(principal),
    )


def can_access_goal_principal(principal: str | None, goal, *, world=None) -> bool:
    """Whether ``principal`` may read/mutate ``goal``.

    ``None`` means auth is off and preserves the dashboard's historical
    single-user behavior. A matter-bound goal requires an exact active
    membership, regardless of ownership or global admin role. Only unfiled
    legacy goals retain the owner/admin compatibility policy.
    """
    if principal is None:
        return True
    project_id = getattr(goal, "project_id", None)
    if project_id is not None:
        return can_access_project_principal(principal, project_id, world=world)
    if is_dashboard_admin(principal):
        return True
    return getattr(goal, "owner", "") == principal


def can_access_goal(request: Request, goal) -> bool:
    """Whether the caller may read/mutate ``goal``.

    Allowed iff auth is off (no principal), the caller is an admin, or the
    caller owns the goal. Legacy ``owner == ""`` goals (created before this
    layer) are therefore reachable only by the
    no-auth/admin paths, never by a different authenticated user.
    """
    return can_access_goal_principal(_authorization_principal(request), goal)


def assert_goal_access(request: Request, goal) -> None:
    """Raise ``HTTPException(404)`` if the caller may not touch ``goal``.

    404 (not 403) on denial so a cross-matter probe can't distinguish "exists
    but forbidden" from "does not exist". Callers fetch the goal first (a real
    miss is its own 404) and then gate on this.
    """
    if not can_access_goal(request, goal):
        raise HTTPException(status_code=404, detail="no such goal")


def _authenticate_oidc_bearer(request: Request, token: str) -> VerifiedPrincipal:
    """Verify an explicit HTTP OIDC bearer and establish its request scope."""
    try:
        principal = verify_oidc_token(token)
    except OIDCError as exc:
        # Opaque 401: never leak which check failed (expiry vs. signature vs.
        # audience) to an unauthenticated caller.
        raise HTTPException(
            status_code=401,
            detail="invalid OIDC token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # A bearer issued before the principal's revocation epoch (logout-all or
    # firm offboarding) is rejected even if its IdP signature still verifies.
    from .session_revocation import is_revoked
    if is_revoked(principal.sub, principal.claims.get("iat")):
        raise HTTPException(
            status_code=401, detail="invalid OIDC token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    request.state.principal = principal
    return principal


def require_principal(request: Request) -> VerifiedPrincipal | None:
    """Authenticate every non-exempt HTTP request.

    An explicit static/OIDC bearer takes precedence so an invalid credential
    cannot borrow ambient authority. Without one, a verified browser or invite
    session may establish identity. Missing identity is
    allowed only in genuine local auth-off mode. Probe,
    login-bootstrap and public-share paths carry their own narrower credential
    or are intentionally public bootstrap routes.
    """
    # An explicit bearer is non-ambient API authority. Resolve it before
    # browser sessions so the middleware's CSRF exemption cannot be
    # triggered by an invalid header and then satisfied by an ambient cookie.
    # Self-authenticated routes keep their own credential contract. A valid
    # static dashboard bearer has its own stable principal; any other bearer
    # must verify as OIDC or fail instead of falling back to ambient identity.
    self_authenticated = _self_authenticated_path(request.url.path)
    authorization = request.headers.get("authorization", "")
    if not self_authenticated and authorization:
        static_principal = _establish_dashboard_token_principal(request)
        if static_principal is not None:
            return static_principal
        explicit_token = _bearer_token(request)
        if explicit_token and oidc_enabled():
            return _authenticate_oidc_bearer(request, explicit_token)
        raise HTTPException(
            status_code=401,
            detail="invalid bearer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # OIDC and invite login mint signed browser sessions. Check it before any
    # anonymous compatibility path.
    sp = _session_principal(request)
    if sp is not None:
        request.state.principal = sp
        return sp

    # Email-invite local sessions use the same cookie name but their own issuer.
    # Accept only a fully verified, unrevoked session before considering any
    # anonymous compatibility path.
    lp = _invite_session_principal(request)
    if lp is not None:
        request.state.principal = lp
        return lp

    if self_authenticated:
        return None

    if not oidc_enabled():
        # ``None`` may mean local-admin only when *every* auth mechanism is off.
        # Invites, require_auth, or an unreadable auth config all
        # turn a missing/invalid identity into a 401 at the global dependency,
        # including routes that happen not to call an RBAC helper themselves.
        if not anonymous_local_access_allowed(request):
            raise HTTPException(status_code=401, detail="authenticated identity required")
        return None

    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="OIDC bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _authenticate_oidc_bearer(request, token)


async def require_principal_in_request_context(
    request: Request,
) -> VerifiedPrincipal | None:
    """Run authentication in the request task before any route handler."""
    return require_principal(request)
