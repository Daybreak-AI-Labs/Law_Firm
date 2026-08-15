"""Tenant lifecycle / provisioning registry — the hosted control plane's roster.

A :class:`Workspace` isolates one tenant's *data*; this registry is the
*operator's* record of which tenants exist, their status (active / suspended),
plan, and per-tenant quota. It lives at the **un-namespaced** root
(``<home>/tenant_registry.json``, ``tenant=None``) because it is cross-tenant
control-plane state, not any one tenant's data.

Lifecycle: ``create`` → ``suspend`` / ``resume`` → ``delete`` (optionally
purging the tenant's data dir). :func:`assert_tenant_active` is the enforcement
hook a request path calls before doing work for a tenant, so a suspended tenant
is refused. The registry is opt-in: with no tenants provisioned the file does
not exist and :func:`assert_tenant_active` is a no-op, so single-tenant and
unprovisioned deployments are unchanged.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, replace

from ..file_lock import (
    atomic_read_text,
    atomic_write_text,
    cross_process_lock,
    ensure_private_directory,
)
from ..paths import (
    _tenant_namespace_key,
    bind_tenant_namespace,
    canonical_tenant_id,
    maverick_home,
)

log = logging.getLogger(__name__)


def _warn_if_unknown_plan(plan: str) -> str | None:
    """Warn (and return the message) if ``plan`` is not a known billing plan.

    ``billing.entitlements_for`` silently resolves an unknown plan to the
    ``free`` entitlements, so an operator who typos ``--plan pro`` would think a
    tenant is paid when it is not. We warn rather than raise so a plan can be
    pre-assigned before it is defined in ``[billing.plans]``."""
    p = str(plan or "free")
    try:
        from ..billing import known_plan_names
        known = known_plan_names()
    except Exception:  # pragma: no cover -- never block provisioning on billing
        return None
    if p in known:
        return None
    msg = (f"plan {p!r} is not a known billing plan "
           f"({', '.join(sorted(known))}); its entitlements fall back to 'free' "
           f"until it is defined in [billing.plans]")
    log.warning("tenant registry: %s", msg)
    return msg

ACTIVE = "active"
SUSPENDED = "suspended"
_STATUSES = frozenset({ACTIVE, SUSPENDED})

# Serializes the roster load-modify-save across threads in this process; the
# cross_process_lock below extends that across processes (the registry is edited
# from both the CLI and the dashboard, which are separate processes).
_REGISTRY_LOCK = threading.Lock()

# Serializes the in-flight reservations load-modify-save (a separate file from
# the roster, so a separate lock: reserving a run's dollars must not contend
# with a suspend/set_quota roster edit). Cross-process flock on the sidecar
# extends it across the CLI / dashboard / serve processes.
_RESERVE_LOCK = threading.Lock()


class TenantSuspended(PermissionError):
    """Raised when work is attempted for a suspended (or deleted) tenant."""


class UnknownTenant(KeyError):
    """Raised when an operation targets a tenant that was never provisioned."""


class TenantRegistryError(RuntimeError):
    """Tenant control-plane state exists but cannot be trusted."""


def _registry_path():
    return maverick_home() / "tenant_registry.json"


@dataclass(frozen=True)
class TenantRecord:
    """One provisioned tenant."""

    id: str
    status: str = ACTIVE
    plan: str = "free"
    display_name: str = ""
    # Per-tenant aggregate spend cap (USD/day); 0 = unlimited (defer to global).
    max_daily_dollars: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "status": self.status, "plan": self.plan,
            "display_name": self.display_name,
            "max_daily_dollars": self.max_daily_dollars,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TenantRecord:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"].strip():
            raise ValueError("tenant record must be an object with an id")
        tenant_id = d["id"]
        if tenant_id != tenant_id.strip():
            raise ValueError("tenant id must be trimmed")
        tenant_id = _validate_id(tenant_id)
        status = d.get("status", ACTIVE)
        if not isinstance(status, str):
            raise ValueError("tenant status must be a string")
        if status not in _STATUSES:
            raise ValueError(f"unknown tenant status: {status!r}")
        try:
            cap = float(d.get("max_daily_dollars", 0.0) or 0.0)
            created = float(d.get("created_at", 0.0) or 0.0)
            updated = float(d.get("updated_at", 0.0) or 0.0)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("tenant numeric fields must be finite numbers") from exc
        if not all(math.isfinite(value) and value >= 0 for value in (cap, created, updated)):
            raise ValueError("tenant numeric fields must be finite and non-negative")
        plan = d.get("plan", "free") or "free"
        display_name = d.get("display_name", "") or ""
        if not isinstance(plan, str) or not isinstance(display_name, str):
            raise ValueError("tenant plan and display_name must be strings")
        return cls(
            id=tenant_id, status=status, plan=plan,
            display_name=display_name, max_daily_dollars=cap,
            created_at=created, updated_at=updated,
        )

    @property
    def active(self) -> bool:
        return self.status == ACTIVE


def _validate_id(tenant_id: str) -> str:
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant id is required")
    # Persist one canonical Unicode spelling and enforce portable path rules.
    return canonical_tenant_id(tid)


def _lookup_id(tenant_id: str | None) -> str:
    tid = (tenant_id or "").strip()
    return canonical_tenant_id(tid) if tid else ""


def _quota_value(value: object) -> float:
    """Return a finite, non-negative daily cap.

    ``inf`` previously persisted successfully and made the next registry read
    fail as corrupt because :meth:`TenantRecord.from_dict` correctly rejects
    it. Validate at the write boundary so an operator input cannot poison the
    control-plane roster.
    """
    try:
        cap = float(value or 0.0)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("tenant quota must be a finite number") from exc
    if not math.isfinite(cap):
        raise ValueError("tenant quota must be a finite number")
    return max(0.0, cap)


def _load() -> dict[str, TenantRecord]:
    from ..file_lock import ensure_private_file

    ensure_private_directory(maverick_home())
    path = _registry_path()
    ensure_private_directory(path.parent)
    if not path.exists():
        return {}
    try:
        ensure_private_file(path)
        raw = _decode_json(atomic_read_text(path), label="tenant registry")
    except TenantRegistryError:
        raise
    except OSError as exc:
        raise TenantRegistryError(f"tenant registry unreadable: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tenants"), list):
        raise TenantRegistryError(
            "tenant registry corrupt: top-level object must contain a tenants list"
        )
    out: dict[str, TenantRecord] = {}
    namespace_owners: dict[str, str] = {}
    for d in raw["tenants"]:
        try:
            rec = TenantRecord.from_dict(d)
        except (TypeError, ValueError) as exc:
            raise TenantRegistryError(f"tenant registry corrupt: {exc}") from exc
        if rec.id in out:
            raise TenantRegistryError(f"tenant registry corrupt: duplicate tenant {rec.id!r}")
        namespace_key = _tenant_namespace_key(rec.id)
        prior = namespace_owners.get(namespace_key)
        if prior is not None and prior != rec.id:
            raise TenantRegistryError(
                "tenant registry corrupt: tenant ids "
                f"{prior!r} and {rec.id!r} alias the same filesystem namespace"
            )
        namespace_owners[namespace_key] = rec.id
        out[rec.id] = rec
    return out


def _decode_json(raw: str, *, label: str) -> object:
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
        raise TenantRegistryError(f"{label} corrupt: {exc}") from exc


def _save(records: dict[str, TenantRecord]) -> None:
    payload = {"tenants": [records[k].to_dict() for k in sorted(records)]}
    # Atomic temp+replace (0600): a bare O_TRUNC write truncates in place, so a
    # concurrent _load()/is_active() reader sees a half-written file -> its
    # JSONDecodeError is swallowed as an EMPTY roster, which spuriously refuses a
    # legitimately-active tenant. Each mutator also holds _REGISTRY_LOCK +
    # cross_process_lock across the whole load-modify-save so two edits (e.g. a
    # suspend racing a set_quota) can't have the second save clobber the first.
    atomic_write_text(
        _registry_path(),
        json.dumps(payload, indent=2, sort_keys=True),
    )


def _locked(*, strict: bool = False):
    """Serialize a roster load-modify-save in-process AND cross-process.

    ``strict=True`` is used when a roster read participates in an admission
    transaction.  On a filesystem without a real process lock, admission must
    fail closed instead of racing a control-plane cap change.
    """
    from contextlib import ExitStack
    ensure_private_directory(maverick_home())
    ensure_private_directory(_registry_path().parent)
    stack = ExitStack()
    stack.enter_context(_REGISTRY_LOCK)
    stack.enter_context(cross_process_lock(_registry_path(), strict=strict))
    return stack


def list_tenants() -> list[TenantRecord]:
    records = _load()
    return [records[k] for k in sorted(records)]


def get_tenant(tenant_id: str) -> TenantRecord | None:
    return _load().get(_lookup_id(tenant_id))


def create_tenant(
    tenant_id: str, *, plan: str = "free", display_name: str = "",
    max_daily_dollars: float = 0.0,
) -> TenantRecord:
    """Provision a tenant + its workspace dir. Raises ValueError if it exists."""
    tid = _validate_id(tenant_id)
    quota = _quota_value(max_daily_dollars)
    # Provisioning a named tenant IS multi-tenant mode — a paid (Platinum)
    # capability. Fail-open: a no-op unless license enforcement is on and the
    # deployment isn't entitled (a genuine single-tenant box never calls this —
    # it uses the default tenant).
    try:
        from ..entitlements import require
        allowed = require("multi_tenant")
    except Exception:  # pragma: no cover - entitlements missing => don't block
        allowed = True
    if not allowed:
        raise PermissionError(
            "multi-tenant provisioning requires a Platinum license "
            "(license enforcement is on and this deployment is not entitled)")
    _warn_if_unknown_plan(plan)
    with _locked():
        records = _load()
        if tid in records:
            raise ValueError(f"tenant already exists: {tid!r}")
        namespace_key = _tenant_namespace_key(tid)
        for existing in records:
            if _tenant_namespace_key(existing) == namespace_key:
                raise ValueError(
                    f"tenant id {tid!r} aliases existing tenant {existing!r}"
                )
        # Reserve the portable name and provision the confidentiality boundary
        # BEFORE publishing an active roster entry. The registry lock keeps the
        # admission check, claim, directory creation, and publication atomic to
        # other provisioning processes.
        bind_tenant_namespace(tid)
        from ..workspace import Workspace
        ensure_private_directory(Workspace(tid).root)
        now = time.time()
        rec = TenantRecord(
            id=tid, status=ACTIVE, plan=plan, display_name=display_name,
            max_daily_dollars=quota,
            created_at=now, updated_at=now,
        )
        records[tid] = rec
        _save(records)
    return rec


def _mutate(tenant_id: str, **changes) -> TenantRecord:
    tid = _lookup_id(tenant_id)
    with _locked():
        records = _load()
        rec = records.get(tid)
        if rec is None:
            raise UnknownTenant(tid)
        rec = replace(rec, updated_at=time.time(), **changes)
        records[tid] = rec
        _save(records)
    return rec


def suspend_tenant(tenant_id: str) -> TenantRecord:
    return _mutate(tenant_id, status=SUSPENDED)


def resume_tenant(tenant_id: str) -> TenantRecord:
    return _mutate(tenant_id, status=ACTIVE)


def _audit_billing_change(field: str, tenant_id: str, *, old, new) -> None:
    """Record a tamper-evident audit row for a change to a tenant's billing
    terms (plan / daily cap), so an upgrade or cap change is provable rather than
    a silent edit. Ordinary writer outages remain fail-soft; an explicit
    policy/custody refusal propagates."""
    from ..audit import EventKind, audit_event

    kind = (EventKind.TENANT_PLAN_CHANGED if field == "plan"
            else EventKind.TENANT_QUOTA_CHANGED)
    audit_event(
        kind, agent="operator", tenant=tenant_id, field=field,
        old=old, new=new,
    )


def _mutate_billing(tenant_id: str, field: str, value: object) -> TenantRecord:
    """Commit a billing mutation and its audit result as one locked operation.

    On a strict audit refusal the roster is restored before the exception
    propagates. Keeping the original read, write, audit, and possible rollback
    under one registry lock also prevents the old/new evidence from racing a
    concurrent operator update.
    """
    tid = _lookup_id(tenant_id)
    attr = "plan" if field == "plan" else "max_daily_dollars"
    with _locked():
        records = _load()
        old_rec = records.get(tid)
        if old_rec is None:
            raise UnknownTenant(tid)
        prior = dict(records)
        old = getattr(old_rec, attr)
        rec = replace(old_rec, updated_at=time.time(), **{attr: value})
        records[tid] = rec
        _save(records)
        from ..audit import AuditRefused

        try:
            _audit_billing_change(field, rec.id, old=old, new=getattr(rec, attr))
        except AuditRefused:
            try:
                _save(prior)
            except Exception as rollback_exc:
                raise TenantRegistryError(
                    "audit refused tenant billing change and rollback failed"
                ) from rollback_exc
            raise
    return rec


def set_quota(tenant_id: str, max_daily_dollars: float) -> TenantRecord:
    rec = _mutate_billing(
        tenant_id,
        "quota",
        _quota_value(max_daily_dollars),
    )
    return rec


def set_plan(tenant_id: str, plan: str) -> TenantRecord:
    _warn_if_unknown_plan(plan)
    rec = _mutate_billing(tenant_id, "plan", str(plan or "free"))
    return rec


def delete_tenant(tenant_id: str, *, purge: bool = False) -> bool:
    """Remove a tenant from the registry. With ``purge=True`` also delete its
    data directory (irreversible). Returns False if the tenant was unknown."""
    tid = _lookup_id(tenant_id)
    with _locked():
        records = _load()
        if tid not in records:
            return False
        del records[tid]
        _save(records)
    if purge:
        import shutil

        from ..workspace import Workspace
        root = Workspace(tid).root
        # Only purge under the tenants/ tree, never the shared root.
        if "tenants" in root.parts:
            shutil.rmtree(root, ignore_errors=True)
    return True


def is_active(tenant_id: str | None) -> bool:
    """Whether a tenant may do work.

    The registry is opt-in: before any roster file exists, named tenants are
    accepted for single-tenant and unprovisioned deployments. Once a roster
    exists, only provisioned active tenants may do work; unknown tenant IDs
    include deleted tenants and are refused.
    """
    tid = _lookup_id(tenant_id)
    if not tid:
        return True
    try:
        rec = _load().get(tid)
    except TenantRegistryError:
        return False
    if rec is None:
        return not _registry_path().exists()
    return rec.active


def assert_tenant_active(tenant_id: str | None) -> None:
    """Enforcement hook: raise :class:`TenantSuspended` for inactive tenants.
    No-op for None / deployments without a registry, so existing flows are
    unchanged until tenant provisioning is enabled.
    """
    tid = _lookup_id(tenant_id)
    if not tid:
        return
    path = _registry_path()
    if not path.exists():
        return
    # Unlike the convenience bool returned by is_active(), admission paths
    # need to distinguish a legitimate suspension from unreadable or corrupt
    # control-plane state.  Let TenantRegistryError propagate so callers can
    # emit an operator-visible policy outage and, critically, still deny work.
    rec = _load().get(tid)
    if rec is None or not rec.active:
        raise TenantSuspended(f"tenant is suspended or unknown: {tenant_id!r}")


def tenant_spend_today(tenant_id: str) -> float:
    """Today's recorded spend (dollars) across the tenant's usage ledger.

    Reads the tenant-scoped ledger (``tenants/<t>/usage/ledger.json`` — the
    same file the orchestrator's per-principal recording lands in when the
    run is tenant-pinned) and sums every principal's bucket for the current
    UTC day. An unreadable/corrupt ledger counts as infinite spend so a policy
    outage cannot reset a capped tenant's apparent usage to zero.
    """
    tid = _lookup_id(tenant_id)
    from ..paths import data_dir
    from ..quotas import UsageLedger, _today
    try:
        ledger = UsageLedger(data_dir("usage", "ledger.json", tenant=tid))
        data = ledger._load()
        day = _today()
        return sum(
            float((days.get(day) or {}).get("dollars", 0.0))
            for days in data.values() if isinstance(days, dict)
        )
    except Exception as e:
        log.error("tenant_spend_today: unreadable usage ledger for %r: %s; "
                  "treating spend as over cap", tid, e)
        return float("inf")


def _enforce_plan_caps() -> bool:
    """Opt-in: when a tenant has no explicit registry spend cap, fall back to its
    billing plan's entitlement-level daily cap so a config-defined plan cap is
    actually enforced rather than decorative (audit #81).

    Off by default: a registry cap of 0 means "unlimited" today, so enabling this
    changes already-provisioned tenants. ``MAVERICK_ENFORCE_PLAN_CAPS`` env wins
    over ``[billing] enforce_plan_caps``."""
    env = os.environ.get("MAVERICK_ENFORCE_PLAN_CAPS")
    if env is not None and env.strip() != "":
        value = env.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise TenantRegistryError("MAVERICK_ENFORCE_PLAN_CAPS must be a boolean")
    try:
        from ..config import config_source_errors, load_config

        cfg = load_config()
        if config_source_errors():
            raise TenantRegistryError(
                "tenant plan-cap policy unavailable: an active config source is invalid"
            )
        billing = cfg.get("billing") or {}
        if not isinstance(billing, dict):
            raise TenantRegistryError("[billing] must be a table")
        value = billing.get("enforce_plan_caps", False)
        if not isinstance(value, bool):
            raise TenantRegistryError("[billing] enforce_plan_caps must be a boolean")
        return value
    except TenantRegistryError:
        raise
    except Exception as exc:
        raise TenantRegistryError(f"tenant plan-cap policy unavailable: {exc}") from exc


def tenant_over_quota(tenant_id: str | None) -> str | None:
    """Human-readable reason when ``tenant_id`` is over its provisioned
    daily-spend cap, else None.

    The cap is ``max_daily_dollars`` on the tenant's roster record (set via
    ``maverick tenant quota``); 0/unset, an unprovisioned tenant, or no
    roster all mean "no cap" — enforcement is opt-in per tenant.
    """
    tid = _lookup_id(tenant_id)
    if not tid:
        return None
    if _load().get(tid) is None:
        return None
    cap = _tenant_daily_cap(tid)
    if cap <= 0:
        return None
    spent = tenant_spend_today(tid)
    if spent >= cap:
        return (f"workspace {tid!r} is over its daily spend cap "
                f"(${spent:.2f} >= ${cap:.2f}); resets at midnight UTC")
    return None


def _tenant_daily_cap(tenant_id: str) -> float:
    """The effective daily spend cap (USD) for ``tenant_id``: the registry cap,
    falling back to the plan entitlement cap when plan-cap enforcement is on.
    ``0`` means no cap. Shared by :func:`tenant_over_quota` and
    :func:`tenant_remaining_today` so both read the same ceiling."""
    rec = _load().get(_lookup_id(tenant_id))
    if rec is None:
        return 0.0
    cap = rec.max_daily_dollars
    if cap <= 0 and _enforce_plan_caps():
        try:
            from ..billing import entitlements_for
            cap = entitlements_for(rec.plan).max_daily_dollars
        except Exception as exc:
            raise TenantRegistryError(
                f"tenant plan-cap entitlement unavailable for {rec.id!r}: {exc}"
            ) from exc
    return cap if cap > 0 else 0.0


# ---- In-flight dollar reservations (concurrent daily-cap enforcement) --------
#
# The usage ledger is written only at run END, so N runs that START for one
# tenant before any of them records spend would each read the same remaining
# allowance and each clamp to the full remainder -- collectively overshooting
# the daily ceiling by up to N x (finding #2). A run therefore RESERVES its
# clamped per-run dollar cap here at start; tenant_remaining_today subtracts
# outstanding (non-expired) reservations as well as recorded spend, so a
# concurrent start sees the in-flight hold. Each reservation carries a TTL, so a
# crashed run that never records spend (and so never releases) only dents the
# cap until the hold expires -- the cap self-heals rather than being permanently
# reduced. Stored as a sibling of the tenant usage ledger
# (``tenants/<t>/usage/reservations.json``) with the same atomic
# temp-file+os.replace + cross-process flock discipline the ledger uses.


def _reservations_path(tenant_id: str):
    from ..paths import data_dir
    return data_dir("usage", "reservations.json", tenant=tenant_id)


def _load_reservations(tenant_id: str) -> dict:
    from ..file_lock import ensure_private_file

    ensure_private_directory(maverick_home())
    path = _reservations_path(tenant_id)
    ensure_private_directory(path.parent)
    if not path.exists():
        return {}
    try:
        ensure_private_file(path)
        raw = _decode_json(atomic_read_text(path), label="tenant reservation store")
    except TenantRegistryError:
        raise
    except OSError as exc:
        raise TenantRegistryError(f"tenant reservation store unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise TenantRegistryError(
            "tenant reservation store corrupt: top-level value must be an object"
        )
    out: dict[str, dict[str, float]] = {}
    for reservation_id, value in raw.items():
        if not reservation_id or reservation_id != reservation_id.strip():
            raise TenantRegistryError("tenant reservation ids must be non-blank strings")
        if not isinstance(value, dict):
            raise TenantRegistryError(
                f"tenant reservation {reservation_id!r} must be an object"
            )
        try:
            amount = float(value["dollars"])
            expiry = float(value["expires_at"])
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise TenantRegistryError(
                f"tenant reservation {reservation_id!r} has invalid numeric fields"
            ) from exc
        if not math.isfinite(amount) or amount <= 0 or not math.isfinite(expiry) or expiry <= 0:
            raise TenantRegistryError(
                f"tenant reservation {reservation_id!r} must be finite and positive"
            )
        out[reservation_id] = {"dollars": amount, "expires_at": expiry}
    return out


def _live_reservations(data: dict, now: float) -> dict:
    """Return only the well-formed, non-expired, positive-amount holds in
    ``data`` (the pruning + parsing shared by the read and write paths)."""
    out: dict = {}
    for rid, v in data.items():
        if not isinstance(v, dict):
            continue
        try:
            exp = float(v.get("expires_at", 0.0))
            amt = float(v.get("dollars", 0.0))
        except (TypeError, ValueError):
            continue
        if exp > now and amt > 0:
            out[str(rid)] = {"dollars": amt, "expires_at": exp}
    return out


def _outstanding_reservations(tenant_id: str | None, *, now: float | None = None) -> float:
    """Sum of a tenant's outstanding (non-expired) in-flight dollar holds."""
    tid = _lookup_id(tenant_id)
    if not tid:
        return 0.0
    at = time.time() if now is None else now
    return sum(v["dollars"] for v in _live_reservations(_load_reservations(tid), at).values())


