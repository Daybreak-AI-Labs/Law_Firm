"""Dashboard authentication and authorization boundary.

The global FastAPI dependency composes trusted-proxy identity, OIDC bearer and
browser sessions, SAML sessions, local invite sessions, and the static dashboard
bearer. Once any mechanism is configured, missing or invalid identity fails
closed. Principal-less local-operator compatibility is available only when auth
is genuinely off and client binding does not forbid ambient loopback trust.

Importing this module never hard-requires PyJWT: ``maverick.oidc`` lazy-imports
it only when an OIDC token is actually verified.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

from fastapi import HTTPException, Request, WebSocket
from maverick.oidc import (
    OIDCError,
    VerifiedPrincipal,
    oidc_enabled,
    verify_oidc_token,
)
from maverick.proxy_auth import (
    principal_from_proxy,
    proxy_auth_enabled,
    proxy_header_name,
    proxy_trusts,
)

# Arm the SCIM-group grant resolver (registered on import with
# maverick.suite_grants) so the HTTP gates and the kernel deploy/dispatch
# gates both see group-derived department grants from the first request.
from . import scim_groups  # noqa: F401

# Probe/discovery endpoints that must answer without a bearer even when OIDC is
# on (load balancers and k8s liveness/readiness probes, plus the OpenAPI docs,
# can't present an ID token). Mirrors the dashboard's existing bearer-auth
# exemptions in ``app._AUTH_EXEMPT`` -- the HMAC-signed webhooks are NOT listed
# here because they carry their own credential and are gated separately.
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

SELF_AUTH_WEBHOOK_PATHS = frozenset(
    {
        "/webhook/start",
        "/webhook/run",
        "/webhook/linear",
        "/webhook/jira",
        "/webhook/github",
        "/webhook/gitlab",
    }
)

_SELF_AUTH_EXEMPT_PATHS = _OIDC_EXEMPT_PATHS | frozenset(
    {"/static/daybreak-logo.jpg"}
) | SELF_AUTH_WEBHOOK_PATHS


def _self_authenticated_path(path: str) -> bool:
    """Whether a route authenticates itself or bootstraps authentication."""
    return path in _SELF_AUTH_EXEMPT_PATHS or path.startswith(
        ("/share/", "/scim/", "/form/", "/saml/", "/auth/invite/")
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


def _saml_policy_configured() -> bool:
    """Whether config contains any SAML identity-policy surface.

    ``saml_enabled`` intentionally reports only a *complete* SP setup so its
    routes can remain inert while an operator fills in the template.  That is
    the wrong predicate for the local-admin compatibility exception: once a
    non-empty ``[auth.saml]`` table exists, incomplete or invalid policy must
    lock anonymous access down instead of silently behaving as auth-off.
    """
    try:
        from maverick.config import config_source_errors, load_config

        config = load_config() or {}
        if config_source_errors():
            return True
    except Exception:
        return True
    if not isinstance(config, dict):
        return True
    if "auth" not in config:
        return False
    auth = config["auth"]
    if not isinstance(auth, dict):
        return True
    if "saml" not in auth:
        return False
    saml = auth["saml"]
    return bool(saml) if isinstance(saml, dict) else True


def non_static_auth_configured() -> bool:
    """Whether an identity mechanism besides the static bearer is configured.

    The static operator token and enterprise/browser identity mechanisms are
    alternatives, not mutually exclusive deployment modes. Configuration
    uncertainty reports identity policy as configured so the request reaches
    the global dependency and fails closed unless an identity verifies.
    """
    try:
        from .invites import invites_enabled
        from .saml import saml_enabled

        return any((
            oidc_enabled(),
            proxy_auth_enabled(),
            invites_enabled(),
            saml_enabled(),
            _saml_policy_configured(),
        ))
    except Exception:
        return True


def _proxy_principal(request: Request) -> VerifiedPrincipal | None:
    """Reverse-proxy SSO: a principal from a forwarded identity header.

    Honored ONLY when proxy auth is enabled AND the request's network peer is a
    trusted upstream (anti-spoofing -- see :mod:`maverick.proxy_auth`). Returns
    ``None`` (fall through to OIDC/loopback) when not applicable.
    """
    if not proxy_auth_enabled():
        return None
    client_host = request.client.host if request.client else ""
    if not proxy_trusts(client_host):
        return None
    value = request.headers.get(proxy_header_name(), "") or ""
    if not value:
        return None
    try:
        return principal_from_proxy(value)
    except ValueError:
        return None


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
# Owner-scoped multi-tenant authorization (stage 2)
#
# The verified principal is the unit of ownership. Goals/fleets created by a
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

    ``None`` only means no end-user principal was established; it is not proof
    that auth is off because static-token and self-authenticated requests also
    have no user subject. Authorization helpers must prove the local exception
    through :func:`_authorization_principal` before granting operator semantics.
    """
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None:
        return None
    name = getattr(principal, "principal", "")
    return name if isinstance(name, str) and name else None


