"""Per-principal usage quotas — the P2 "cost as a managed resource" primitive.

:class:`Budget` caps a *single run*. Quotas cap a *principal* across runs over
a rolling time-window (a calendar day), so an operator can do chargeback /
rate-limit spend by user or team — the cost-governance layer the canonical
agent-OS leaves unowned.

A small persistent :class:`UsageLedger` records cumulative spend (dollars +
input/output tokens) per ``(principal, UTC day)`` under the tenant-aware data
dir (``<data>/usage/ledger.json``), so it is already tenant-isolated. Every
write is atomic-ish (temp file + ``os.replace``) and the whole module is
**fail-soft**: a ledger error logs a warning and never crashes a run — cost
accounting must not be able to take down the agent loop.

Default-off and opt-in, exactly like :func:`maverick.capability.capability_enforced`
and :func:`maverick.agent._risk_proportional_verify_enabled`: with nothing
configured :func:`over_quota` returns ``None`` and behaviour is unchanged. Turn
it on with ``[quotas] enforce = true`` (plus ``max_dollars_per_day`` /
``max_tokens_per_day``) or the ``MAVERICK_QUOTA_*`` env vars.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone

from .file_lock import (
    atomic_read_text,
    atomic_write_text,
    cross_process_lock,
    ensure_private_directory,
    ensure_private_file,
)
from .paths import data_dir

log = logging.getLogger(__name__)

# Serializes the ledger read-modify-write across threads in this process; the
# flock below extends that across processes (dashboard + serve sharing a tenant
# ledger). Without it, two concurrent record()s load the same totals and the
# second save clobbers the first -- spend is undercounted and a principal slips
# past its quota.
_RECORD_LOCK = threading.Lock()


_cross_process_lock = cross_process_lock


class UsageLedgerError(RuntimeError):
    """Usage state exists but cannot be trusted for quota enforcement."""


class QuotaPolicyError(RuntimeError):
    """Quota configuration cannot be resolved safely."""


def _ledger_path():
    """Tenant-scoped ledger location: ``<data>/usage/ledger.json``."""
    return data_dir("usage", "ledger.json")


def _today() -> str:
    """UTC calendar day key (``YYYY-MM-DD``). UTC so the window doesn't shift
    with the host timezone or DST."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _decode_ledger(raw: str) -> object:
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
        raise UsageLedgerError(f"usage ledger corrupt: {exc}") from exc


def _validate_ledger(data: object) -> dict:
    if not isinstance(data, dict):
        raise UsageLedgerError("usage ledger corrupt: top-level value must be an object")
    validated: dict[str, dict[str, dict[str, int | float]]] = {}
    for principal, days in data.items():
        if not principal or principal != principal.strip() or len(principal) > 1024:
            raise UsageLedgerError("usage ledger corrupt: invalid principal")
        if not isinstance(days, dict):
            raise UsageLedgerError(f"usage ledger corrupt: days for {principal!r} must be an object")
        clean_days: dict[str, dict[str, int | float]] = {}
        for day, cell in days.items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError) as exc:
                raise UsageLedgerError(
                    f"usage ledger corrupt: invalid day for {principal!r}"
                ) from exc
            if not isinstance(cell, dict):
                raise UsageLedgerError(
                    f"usage ledger corrupt: bucket {principal!r}/{day!r} must be an object"
                )
            dollars = cell.get("dollars", 0.0)
            in_tokens = cell.get("in_tokens", 0)
            out_tokens = cell.get("out_tokens", 0)
            if isinstance(dollars, bool) or not isinstance(dollars, (int, float)):
                raise UsageLedgerError("usage ledger corrupt: dollars must be numeric")
            if not math.isfinite(float(dollars)) or float(dollars) < 0:
                raise UsageLedgerError("usage ledger corrupt: dollars must be finite and non-negative")
            if (
                isinstance(in_tokens, bool)
                or not isinstance(in_tokens, int)
                or in_tokens < 0
                or isinstance(out_tokens, bool)
                or not isinstance(out_tokens, int)
                or out_tokens < 0
            ):
                raise UsageLedgerError("usage ledger corrupt: token counts must be non-negative integers")
            clean_days[day] = {
                "dollars": float(dollars),
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
            }
        validated[principal] = clean_days
    return validated


