"""Dashboard RBAC: admin-managed user roles (admin / operator / auditor / viewer).

This is the dashboard's *access-control* layer and is deliberately distinct from
the kernel's ``[roles]`` (``maverick.capability``), which only ATTENUATES an
agent's tool scope. Here a role GRANTS UI/API privilege, so it must never be
conflated with the kernel's attenuating roles.

Safety invariants (see maverick_dashboard.auth):
  * Meaningful only when an auth mode is on (OIDC / reverse-proxy / session).
    In no-token local mode ``caller_principal`` is None and every gate is a
    no-op — the local operator stays omnipotent, exactly as before.
  * A config-pinned bootstrap admin (``MAVERICK_DASHBOARD_ADMINS`` /
    ``[dashboard] admins``) is ALWAYS admin and is not stored here, so a wiped
    or tampered store can never lock every admin out.
  * The roster is control-plane data: one GLOBAL file, never per-tenant.

Store: ``~/.maverick/dashboard-users.json`` (0600), ``{principal: role}``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROLES = ("admin", "operator", "auditor", "viewer")


class RbacStoreError(RuntimeError):
    """RBAC policy exists but cannot be trusted.

    Treating a damaged roster as empty restores the authenticated-user default
    and can promote a deliberately-demoted viewer back to operator. Readers and
    mutators therefore refuse present-but-invalid state instead of overwriting
    or broadening it.
    """

# Serializes a roster load-modify-save in-process; cross_process_lock in
# _locked() extends it across processes (multiple dashboard workers edit roles).
_RBAC_LOCK = threading.Lock()


def _locked(path: Path):
    from contextlib import ExitStack

    from maverick.file_lock import cross_process_lock, ensure_private_directory
    from maverick.paths import maverick_home

    ensure_private_directory(maverick_home())
    ensure_private_directory(path.parent)
    stack = ExitStack()
    stack.enter_context(_RBAC_LOCK)
    stack.enter_context(cross_process_lock(path))
    return stack

# Permission lattice. The "audit" permission gates the audit-trail read surface
# (/api/v1/audit/*): the who-did-what-when record that can name principals, tool
# inputs and costs. It is held by "admin" and by the dedicated read-only
# "auditor" role -- separation of duties, so a compliance reviewer can read the
# audit log WITHOUT also holding operate/admin (run goals, change settings,
# manage users). "auditor" deliberately grants NOTHING operational: audit + view
# only. "operator"/"viewer" do NOT get "audit" -- reading the trail is a
# distinct grant, not implied by operate.
_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({"admin", "audit", "operate", "view"}),  # users, settings, secrets, + all
    "operator": frozenset({"operate", "view"}),                 # run/cancel goals, approve, tools
    "auditor": frozenset({"audit", "view"}),                    # read audit trail (read-only)
    "viewer": frozenset({"view"}),                              # read-only
}


def store_path() -> Path:
    from maverick.paths import maverick_home

    return maverick_home() / "dashboard-users.json"


def default_role() -> str:
    """Role for an authenticated user with no explicit assignment. Defaults to
    ``operator`` (authenticated users keep today's access); set
    ``[dashboard] default_role = "viewer"`` for deny-by-default."""
    try:
        from maverick.config import config_source_errors, load_config

        cfg = load_config()
        if config_source_errors():
            raise RbacStoreError("RBAC defaults unavailable: an active config source is invalid")
        dashboard = cfg.get("dashboard", {})
        if dashboard is None:
            dashboard = {}
        if not isinstance(dashboard, dict):
            raise RbacStoreError("RBAC defaults invalid: [dashboard] must be a table")
        if "default_role" not in dashboard:
            return "operator"
        r = dashboard.get("default_role")
        if isinstance(r, str) and r in ROLES:
            return r
        raise RbacStoreError("RBAC default_role is invalid")
    except RbacStoreError:
        raise
    except Exception as exc:
        raise RbacStoreError(f"RBAC defaults unavailable: {exc}") from exc
def permissions_for(role: str | None) -> frozenset[str]:
    return _PERMISSIONS.get(role or "", frozenset())


def _load() -> dict[str, str]:
    from maverick.file_lock import (
        atomic_read_text,
        ensure_private_directory,
        ensure_private_file,
    )
    from maverick.paths import maverick_home

    p = store_path()
    ensure_private_directory(maverick_home())
    ensure_private_directory(p.parent)
    if not p.exists():
        return {}
    try:
        ensure_private_file(p)
        data = _decode_json_object(atomic_read_text(p), label="RBAC roster")
    except RbacStoreError:
        raise
    except (OSError, ValueError) as exc:
        raise RbacStoreError(f"RBAC roster unreadable or corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise RbacStoreError("RBAC roster corrupt: top-level value must be an object")
    out: dict[str, str] = {}
    for principal, role in data.items():
        if not isinstance(principal, str) or not principal.strip() or principal != principal.strip():
            raise RbacStoreError("RBAC roster corrupt: principals must be non-blank trimmed strings")
        if not isinstance(role, str) or role not in ROLES:
            raise RbacStoreError(f"RBAC roster corrupt: invalid role for {principal!r}")
        out[principal] = role
    return out


def _decode_json_object(raw: str, *, label: str) -> object:
    def _reject_constant(value: str):
        raise ValueError(f"non-finite number {value!r}")

    def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = value
        return out

    try:
        return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_object)
    except (TypeError, ValueError) as exc:
        raise RbacStoreError(f"{label} corrupt: {exc}") from exc


def _write(data: dict[str, str]) -> None:
    # Unique temp + os.replace (0600): the fixed ".json.tmp" collided between
    # two concurrent dashboard workers (one os.replace moved it out from under
    # the other). Cross-process serialization of the RMW is in the mutators.
    from maverick.file_lock import atomic_write_text
    atomic_write_text(store_path(), json.dumps(data, indent=2, sort_keys=True))


def list_users() -> dict[str, str]:
    """All explicitly-assigned {principal: role} (bootstrap admins excluded)."""
    return _load()


def get_stored_role(principal: str) -> str | None:
    return _load().get((principal or "").strip())


def _audit_role_change(actor: str, principal: str, field: str, tenant: str,
                       old, new) -> None:
    """One tamper-evident audit row per role change, so who-granted-whom-what
    is provable rather than a silent JSON edit. Ordinary writer outages remain
    fail-soft; a configured policy/custody refusal propagates."""
    from maverick.audit import EventKind, audit_event

    audit_event(
        EventKind.ACCESS_GRANT_CHANGED,
        agent=actor or "local",
        actor=actor or "local",
        principal=principal,
        field=field,
        tenant=tenant,
        old=old,
        new=new,
    )


def _audit_role_change_with_rollback(
    actor: str,
    principal: str,
    field: str,
    tenant: str,
    old,
    new,
    rollback,
) -> None:
    """Restore the prior roster before propagating an explicit refusal."""
    from maverick.audit import AuditRefused

    try:
        _audit_role_change(actor, principal, field, tenant, old, new)
    except AuditRefused:
        try:
            rollback()
        except Exception as rollback_exc:
            raise RbacStoreError(
                "audit refused RBAC mutation and roster rollback failed"
            ) from rollback_exc
        raise


def set_role(principal: str, role: str, *, actor: str = "") -> None:
    principal = (principal or "").strip()
    if not principal:
        raise ValueError("empty principal")
    if role not in ROLES:
        raise ValueError("unknown role")
    with _locked(store_path()):
        data = _load()
        old = data.get(principal)
        if old == role:
            return
        prior = dict(data)
        data[principal] = role
        _write(data)
        _audit_role_change_with_rollback(
            actor,
            principal,
            "role",
            "",
            old,
            role,
            lambda: _write(prior),
        )


def remove_user(principal: str, *, actor: str = "") -> None:
    principal = (principal or "").strip()
    with _locked(store_path()):
        data = _load()
        prior = dict(data)
        removed = data.pop(principal, None)
        if removed is not None:
            _write(data)
            _audit_role_change_with_rollback(
                actor,
                principal,
                "role",
                "",
                removed,
                None,
                lambda: _write(prior),
            )


# --- Per-tenant role memberships ---------------------------------------------
# A principal can hold a different role per tenant (e.g. admin of "acme",
# viewer of "globex"). This OVERRIDES the global stored role for that tenant
# only. The config-pinned bootstrap admin stays globally admin regardless, so
# tenant memberships can never lock every admin out. Store is a separate global
# control-plane file: ``{tenant: {principal: role}}``.


def tenant_store_path() -> Path:
    from maverick.paths import maverick_home

    return maverick_home() / "dashboard-tenant-roles.json"


def _load_tenant() -> dict[str, dict[str, str]]:
    from maverick.file_lock import (
        atomic_read_text,
        ensure_private_directory,
        ensure_private_file,
    )
    from maverick.paths import maverick_home

    p = tenant_store_path()
    ensure_private_directory(maverick_home())
    ensure_private_directory(p.parent)
    if not p.exists():
        return {}
    try:
        ensure_private_file(p)
        data = _decode_json_object(atomic_read_text(p), label="tenant RBAC roster")
    except RbacStoreError:
        raise
    except (OSError, ValueError) as exc:
        raise RbacStoreError(f"tenant RBAC roster unreadable or corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise RbacStoreError("tenant RBAC roster corrupt: top-level value must be an object")
    out: dict[str, dict[str, str]] = {}
    for tenant, members in data.items():
        if not isinstance(tenant, str) or not tenant.strip() or tenant != tenant.strip():
            raise RbacStoreError("tenant RBAC roster corrupt: tenant ids must be trimmed strings")
        if not isinstance(members, dict):
            raise RbacStoreError(f"tenant RBAC roster corrupt: members for {tenant!r} must be an object")
        clean: dict[str, str] = {}
        for principal, role in members.items():
            if not isinstance(principal, str) or not principal.strip() or principal != principal.strip():
                raise RbacStoreError(
                    f"tenant RBAC roster corrupt: invalid principal in {tenant!r}"
                )
            if not isinstance(role, str) or role not in ROLES:
                raise RbacStoreError(
                    f"tenant RBAC roster corrupt: invalid role for {principal!r}"
                )
            clean[principal] = role
        out[tenant] = clean
    return out


def _write_tenant(data: dict[str, dict[str, str]]) -> None:
    from maverick.file_lock import atomic_write_text
    atomic_write_text(tenant_store_path(),
                      json.dumps(data, indent=2, sort_keys=True))


def get_tenant_role(tenant: str, principal: str) -> str | None:
    """The principal's role within ``tenant``, or None if no membership."""
    members = _load_tenant().get((tenant or "").strip(), {})
    return members.get((principal or "").strip())


def set_tenant_role(tenant: str, principal: str, role: str, *,
                    actor: str = "") -> None:
    tenant = (tenant or "").strip()
    principal = (principal or "").strip()
    if not tenant or not principal:
        raise ValueError("empty tenant or principal")
    if role not in ROLES:
        raise ValueError("unknown role")
    with _locked(tenant_store_path()):
        data = _load_tenant()
        old = data.get(tenant, {}).get(principal)
        if old == role:
            return
        prior = json.loads(json.dumps(data))
        data.setdefault(tenant, {})[principal] = role
        _write_tenant(data)
        _audit_role_change_with_rollback(
            actor,
            principal,
            "tenant_role",
            tenant,
            old,
            role,
            lambda: _write_tenant(prior),
        )


def remove_tenant_role(tenant: str, principal: str, *, actor: str = "") -> None:
    tenant = (tenant or "").strip()
    principal = (principal or "").strip()
    removed = None
    with _locked(tenant_store_path()):
        data = _load_tenant()
        prior = json.loads(json.dumps(data))
        members = data.get(tenant)
        if members:
            removed = members.pop(principal, None)
            if removed is not None:
                if not members:
                    data.pop(tenant, None)
                _write_tenant(data)
                _audit_role_change_with_rollback(
                    actor,
                    principal,
                    "tenant_role",
                    tenant,
                    removed,
                    None,
                    lambda: _write_tenant(prior),
                )


def list_tenant_roles(tenant: str) -> dict[str, str]:
    """All {principal: role} memberships within ``tenant``."""
    return dict(_load_tenant().get((tenant or "").strip(), {}))


__all__ = [
    "ROLES", "RbacStoreError", "store_path", "default_role", "permissions_for",
    "list_users", "get_stored_role", "set_role", "remove_user",
    "tenant_store_path", "get_tenant_role", "set_tenant_role",
    "remove_tenant_role", "list_tenant_roles",
]
