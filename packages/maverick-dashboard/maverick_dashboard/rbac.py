"""Dashboard RBAC: admin-managed user roles.

This is the dashboard's *access-control* layer and is deliberately distinct from
the kernel's ``[roles]`` (``maverick.capability``), which only ATTENUATES an
agent's tool scope. Here a role GRANTS UI/API privilege, so it must never be
conflated with the kernel's attenuating roles.

Safety invariants (see maverick_dashboard.auth):
  * Meaningful only when named local or OIDC authentication is on.
    In no-token local mode ``caller_principal`` is None and every gate is a
    no-op — the local operator stays omnipotent, exactly as before.
  * A config-pinned bootstrap admin (``MAVERICK_DASHBOARD_ADMINS`` /
    ``[dashboard] admins``) is ALWAYS admin and is not stored here, so a wiped
    or tampered store can never lock every admin out.
  * The roster is firm-wide software authorization; client access comes only
    from exact matter memberships.

Store: ``~/.maverick/dashboard-users.json`` (0600), ``{principal: role}``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROLES = ("admin", "attorney", "operator", "auditor", "viewer")


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
    "admin": frozenset({"admin", "audit", "legal_signoff", "operate", "view"}),
    # A qualified attorney can operate the product and certify legal work, but
    # does not inherit user/settings administration or audit-log custody.
    "attorney": frozenset({"legal_signoff", "operate", "view"}),
    "operator": frozenset({"operate", "view"}),                 # run/cancel goals, tools
    "auditor": frozenset({"audit", "view"}),                    # read audit trail (read-only)
    "viewer": frozenset({"view"}),                              # read-only
}


def store_path() -> Path:
    from maverick.paths import maverick_home

    return maverick_home() / "dashboard-users.json"


def default_role() -> str:
    """Role for an authenticated user with no explicit assignment. Defaults to
    ``viewer`` so a verified but unassigned identity is read-only. Deployments
    may explicitly configure a different default, but privileged roles should
    normally be granted to named principals."""
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
            return "viewer"
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


def _audit_role_change(actor: str, principal: str, field: str, old, new) -> None:
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
        old=old,
        new=new,
    )


def _audit_role_change_with_rollback(
    actor: str,
    principal: str,
    field: str,
    old,
    new,
    rollback,
) -> None:
    """Restore the prior roster before propagating an explicit refusal."""
    from maverick.audit import AuditRefused

    try:
        _audit_role_change(actor, principal, field, old, new)
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
                removed,
                None,
                lambda: _write(prior),
            )


__all__ = [
    "ROLES", "RbacStoreError", "store_path", "default_role", "permissions_for",
    "list_users", "get_stored_role", "set_role", "remove_user",
]