class UsageLedger:
    """A persistent ``(principal, day) -> {dollars, in_tokens, out_tokens}`` tally.

    Not a hot path (written once per run, read once per goal start), so each
    call reloads from disk rather than holding shared mutable state — that keeps
    concurrent runs / processes from clobbering each other's totals on the
    last-writer-wins of an in-memory cache. Missing state is empty; corrupt or
    unreadable existing state raises so enforcement cannot interpret it as zero.
    """

    def __init__(self, path=None) -> None:
        self.path = path if path is not None else _ledger_path()

    def _load(self) -> dict:
        ensure_private_directory(self.path.parent)
        if not self.path.exists():
            return {}
        try:
            ensure_private_file(self.path)
            data = _decode_ledger(atomic_read_text(self.path))
        except UsageLedgerError:
            raise
        except OSError as exc:
            raise UsageLedgerError(f"usage ledger unreadable: {exc}") from exc
        return _validate_ledger(data)

    def _save(self, data: dict) -> None:
        ensure_private_directory(self.path.parent)
        atomic_write_text(
            self.path,
            json.dumps(data, sort_keys=True, allow_nan=False),
        )

    def record(
        self,
        principal: str,
        dollars: float,
        in_tokens: int,
        out_tokens: int,
        *,
        day: str | None = None,
    ) -> None:
        """Add one run's spend to ``(principal, day)``. Negative inputs are
        clamped to zero; a blank principal is ignored. Never raises."""
        if not principal:
            return
        if principal != principal.strip() or len(principal) > 1024:
            raise ValueError("principal must be a trimmed string of at most 1024 characters")
        day = day or _today()
        try:
            amount = float(dollars or 0.0)
            in_count = int(in_tokens or 0)
            out_count = int(out_tokens or 0)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("usage values must be finite numbers") from exc
        if not math.isfinite(amount):
            raise ValueError("usage dollars must be finite")
        # Serialize the whole load-modify-save so concurrent records accumulate
        # instead of clobbering each other (in-process lock + cross-process
        # flock on a sidecar file).
        with _RECORD_LOCK, _cross_process_lock(self.path):
            data = self._load()
            bucket = data.setdefault(principal, {})
            cell = bucket.setdefault(day, {"dollars": 0.0, "in_tokens": 0, "out_tokens": 0})
            cell["dollars"] = float(cell.get("dollars", 0.0)) + max(0.0, amount)
            cell["in_tokens"] = int(cell.get("in_tokens", 0)) + max(0, in_count)
            cell["out_tokens"] = int(cell.get("out_tokens", 0)) + max(0, out_count)
            self._save(data)

    def usage(self, principal: str, *, day: str | None = None) -> dict:
        """Return ``{dollars, in_tokens, out_tokens}`` for ``(principal, day)``;
        zeros when nothing is recorded."""
        day = day or _today()
        cell = (self._load().get(principal) or {}).get(day) or {}
        return {
            "dollars": float(cell.get("dollars", 0.0)),
            "in_tokens": int(cell.get("in_tokens", 0)),
            "out_tokens": int(cell.get("out_tokens", 0)),
        }

    def spend_by_principal(self, *, day: str | None = None) -> dict[str, float]:
        """``{principal: dollars}`` for ``day`` (default today) — the per-user
        spend enumeration the point lookup :meth:`usage` couldn't provide (no
        way to answer "which user is burning budget?"). Skips zero-spend
        principals; empty when nothing is recorded."""
        day = day or _today()
        out: dict[str, float] = {}
        for principal, days in (self._load() or {}).items():
            dollars = float(((days or {}).get(day) or {}).get("dollars", 0.0))
            if dollars:
                out[str(principal)] = dollars
        return out

    def prune(self, keep_days: int, *, now: float | None = None,
              dry_run: bool = False) -> dict:
        """Drop ``(principal, day)`` buckets older than ``keep_days`` days.

        The ledger accrues a row per ``(principal, UTC day)`` forever; this is
        the retention valve. A day is expired when it is on or before the cutoff
        (``now - keep_days*86400``), matching the audit-file window. Principals
        left with no days are dropped. ``keep_days <= 0`` is a no-op (retention
        disabled). Returns a report; never raises.
        """
        if not keep_days or int(keep_days) <= 0:
            return {"removed_buckets": 0, "removed_principals": 0, "reason": "disabled"}
        now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        cutoff_ts = now_ts - max(1, int(keep_days)) * 86400.0
        # YYYY-MM-DD sorts lexicographically == chronologically, so a string
        # compare against the cutoff day is the same window the SQL purges use.
        cutoff_day = datetime.fromtimestamp(cutoff_ts, timezone.utc).strftime("%Y-%m-%d")
        removed_buckets = 0
        removed_principals = 0
        with _RECORD_LOCK, _cross_process_lock(self.path):
            data = self._load()
            for principal in list(data.keys()):
                days = data.get(principal) or {}
                expired = [d for d in days if isinstance(d, str) and d <= cutoff_day]
                remaining = len(days) - len(expired)
                for day in expired:
                    removed_buckets += 1
                    if not dry_run:
                        del days[day]
                # A principal is dropped when no day-buckets would remain after
                # the purge. Decide from the pre-purge counts so the dry-run
                # report matches what a real run removes.
                if remaining <= 0:
                    removed_principals += 1
                    if not dry_run:
                        del data[principal]
            if not dry_run and removed_buckets:
                self._save(data)
        return {"removed_buckets": removed_buckets,
                "removed_principals": removed_principals,
                "cutoff_day": cutoff_day}


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise QuotaPolicyError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise QuotaPolicyError(f"{name} must be a finite non-negative number")
    return value


