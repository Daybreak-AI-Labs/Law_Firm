"""Named SaaS connections -- a first-class, sealed alternative to env-var creds.

An enterprise connector resolves ``<NAME>_BASE_URL`` / ``<NAME>_TOKEN`` from the
environment. That's fine for an operator but a non-starter for a citizen user
who can't touch env vars. A *connection* stores those same two values under a
name (``salesforce`` / ``salesforce-eu``), sealed at rest with the tenant's KMS
key exactly like the OAuth vault, so a connector can be wired from the dashboard
without shell access.

Standard/local resolution keeps legacy env precedence. Authenticated enterprise
execution requires a saved connection whose owner/access grants admit the
current principal; operator-global env credentials are deliberately unavailable
at that boundary.

Gated behind :func:`enabled` (``[connections] enable`` / ``MAVERICK_CONNECTIONS``);
off by default. Token values are sealed and never returned by the listing API --
only ``has_token`` is surfaced.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator, Sequence

from .paths import data_dir
from .tenant.kms import seal_text_for_tenant, unseal_text_for_tenant

_lock = threading.Lock()
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ACCESS = frozenset({"owner", "tenant"})
_MAX_PRINCIPAL_LENGTH = 256
_runtime_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "maverick_connection_principal", default=None,
)


class ConnectionVersionConflict(RuntimeError):
    """The credential changed after a caller began an operation on it."""


def enabled() -> bool:
    from .config import env_flag
    if env_flag("MAVERICK_CONNECTIONS"):
        return True
    try:
        from .config import load_config
        return bool((load_config().get("connections") or {}).get("enable", False))
    except Exception:  # pragma: no cover -- config never gates a read to a crash
        return False


def _path():
    return data_dir("connections", "connections.sealed")


def _load() -> dict[str, dict]:
    from .file_lock import atomic_read_bytes, ensure_private_file
    from .paths import current_tenant_id

    path = _path()
    try:
        ensure_private_file(path)
    except OSError:
        return {}
    try:
        raw = unseal_text_for_tenant(current_tenant_id(), atomic_read_bytes(path))
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data


def _save(data: dict[str, dict]) -> None:
    from .file_lock import atomic_write_bytes, ensure_private_directory
    from .paths import current_tenant_id

    path = _path()
    blob = seal_text_for_tenant(current_tenant_id(), json.dumps(data, sort_keys=True))
    ensure_private_directory(path.parent)
    atomic_write_bytes(path, blob, mode=0o600)


@contextlib.contextmanager
def _mutation_lock() -> Iterator[None]:
    """Serialize credential read-modify-write across threads and processes."""
    from .file_lock import cross_process_lock

    path = _path()
    with _lock, cross_process_lock(path, strict=True):
        yield


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")[:64]


def _valid_principal(value: object) -> bool:
    """Whether ``value`` is in the bounded opaque principal domain."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_PRINCIPAL_LENGTH
        and value.isprintable()
    )


def _require_principal(value: object) -> str:
    if not _valid_principal(value):
        raise ValueError("connection principals must be 1-256 printable characters")
    return value