def _dashboard_token_authenticated(request) -> bool:
    """Whether this request carries the configured static dashboard bearer."""
    import hmac

    expected = os.environ.get("MAVERICK_DASHBOARD_TOKEN", "")
    if not expected:
        return False
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def auth_genuinely_off() -> bool:
    """Whether the dashboard has no configured authentication mechanism.

    This is deliberately stricter than ``caller_principal(...) is None``:
    HMAC webhook requests are exempt from browser/OIDC authentication, and a
    static dashboard token authenticates the request without establishing an
    end-user principal. Legacy ownerless automations are safe only on a truly
    unauthenticated local deployment. Any unreadable auth configuration denies
    that legacy exception.
    """
    if os.environ.get("MAVERICK_DASHBOARD_TOKEN") or _dashboard_require_auth_enabled():
        return False
    return not non_static_auth_configured()


def anonymous_local_access_allowed() -> bool:
    """Whether principal-less loopback requests may act as the local operator.

    Having no configured identity mechanism is necessary but not sufficient:
    client-bound/enterprise deployments explicitly disable ambient loopback
    trust. An unreadable binding policy denies the compatibility path.
    """
    if not auth_genuinely_off():
        return False
    try:
        from maverick.client import client_binding_enforced

        return not client_binding_enforced()
    except Exception:
        return False


def _authorization_principal(request: Request) -> str | None:
    """Return the caller or prove that principal-less local access is safe.

    ``None`` is an authorization grant only in genuine auth-off local mode or
    after the static dashboard bearer has authenticated the request. A missing
    proxy/OIDC/session identity must never inherit the legacy local-admin path.
    """
    principal = caller_principal(request)
    if principal is not None:
        return principal
    if anonymous_local_access_allowed() or _dashboard_token_authenticated(request):
        return None
    raise HTTPException(status_code=401, detail="authenticated identity required")


def durable_automation_owner(request: Request) -> str:
    """Stable owner for a trigger/import that will execute without a request.

    Principal-bearing auth uses that exact principal. Auth-off preserves the
    historical ownerless local mode. A configured static dashboard token has
    no user subject, so bind it to the explicit local execution identity rather
    than persisting an ownerless row that would later bypass revocation checks.
    """
    principal = caller_principal(request)
    if principal:
        return principal
    return "" if anonymous_local_access_allowed() else "user:local"


def _pin_tenant_from_principal(request: Request, principal: VerifiedPrincipal) -> None:
    """Pin per-user tenant state once the verified principal is known.

    HTTP middleware runs before FastAPI dependencies populate
    ``request.state.principal``, so tenant pinning must happen here, in the
    authentication dependency, immediately after a principal is established and
    before route handlers choose tenant-scoped world state. The reset token is
    stored on request state for the outer middleware to clean up after the
    response. If an opted-in isolation policy cannot be resolved, authentication
    is refused instead of silently serving the caller from the shared root.
    """
    try:
        from maverick.paths import set_tenant, tenant_by_user_enabled

        if not tenant_by_user_enabled():
            return
        name = getattr(principal, "principal", "")
        if not isinstance(name, str):
            name = ""
        if not name or getattr(request.state, "tenant_pin_token", None) is not None:
            return
        request.state.tenant_pin_token = set_tenant(f"api:{name}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="tenant isolation policy unavailable",
        ) from exc


def is_dashboard_admin(principal: str) -> bool:
    """True iff ``principal`` is a configured (bootstrap) dashboard admin.

    Admins bypass owner scoping AND department scoping (they see and control
    every goal/fleet/department). The roster is the ``[dashboard] admins`` list
    in ``~/.maverick/config.toml`` with an optional ``MAVERICK_DASHBOARD_ADMINS``
    (comma-separated) env override; comparison is exact against the full
    ``"user:<sub>"`` form.

    Single source of truth: delegates to :func:`maverick.suite_grants.is_admin_principal`
    so the dashboard HTTP gates and the kernel deploy/dispatch gates can never
    disagree about who bypasses scoping. NOTE this is the *config-pinned*
    roster, not the effective RBAC role — a user assigned ``admin`` via the
    Users page or a group mapping holds admin *permissions* but remains
    department-scoped (an admin-of-finance administers finance); only the
    config roster is the "never scoped" break-glass set.
    """
    from maverick.suite_grants import is_admin_principal
    return is_admin_principal(principal)