def _quota_config() -> dict:
    """Merge ``[quotas]`` config under the ``MAVERICK_QUOTA_*`` env vars.

    Env wins over config (same precedence the rest of the kernel uses for
    opt-in toggles). Returns ``enforce`` plus the two caps; ``0``/unset cap
    means "no limit on this dimension". Invalid or unreadable policy raises so
    enforcement cannot silently switch itself off.
    """
    cfg: dict = {}
    try:
        from .config import config_source_errors, load_config

        loaded = load_config()
        if config_source_errors():
            raise QuotaPolicyError("quota policy unavailable: an active config source is invalid")
        cfg = loaded.get("quotas") or {}
        if not isinstance(cfg, dict):
            raise QuotaPolicyError("[quotas] must be a table")
    except QuotaPolicyError:
        raise
    except Exception as exc:
        raise QuotaPolicyError(f"quota policy unavailable: {exc}") from exc

    raw_enforce = cfg.get("enforce", False)
    if not isinstance(raw_enforce, bool):
        raise QuotaPolicyError("[quotas] enforce must be a boolean")
    enforce = raw_enforce
    env_enforce = os.environ.get("MAVERICK_QUOTA_ENFORCE")
    if env_enforce is not None and env_enforce.strip():
        value = env_enforce.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            enforce = True
        elif value in {"0", "false", "no", "off"}:
            enforce = False
        else:
            raise QuotaPolicyError("MAVERICK_QUOTA_ENFORCE must be a boolean")

    def _cap(env_name: str, cfg_key: str) -> float:
        env_val = _env_float(env_name)
        if env_val is not None:
            return env_val
        try:
            value = float(cfg.get(cfg_key, 0) or 0)
        except (OverflowError, TypeError, ValueError) as exc:
            raise QuotaPolicyError(f"[quotas] {cfg_key} must be numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise QuotaPolicyError(f"[quotas] {cfg_key} must be finite and non-negative")
        return value

    return {
        "enforce": enforce,
        "max_dollars_per_day": _cap("MAVERICK_QUOTA_MAX_DOLLARS_PER_DAY", "max_dollars_per_day"),
        "max_tokens_per_day": _cap("MAVERICK_QUOTA_MAX_TOKENS_PER_DAY", "max_tokens_per_day"),
    }


def quotas_enforced() -> bool:
    """Opt-in, off by default. ``MAVERICK_QUOTA_ENFORCE=1`` or ``[quotas]
    enforce = true`` turns on the per-principal daily quota check."""
    return _quota_config()["enforce"]


def record_usage(
    principal: str,
    dollars: float,
    in_tokens: int = 0,
    out_tokens: int = 0,
    *,
    reservation_id: str | None = None,
) -> None:
    """Record a finished run's spend against ``principal`` for today's window.

    Always safe to call (even when enforcement is off — recording is how the
    ledger accrues chargeback data); fail-soft on any ledger error.

    ``reservation_id``: when the run's budget reserved an in-flight tenant dollar
    hold at start (``budget._tenant_reservation_id`` from
    :func:`maverick.budget.budget_from_config`), pass it here so the hold is
    released now that this run's actual spend is on the ledger — otherwise the
    reservation lingers against the tenant's remaining allowance until its TTL
    backstop expires. Lingering is conservative (it only ever *lowers* remaining,
    never raises it), so releasing is an optimization, not a correctness
    requirement. Release is fail-soft on the same tenant scope the ledger write
    used.
    """
    recorded = False
    try:
        UsageLedger().record(principal, dollars, in_tokens, out_tokens)
        recorded = bool(principal)
    except Exception as e:  # pragma: no cover - ledger is fully fail-soft
        log.warning("quotas: failed to record usage for %r: %s", principal, e)
    # Release only after the ledger accepted the actual spend. If recording
    # failed, retaining the hold until its TTL is conservative and prevents an
    # accounting outage from reopening daily allowance for unmetered work.
    if reservation_id and recorded:
        try:
            from .paths import current_tenant_id
            from .tenant.registry import release_tenant_reservation
            release_tenant_reservation(current_tenant_id(), reservation_id)
        except Exception as e:  # pragma: no cover -- reservation release is fail-soft
            log.warning("quotas: failed to release reservation %r: %s",
                        reservation_id, e)


def over_quota(principal: str) -> str | None:
    """Return a human-readable reason if ``principal`` is over its daily quota,
    else ``None``.

    Returns ``None`` (allow) when enforcement is off, no caps are configured,
    or the principal is blank. An unreadable/corrupt ledger raises so the
    orchestration chokepoint can block instead of treating unknown spend as 0.
    """
    cfg = _quota_config()
    if not cfg["enforce"] or not principal:
        return None
    max_dollars = cfg["max_dollars_per_day"]
    max_tokens = cfg["max_tokens_per_day"]
    if max_dollars <= 0 and max_tokens <= 0:
        return None
    used = UsageLedger().usage(principal)
    if max_dollars > 0 and used["dollars"] >= max_dollars:
        return (
            f"principal {principal!r} is over its daily spend quota "
            f"(${used['dollars']:.2f} >= ${max_dollars:.2f})"
        )
    total_tokens = used["in_tokens"] + used["out_tokens"]
    if max_tokens > 0 and total_tokens >= max_tokens:
        return (
            f"principal {principal!r} is over its daily token quota "
            f"({total_tokens} >= {int(max_tokens)} tokens)"
        )
    return None


__all__ = [
    "UsageLedger",
    "UsageLedgerError",
    "QuotaPolicyError",
    "quotas_enforced",
    "record_usage",
    "over_quota",
]