def reserve_tenant_dollars(tenant_id: str | None, amount: float,
                           ttl_seconds: float, reservation_id: str) -> None:
    """Hold ``amount`` dollars in-flight against ``tenant_id`` under
    ``reservation_id`` for ``ttl_seconds`` from now.

    Makes concurrent run starts see each other's not-yet-recorded spend:
    :func:`tenant_remaining_today` subtracts outstanding reservations. The TTL is
    a backstop -- a crashed run that never releases only reduces the cap until
    the hold expires. Atomic read-modify-write (in-process lock + cross-process
    flock + temp-file replace) that also prunes expired holds. A blank tenant/id
    or a non-positive amount is a no-op. Callers wrap this (it must never raise
    into the run path)."""
    tid = _lookup_id(tenant_id)
    if not tid or not reservation_id:
        return
    try:
        amt = float(amount or 0.0)
        ttl = float(ttl_seconds or 0.0)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("reservation amount and TTL must be finite numbers") from exc
    if amt <= 0:
        return
    if not math.isfinite(amt) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("reservation amount and TTL must be finite and positive")
    now = time.time()
    path = _reservations_path(tid)
    with _RESERVE_LOCK, cross_process_lock(path, strict=True):
        data = _live_reservations(_load_reservations(tid), now)
        if str(reservation_id) in data:
            raise TenantRegistryError(
                f"tenant reservation already exists: {reservation_id!r}"
            )
        data[str(reservation_id)] = {"dollars": amt, "expires_at": now + ttl}
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