def _normalize_principals(values: Sequence[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values or ():
        principal = _require_principal(raw)
        if principal not in out:
            out.append(principal)
    return tuple(out)


@contextlib.contextmanager
def bind_principal(principal: str | None) -> Iterator[None]:
    """Bind the authenticated principal allowed to consume saved credentials.

    Tool registries establish this context at the dispatch boundary.  A
    ContextVar is required because synchronous connector functions execute in
    ``asyncio.to_thread`` and concurrent runs must never borrow one another's
    identity.
    """
    if principal is None or principal == "":
        value = None
    else:
        value = _require_principal(principal)
    token = _runtime_principal.set(value)
    try:
        yield
    finally:
        _runtime_principal.reset(token)


def current_principal() -> str | None:
    """Authenticated identity at the connector credential-use boundary."""
    return _runtime_principal.get()


def set_connection(name: str, *, connector: str = "", base_url: str = "",
                   token: str = "", owner: str = "",
                   expected_owner: str | None = None,
                   access: str = "owner",
                   allowed_principals: Sequence[str] | None = None) -> dict:
    """Create or replace a named connection (sealed). ``connector`` is the tool
    name it credentials (e.g. ``salesforce``); ``name`` may differ so one tool
    can have several accounts. Raises ValueError on an unusable name.

    ``expected_owner`` (when not None) makes the overwrite conditional UNDER THE
    STORE LOCK: replacing an existing connection owned by someone else raises
    PermissionError -- the owner check and the write are one atomic step, so an
    API-layer read-then-write can't race a concurrent create (mirrors
    :func:`delete_connection`'s in-store owner enforcement)."""
    slug = normalize_name(name)
    if not _NAME_RE.match(slug):
        raise ValueError("connection name must be a slug (a-z, 0-9, -, _)")
    access = str(access or "owner").strip().lower()
    if access not in _ACCESS:
        raise ValueError("connection access must be 'owner' or 'tenant'")
    grants = _normalize_principals(allowed_principals)
    if owner is None or owner == "":
        owner_value = ""
    else:
        owner_value = _require_principal(owner)
    rec = {
        "name": slug, "connector": normalize_name(connector) or slug,
        "base_url": str(base_url).strip().rstrip("/"), "token": str(token),
        "created": time.time(), "owner": owner_value,
        "access": access, "allowed_principals": list(grants),
        # Internal CAS token. A slow readiness probe for an old credential must
        # never mark a concurrently rotated replacement as authenticated.
        "revision": uuid.uuid4().hex,
    }
    with _mutation_lock():
        data = dict(_load())
        existing = data.get(slug)
        if (expected_owner is not None and existing is not None
                and existing.get("owner", "") != expected_owner):
            raise PermissionError("a connection with that name already exists")
        if isinstance(existing, dict):
            rec["created"] = float(existing.get("created") or rec["created"])
            # Updating credentials invalidates the prior readiness assertion.
            rec.pop("last_test", None)
        data[slug] = rec
        _save(data)
    return _public(rec)


def get_connection(name: str) -> dict | None:
    """The full sealed record (INCLUDING the token) for internal resolution."""
    rec = _load().get(normalize_name(name))
    # Never expose the freshly decrypted store object itself: an internal
    # caller must not mutate credential state without the locked sealed write.
    return dict(rec) if isinstance(rec, dict) else None


def delete_connection(name: str, owner: str | None = None) -> bool:
    slug = normalize_name(name)
    with _mutation_lock():
        data = dict(_load())
        rec = data.get(slug)
        if rec is None or (owner is not None and rec.get("owner", "") != owner):
            return False
        del data[slug]
        _save(data)
    return True


def _public(rec: dict) -> dict:
    """A record safe to return over the API -- the token is never included, only
    whether one is set."""
    access = str(rec.get("access") or ("owner" if rec.get("owner") else "tenant"))
    last_test = rec.get("last_test") if isinstance(rec.get("last_test"), dict) else None
    return {
        "name": rec.get("name", ""), "connector": rec.get("connector", ""),
        "base_url": rec.get("base_url", ""), "has_token": bool(rec.get("token")),
        "created": float(rec.get("created") or 0.0), "owner": rec.get("owner", ""),
        "access": access,
        "allowed_principals": list(rec.get("allowed_principals") or []),
        "last_test": dict(last_test) if last_test is not None else None,
    }


def list_connections(owner: str | None = None) -> list[dict]:
    out = [_public(r) for r in _load().values() if isinstance(r, dict)]
    if owner is not None:
        out = [c for c in out if c.get("owner", "") == owner]
    out.sort(key=lambda c: (c["created"], c["name"]))
    return out


def _may_use(rec: dict, principal: str | None) -> bool:
    """Whether ``principal`` may consume this credential record.

    Legacy ownerless records remain tenant-shared.  Legacy owned records become
    owner-only, closing the historical cross-principal fallback without a data
    migration.  An absent principal is the local/operator path and may use only
    ownerless tenant records; it can never silently borrow an authenticated
    user's credential.
    """
    raw_owner = rec.get("owner")
    if raw_owner is None or raw_owner == "":
        owner = ""
    elif _valid_principal(raw_owner):
        owner = raw_owner
    else:
        return False
    if principal is not None and not _valid_principal(principal):
        return False
    raw_grants = rec.get("allowed_principals") or []
    if not isinstance(raw_grants, list) or any(
        not _valid_principal(p) for p in raw_grants
    ):
        return False
    grants = set(raw_grants)
    access = str(rec.get("access") or ("owner" if owner else "tenant")).strip().lower()
    if principal is not None and (principal == owner or principal in grants):
        return True
    if access == "tenant":
        return principal is not None or not owner
    return principal is None and not owner


def resolve(connector: str, *, principal: str | None = None) -> tuple[str, str] | None:
    """(base_url, token) for the first connection credentialing ``connector``,
    or None. Used as a FALLBACK by the connector layer only when the env var is
    unset, so it never overrides an operator's environment."""
    if not enabled():
        return None
    slug = normalize_name(connector)
    if principal is None:
        caller = _runtime_principal.get()
    elif principal == "":
        caller = None
    else:
        caller = _require_principal(principal)
    # a connection named exactly after the connector wins; else the first one
    # whose "connector" field points at it AND whose use grant admits the
    # authenticated caller.  Sort the fallback so selection is deterministic.
    data = _load()
    rec = data.get(slug)
    if not isinstance(rec, dict) or not _may_use(rec, caller):
        rec = next((r for _, r in sorted(data.items())
                    if isinstance(r, dict) and r.get("connector") == slug
                    and _may_use(r, caller)), None)
    if not rec or not rec.get("token"):
        return None
    return (str(rec.get("base_url") or ""), str(rec.get("token")))


def record_test_result(
    name: str,
    *,
    reachable: bool,
    authenticated: bool,
    status: int | None = None,
    expected_owner: str | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Persist a bounded, non-secret readiness receipt for a connection.

    The receipt is invalidated whenever credentials are replaced.  It is useful
    to authoring/preflight code without retaining response bodies or headers.
    """
    slug = normalize_name(name)
    with _mutation_lock():
        data = dict(_load())
        rec = data.get(slug)
        if not isinstance(rec, dict):
            raise KeyError(slug)
        if expected_owner is not None and rec.get("owner", "") != expected_owner:
            raise PermissionError("connection is not owned by this principal")
        if (
            expected_revision is not None
            and str(rec.get("revision") or "") != str(expected_revision)
        ):
            raise ConnectionVersionConflict(
                "connection changed while its readiness probe was running"
            )
        updated = dict(rec)
        updated["last_test"] = {
            "tested_at": time.time(),
            "reachable": bool(reachable),
            "authenticated": bool(authenticated),
            "status": int(status) if status is not None else None,
        }
        data[slug] = updated
        _save(data)
    return _public(updated)


__all__ = [
    "enabled", "set_connection", "get_connection", "delete_connection",
    "list_connections", "resolve", "normalize_name", "bind_principal",
    "current_principal",
    "record_test_result", "ConnectionVersionConflict",
]