def global_role_for_principal(principal: str | None) -> str | None:
    """The principal's dashboard-wide RBAC role, ignoring tenant memberships.

    Resolution: explicit stored assignment, then the role mapped to the user's
    SCIM groups (``[dashboard] group_roles`` — team membership in the IdP), then
    the configured default. Explicit always beats derived, so an admin can still
    pin an individual without fighting the group mapping."""
    if principal is None:
        return None
    if is_dashboard_admin(principal):
        return "admin"
    from . import rbac
    try:
        stored = rbac.get_stored_role(principal)
        if stored:
            return stored
        from . import scim_groups
        group_role = scim_groups.role_for_principal(principal)
        if group_role:
            return group_role
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
    configured default (``operator``) for an authenticated user with no explicit
    assignment.
    """
    if principal is None:
        return None
    if is_dashboard_admin(principal):
        return "admin"
    from . import rbac
    # Per-tenant membership wins over the global role, but only for the active
    # tenant. No active tenant or no membership -> the global behaviour below,
    # unchanged. Bootstrap admin (above) always wins, so this can't lock admins
    # out of a tenant.
    try:
        from maverick.paths import current_tenant_id
        tid = current_tenant_id()
        if tid:
            tenant_role = rbac.get_tenant_role(tid, principal)
            if tenant_role:
                return tenant_role
    except Exception:
        # Never replace an unreadable tenant-local demotion with the broader
        # global/default role.
        return None
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


def has_global_permission(request: Request, permission: str) -> bool:
    """Whether the caller may perform a dashboard-wide control-plane action.

    Tenant memberships are intentionally ignored so a tenant-local admin cannot
    satisfy global admin gates for other tenants or dashboard settings.
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
    """The department suites the caller may use, or ``None`` for unrestricted.

    Job-function scoping (``maverick_dashboard.suite_grants``), orthogonal to
    the privilege role: it decides WHICH specialists a user works with, not how
    much they may do. ``None`` — auth off, a dashboard admin, or a user with no
    grant (and no configured default) — disables every suite gate, preserving
    single-user behaviour."""
    principal = _authorization_principal(request)
    if principal is None or is_dashboard_admin(principal):
        return None
    from . import suite_grants
    return suite_grants.granted_suites(principal)


class AutomationAuthorizationError(RuntimeError):
    """A request-less automation owner no longer has execution authority."""


def stored_automation_identity(
    owner: str,
) -> tuple[str, str | None, frozenset[str] | None]:
    """Revalidate a durable trigger owner and return its current run identity.

    Trigger possession is not a perpetual grant. Every request-less fire checks
    SCIM lifecycle, current RBAC ``operate`` permission, and the current suite
    floor. Legacy ownerless rows execute only in genuinely auth-off local mode.
    """
    owner = str(owner or "").strip()
    if not owner:
        if anonymous_local_access_allowed():
            return "", None, None
        raise AutomationAuthorizationError("automation owner is unavailable")
    try:
        from .scim_groups import active_for_principal

        if active_for_principal(owner) is False:
            raise AutomationAuthorizationError("automation owner is unavailable")
        from .rbac import permissions_for

        if "operate" not in permissions_for(role_for_principal(owner)):
            raise AutomationAuthorizationError("automation owner is unavailable")
        suites = None
        if not is_dashboard_admin(owner):
            from .suite_grants import granted_suites

            suites = granted_suites(owner)
    except AutomationAuthorizationError:
        raise
    except Exception as exc:
        raise AutomationAuthorizationError(
            "automation authorization policy is unavailable") from exc
    user_id = owner[len("user:"):] if owner.startswith("user:") else ""
    return owner, (user_id or None), suites


def suite_allowed(request: Request, suite: str | None) -> bool:
    """Whether the caller may use department ``suite``.

    ``suite`` is a suite key (``maverick.domain.suite_for``); ``None`` means a
    generic/legacy pack that belongs to no department and is never scoped."""
    allowed = caller_suites(request)
    if suite is None:
        return True
    return allowed is None or suite in allowed