def reserve_tenant_budget(
    tenant_id: str | None,
    requested_amount: float,
    ttl_seconds: float,
    reservation_id: str,
) -> float | None:
    """Atomically clamp and reserve one run against a tenant's daily cap.

    ``None`` means the tenant has no aggregate cap and the caller should keep
    its own per-run ceiling.  Otherwise the return value is the amount granted
    (possibly ``0.0`` when the daily allowance is exhausted).

    The cap/spend/reservation calculation and reservation write share the
    registry lock followed by the strict reservation lock. Consequently two
    starts cannot both observe the same unreserved remainder, and a concurrent
    ``set_quota``/``set_plan`` cannot publish a changed ceiling between this
    transaction's cap read and hold write. Usage settlement records spend
    *before* releasing its hold, so a concurrent allocator sees either the old
    spend plus the hold, the new spend plus the hold, or the new spend after
    release -- never neither.
    """
    tid = _lookup_id(tenant_id)
    if not tid:
        return None
    if not reservation_id or reservation_id != reservation_id.strip():
        raise ValueError("reservation id must be a non-blank trimmed string")
    try:
        requested = float(requested_amount)
        ttl = float(ttl_seconds)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("reservation amount and TTL must be finite numbers") from exc
    if not math.isfinite(requested) or requested < 0:
        raise ValueError("requested reservation amount must be finite and non-negative")
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("reservation TTL must be finite and positive")

    # Avoid creating a tenant data tree merely to discover that a provisioned
    # tenant is unlimited.  The cap is read again under the allocation lock;
    # this first read is only a side-effect-free fast path.
    if _tenant_daily_cap(tid) <= 0:
        return None

    now = time.time()
    path = _reservations_path(tid)
    # Global lock order is roster -> reservation thread lock -> reservation
    # sidecar. Roster mutators take only the first; direct hold/release paths
    # take only the latter two. Keeping this order prevents lock inversion while
    # making the cap read and hold publication one control-plane transaction.
    with _locked(strict=True):
        with _RESERVE_LOCK, cross_process_lock(path, strict=True):
            cap = _tenant_daily_cap(tid)
            if cap <= 0:
                return None
            data = _live_reservations(_load_reservations(tid), now)
            if reservation_id in data:
                raise TenantRegistryError(
                    f"tenant reservation already exists: {reservation_id!r}"
                )
            outstanding = sum(value["dollars"] for value in data.values())
            remaining = max(0.0, cap - tenant_spend_today(tid) - outstanding)
            granted = min(requested, remaining)
            if granted > 0:
                data[reservation_id] = {
                    "dollars": granted,
                    "expires_at": now + ttl,
                }
                atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))
            return granted


