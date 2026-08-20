"""Law-firm dashboard and retained REST surface."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from maverick.runtime_overrides import RuntimeOverridesSecurityError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth_metrics as _auth_metrics
from ._shared import (
    _any_provider_key_set,
    _world,
    require_provider_or_400,
)
from ._shared import _world_cache as _world_cache  # re-export: tests clear app._world_cache
from .api import router as api_router
from .auth import (
    assert_goal_access,
    assert_project_access,
    caller_principal,
    caller_role,
    caller_suites,
    execution_user_id_from_request,
    goal_owner_filter,
    has_permission,
    is_qualified_attorney_principal,
    list_accessible_goals,
    list_accessible_projects,
    non_static_auth_configured,
    qualified_attorney_policy,
    require_permission,
    require_principal_in_request_context,
)
from .health_routes import (
    health_response as _health_response,
)
from .health_routes import (
    metrics_response as _metrics_response,
)
from .health_routes import (
    readiness_response as _readiness_response,
)
from .law_api import router as law_api_router
from .oidc_login import router as oidc_login_router

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def _nav_context(request: Request) -> dict:
    try:
        role = caller_role(request)
    except Exception:  # pragma: no cover -- nav must never break a render
        role = None
    work = [
        {"label": "Matters", "href": "/projects"},
        {"label": "New work", "href": "/chat"},
        {"label": "Goals", "href": "/goals"},
        {"label": "Deliverables", "href": "/deliverables"},
    ]
    controls = []
    if role in {None, "admin", "auditor"}:
        controls.append({"label": "Audit", "href": "/audit"})
    if role in {None, "admin"}:
        controls.extend([
            {"label": "Settings", "href": "/settings"},
            {"label": "Users", "href": "/users"},
        ])
    groups = [{"label": "Firm", "links": work}]
    if controls:
        groups.append({"label": "Controls", "links": controls})
    show_settings = role in {None, "admin"}
    return {"nav_groups": groups, "show_settings_link": show_settings}

templates.context_processors.append(_nav_context)

templates.env.globals.setdefault("nav_groups", [])

def _require_auth_enabled() -> bool:
    """Opt-in guard: refuse to serve the dashboard with no auth configured.
    ``MAVERICK_DASHBOARD_REQUIRE_AUTH`` env wins over ``[dashboard] require_auth``;
    off by default so explicit localhost development is unchanged."""
    env = os.environ.get("MAVERICK_DASHBOARD_REQUIRE_AUTH", "").strip().lower()
    try:
        from maverick.config import config_source_errors, load_config

        config = load_config() or {}
        if config_source_errors():
            return True
    except Exception:
        # A malformed/unreadable config may conceal ``require_auth = true``.
        # Treat ambiguity as required so startup cannot silently enable local
        # administrator access.
        return True
    if not isinstance(config, dict):
        return True
    if env:
        if env in {"1", "true", "yes", "on"}:
            return True
        if env in {"0", "false", "no", "off"}:
            return False
        return True
    if "dashboard" not in config:
        dashboard = {}
    else:
        dashboard = config["dashboard"]
    if not isinstance(dashboard, dict):
        return True
    configured = dashboard.get("require_auth", False)
    return configured if isinstance(configured, bool) else True

def _assert_dashboard_auth_configured() -> None:
    """When ``require_auth`` is set, refuse to boot if NO auth mechanism is
    configured (OIDC or local invites), so an operator
    who asked for auth can never accidentally serve the (loopback) control
    surface unauthenticated. No-op unless ``require_auth`` is explicitly on, so
    a fresh localhost install is never locked out."""
    if not _require_auth_enabled():
        return
    try:
        from maverick.oidc import oidc_enabled
        if oidc_enabled():
            return
    except Exception:  # pragma: no cover
        pass
    try:
        # Email-invite local sessions count as a configured mechanism: with
        # require_auth on, only a verified invite-minted session (or one of the
        # modes above) passes the middleware — everything else 401s.
        from .invites import invites_enabled
        if invites_enabled():
            return
    except Exception:  # pragma: no cover
        pass
    raise RuntimeError(
        "[dashboard] require_auth is set, but no auth mechanism is configured. "
        "Enable OIDC ([auth.oidc]) or email invites ([dashboard] invites) -- "
        "refusing to serve the dashboard "
        "unauthenticated."
    )


def _secure_default_policy_valid() -> bool:
    """Reject malformed security policy instead of accepting its fallback."""
    from maverick.security_defaults import _FALSE, _TRUE

    raw = os.environ.get("MAVERICK_SECURE_DEFAULT")
    if raw is not None and raw.strip() and raw.strip().lower() not in _TRUE | _FALSE:
        return False
    try:
        from maverick.config import config_source_errors, load_global_config

        if config_source_errors(include_tenant=False):
            return False
        config = load_global_config() or {}
    except Exception:
        return False
    if not isinstance(config, dict):
        return False
    security = config.get("security")
    if security is None:
        return True
    if not isinstance(security, dict):
        return False
    value = security.get("secure_defaults")
    if value is None or isinstance(value, bool):
        return True
    return isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE


def _required_legal_knowledge_config_valid() -> bool:
    """Admit the on-box semantic model required by enabled legal packs.

    The deterministic embedder is deliberately retained for tests, but it is
    lexical hashing rather than semantic recall.  A remotely reachable or
    authenticated firm therefore may not start with that stub (including via
    an environment override).  Constructing ``LocalEmbedder`` here validates
    the absolute model tree and its configured SHA-256 commitment without
    loading model code or sending client text anywhere.
    """
    try:
        from maverick.config import get_knowledge
        from maverick.domain import enabled_domains, suite_for

        requires_knowledge = any(
            suite_for(name) == "legal"
            and (
                "knowledge_search" in profile.allow_tools
                or bool(profile.knowledge_sources)
            )
            for name, profile in enabled_domains().items()
        )
        if not requires_knowledge:
            return True

        cfg = get_knowledge()
        if cfg.get("enable") is not True:
            return False
        provider = str(
            os.environ.get("MAVERICK_EMBED_PROVIDER")
            or cfg.get("embedder")
            or ""
        ).strip().lower()
        if provider != "local":
            return False
        model = str(
            os.environ.get("MAVERICK_EMBED_MODEL")
            or cfg.get("model")
            or ""
        ).strip()
        digest = str(
            os.environ.get("MAVERICK_EMBED_MODEL_DIGEST")
            or cfg.get("model_digest")
            or ""
        ).strip()

        from maverick_knowledge.local_embed import LocalEmbedder

        LocalEmbedder(model, digest)
        return True
    except Exception:
        return False


def _assert_firm_security_posture(*, remotely_reachable: bool = False) -> None:
    """Require firm security floors for named-auth or remotely bound service."""
    firm_mode = remotely_reachable or non_static_auth_configured() or _require_auth_enabled()
    if not firm_mode:
        return
    from maverick.crypto_at_rest import at_rest_enabled, strict_at_rest
    from maverick.security_defaults import secure_by_default

    if not _secure_default_policy_valid() or not secure_by_default():
        raise RuntimeError(
            "Firm dashboard requires secure defaults. MAVERICK_SECURE_DEFAULT=0 "
            "is permitted only for localhost auth-off development."
        )
    counsel_valid, _counsel = qualified_attorney_policy()
    if not counsel_valid:
        raise RuntimeError(
            "Firm dashboard requires a non-empty exact-principal "
            "[firm] qualified_attorneys roster."
        )
    from .public_origin import public_origin_policy

    origin_valid, _base, _hosts = public_origin_policy()
    if not origin_valid:
        raise RuntimeError(
            "Firm dashboard requires canonical HTTPS [dashboard] "
            "public_base_url and an exact trusted_hosts list."
        )
    if not at_rest_enabled() or not strict_at_rest():
        raise RuntimeError(
            "Firm dashboard requires encrypted storage with strict reads. Run "
            "`maverick encryption migrate`, then set MAVERICK_ENCRYPT_AT_REST=1 "
            "and MAVERICK_ENCRYPT_STRICT=1 before enabling auth or remote access."
        )
    if not _required_legal_knowledge_config_valid():
        raise RuntimeError(
            "Firm dashboard requires enabled legal knowledge with the local "
            "semantic embedder, an absolute operator-provisioned model path, "
            "and its exact sha256 model-tree digest. The deterministic embedder "
            "is permitted only for localhost auth-off development and tests."
        )

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the single-firm control surface and drain in-flight work on exit."""
    from maverick.control_plane_lease import acquire as _acquire_control_plane
    _acquire_control_plane(role="dashboard")
    _assert_dashboard_auth_configured()
    _assert_firm_security_posture()
    await _reclaim_orphans()
    yield
    try:
        from maverick.runner import drain_inflight
        timeout = float(os.environ.get("MAVERICK_DRAIN_TIMEOUT", "25") or 25)
        left = await run_in_threadpool(drain_inflight, timeout)
        if left:
            log.warning(
                "shutdown: %d goal(s) still in-flight after %.0fs drain timeout", left, timeout
            )
    except Exception:  # pragma: no cover
        pass

