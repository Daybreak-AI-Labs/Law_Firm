"""Admin pages router: /settings and /users (pages + form handlers).

The first slice of decomposing ``app.py`` by nav group: everything on this
router is the site admin's control surface (RBAC "admin" permission via
``auth.require_permission`` for tenant-scoped administration and
``auth.require_global_permission`` for global control-plane writes),
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
    """Operator settings: appearance (theme/density/font/language) + which model
    Lightwork uses by default. Appearance is applied via the existing
    persist_theme middleware (the form GETs back here with the params); the
    model choice is saved to the dashboard-owned runtime overlay."""
    auth.require_permission(request, "admin")
    import re as _re

    from maverick.llm import catalog_specs, model_for_role
    from maverick.runner import DEFAULT_MAX_DOLLARS
    from maverick.runtime_overrides import (
        allowed_models,
        budget_override,
        default_model_override,
        role_model_override,
    )
    roles = [
        ("Orchestrator", "orchestrator"), ("Coder", "coder"),
        ("Researcher", "researcher"), ("Writer", "writer"),
        ("Analyst", "analyst"), ("Verifier", "verifier"),
        ("Summarizer", "summarizer"),
    ]
    role_models = [
        (label, role, model_for_role(role), role_model_override(role))
        for label, role in roles
    ]
    model_options = list(catalog_specs())
    seen = {s for s, _ in model_options}
    try:  # admins extend the picker via [models] catalog in config.toml
        from maverick.config import load_config
        for spec in (load_config().get("models", {}) or {}).get("catalog") or []:
            s = str(spec).strip()
            if s and s not in seen and _re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", s):
                model_options.append((s, "Custom (config)"))
                seen.add(s)
    except Exception:  # pragma: no cover -- config read fails soft
        pass
    # Admin allow-list: when set, every model picker is capped to it (and the
    # resolver enforces it as a hard cap). Empty = no restriction. The checkbox
    # section lets the admin pick which catalogue models everyone may use; the
    # default/per-role pickers then offer only those.
    allow = allowed_models()
    label_by_spec = dict(model_options)
    allow_options = [(s, label_by_spec.get(s, "Allowed")) for s in sorted(allow)]
    picker_options = allow_options if allow else model_options
    from maverick_dashboard import settings_store
    cfg_state = settings_store.state()
    saved_msg = {
        "appearance": "Appearance updated.",
        "models": "Default model updated.",
        "roles": "Per-role models updated.",
        "budget": "Spend cap updated.",
        "allowed": "Allowed models updated.",
        "providers": "Provider keys updated.",
        "webhooks": "Webhook signing secret updated.",
        "capabilities": "Capabilities updated.",
        "features": "Features updated.",
        "visibility": "Page visibility updated.",
        "visibility_reset": "Page visibility reset to defaults.",
    }.get(saved, "")
    from maverick_dashboard import rbac, ui_visibility
    return _app().templates.TemplateResponse(request, "settings.html", {
        "visibility_matrix": ui_visibility.matrix(),
        "visibility_roles": list(rbac.ROLES),
        "role_models": role_models,
        "model_options": model_options,
        "picker_options": picker_options,
        "allowed_models": sorted(allow),
        "pinned_model": default_model_override(),
        "providers": cfg_state["providers"],
        "capabilities": cfg_state["capabilities"],
        "features": cfg_state["features"],
        "budget": budget_override(),
        "default_budget": DEFAULT_MAX_DOLLARS,
        "webhook_configured": _webhook_secret_configured(),
        "saved": saved_msg,
    })


@router.post("/settings/models")
async def settings_set_model(request: Request, model: str = Form("")) -> RedirectResponse:
    """Pin (or clear) the dashboard's default model via the runtime overlay.

    An empty value clears the pin, reverting to config.toml / built-in
    defaults. config.toml is never written."""
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


@router.post("/settings/models/roles")
async def settings_set_role_models(request: Request) -> RedirectResponse:
    """Set/clear per-role model pins from the settings page in one write. Each
    form field is named for a role; an empty value clears that role's pin."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick.runtime_overrides import allowed_models, set_role_models
    roles = ("orchestrator", "coder", "researcher", "writer",
             "analyst", "verifier", "summarizer")
    form = await request.form()
    updates = {r: (form.get(r) or "").strip() for r in roles}
    allow = allowed_models()
    if allow:
        for spec in updates.values():
            if spec and spec not in allow:
                raise HTTPException(
                    status_code=400, detail="model not in the allowed list")
    try:
        set_role_models(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid model id") from exc
    return RedirectResponse("/settings?saved=roles", status_code=303)

@router.post("/settings/models/allowed")
async def settings_set_allowed_models(request: Request) -> RedirectResponse:
    """Set the admin model allow-list from the settings page. Each checked
    ``models`` field is an allowed spec; none checked clears the restriction
    (every model allowed again). Saved to the dashboard overlay, never
    config.toml; ``llm.model_for_role`` then caps every role to this set."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick.runtime_overrides import set_allowed_models
    form = await request.form()
    try:
        set_allowed_models(form.getlist("models"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid model id") from exc
    return RedirectResponse("/settings?saved=allowed", status_code=303)


def _webhook_secret_configured() -> bool:
    """Whether inbound webhooks currently have a signing secret (env, config,
    or the dashboard overlay) -- reported as a boolean only, never the value."""
    try:
        from maverick.webhooks import inbound_secret
        return bool(inbound_secret())
    except Exception:  # pragma: no cover -- never break the settings page
        return False


@router.post("/settings/webhooks")
async def settings_set_webhooks(
    request: Request,
    secret: str = Form(""),
) -> RedirectResponse:
    """Set or clear the webhook signing secret from the settings page (admin).
    Saved to the dashboard config overlay (0600), never the operator's config
    file; an env-provided secret still wins. Global-admin gated: this HMAC
    secret guards every auth-exempt inbound webhook endpoint deployment-wide,
    so a tenant-scoped admin must not be able to set/rotate it."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    settings_store.set_webhooks_secret(secret)
    return RedirectResponse("/settings?saved=webhooks", status_code=303)


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


@router.post("/settings/capabilities")
async def settings_set_capabilities(request: Request) -> RedirectResponse:
    """Activate/deactivate capabilities via the dashboard config overlay."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    form = await request.form()
    for name in settings_store.CAPABILITY_DEFAULTS:
        settings_store.set_toggle("capabilities", name, name in form)
    return RedirectResponse("/settings?saved=capabilities", status_code=303)


@router.post("/settings/features")
async def settings_set_features(request: Request) -> RedirectResponse:
    """Activate/deactivate features via the dashboard config overlay."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    form = await request.form()
    for name in settings_store.FEATURE_DEFAULTS:
        settings_store.set_toggle("features", name, name in form)
    return RedirectResponse("/settings?saved=features", status_code=303)


@router.post("/settings/ui-visibility")
async def settings_set_ui_visibility(request: Request) -> RedirectResponse:
    """Save the page-visibility matrix (admin). Every eligible, unlocked
    page x role checkbox is submitted as ``cell=role:path``; an absent cell is
    hidden. Only differences from the defaults are stored (0600 JSON, never
    config.toml); a role can never be shown a page its permissions don't reach,
    and admins always keep Settings + Users."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import ui_visibility
    form = await request.form()
    ui_visibility.apply_selection(set(form.getlist("cell")))
    return RedirectResponse("/settings?saved=visibility#ui-visibility", status_code=303)


@router.post("/settings/ui-visibility/reset")
async def settings_reset_ui_visibility(request: Request) -> RedirectResponse:
    """Reset every page x role cell to its default visibility (admin)."""
    _app()._require_same_origin(request)
    auth.require_global_permission(request, "admin")
    from maverick_dashboard import ui_visibility
    ui_visibility.clear_overrides()
    return RedirectResponse("/settings?saved=visibility_reset#ui-visibility", status_code=303)


def _users_context(request: Request) -> dict:
    """Template context for the /users page (shared with the invite handler)."""
    import os as _os

    from maverick.oidc import oidc_enabled
    from maverick.proxy_auth import proxy_auth_enabled

    from . import rbac
    auth_on = (auth.caller_principal(request) is not None
               or oidc_enabled() or proxy_auth_enabled())
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
             "suites": "Department access updated.",
             "suites_removed": "Department access removed.",
             "invite_revoked": "Invitation revoked."}.get(
        request.query_params.get("saved", ""), "")
    from maverick.departments import department_title

    from . import invites, suite_grants
    default_suites = suite_grants.default_suites()
    return {
        "users": sorted(rbac.list_users().items()),
        "roles": rbac.ROLES,
        "bootstrap_admins": sorted(bootstrap),
        "default_role": rbac.default_role(),
        "auth_on": auth_on,
        "you": auth.caller_principal(request),
        "saved": saved,
        "suite_grants": sorted(suite_grants.list_grants().items()),
        "all_suites": sorted(((k, department_title(k))
                              for k in suite_grants.known_suites()),
                             key=lambda kv: kv[1]),
        "default_suites": (sorted(default_suites)
                           if default_suites is not None else None),
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
    """Remove a user's explicit role assignment (admin only). A bootstrap admin
    pinned in config is unaffected — it can't be removed here."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import rbac
    rbac.remove_user(principal.strip(),
                     actor=auth.caller_principal(request) or "local")
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
        inv, token = invites.create_invite(email, role, created_by=invited_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    link = str(request.base_url).rstrip("/") + f"/auth/invite/{token}"
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


@router.post("/users/suites/set")
async def users_set_suites(request: Request) -> RedirectResponse:
    """Scope a user to a set of departments (global admin only).

    The grant store is GLOBAL control-plane data (like the role roster), so
    this requires the global admin role — a tenant-local admin may not scope
    users across the deployment. An empty selection is a valid grant meaning
    "no departments"; use Remove to lift scoping entirely."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import suite_grants
    form = await request.form()
    principal = str(form.get("principal", "")).strip()
    suites = [str(s) for s in form.getlist("suites")]
    try:
        suite_grants.set_suites(principal, suites,
                                actor=auth.caller_principal(request) or "local")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid principal or department") from exc
    return RedirectResponse("/users?saved=suites", status_code=303)


@router.post("/users/suites/remove")
async def users_remove_suites(
    request: Request, principal: str = Form(...),
) -> RedirectResponse:
    """Lift a user's department scoping (global admin only)."""
    auth.require_global_permission(request, "admin")
    _app()._require_same_origin(request)
    from . import suite_grants
    suite_grants.remove_grant(principal.strip(),
                              actor=auth.caller_principal(request) or "local")
    return RedirectResponse("/users?saved=suites_removed", status_code=303)

