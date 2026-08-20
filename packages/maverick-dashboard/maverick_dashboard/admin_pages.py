"""Admin pages router: /settings and /users (pages + form handlers).

The first slice of decomposing ``app.py`` by nav group: everything on this
router is the firm admin's software control surface (RBAC "admin" permission
via ``auth.require_global_permission``),
same-origin-checked on every mutation, and rendered with the shared app
templates.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth

router = APIRouter()

def _app():
    """The dashboard app module, resolved lazily.

    This router is included while ``maverick_dashboard.app`` is still
    executing, so a top-level ``from .app import templates`` would race that
    module's initialization; by request time it is fully loaded.
    """
    from . import app as app_module
    return app_module

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = "") -> HTMLResponse:
    """Firm settings for the run model, spend, and provider credentials."""
    auth.require_permission(request, "admin")
    import re as _re

    from maverick.llm import catalog_specs
    from maverick.runner import DEFAULT_MAX_DOLLARS
    from maverick.runtime_overrides import (
        allowed_models,
        budget_override,
        default_model_override,
    )
    model_options = list(catalog_specs())
    seen = {s for s, _ in model_options}
    try:  # admins extend the picker via [models] catalog in config.toml
        from maverick.config import load_config
        for spec in (load_config().get("models", {}) or {}).get("catalog") or []:
            s = str(spec).strip()
            if (
                s
                and ":" in s
                and s.casefold() != "openrouter:auto"
                and s not in seen
                and _re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", s)
            ):
                model_options.append((s, "Custom (config)"))
                seen.add(s)
    except Exception:  # pragma: no cover -- config read fails soft
        pass
    # Admin allow-list: when set, every model picker is capped to it (and the
    # resolver enforces it as a hard cap). Empty = no restriction. The checkbox
    # section lets the admin pick which catalogue models everyone may use; the
    # global picker then offers only those.
    allow = allowed_models()
    label_by_spec = dict(model_options)
    allow_options = [(s, label_by_spec.get(s, "Allowed")) for s in sorted(allow)]
    picker_options = allow_options if allow else model_options
    from maverick_dashboard import settings_store
    cfg_state = settings_store.state()
    saved_msg = {
        "models": "Default model updated.",
        "budget": "Spend cap updated.",
        "allowed": "Allowed models updated.",
        "providers": "Provider keys updated.",
    }.get(saved, "")
    return _app().templates.TemplateResponse(request, "settings.html", {
        "model_options": model_options,
        "picker_options": picker_options,
        "allowed_models": sorted(allow),
        "pinned_model": default_model_override(),
        "providers": cfg_state["providers"],
        "budget": budget_override(),
        "default_budget": DEFAULT_MAX_DOLLARS,
        "saved": saved_msg,
    })

@router.post("/settings/models")
async def settings_set_model(request: Request, model: str = Form("")) -> RedirectResponse:
    """Pin (or clear) the dashboard's default model via the runtime overlay.

    An empty value clears the pin, reverting to config.toml. Secure startup
    fails closed when neither location contains an exact pin. config.toml is
    never written."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick.runtime_overrides import (
        allowed_models,
        clear_default_model,
        set_default_model,
    )
    model = (model or "").strip()
    allow = allowed_models()
    if model and allow and model not in allow:
        raise HTTPException(status_code=400, detail="model not in the allowed list")
    try:
        if model:
            set_default_model(model)
        else:
            clear_default_model()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid model id") from exc
    return RedirectResponse("/settings?saved=models", status_code=303)