app = FastAPI(
    title="Maverick Dashboard + REST API",
    description="Local browser UI plus REST API for programmatic access.",
    version="0.1.0",
    dependencies=[Depends(require_principal_in_request_context)],
    lifespan=_lifespan,
)

app.include_router(api_router)

app.include_router(law_api_router)

from .admin_pages import router as admin_pages_router  # noqa: E402

app.include_router(admin_pages_router)

app.include_router(oidc_login_router)

from .invite_routes import router as invite_router  # noqa: E402

app.include_router(invite_router)

_DOCS_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)

_DEFAULT_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)

async def _reclaim_orphans() -> None:
    """Mark goals stuck in active/pending as blocked after a crash.

    Without this, SIGKILL/OOM mid-run strands rows in 'active' forever
    and `active_goal()` returns a ghost. Council finding (Tier 0).
    """
    try:
        wm = _world()  # honor the configured backend (SQLite or Postgres)
        n = wm.reclaim_orphan_goals()
        if n:
            log.warning("reclaimed %d orphan goal(s) from prior crash", n)
    except Exception:
        log.exception("orphan reclaim failed on startup")

_AUTH_EXEMPT = {
    "/healthz", "/livez", "/readyz",
    "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect",
    # Built-in OIDC browser-login endpoints: they bootstrap a session and carry
    # their own flow-level security (state/PKCE/signed cookies). They must be
    # reachable by a browser that has no dashboard token yet; each self-gates on
    # login_enabled() and 404s when the login flow is off.
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
    "/auth/error",
}

_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

def _is_same_origin(request: Request) -> bool:
    """Allow only same-origin browser submissions for mutating form POSTs.

    Fails closed when no Origin or Referer is present on a mutating
    request. The previous fail-open branch ("Non-browser/API clients
    commonly omit both headers") was a soft-CSRF: any tab on the same
    machine could fire a no-cors fetch with both headers stripped and
    have it accepted. Real API clients send Authorization headers and
    are exempted by the bearer-auth middleware before they reach here.
    """
    if request.method in _CSRF_SAFE_METHODS:
        return True
    expected = request.url.netloc
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.netloc == expected:
            return True
        return False
    return False

def _require_same_origin(request: Request) -> None:
    """Reject a cross-site mutating form POST (403). Shared CSRF guard inlined by
    ~20 form handlers."""
    if not _is_same_origin(request):
        raise HTTPException(
            status_code=403,
            detail="cross-site form post blocked — for your security, changes "
                   "must be made from the Maverick page itself. Reload the "
                   "dashboard and try again.",
        )

