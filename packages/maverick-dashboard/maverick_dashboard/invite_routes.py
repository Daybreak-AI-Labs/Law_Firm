"""Invite-link accept flow: ``GET/POST /auth/invite/{token}``.

Security mechanics (see :mod:`maverick_dashboard.invites` for the store):

- **Scanner-proof**: the GET only *peeks* (renders a confirmation page); the
  POST consumes. Corporate mail scanners that prefetch every link therefore
  can't burn a single-use invite.
- **CSRF on the consume**: the POST requires a same-origin Origin/Referer
  (this path is exempt from the app-wide auth/CSRF middleware because the
  invitee has no credential yet — the token in the path is the credential).
- **SSO mode** (OIDC browser login configured): accepting requires signing in
  first — the page links to ``/auth/login?return_to=<this invite>`` — and the
  invite then binds the IdP-verified principal to the invited role. The
  session comes from the IdP; the invite never mints one.
- **Local mode** (no IdP): accepting mints a signed ``mvk_session`` cookie for
  ``user:<invited email>`` and binds the role. The link is the credential —
  single-use, expiring, revocable.

Every route 404s while ``[dashboard] invites`` is off (fail-closed default).
"""
from __future__ import annotations

import html
import logging
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import invites

log = logging.getLogger(__name__)

router = APIRouter()


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """A small self-contained page (no app nav/session context needed)."""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)} · Bjerken and Day</title>
<style>
  body{{font:15px/1.5 system-ui,sans-serif;display:flex;min-height:100vh;margin:0;
       align-items:center;justify-content:center;background:#0d1117;color:#e6edf3}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:14px;
        padding:34px 32px;max-width:420px}}
  h1{{font-size:19px;margin:0 0 10px}} p{{color:#9da7b3;margin:8px 0}}
  code{{color:#e6edf3}} a{{color:#58a6ff}}
  button{{background:#d4a72c;color:#3a2a06;border:none;border-radius:8px;
         padding:10px 18px;font:inherit;font-weight:700;cursor:pointer;margin-top:14px}}
</style></head><body><div class="card"><h1>{html.escape(title)}</h1>{body}</div>
</body></html>""", status_code=status)


def _gone() -> HTMLResponse:
    return _page("Invitation not available",
                 "<p>This invitation link is invalid, expired, already used, or "
                 "revoked.</p><p>Ask your administrator to send a new one.</p>",
                 status=410)


def _same_origin(request: Request) -> bool:
    """Fail-closed same-origin check for the consuming POST: the accept form is
    served by this host, so a genuine submit carries a matching Origin (or
    Referer). Cross-site or headerless posts are rejected."""
    expected = request.url.netloc
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc == expected
    return False


def _session_principal(request: Request):
    """The signed-in identity, from whichever session mode is active."""
    if invites.local_login_mode():
        return invites.local_session_principal(request)
    try:
        from .oidc_login import _principal_from_request_session
        return _principal_from_request_session(request)
    except Exception:  # pragma: no cover - login module import never breaks invites
        return None


@router.get("/auth/invite/{token}")
async def invite_confirm(request: Request, token: str):
    """Peek-only confirmation page — inert for mail-scanner prefetches."""
    if not invites.invites_enabled():
        return _page("Not found", "<p>Nothing here.</p>", status=404)
    inv = invites.peek_invite(token)
    if inv is None:
        return _gone()
    email = html.escape(inv.email)
    if invites.local_login_mode():
        return _page("You're invited",
                     f"<p>This invitation signs you in to this Bjerken and Day "
                     f"dashboard as <code>{email}</code> with the "
                     f"<code>{html.escape(inv.role)}</code> role.</p>"
                     f"<form method='post'><button type='submit'>"
                     f"Accept invitation</button></form>")
    principal = _session_principal(request)
    if principal is None:
        # SSO mode, not signed in yet: sign in first, then land back here.
        target = quote(f"/auth/invite/{token}", safe="/")
        return _page("You're invited",
                     f"<p>This invitation (for <code>{email}</code>) grants the "
                     f"<code>{html.escape(inv.role)}</code> role on this "
                     f"Bjerken and Day dashboard.</p>"
                     f"<p><a href='/auth/login?return_to={target}'>Sign in with "
                     f"your company account to accept</a></p>")
    return _page("Accept invitation",
                 f"<p>Signed in as <code>{html.escape(principal.sub)}</code>. "
                 f"Accepting binds this account to the "
                 f"<code>{html.escape(inv.role)}</code> role "
                 f"(invitation sent to <code>{email}</code>).</p>"
                 f"<form method='post'><button type='submit'>"
                 f"Accept invitation</button></form>")


@router.post("/auth/invite/{token}")
async def invite_accept(request: Request, token: str):
    """Consume the invite: bind the role and (in local mode) sign the browser in."""
    if not invites.invites_enabled():
        return _page("Not found", "<p>Nothing here.</p>", status=404)
    if not _same_origin(request):
        return _page("Blocked", "<p>Cross-site request blocked — open the "
                                "invitation link directly and try again.</p>",
                     status=400)
    from . import rbac
    if invites.local_login_mode():
        inv = invites.consume_invite(token)
        if inv is None:
            return _gone()
        try:
            rbac.set_role(f"user:{inv.email}", inv.role)
        except ValueError:  # pragma: no cover - role validated at mint time
            return _gone()
        log.info("invite accepted (local): %s as %s", inv.email, inv.role)
        response = RedirectResponse("/", status_code=303)
        from .oidc_login import _cookie_secure, _set_cookie
        _set_cookie(response, "mvk_session", invites.mint_local_session(inv.email),
                    max_age=invites.session_ttl_seconds(),
                    secure=_cookie_secure(request))
        return response
    principal = _session_principal(request)
    if principal is None:
        return _page("Sign in required",
                     "<p>Sign in with your company account first, then reopen "
                     "the invitation link.</p>", status=401)
    inv = invites.consume_invite(token, used_by=principal.principal)
    if inv is None:
        return _gone()
    try:
        rbac.set_role(principal.principal, inv.role)
    except ValueError:  # pragma: no cover - role validated at mint time
        return _gone()
    log.info("invite accepted (sso): %s bound to %s (invite for %s)",
             principal.principal, inv.role, inv.email)
    return RedirectResponse("/", status_code=303)