def scope_to_suites(request: Request, items, *, suite_of, keep_none: bool = True):
    """Filter ``items`` to the caller's granted departments (job-function scope).

    ``suite_of(item)`` returns the item's suite key, or ``None`` for a generic /
    department-less item. Unscoped callers (auth off, admin, no grant) get every
    item back unchanged; otherwise an item is kept when its suite is granted, or
    when it is generic and ``keep_none`` is set (packs outside every department
    stay visible). One helper so every catalog surface scopes identically —
    the inline per-surface copies drifted (some kept generics, some didn't)."""
    allowed = caller_suites(request)
    if allowed is None:
        return list(items)
    return [it for it in items
            if (suite_of(it) is None and keep_none) or suite_of(it) in allowed]


def require_suite(request: Request, suite: str | None) -> None:
    """Raise ``HTTPException(403)`` unless the caller may use ``suite``."""
    if not suite_allowed(request, suite):
        raise HTTPException(
            status_code=403, detail="insufficient department access for this action")


def goal_owner_filter(request: Request) -> str | None:
    """The ``owner`` value to pass to ``WorldModel.list_goals``.

    Returns None (no owner filter -> all goals) when the caller is unauthenticated
    (auth off) or an admin; otherwise the caller's principal so the listing is
    scoped to the rows they own. ``owner=None`` is the historical default, so the
    auth-off path is unchanged.
    """
    principal = _authorization_principal(request)
    if principal is None or is_dashboard_admin(principal):
        return None
    return principal


def can_access_goal_principal(principal: str | None, goal) -> bool:
    """Whether ``principal`` may read/mutate ``goal``.

    ``None`` means auth is off and preserves the dashboard's historical
    single-user behavior. Authenticated non-admin callers may access only goals
    stamped with their exact owner principal.
    """
    if principal is None:
        return True
    if is_dashboard_admin(principal):
        return True
    return getattr(goal, "owner", "") == principal


def can_access_goal(request: Request, goal) -> bool:
    """Whether the caller may read/mutate ``goal``.

    Allowed iff auth is off (no principal), the caller is an admin, or the
    caller owns the goal. Legacy ``owner == ""`` goals (created before this
    layer, or by an external/webhook path) are therefore reachable only by the
    no-auth/admin paths, never by a different authenticated user.
    """
    return can_access_goal_principal(_authorization_principal(request), goal)


def assert_goal_access(request: Request, goal) -> None:
    """Raise ``HTTPException(404)`` if the caller may not touch ``goal``.

    404 (not 403) on denial so a cross-tenant probe can't distinguish "exists
    but forbidden" from "does not exist". Callers fetch the goal first (a real
    miss is its own 404) and then gate on this.
    """
    if not can_access_goal(request, goal):
        raise HTTPException(status_code=404, detail="no such goal")


def _enforce_scim_active(principal: VerifiedPrincipal) -> None:
    """Reject a SCIM-matched principal that has been deprovisioned.

    Revocation epochs terminate credentials minted before deprovisioning, but
    an IdP that mistakenly continues issuing fresh tokens could otherwise give
    the inactive user the dashboard's global default role. The live SCIM roster
    is therefore an authentication gate as well as a group-grant source.
    """
    try:
        from .scim_groups import active_for_principal

        active = active_for_principal(principal.principal)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="identity lifecycle policy unavailable",
        ) from exc
    if active is False:
        raise HTTPException(status_code=401, detail="invalid identity")