def _is_loopback_client(host: str) -> bool:
    """True for in-process/loopback callers (safe to serve without a token)."""
    if not host:
        return False
    # Starlette's in-process TestClient reports host="testclient"; a real
    # network peer can never present that (request.client.host is the
    # socket peer, set by the server, not user-controllable).
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def _is_loopback_request_host(request: Request) -> bool:
    """True when the user-controlled Host header names a loopback host.

    No-token mode is intentionally limited to local dashboards.  Checking only
    the socket peer is not enough for browser requests: a DNS-rebinding page can
    connect to 127.0.0.1 while preserving an attacker-controlled Host/Origin
    pair, which would otherwise satisfy Host-derived same-origin checks.
    """
    host = (request.url.hostname or "").rstrip(".").lower()
    if host in ("localhost", "testserver"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

_PROXY_FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")

def _is_proxied(request: Request) -> bool:
    """True if a reverse proxy forwarded this request.

    In no-token mode the dashboard trusts the loopback peer
    (``request.client.host``). A reverse proxy on the same host connects over
    loopback, so a deploy that fronts the app with a public proxy but forgets
    to set ``MAVERICK_DASHBOARD_TOKEN`` on the app process would serve the
    control surface unauthenticated to the internet — the loopback peer is the
    proxy, not the real remote client. Treat any standard forwarding header as
    proof a proxy is in front and fall through to the token requirement (fail
    closed). Reading these headers only ever makes auth STRICTER, so a forged
    header cannot grant access — at worst a direct caller locks itself out by
    sending one.
    """
    return any(request.headers.get(h) for h in _PROXY_FORWARD_HEADERS)

def _configured_oidc_bearer_present(request: Request) -> bool:
    """Whether this request explicitly presents a bearer to enabled OIDC.

    Such API credentials do not use ambient browser authority, so they do not
    need an Origin/Referer. The global auth dependency remains authoritative:
    it verifies the token (and rejects an invalid one) before any cookie/proxy
    identity can be selected.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return False
    try:
        from . import auth as auth_boundary

        return auth_boundary.oidc_enabled()
    except Exception:
        return False

@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    expected = os.environ.get("MAVERICK_DASHBOARD_TOKEN")
    if expected:
        from .auth import legacy_static_bearer_allowed

        if not legacy_static_bearer_allowed(request):
            expected = None
    if (request.url.path in _AUTH_EXEMPT or request.url.path.startswith("/share/")
            or request.url.path.startswith("/auth/invite/")):
        # /share/<token> self-authenticates with its signed, revocable token
        # (verified in the route, which 404s an invalid/expired/revoked one) --
        # an external recipient has no dashboard session.
        # /auth/invite/<token> is the same shape: the invitee has no credential
        # yet; the single-use token IS the auth, verified (and same-origin-
        # gated on the consuming POST) in the route, which 404s when invites
        # are disabled.
        return await call_next(request)
    if not expected:
        # Any configured identity mechanism disables legacy loopback-as-admin,
        # even when the separate startup assertion ``dashboard.require_auth``
        # is unset. The global dependency verifies OIDC or invite
        # identity and fails closed on unreadable auth configuration.
        # Resolve through the module so identity-policy changes made by a
        # deployment/test harness cannot leave this long-lived middleware with
        # a stale function binding.
        from . import auth as auth_boundary

        if not auth_boundary.auth_genuinely_off():
            explicit_oidc_bearer = _configured_oidc_bearer_present(request)
            if (
                not explicit_oidc_bearer
                and not _is_same_origin(request)
            ):
                return JSONResponse(
                    {"detail": "cross-site request blocked - for your security, "
                               "changes must be made from the Maverick page "
                               "itself. Reload the dashboard and try again."},
                    status_code=403,
                )
            return await call_next(request)
        # A client-bound firm never receives ambient loopback administrator rights.
        # Any process sharing the loopback
        # namespace (a sidecar, a co-located container, an SSRF pivot to
        # 127.0.0.1) must never become an unauthenticated admin — require a
        # token (or OIDC) explicitly.
        try:
            from maverick.client import client_binding_enforced
            if client_binding_enforced():
                _auth_metrics.record_auth_failure("client_binding")
                return JSONResponse(
                    {"detail": "Sign-in is required on this deployment — an "
                               "administrator must configure dashboard "
                               "authentication for client-bound firm "
                               "mode (enable a named local or OIDC identity)."},
                    status_code=401,
                )
        except Exception:  # pragma: no cover - never break auth on a read error
            pass
        # No named identity configured: serve loopback only. An operator who binds
        # --host 0.0.0.0 must NOT silently expose
        # run history, spend, and the control surface unauthenticated to
        # the network. Configure named local/OIDC authentication for remote access.
        client_host = request.client.host if request.client else ""
        if (
            _is_loopback_client(client_host)
            and _is_loopback_request_host(request)
            and not _is_proxied(request)
        ):
            # Loopback is served without a bearer, so a malicious page open in
            # the user's browser could otherwise drive mutating endpoints via an
            # ambient cross-site request (CSRF): cancel/resume goals, disable
            # safety tools, arm the killswitch, purge caches. Gate unsafe methods
            # behind the same-origin check centrally (the one /chat/send already
            # enforces per-route) so every current and future /api/v1 mutation is
            # covered. Token mode needs no such check — a cross-site page cannot
            # attach the Authorization header.
            if not _is_same_origin(request):
                return JSONResponse(
                    {"detail": "cross-site request blocked — for your security, "
                               "changes must be made from the Maverick page "
                               "itself. Reload the dashboard and try again."},
                    status_code=403,
                )
            return await call_next(request)
        # A VERIFIED invite-minted session is a real credential: accept it for
        # remote access in no-token mode (same-origin-gated like every other
        # cookie mode), so an invites-only deployment works over the network
        # without extra config. Anonymous remote requests still 401 below.
        try:
            from .invites import invites_enabled, local_session_principal
            if invites_enabled() and local_session_principal(request) is not None:
                if not _is_same_origin(request):
                    return JSONResponse(
                        {"detail": "cross-site request blocked — for your "
                                   "security, changes must be made from the "
                                   "Maverick page itself. Reload the "
                                   "dashboard and try again."},
                        status_code=403,
                    )
                return await call_next(request)
        except Exception:  # pragma: no cover - invites must never open access
            pass
        _auth_metrics.record_auth_failure("remote_no_token")
        return JSONResponse(
            {"detail": "Remote access to this dashboard requires named local "
                       "or OIDC authentication."},
            status_code=401,
        )
    auth = request.headers.get("authorization", "")
    header_token = auth[7:] if auth.startswith("Bearer ") else ""
    # ``?token=`` query auth was removed: it leaks the bearer through
    # browser history, Referer headers on outbound link clicks, uvicorn
    # access logs, and any logging proxy in front. Require the
    # ``Authorization: Bearer`` header.
    if header_token and hmac.compare_digest(header_token.encode(), expected.encode()):
        return await call_next(request)
    if auth:
        # A non-static bearer may be an OIDC token in a mixed deployment. Let
        # the global dependency verify it; malformed/invalid credentials never
        # fall back to proxy or cookie identity there.
        if _configured_oidc_bearer_present(request):
            return await call_next(request)
        _auth_metrics.record_auth_failure("bad_token")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    if non_static_auth_configured():
        # The static operator token composes with proxy/OIDC/invite browser
        # identities. Ambient authority remains CSRF-gated; the dependency then
        # verifies the actual identity and rejects a missing/invalid session.
        if not _is_same_origin(request):
            return JSONResponse(
                {"detail": "cross-site request blocked - for your security, "
                           "changes must be made from the Maverick page "
                           "itself. Reload the dashboard and try again."},
                status_code=403,
            )
        return await call_next(request)
    # The bad/missing-bearer case — the credential-stuffing signal an alert
    # rule watches (a flood here means someone is probing the token).
    _auth_metrics.record_auth_failure("bad_token")
    return JSONResponse({"detail": "unauthorized"}, status_code=401)

def _wants_html(request: Request) -> bool:
    """True when the client prefers HTML (browser nav) over JSON (API)."""
    accept = (request.headers.get("accept") or "").lower()
    if request.url.path.startswith(("/api/", "/openapi", "/healthz", "/livez", "/readyz", "/metrics")):
        return False
    return "text/html" in accept or "*/*" in accept

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Branded HTML for browser 404s; JSON for API callers."""
    if exc.status_code == 404 and _wants_html(request):
        return templates.TemplateResponse(
            request, "404.html",
            {"path": request.url.path},
            status_code=404,
        )
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers,
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """422 for browser nav becomes 400 with the branded error page."""
    if _wants_html(request):
        return templates.TemplateResponse(
            request, "500.html",
            {"path": request.url.path},
            status_code=400,
        )
    # exc.errors() can embed the raw request body (bytes) in 'input' when the
    # client sent a wrong/garbage Content-Type; json.dumps can't serialize bytes,
    # so a 422 turned into an opaque 500. jsonable_encoder coerces bytes -> str.
    from fastapi.encoders import jsonable_encoder
    errors = jsonable_encoder(exc.errors())
    # Legal briefs and party names can appear in malformed inputs. Validation
    # diagnostics expose location and reason, never echo client data.
    for error in errors:
        if isinstance(error, dict) and "input" in error:
            error["input"] = "[redacted]"
    return JSONResponse({"detail": errors}, status_code=422)

@app.exception_handler(OverflowError)
async def overflow_exception_handler(request: Request, exc: OverflowError):
    """An out-of-range integer path param (e.g. goal_id > 2**63-1) can't be a real
    row -- SQLite raises OverflowError binding it. Treat it as not-found rather
    than letting it fall through to a 500 (user-testing finding)."""
    if _wants_html(request):
        return templates.TemplateResponse(
            request, "404.html", {"path": request.url.path}, status_code=404,
        )
    return JSONResponse({"detail": "not found"}, status_code=404)

@app.exception_handler(RuntimeOverridesSecurityError)
async def runtime_policy_exception_handler(
    request: Request,
    exc: RuntimeOverridesSecurityError,
):
    """Make a fail-closed operator-policy stop actionable without leaking paths."""
    log.error(
        "operator runtime policy is unavailable on %s: %s",
        request.url.path,
        exc,
    )
    if _wants_html(request):
        return templates.TemplateResponse(
            request,
            "500.html",
            {"path": request.url.path, "policy_error": True},
            status_code=503,
        )
    return JSONResponse(
        {
            "detail": (
                "operator policy is unavailable; restore or repair "
                "runtime-overrides.toml before retrying"
            ),
            "code": "operator_policy_unavailable",
        },
        status_code=503,
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all so we never serve the default white "Internal Server Error"."""
    log.exception("unhandled dashboard exception on %s", request.url.path)
    if _wants_html(request):
        return templates.TemplateResponse(
            request, "500.html",
            {"path": request.url.path},
            status_code=500,
        )
    return JSONResponse({"detail": "internal server error"}, status_code=500)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply baseline browser-security headers to every response.

    These are cheap, well-supported, and close a class of attacks
    (clickjacking, MIME sniffing, Referer leakage, cross-origin
    exfiltration) the dashboard had no defense against before.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # "same-origin", NOT "no-referrer": under no-referrer the Fetch spec makes
    # browsers send "Origin: null" on same-origin form POST navigations, which
    # the CSRF guard (_is_same_origin) must reject — so every native form save
    # in the dashboard 403'd in a real browser ("cross-site request blocked")
    # while fetch()-based saves (mode "cors" keeps the real Origin) and tests
    # (explicit Origin header) still passed. same-origin keeps the privacy win
    # (no Referer leaks to external sites) and restores Origin/Referer on
    # first-party requests.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Cross-Origin-Opener-Policy", "same-origin",
    )
    # Drop ambient browser capabilities; retained matter work needs none.
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), camera=(), payment=(), microphone=()",
    )
    # no-store on everything by default so authenticated HTML and sensitive
    # JSON aren't left in the browser's back/forward cache or a shared proxy
    # after logout. setdefault preserves the explicit caching the static-asset
    # and SSE routes already set (public max-age / no-cache).
    response.headers.setdefault("Cache-Control", "no-store")
    # HSTS only when the request actually arrived over TLS (directly or via a
    # terminating proxy) — never on plain-HTTP loopback dev, which HSTS would
    # otherwise pin to https and break.
    if (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    ):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
        )
    # Content-Security-Policy. The templates use first-party inline
    # <style>, <script>, and style="" attributes, so script/style-src
    # need 'unsafe-inline' for now (a nonce-based tightening is tracked
    # tech debt). The value still hardens the dashboard meaningfully:
    #   - default/connect/script/style 'self' → injected JS can't fetch()
    #     to an external exfil endpoint or pull a remote script
    #   - frame-ancestors 'none' → reinforces X-Frame-Options (clickjack)
    #   - form-action 'self' → an injected <form> can't POST off-origin
    #   - object-src 'none', base-uri 'none' → kill plugin + <base> tricks
    # This matters because the dashboard renders agent-produced text;
    # if any of it ever reaches an HTML sink, CSP is the backstop.
    path = request.url.path
    csp = _DOCS_CSP if path in {"/docs", "/redoc"} else _DEFAULT_CSP
    response.headers.setdefault("Content-Security-Policy", csp)
    return response

@app.middleware("http")
async def trusted_firm_host(request: Request, call_next):
    """Reject Host poisoning before a firm route can mint or email a token."""
    firm_mode = non_static_auth_configured() or _require_auth_enabled()
    if firm_mode:
        from .public_origin import request_host_allowed

        if not request_host_allowed(request):
            return JSONResponse({"detail": "untrusted host"}, status_code=400)
    return await call_next(request)

_goal_times: dict[str, deque[float]] = {}

_goal_times_global: deque[float] = deque()

_goal_rl_lock = threading.Lock()

def _max_goals_per_min() -> int:
    try:
        return max(1, int(os.environ.get("MAVERICK_DASHBOARD_MAX_GOALS_PER_MIN", "30")))
    except ValueError:
        return 30

def _max_goals_global_per_min() -> int:
    # Process-wide ceiling across all clients; defaults to 10x the per-client
    # cap so one client can't starve others yet a distributed flood is still
    # bounded.
    try:
        return max(1, int(os.environ.get(
            "MAVERICK_DASHBOARD_MAX_GOALS_GLOBAL_PER_MIN",
            str(_max_goals_per_min() * 10),
        )))
    except ValueError:
        return _max_goals_per_min() * 10

def _rate_limit_key(request: Request | None, source: str | None = None) -> str:
    """Identify the principal/source for rate-limiting.

    Prefer the authenticated principal when one is established, falling back
    to the client IP otherwise. Explicit local sources may supply a bounded
    source label.
    """
    if source:
        return f"source:{source}"
    principal = caller_principal(request) if request else None
    if principal:
        return f"principal:{principal}"
    host = request.client.host if (request and request.client) else "unknown"
    return f"ip:{host}"

def check_goal_rate_limit(
    request: Request | None = None, *, source: str | None = None
) -> None:
    """Raise HTTPException(429) if the goal-creation rate exceeds a cap.

    Two 60-second sliding windows are enforced: a per-client window keyed
    by principal/source (so one noisy client can't 429 everyone) and a
    process-wide global ceiling (so a distributed flood still can't spawn
    unbounded paid goals).
    """
    key = _rate_limit_key(request, source)
    cap = _max_goals_per_min()
    global_cap = _max_goals_global_per_min()
    now = time.monotonic()
    cutoff = now - 60.0
    with _goal_rl_lock:
        # Global ceiling first.
        while _goal_times_global and _goal_times_global[0] < cutoff:
            _goal_times_global.popleft()
        if len(_goal_times_global) >= global_cap:
            retry = int(60 - (now - _goal_times_global[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"goal rate limit reached ({global_cap}/min total). "
                       f"Try again in {retry}s.",
                headers={"Retry-After": str(max(1, retry))},
            )

        window = _goal_times.get(key)
        if window is None:
            window = _goal_times[key] = deque()
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= cap:
            retry = int(60 - (now - window[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"goal rate limit reached ({cap}/min). Try again in {retry}s.",
                headers={"Retry-After": str(max(1, retry))},
            )

        # Sweep EVERY other key's window of expired entries, then drop the ones
        # that became empty. The old check only collected ALREADY-empty deques,
        # but a key whose lone entry expired and whose IP/principal never recurs
        # was never pruned (its deque kept len 1, so `not w` stayed False) -- so
        # a stream of distinct one-shot keys (many principals / direct client
        # IPs) grew the dict without bound. Bounded by the number of keys active
        # within the window now.
        for stale_key in list(_goal_times):
            if stale_key == key:
                continue
            w = _goal_times[stale_key]
            while w and w[0] < cutoff:
                w.popleft()
            if not w:
                del _goal_times[stale_key]

        window.append(now)
        _goal_times_global.append(now)

@app.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request) -> HTMLResponse:
    w = _world()
    goals = list_accessible_goals(request, w, limit=200, order="desc")
    return templates.TemplateResponse(request, "goals.html", {"goals": goals})

@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request) -> HTMLResponse:
    """Client matters visible through the caller's exact ethical-wall rows."""
    w = _world()
    projects = list_accessible_projects(request, w)
    principal = caller_principal(request)
    verified = getattr(getattr(request, "state", None), "principal", None)
    named_human = (
        getattr(verified, "issuer", "") != "maverick:dashboard-static-bearer"
        and is_qualified_attorney_principal(principal)
    )
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "projects": projects,
            "clients": w.list_clients(principal=principal),
            "legal_domains": _gated_legal_domains(),
            "can_intake": has_permission(request, "legal_signoff") and named_human,
        },
    )

_OPAQUE_CONFLICT_DETAIL = (
    "intake could not be cleared; conflicts-counsel review required"
)

def _audit_matter_intent_or_503(
    *,
    actor: str,
    operation: str,
    candidate_count: int,
    matter_id: int | None = None,
    client_id: int | None = None,
) -> None:
    """Persist a privacy-minimized signed intent before an intake operation."""
    from maverick.audit import EventKind, audit_event

    payload: dict[str, object] = {
        "actor": actor,
        "operation": operation,
        "candidate_count": int(candidate_count),
    }
    if matter_id is not None:
        payload["matter_id"] = int(matter_id)
    if client_id is not None:
        payload["client_id"] = int(client_id)
    try:
        written = audit_event(
            EventKind.MATTER_INTAKE,
            agent=actor,
            **payload,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="matter intake audit is temporarily unavailable",
        ) from exc
    if not written:
        raise HTTPException(
            status_code=503,
            detail="matter intake audit is temporarily unavailable",
        )

def _audit_access_change_or_503(
    *,
    actor: str,
    operation: str,
    matter_id: int,
    target_principal: str | None = None,
    target_role: str | None = None,
    requested_mode: str | None = None,
) -> None:
    """Persist a privacy-minimized access-change intent before state changes."""
    from maverick.audit import EventKind, audit_event

    payload: dict[str, object] = {
        "actor": actor,
        "matter_id": int(matter_id),
        "action": operation,
    }
    if target_principal is not None:
        payload["target_principal"] = target_principal
    if target_role is not None:
        payload["target_role"] = target_role
    if requested_mode is not None:
        payload["requested_mode"] = requested_mode
    try:
        written = audit_event(
            EventKind.ACCESS_GRANT_CHANGED,
            agent=actor,
            **payload,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="matter access audit is temporarily unavailable",
        ) from exc
    if not written:
        raise HTTPException(
            status_code=503,
            detail="matter access audit is temporarily unavailable",
        )

def _named_legal_principal(request: Request) -> str | None:
    """Require legal authority and reject non-human deployment credentials."""
    require_permission(request, "legal_signoff")
    principal = caller_principal(request)
    if principal is None:
        return None
    from .auth import require_qualified_attorney

    return require_qualified_attorney(request)

@app.post("/projects")
async def projects_create(request: Request, name: str = Form(...),
                          description: str = Form(""), domain: str = Form(""),
                          client_name: str = Form(""), client_id: str = Form(""),
                          matter_number: str = Form(""),
                          jurisdiction: str = Form(""),
                          adverse_parties: str = Form("")) -> RedirectResponse:
    """Conflict-clear and atomically open a legal client matter.

    Auth-off loopback mode retains the old lightweight ``create_project`` path
    for local development.  Every authenticated request uses the principal-
    bound client/matter transaction; there is no authenticated legacy fallback.
    """
    _require_same_origin(request)
    if not name.strip():
        raise HTTPException(status_code=422, detail="a matter needs a name")
    actor = _named_legal_principal(request)
    w = _world()
    if actor is None:
        # Explicit auth-off compatibility only.  The global authentication
        # dependency prevents an authenticated request from reaching this arm
        # without a stable principal.
        _audit_matter_intent_or_503(
            actor="local",
            operation="open_matter",
            candidate_count=0,
        )
        pid = w.create_project(
            name.strip(), description=description.strip(), domain=domain.strip(),
        )
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    selected_client_id: int | None = None
    if client_id.strip():
        try:
            selected_client_id = int(client_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="existing client is invalid") from exc
        if selected_client_id <= 0:
            raise HTTPException(status_code=422, detail="existing client is invalid")
    legal_domain = domain.strip()
    if legal_domain not in set(_gated_legal_domains()):
        raise HTTPException(
            status_code=422,
            detail="an enabled legal domain with a terminal review gate is required",
        )
    parties = [line.strip() for line in adverse_parties.splitlines() if line.strip()]
    _audit_matter_intent_or_503(
        actor=actor,
        operation="open_matter",
        candidate_count=len(parties) + (1 if client_name.strip() else 0),
        client_id=selected_client_id,
    )
    from maverick.world_model import PotentialConflict

    try:
        pid = w.create_client_matter(
            name.strip(),
            principal=actor,
            domain=legal_domain,
            matter_number=matter_number,
            jurisdiction=jurisdiction,
            description=description,
            client_name=client_name,
            client_id=selected_client_id,
            adverse_parties=parties,
        )
    except PotentialConflict as exc:
        raise HTTPException(status_code=409, detail=_OPAQUE_CONFLICT_DETAIL) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{pid}", status_code=303)

@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: int) -> HTMLResponse:
    w = _world()
    principal = caller_principal(request)
    # The project row itself carries the exact active-membership predicate, so
    # revocation cannot race an authorize-then-unscoped-decrypt sequence.
    project = w.get_project(project_id, principal=principal)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    # Every active member sees the complete matter workspace. The membership
    # predicate, not each goal's provenance owner, is the ethical wall.
    goals = list_accessible_goals(
        request, w, project_id=project_id, order="desc",
    )
    members = w.list_project_members(project_id, principal=principal)
    membership_role = (
        w.project_member_role(project_id, principal) if principal else None
    )
    return templates.TemplateResponse(
        request, "project_detail.html",
        {
            "project": project,
            "goals": goals,
            "counts": w.project_status_counts(project_id, principal=principal),
            "members": members,
            "membership_role": membership_role,
            "parties": w.list_matter_parties(project_id, principal=principal),
        },
    )