def release_tenant_reservation(tenant_id: str | None, reservation_id: str) -> bool:
    """Release a hold taken by :func:`reserve_tenant_dollars` (e.g. once the run
    has recorded its actual spend, so it isn't double-counted against the cap for
    the rest of its TTL). Also prunes expired holds. Returns True if the id was
    present; a no-op returning False for a blank tenant/id."""
    tid = _lookup_id(tenant_id)
    if not tid or not reservation_id:
        return False
    now = time.time()
    path = _reservations_path(tid)
    with _RESERVE_LOCK, cross_process_lock(path, strict=True):
        data = _load_reservations(tid)
        existed = str(reservation_id) in data
        data.pop(str(reservation_id), None)
        atomic_write_text(
            path, json.dumps(_live_reservations(data, now), indent=2, sort_keys=True))
    return existed


def tenant_remaining_today(tenant_id: str | None) -> float | None:
    """Dollars a tenant may still spend today before hitting its daily cap.

    Returns ``None`` when no cap applies (no tenant, unprovisioned, cap 0/unset,
    enforcement off) so callers leave their own ceiling untouched. Otherwise the
    non-negative remainder ``cap - spent_today - outstanding_reservations`` --
    which a per-run budget can clamp to so a single run can't overshoot the
    tenant's aggregate ceiling (#78). Subtracting outstanding in-flight
    reservations (spend that concurrent runs have committed to but not yet
    recorded) is what closes the concurrency hole in finding #2: two runs that
    start before either records spend see each other's holds instead of both
    clamping to the full remainder. Coordinates the per-run cap with the
    per-tenant cap; without it the over-quota gate only fires *between* runs,
    after the overshoot."""
    tid = _lookup_id(tenant_id)
    if not tid:
        return None
    cap = _tenant_daily_cap(tid)
    if cap <= 0:
        return None
    return max(0.0, cap - tenant_spend_today(tid) - _outstanding_reservations(tid))


__all__ = [
    "ACTIVE", "SUSPENDED", "TenantRecord", "TenantSuspended", "UnknownTenant",
    "TenantRegistryError",
    "list_tenants", "get_tenant", "create_tenant", "suspend_tenant",
    "resume_tenant", "delete_tenant", "set_quota", "set_plan",
    "is_active", "assert_tenant_active", "tenant_spend_today",
    "tenant_over_quota", "tenant_remaining_today",
    "reserve_tenant_dollars", "reserve_tenant_budget",
    "release_tenant_reservation",
]