def _require_websocket_principal(websocket) -> VerifiedPrincipal | None:
    """Authenticate a WebSocket handshake with the configured mechanisms."""
    if websocket is None:
        if anonymous_local_access_allowed():
            return None
        raise HTTPException(status_code=401, detail="authenticated identity required")

    # Explicit credentials take precedence over ambient proxy/cookie identity.
    # Otherwise an invalid bearer could be ignored and the handshake satisfied
    # by a browser session riding on the same request.
    authorization = websocket.headers.get("authorization", "")
    if authorization:
        if _dashboard_token_authenticated(websocket):
            return None
        ws_token = _bearer_token(websocket)
        if not ws_token or not oidc_enabled():
            raise HTTPException(status_code=401, detail="invalid bearer credential")
        try:
            ws_principal = verify_oidc_token(ws_token)
        except OIDCError as exc:
            raise HTTPException(status_code=401, detail="invalid OIDC token") from exc
        from .session_revocation import is_revoked
        if is_revoked(ws_principal.sub, ws_principal.claims.get("iat")):
            raise HTTPException(status_code=401, detail="invalid OIDC token")
        _enforce_scim_active(ws_principal)
        _pin_tenant_from_principal(websocket, ws_principal)
        return ws_principal

    # Proxy/session identities are valid WebSocket credentials too. Their
    # browser Origin is checked by ``websocket_authorized`` before accept.
    pp = _proxy_principal(websocket)
    if pp is not None:
        _enforce_scim_active(pp)
        _pin_tenant_from_principal(websocket, pp)
        return pp
    sp = _session_principal(websocket)
    if sp is not None:
        _enforce_scim_active(sp)
        _pin_tenant_from_principal(websocket, sp)
        return sp
    lp = _invite_session_principal(websocket)
    if lp is not None:
        _enforce_scim_active(lp)
        _pin_tenant_from_principal(websocket, lp)
        return lp

    if oidc_enabled():
        raise HTTPException(status_code=401, detail="OIDC bearer token required")
    if not anonymous_local_access_allowed():
        raise HTTPException(status_code=401, detail="authenticated identity required")
    return None


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
    # Revocation: a bearer issued before the principal's revocation epoch
    # ("log out everywhere" / SCIM deprovision) is rejected even if it verifies.
    from .session_revocation import is_revoked
    if is_revoked(principal.sub, principal.claims.get("iat")):
        raise HTTPException(
            status_code=401, detail="invalid OIDC token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    _enforce_scim_active(principal)
    request.state.principal = principal
    _pin_tenant_from_principal(request, principal)
    return principal


def require_principal(
    request: Request = None,  # type: ignore[assignment]
    websocket: WebSocket = None,  # type: ignore[assignment,name-defined]
) -> VerifiedPrincipal | None:
    """Authenticate every non-exempt HTTP or WebSocket request.

    An explicit static/OIDC bearer takes precedence so an invalid credential
    cannot borrow ambient authority. Without one, trusted proxy identity is
    followed by a verified browser or invite session. Missing identity is
    allowed only in genuine local auth-off mode. Probe,
    discovery, login-bootstrap, share, SCIM, SAML, form, and webhook paths carry
    their own narrower credential or are intentionally public bootstrap routes.
    """
    if request is None:
        return _require_websocket_principal(websocket)

    # An explicit bearer is non-ambient API authority. Resolve it before
    # proxy/browser sessions so the middleware's CSRF exemption cannot be
    # triggered by an invalid header and then satisfied by an ambient cookie.
    # Self-authenticated routes keep their own credential contract. A valid
    # static dashboard bearer has local-operator semantics; any other bearer
    # must verify as OIDC or fail instead of falling back to ambient identity.
    self_authenticated = _self_authenticated_path(request.url.path)
    authorization = request.headers.get("authorization", "")
    if not self_authenticated and authorization:
        if _dashboard_token_authenticated(request):
            return None
        explicit_token = _bearer_token(request)
        if explicit_token and oidc_enabled():
            return _authenticate_oidc_bearer(request, explicit_token)
        raise HTTPException(
            status_code=401,
            detail="invalid bearer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    pp = _proxy_principal(request)
    if pp is not None:
        _enforce_scim_active(pp)
        request.state.principal = pp
        _pin_tenant_from_principal(request, pp)
        return pp

    # OIDC and SAML mint the same signed browser session. Check it before the
    # protocol switches so a SAML-only deployment (and a proxy+session fallback)
    # cannot degrade to an anonymous local operator.
    sp = _session_principal(request)
    if sp is not None:
        _enforce_scim_active(sp)
        request.state.principal = sp
        _pin_tenant_from_principal(request, sp)
        return sp

    # Email-invite local sessions use the same cookie name but their own issuer.
    # Accept only a fully verified, unrevoked session before considering any
    # anonymous compatibility path.
    lp = _invite_session_principal(request)
    if lp is not None:
        _enforce_scim_active(lp)
        request.state.principal = lp
        _pin_tenant_from_principal(request, lp)
        return lp

    if self_authenticated:
        return None

    if not oidc_enabled():
        # ``None`` may mean local-admin only when *every* auth mechanism is off.
        # Proxy, SAML, invites, require_auth, or an unreadable auth config all
        # turn a missing/invalid identity into a 401 at the global dependency,
        # including routes that happen not to call an RBAC helper themselves.
        if not anonymous_local_access_allowed():
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
    request: Request = None,  # type: ignore[assignment]
    websocket: WebSocket = None,  # type: ignore[assignment,name-defined]
) -> VerifiedPrincipal | None:
    """Run authentication in the request task so tenant ContextVars propagate.

    FastAPI executes synchronous dependencies in a worker thread. Calling
    :func:`set_tenant` there records a pin, but that worker's ContextVar value
    is not visible to the async route or its child worker calls. The app-level
    dependency therefore uses this async adapter; direct and WebSocket callers
    can continue using the synchronous implementation above.
    """
    return require_principal(request=request, websocket=websocket)


async def require_websocket_principal_in_context(
    websocket: WebSocket,
):
    """Authenticate and scope a WebSocket, then always reset its tenant pin.

    WebSocket routes do not pass through HTTP tenant cleanup middleware. This
    yield dependency keeps the ContextVar in the async handler task (so world
    selection and threadpool calls inherit it) and releases it on every normal,
    rejected, disconnected, or exceptional exit.
    """
    try:
        principal = _require_websocket_principal(websocket)
        yield principal
    finally:
        token = getattr(websocket.state, "tenant_pin_token", None)
        if token is not None:
            try:
                from maverick.paths import reset_tenant

                reset_tenant(token)
            finally:
                websocket.state.tenant_pin_token = None


def websocket_caller_principal(principal: VerifiedPrincipal | None) -> str | None:
    """Return the owner principal established for a WebSocket connection.

    The app-level dependency returns a ``VerifiedPrincipal`` for OIDC-authenticated
    WebSockets but has no ``Request.state`` to persist it on. WebSocket handlers
    pass that dependency result here so owner checks use the same
    ``user:<sub>`` string as HTTP routes. ``None`` preserves auth-off behavior.
    """
    if principal is None:
        return None
    name = getattr(principal, "principal", "")
    return name if isinstance(name, str) and name else None


def _websocket_same_origin(websocket) -> bool:
    """Require a browser WebSocket Origin matching the requested Host."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    return urlparse(origin).netloc == host


def _websocket_loopback_request_host(websocket) -> bool:
    """True when a WebSocket's user-controlled Host names loopback.

    A loopback socket peer is insufficient in auth-off mode: DNS rebinding can
    connect to 127.0.0.1 while preserving an attacker-controlled Host/Origin
    pair that passes the same-origin comparison. Parse the Host independently
    and require the same loopback boundary as HTTP requests.
    """
    raw_host = str(websocket.headers.get("host") or "")
    if (
        not raw_host
        or raw_host != raw_host.strip()
        or any(char in raw_host for char in "/\\@,?#")
    ):
        return False
    try:
        parsed = urlparse(f"//{raw_host}")
        # Accessing ``port`` validates malformed/non-numeric/out-of-range ports.
        _ = parsed.port
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if host in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def websocket_authorized(
    websocket,
    principal: VerifiedPrincipal | None = None,
) -> bool:
    """Auth gate for WebSocket endpoints (the HTTP middleware doesn't run
    for WS connections).

    Verified proxy/cookie principals additionally require same-origin browser
    handshakes (CSWSH defense). Explicit OIDC/static bearers do not rely on
    browser ambient authority. Principal-less handshakes are same-origin,
    loopback-only, and available solely in genuine local auth-off mode.
    """
    import hmac as _hmac
    import os as _os

    if principal is not None:
        via = str((principal.claims or {}).get("via") or "")
        # Cookie/proxy credentials ride a browser handshake and therefore need
        # a same-origin check (CSWSH defense). A verified OIDC bearer is explicit
        # request authentication and does not require a browser Origin header.
        return _websocket_same_origin(websocket) if via in {
            "session", "invite", "proxy",
        } else True
    expected = _os.environ.get("MAVERICK_DASHBOARD_TOKEN")
    if expected:
        auth = websocket.headers.get("authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        return bool(supplied) and _hmac.compare_digest(expected.encode(), supplied.encode())
    if not anonymous_local_access_allowed():
        return False
    from .app import _PROXY_FORWARD_HEADERS, _is_loopback_client
    host = websocket.client.host if websocket.client else ""
    proxied = any(websocket.headers.get(h) for h in _PROXY_FORWARD_HEADERS)
    return (
        _is_loopback_client(host)
        and _websocket_loopback_request_host(websocket)
        and not proxied
        and _websocket_same_origin(websocket)
    )