def _require_matter_membership_manager(request: Request, w, project_id: int) -> str:
    """Return the actor allowed to change the exact-principal matter ACL."""
    assert_project_access(request, project_id, world=w)
    principal = caller_principal(request)
    if principal is None:
        # Explicit auth-off single-user compatibility. Once authentication is
        # configured, a missing identity never reaches this handler.
        return "local"
    if w.project_member_role(project_id, principal) != "responsible_attorney":
        # Hide membership-management capability along with matter existence.
        raise HTTPException(status_code=404, detail="no such project")
    return principal

@app.post("/projects/{project_id}/parties")
async def project_party_add(
    request: Request,
    project_id: int,
    name: str = Form(...),
    role: str = Form(...),
) -> RedirectResponse:
    """Add one conflict-cleared party under responsible-attorney control."""
    _require_same_origin(request)
    w = _world()
    actor = _require_matter_membership_manager(request, w, project_id)
    _audit_matter_intent_or_503(
        actor=actor,
        operation="add_party",
        candidate_count=1,
        matter_id=project_id,
    )
    from maverick.world_model import PotentialConflict

    try:
        party_id = w.add_matter_party(
            project_id, name, role, principal=actor,
        )
    except PotentialConflict as exc:
        raise HTTPException(status_code=409, detail=_OPAQUE_CONFLICT_DETAIL) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if party_id is None:
        # Revocation may race the page authorization; the database rechecks it
        # inside the same transaction as the conflict check and insert.
        raise HTTPException(status_code=404, detail="no such project")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/members")