@router.post("/settings/budget")
async def settings_set_budget(request: Request, max_dollars: str = Form("")) -> RedirectResponse:
    """Set (or clear) the dashboard's per-goal spend cap via the runtime
    overlay. An empty value clears it, reverting to config.toml / defaults.
    config.toml is never written."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick.runtime_overrides import clear_budget, set_budget
    val = (max_dollars or "").strip()
    try:
        if val:
            set_budget(float(val))
        else:
            clear_budget()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="spend cap must be a positive number",
        ) from exc
    return RedirectResponse("/settings?saved=budget", status_code=303)

@router.post("/settings/models/allowed")
async def settings_set_allowed_models(request: Request) -> RedirectResponse:
    """Set the admin model allow-list from the settings page. Each checked
    ``models`` field is an allowed spec; none checked clears the restriction
    (every exact model allowed again). Saved to the dashboard overlay, never
    config.toml; a selected model outside the set is denied, never replaced."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick.runtime_overrides import set_allowed_models
    form = await request.form()
    try:
        set_allowed_models(form.getlist("models"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid model id") from exc
    return RedirectResponse("/settings?saved=allowed", status_code=303)

@router.post("/settings/providers")
async def settings_set_provider(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
) -> RedirectResponse:
    """Save a provider's API key / base URL to the dashboard config overlay
    (0600). Empty fields are left unchanged (so re-saving a base URL never wipes
    a key you can't see); resolved before env vars; config.toml is never written."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    try:
        settings_store.set_provider(provider.strip(), api_key=api_key, base_url=base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unknown provider") from exc
    return RedirectResponse("/settings?saved=providers", status_code=303)

@router.post("/settings/providers/clear")
async def settings_clear_provider(request: Request, provider: str = Form(...)) -> RedirectResponse:
    """Remove a provider's dashboard-set key (config.toml / env unaffected)."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    try:
        settings_store.clear_provider(provider.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unknown provider") from exc
    return RedirectResponse("/settings?saved=providers", status_code=303)

def _users_context(request: Request) -> dict:
    """Template context for the /users page (shared with the invite handler)."""
    import os as _os

    from maverick.oidc import oidc_enabled

    from . import rbac
    auth_on = (auth.caller_principal(request) is not None
               or oidc_enabled())
    env = (_os.environ.get("MAVERICK_DASHBOARD_ADMINS") or "").strip()
    if env:
        bootstrap = [a.strip() for a in env.split(",") if a.strip()]
    else:
        try:
            from maverick.config import load_config
            raw = (load_config().get("dashboard", {}) or {}).get("admins", []) or []
            seq = raw if isinstance(raw, (list, tuple)) else [raw]
            bootstrap = [str(a).strip() for a in seq if str(a).strip()]
        except Exception:
            bootstrap = []
    saved = {"set": "Role updated.", "removed": "User removed.",
             "invite_revoked": "Invitation revoked."}.get(
        request.query_params.get("saved", ""), "")
    from . import invites
    return {
        "users": sorted(rbac.list_users().items()),
        "roles": rbac.ROLES,
        "bootstrap_admins": sorted(bootstrap),
        "default_role": rbac.default_role(),
        "auth_on": auth_on,
        "you": auth.caller_principal(request),
        "saved": saved,
        "invites_on": invites.invites_enabled(),
        "pending_invites": [i for i in invites.list_invites() if i.pending],
    }

@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request) -> HTMLResponse:
    """Admin: manage dashboard user roles (admin / operator / auditor / viewer)."""
    auth.require_permission(request, "admin")
    return _app().templates.TemplateResponse(request, "users.html",
                                             _users_context(request))

@router.post("/users/set")
async def users_set_role(
    request: Request, principal: str = Form(...), role: str = Form(...),
) -> RedirectResponse:
    """Assign a dashboard role to a user principal (admin only)."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import rbac
    try:
        rbac.set_role(principal.strip(), role.strip(),
                      actor=auth.caller_principal(request) or "local")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid principal or role") from exc
    return RedirectResponse("/users?saved=set", status_code=303)

@router.post("/users/remove")
async def users_remove(request: Request, principal: str = Form(...)) -> RedirectResponse:
    """Offboard a person across sessions, matter ACLs, signoffs, and RBAC."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    target = principal.strip()
    if not target.startswith("user:") or target == "user:dashboard-static-bearer":
        raise HTTPException(status_code=422, detail="a named user principal is required")
    actor = auth.caller_principal(request) or "local"
    from maverick.audit import EventKind, audit_event

    try:
        audited = audit_event(
            EventKind.ACCESS_GRANT_CHANGED,
            agent=actor,
            actor=actor,
            principal=target,
            operation="offboard",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="offboarding audit unavailable") from exc
    if not audited:
        raise HTTPException(status_code=503, detail="offboarding audit unavailable")
    try:
        _app()._world().offboard_matter_principal(target)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="offboarding could not be completed") from exc
    try:
        from .session_revocation import revoke_principal

        revoke_principal(target[len("user:"):])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="session revocation unavailable") from exc
    from . import rbac
    rbac.remove_user(target, actor=actor)
    return RedirectResponse("/users?saved=removed", status_code=303)

@router.post("/users/invite", response_class=HTMLResponse)
async def users_invite(request: Request, email: str = Form(...),
                       role: str = Form(...)) -> HTMLResponse:
    """Mint a single-use invite link (admin only) and show it ONCE.

    The token is never stored (only its hash), so this response is the only
    place the link exists — the admin copies it into an email/chat to the
    invitee. Renders the users page directly instead of redirecting, because a
    redirect would drop the one-time link."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import invites
    if not invites.invites_enabled():
        raise HTTPException(status_code=404, detail="invites are not enabled")
    invited_by = auth.caller_principal(request) or "local-admin"
    try:
        from .public_origin import canonical_url

        public_base = canonical_url("").rstrip("/")
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="firm public URL policy is unavailable"
        ) from exc
    try:
        inv, token = invites.create_invite(email, role, created_by=invited_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    link = public_base + f"/auth/invite/{token}"
    # Best-effort email (fail-soft): the copyable link below is always the
    # fallback, so a broken SMTP account can never strand an invite.
    sent, send_note = invites.send_invite_email(inv, link, invited_by=invited_by)
    return _app().templates.TemplateResponse(request, "users.html", {
        **_users_context(request), "invite_link": link, "invite_email": inv.email,
        "invite_sent": sent, "invite_send_note": send_note,
    })

@router.post("/users/invite/revoke")
async def users_invite_revoke(request: Request,
                              invite_id: str = Form(...)) -> RedirectResponse:
    """Revoke a pending invite link (admin only)."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import invites
    invites.revoke_invite(invite_id.strip())
    return RedirectResponse("/users?saved=invite_revoked", status_code=303)