async def project_member_add(
    request: Request,
    project_id: int,
    principal: str = Form(...),
    role: str = Form(...),
) -> RedirectResponse:
    """Enroll an exact user principal in a matter's ethical wall."""
    _require_same_origin(request)
    w = _world()
    if w.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="no such project")
    actor = _require_matter_membership_manager(request, w, project_id)
    member = principal.strip()
    if (
        len(member) > 255
        or not member.startswith("user:")
        or len(member) <= len("user:")
        or any(ord(ch) < 32 for ch in member)
        or member == "user:dashboard-static-bearer"
    ):
        raise HTTPException(status_code=422, detail="a valid user principal is required")
    _audit_access_change_or_503(
        actor=actor,
        operation="matter_member_add_requested",
        matter_id=project_id,
        target_principal=member,
        target_role=role,
    )
    try:
        w.add_project_member(project_id, member, role, added_by=actor)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/members/{principal}/revoke")
async def project_member_revoke(
    request: Request, project_id: int, principal: str,
) -> RedirectResponse:
    """Revoke a matter membership without deleting its provenance row."""
    _require_same_origin(request)
    w = _world()
    if w.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="no such project")
    actor = _require_matter_membership_manager(request, w, project_id)
    _audit_access_change_or_503(
        actor=actor,
        operation="matter_member_revoke_requested",
        matter_id=project_id,
        target_principal=principal,
    )
    try:
        changed = w.deactivate_project_member(project_id, principal)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="no such membership")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/egress")
async def project_egress_mode_update(
    request: Request,
    project_id: int,
    egress_mode: str = Form(...),
) -> RedirectResponse:
    """Set a matter's outbound-confidentiality posture.

    Only the exact responsible attorney can loosen or tighten this boundary;
    dashboard administrators do not bypass the ethical wall.  The signed audit
    request is recorded before the atomic membership-checked mutation so a
    logging refusal can never produce an invisible policy change.
    """
    _require_same_origin(request)
    w = _world()
    if w.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="no such project")
    actor = _require_matter_membership_manager(request, w, project_id)
    mode = egress_mode.strip()
    if mode not in {"local_only", "approved_services"}:
        raise HTTPException(status_code=422, detail="invalid matter egress mode")

    _audit_access_change_or_503(
        actor=actor,
        matter_id=project_id,
        operation="matter_egress_mode_requested",
        requested_mode=mode,
    )
    if not w.set_project_egress_mode(project_id, mode, principal=actor):
        # Membership may have been revoked between page authorization and the
        # database-atomic mutation.  Keep the ethical wall opaque.
        raise HTTPException(status_code=404, detail="no such project")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

def _deliverable_specs() -> list[dict]:
    """The packs that declare a deliverable -- the rows of the persona inbox.

    A pack earns a spec once its output contract names a deliverable or its
    consumers; packs that only emit prose stay out of the inbox (they're plain
    runs on ``/goals``). Fail-soft to an empty list so the page never 500s if
    the factory layer is unavailable."""
    try:
        from maverick.domain import available_domains, enforced_gate, suite_for
    except Exception:  # pragma: no cover -- factory layer unavailable
        return []
    specs: list[dict] = []
    for name, prof in sorted(available_domains().items()):
        if suite_for(name) != "legal":
            continue
        out = prof.output
        if not (out.deliverable or out.consumers):
            continue
        specs.append({
            "domain": name,
            "deliverable": out.deliverable or name,
            "shape": out.shape,
            "consumers": list(out.consumers),
            "cadence": out.cadence,
            "gate": enforced_gate(prof),
            "suite": suite_for(name),
        })
    return specs

@app.get("/deliverables", response_class=HTMLResponse)
async def deliverables_page(request: Request) -> HTMLResponse:
    """Matter-accessible runs grouped by retained legal deliverable."""
    from .deliverables import build_inbox

    specs = _deliverable_specs()
    runs_by_domain: dict[str, list] = {}
    world = _world()
    for s in specs:
        try:
            runs_by_domain[s["domain"]] = list_accessible_goals(
                request,
                world,
                domain=s["domain"],
                limit=5,
                order="desc",
            )
        except Exception:  # pragma: no cover -- a bad domain query never breaks the page
            runs_by_domain[s["domain"]] = []
    ids = [g.id for runs in runs_by_domain.values() for g in runs]
    try:
        signoffs = world.signoffs_for_goals(ids)
    except Exception:  # pragma: no cover -- never break the page on the sign-off lookup
        signoffs = {}
    model = build_inbox(specs, runs_by_domain, signoffs)
    return templates.TemplateResponse(request, "deliverables.html", model)

def safe_audit_day(day: str | None) -> str | None:
    """Validate a ``?day=`` value as YYYY-MM-DD before it reaches the
    audit log's path builder.

    The audit log resolves ``day`` to ``audit_dir/{day}.ndjson``; an
    unvalidated value like ``../../../etc/foo`` would escape the audit
    directory. Anything that isn't a bare date is rejected to ``None``
    (today), neutralizing path traversal at the HTTP boundary.
    """
    # Delegate to the core's canonical validator so the HTTP boundary and the
    # CLI stay in lockstep: it enforces the anchored shape (path-safety) AND a
    # real calendar date, so a typo like 2026-13-99 falls back to today rather
    # than resolving to a nonexistent day-file.
    from maverick.audit.events import is_valid_day
    if is_valid_day(day):
        return day
    return None

@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request) -> HTMLResponse:
    """Tail of the local audit log."""
    # Gate on ``audit`` to match the ``/api/v1/audit/tail`` twin -- otherwise a
    # view-only caller reads the full audit log via the HTML page despite the
    # API denying them.
    require_permission(request, "audit")
    from maverick.audit import default_audit_log
    try:
        n = max(1, min(int(request.query_params.get("n") or 200), 1000))
    except (TypeError, ValueError):
        n = 200
    day = safe_audit_day(request.query_params.get("day"))
    events = default_audit_log().tail(n, day=day)
    # Distinct kinds in this tail (for the filter dropdown), computed before
    # filtering so every available kind stays selectable; then optionally
    # narrow to one kind (e.g. shield_block, tool_call).
    kinds = sorted({str(e.get("kind")) for e in events if e.get("kind")})
    kind = (request.query_params.get("kind") or "").strip()
    if kind:
        events = [e for e in events if e.get("kind") == kind]
    return templates.TemplateResponse(
        request, "audit.html",
        {"events": events, "n": n, "day": day, "kind": kind, "kinds": kinds},
    )

def _gated_legal_domains() -> list[str]:
    """Enabled legal specialists whose workflow ends at a human gate."""
    from maverick.domain import enabled_domains, suite_for

    return sorted(
        name
        for name, profile in enabled_domains().items()
        if suite_for(name) == "legal"
        and profile.workflow
        and profile.workflow[-1].gate in {"review", "approval"}
    )

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    w = _world()
    recent = list_accessible_goals(request, w, limit=10, order="desc")
    principal = caller_principal(request)
    projects = list_accessible_projects(request, w) if principal is not None else []
    legal_domains = _gated_legal_domains() if principal is not None else []
    # "Use template" on /templates links here with ?title=&description= to
    # prefill the form (never auto-start; the user reviews, edits, submits).
    prefill_title = (request.query_params.get("title") or "")[:200]
    prefill_description = (request.query_params.get("description") or "")[:8000]
    return templates.TemplateResponse(
        request, "chat.html",
        {"recent": recent, "prefill_title": prefill_title,
         "prefill_description": prefill_description,
         "matter_required": principal is not None,
         "projects": projects,
         "legal_domains": legal_domains},
    )

@app.post("/chat/send")
async def chat_send(
    request: Request,
    bg: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(""),
    project_id: int | None = Form(None),
    domain: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    _require_same_origin(request)
    require_permission(request, "operate")
    require_provider_or_400()
    check_goal_rate_limit(request)
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="goal text is required")
    w = _world()
    principal = caller_principal(request)
    # Share the REST admission policy: authenticated interactive work must use
    # an existing matter and a gated legal profile. The WorldModel then repeats
    # the membership predicate inside the INSERT to close revocation races.
    from .api import _validated_goal_filing

    domain, project_id = _validated_goal_filing(
        request,
        w,
        principal,
        domain=domain,
        project_id=project_id,
    )
    # The optional "Add details" textarea gives the agent a real brief; fall
    # back to the title when empty (prior behavior was description == title).
    description = (description or "").strip()
    if principal is not None:
        goal_id = w.create_matter_goal(
            title[:200],
            (description or title)[:8000],
            principal=principal,
            domain=domain or "",
            project_id=project_id,
        )
        if goal_id is None:
            raise HTTPException(status_code=404, detail="no such project")
    else:
        goal_id = w.create_goal(
            title[:200],
            (description or title)[:8000],
            owner="",
            domain=domain or "",
            project_id=project_id,
        )
    # Persist any uploaded files as goal attachments. The agent reaches them
    # via its goal-bound list_attachments + read_attachment tools (images are
    # vision blocks). Size/mime caps + on-disk storage live in
    # maverick.attachments.store; we just record each one against the goal.
    real_files = [f for f in files if f and (f.filename or "").strip()]
    if real_files:
        from maverick import attachments as _att
        total = 0
        for f in real_files:
            data = await f.read()
            if not data:
                continue  # an unfilled file input still posts an empty part
            try:
                rec = _att.store(
                    goal_id, f.filename,
                    f.content_type or "application/octet-stream",
                    data, existing_total=total,
                )
            except _att.AttachmentRejected as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Attachment '{f.filename}': {exc}",
                ) from exc
            w.add_attachment(goal_id, rec.filename, rec.mime, rec.size_bytes,
                             rec.sha256, str(rec.path))
            total += rec.size_bytes
        # Text companions (audio/video transcripts, extracted Office-doc
        # text) BEFORE the goal runs, so they embed on the agent's first
        # message. Background tasks run sequentially in add order;
        # generate_companions is best-effort and never raises.
        bg.add_task(_att.generate_companions, w, goal_id)
    # Use the shared runner so this path gets the same concurrency cap,
    # budget defaults, and error handling as the REST API and MCP server.
    from maverick.runner import run_goal_in_background_async
    allowed_suites = caller_suites(request)
    user_id = execution_user_id_from_request(request)
    if user_id:
        bg.add_task(
            run_goal_in_background_async, goal_id,
            channel="dashboard", user_id=user_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async,
            goal_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    return RedirectResponse(f"/chat/goal/{goal_id}", status_code=303)

def _goal_deliverable(g):
    """A goal's result rendered as the deliverable its pack declares.

    Returns ``(contract, rendered)`` -- the pack's output contract (label /
    consumers / cadence / gate) and the result parsed per its ``shape`` -- or
    ``(None, None)`` for a generic/ungated prose goal or when the factory layer
    is unavailable. A terminal-gated prose pack still returns its contract so
    the reviewer gets a sign-off panel even when an AI-generated playbook did
    not also generate a structured output contract."""
    try:
        from maverick.deliverable import render_deliverable
        from maverick.domain import available_domains, enforced_gate
        prof = available_domains().get(g.domain) if g.domain else None
        if prof is None:
            return None, None
        rendered = render_deliverable(prof.output.shape, g.result)
        gate = enforced_gate(prof)
        if not rendered.structured and not gate:
            return None, None
        out = prof.output
        contract = {"shape": out.shape, "deliverable": out.deliverable,
                    "consumers": list(out.consumers), "cadence": out.cadence,
                    "gate": gate}
        return contract, rendered
    except Exception:  # never 500 the goal page if the factory layer is unavailable
        return None, None

@app.get("/chat/goal/{goal_id}", response_class=HTMLResponse)
async def chat_goal(request: Request, goal_id: int) -> HTMLResponse:
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    contract, rendered = _goal_deliverable(g)
    # The current sign-off, only for a gated deliverable (the panel that lets a
    # human certify it and hand it off). None elsewhere -> no panel.
    signoff = None
    if contract and contract.get("gate"):
        try:
            signoff = w.signoff_for(goal_id)
        except Exception:  # pragma: no cover -- never 500 the goal page
            signoff = None
    artifacts = _goal_artifacts(w, goal_id)
    try:
        projects = list_accessible_projects(request, w)
    except Exception:  # pragma: no cover -- never 500 the goal page
        projects = []
    try:
        share_links = [s for s in w.share_links_for_goal(goal_id) if s["active"]]
    except Exception:  # pragma: no cover -- never 500 the goal page
        share_links = []
    return templates.TemplateResponse(
        request, "chat_goal.html",
        {"goal": g, "deliverable": contract, "rendered": rendered,
         "signoff": signoff, "artifacts": artifacts, "projects": projects,
         "share_links": share_links},
    )

def _goal_artifacts(w, goal_id: int) -> list[dict]:
    """A goal's latest artifacts, each pre-rendered by kind: a ``table`` artifact
    parses to a grid (reusing the deliverable renderer), everything else falls
    back to text. ``[]`` if the goal has none (the panel then doesn't render)."""
    try:
        from maverick.deliverable import render_deliverable
        out: list[dict] = []
        for a in w.latest_artifacts(goal_id):
            shape = "table" if a.get("kind") == "table" else "prose"
            r = render_deliverable(shape, a.get("content"))
            out.append({**a, "table": r.table, "prose": r.prose})
        return out
    except Exception:  # pragma: no cover -- never 500 the goal page
        return []

@app.get("/share/{token}", response_class=HTMLResponse)
async def shared_goal(request: Request, token: str) -> HTMLResponse:
    """Public, read-only view of a goal's deliverable behind a share token.

    Auth-exempt (the token IS the credential): an unknown, revoked, or expired
    token 404s with no detail. Renders only the title + deliverable + artifacts
    -- never the worklog, spend, controls, or nav."""
    w = _world()
    goal_id = w.resolve_share_link(token)
    if goal_id is None:
        raise HTTPException(status_code=404, detail="this share link is invalid, revoked, or expired")
    g = w.get_goal(goal_id)
    if g is None:  # pragma: no cover -- link resolved but goal gone
        raise HTTPException(status_code=404, detail="not found")
    # Resolve-time authorization matters as much as mint-time authorization:
    # an edit/rerun/rejection or matter/policy removal cannot recall a token
    # already copied. Fail closed without revealing which prerequisite failed.
    try:
        from .release_policy import authorize_goal_release

        authorize_goal_release(w, g)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    contract, rendered = _goal_deliverable(g)
    if contract is None or rendered is None:
        raise HTTPException(status_code=404, detail="not found")
    artifacts = _goal_artifacts(w, goal_id)
    # Re-check after materializing the composite payload. If an artifact or
    # result/policy/matter changed between authorization and rendering, never
    # return a mixed-version snapshot under the copied bearer.
    try:
        latest = w.get_goal(goal_id)
        if latest is None or latest.updated_at != g.updated_at:
            raise RuntimeError("deliverable changed during release")
        authorize_goal_release(w, latest)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    return templates.TemplateResponse(
        request, "shared_goal.html",
        {"goal": g, "deliverable": contract, "rendered": rendered, "artifacts": artifacts})

_STATIC_DIR = Path(__file__).parent / "static"

@app.get("/static/maverick.css")
async def shell_stylesheet() -> FileResponse:
    """The dashboard shell's design system, extracted from base.html so the
    template stays markup. Short-lived cache: the browser revalidates cheaply
    but a dashboard upgrade restyles without a hard refresh."""
    return FileResponse(
        _STATIC_DIR / "maverick.css",
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )

@app.get("/static/maverick-ui.js")
async def shell_behaviors_js() -> FileResponse:
    """The shell behaviors (mvToast/mvConfirm/mvForm primitives, halt pill,
    sidebar + preferences) extracted from base.html; loaded at the end of
    <body> on every page."""
    return FileResponse(
        _STATIC_DIR / "maverick-ui.js",
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )

@app.get("/livez")
async def livez() -> dict:
    """Process is alive (TCP-accept liveness only)."""
    return {"status": "ok"}

@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Deep health: DB writable, LLM provider key present, runner alive."""
    return await _health_response(
        world_provider=_world,
        provider_key_set=_any_provider_key_set,
    )

@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Ready to serve traffic: the /healthz checks PLUS the deep readiness
    checks (client binding, shield-required, agent-trust registry) so a
    k8s/LB never routes to a pod that is up but configured to refuse all work.
    """
    return await _readiness_response(
        world_provider=_world,
        provider_key_set=_any_provider_key_set,
    )

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus text format. Gated by the same bearer as /api/v1."""
    return await _metrics_response(
        request,
        world_provider=_world,
        owner_filter=goal_owner_filter,
    )

def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}

def main() -> None:
    parser = argparse.ArgumentParser(description="Maverick dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    try:
        _assert_firm_security_posture(
            remotely_reachable=not _is_loopback_host(args.host)
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if not _is_loopback_host(args.host) and not non_static_auth_configured():
        raise SystemExit(
            "Refusing to bind dashboard remotely without named local/OIDC "
            "authentication. A shared static bearer is not a matter identity."
        )

    # Apply Maverick's shared logging config (JSON via MAVERICK_LOG_FORMAT=json,
    # correlation-id context filter, secret scrubbing) at the real process
    # entrypoint — not in the lifespan, so the in-process TestClient never
    # reconfigures global logging. The most network-exposed process otherwise
    # inherited raw uvicorn logging with none of the CLI's hygiene.
    try:
        from maverick.logging_config import configure_logging
        configure_logging()
    except Exception:  # pragma: no cover - logging setup never blocks startup
        pass

    # Validate config at startup: a typo'd section/key silently falls back to a
    # default (e.g. an uncapped budget), so surface it now instead of only via
    # `maverick config-lint`. Warn-only unless MAVERICK_CONFIG_STRICT=1.
    try:
        from maverick.config_lint import warn_config_at_startup
        warn_config_at_startup()
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - linting never blocks a non-strict start
        pass

    import uvicorn
    # JSON logging consistency: uvicorn installs its OWN plaintext access/error
    # formatters by default, so even after configure_logging() switches the root
    # handler to JSON the access lines stayed plaintext -- a mixed JSON+text
    # stream that breaks strict ingestion (Loki/CloudWatch). In JSON mode pass
    # log_config=None so uvicorn doesn't reconfigure those loggers; they then
    # propagate to the JSON root handler. In text mode omit the kwarg entirely so
    # uvicorn keeps its colored default for the local-dev experience.
    _json_logs = os.environ.get("MAVERICK_LOG_FORMAT", "text").strip().lower() == "json"
    if _json_logs:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                    log_config=None)
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
