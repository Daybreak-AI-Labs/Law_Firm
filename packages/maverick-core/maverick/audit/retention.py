"""Data-retention enforcement.

Reads ``[retention]`` from ``~/.maverick/config.toml`` and prunes:

  - ``~/.maverick/audit/YYYY-MM-DD.ndjson`` files older than
    ``audit_days``.
  - ``episodes`` rows in ``~/.maverick/world.db`` with
    ``ended_at`` older than ``episodes_days``.
  - ``goal_events`` rows with ``ts`` older than ``events_days``.
  - usage-ledger ``(principal, day)`` cost buckets older than
    ``usage_days`` (the per-principal chargeback tally grows forever
    otherwise).

Config defaults are "no pruning" — retention is opt-in. The CLI
exposes ``maverick retention enforce [--dry-run]``.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class RetentionScopeError(RuntimeError):
    """Retention cannot prove which tenant/backend it is authorized to prune."""


def _config() -> dict:
    try:
        from ..config import load_config
        return (load_config() or {}).get("retention") or {}
    # failure-policy: best_effort
    except Exception as e:
        log.debug("retention: cannot load config: %s", e)
        return {}


def _cutoff_for_days(days: int, *, now: float | None = None) -> float:
    now = now if now is not None else time.time()
    return now - max(1, int(days)) * 86400.0


def purge_audit_files(
    *,
    days: int,
    audit_dir: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Remove ``YYYY-MM-DD.ndjson`` files older than ``days``.

    Filename date wins over mtime — easier to reason about, immune to
    filesystem mtime drift from backups/rsync.
    """
    if days is None or int(days) <= 0:
        return {"removed": [], "kept": 0, "reason": "disabled"}
    if audit_dir is None:
        # Match the writer's (home + active-tenant) audit dir; the old frozen
        # ~/.maverick/audit default made enforce() report "no audit dir" and
        # purge nothing whenever MAVERICK_HOME / a tenant moved the real files.
        from ..paths import data_dir
        audit_dir = data_dir("audit")
    if not audit_dir.exists():
        return {"removed": [], "kept": 0, "reason": "no audit dir"}

    cutoff_ts = _cutoff_for_days(days, now=now)
    cutoff_day = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).date()
    removed: list[str] = []
    purged: list[dict] = []
    kept = 0
    for path in sorted(audit_dir.glob("*.ndjson")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            kept += 1
            continue
        # <= : the day-file dated exactly `days` ago is expired and must be
        # purged. Strict < kept it, making the window days+1.
        if day <= cutoff_day:
            if dry_run:
                # Dry-run is intentionally non-mutating, including no lock
                # sidecar creation. The result is advisory and can race a live
                # writer; the enforcing pass takes the strict day lock below.
                tip, count = _tip_and_count(path)
                removed.append(path.name)
                purged.append(
                    {"day": path.stem, "tip_hash": tip, "row_count": count}
                )
                continue

            # Capture and unlink under the exact sidecar lock used by append,
            # GDPR rewrite, and checkpoint publication. Without this, a writer
            # can append between the captured tip and unlink, making the signed
            # purge marker attest to bytes other than those actually destroyed.
            from ..file_lock import cross_process_lock

            try:
                with cross_process_lock(path, strict=True):
                    if not path.exists():
                        continue
                    tip, count = _tip_and_count(path)
                    path.unlink()
            except (OSError, RuntimeError) as e:
                # Retention must not destroy a file when it cannot coordinate
                # with writers or prove which bytes it is deleting.
                log.warning("retention: cannot safely purge %s: %s", path, e)
                kept += 1
                continue

            removed.append(path.name)
            purged.append(
                {"day": path.stem, "tip_hash": tip, "row_count": count}
            )
            # Keep the sidecar as the durable coordination inode. Unlinking it
            # after releasing the OS lock creates an ABA window: another writer
            # may already hold the old inode while a third process creates and
            # locks a replacement. There is at most one sidecar per retained
            # audit day, and checkpoint retirement deliberately reuses it.
        else:
            kept += 1
    if purged and not dry_run:
        # Record the deletion in the append-only anchor ledger, which retention
        # never prunes. Without it every purged day is reported forever as
        # `anchored_file_deleted` -- policy retention forging a permanent
        # tamper alert.
        try:
            from .signing import record_retention_purge
            record_retention_purge(audit_dir, purged)
        # failure-policy: fail_soft_with_audit
        except Exception as e:  # pragma: no cover - must not undo the purge
            log.warning("retention: could not record anchor purge marker: %s", e)
    log.info(
        "retention: audit purge cutoff=%s removed=%d kept=%d dry_run=%s",
        cutoff_day, len(removed), kept, dry_run,
    )
    return {"removed": removed, "kept": kept, "cutoff_day": str(cutoff_day)}


def _tip_and_count(path: Path) -> tuple[str, int]:
    """Signed tip hash and row count of a day-file, or ``("", 0)``.

    Storage limitation is a legal obligation, so an unreadable file is still
    deleted -- but the deletion then cannot be attested, and ``audit verify``
    will report it as ``anchored_file_deleted`` because a purge record with no
    tip is (correctly) refused. Say so at the point of loss rather than leaving
    an operator to discover an unexplained permanent break later.
    """
    try:
        from .signing import _file_tip_and_count
        tip, count = _file_tip_and_count(path)
        return str(tip or ""), int(count or 0)
    # failure-policy: best_effort
    except Exception as e:
        log.warning(
            "retention: cannot read the signed tip of %s (%s); deleting it "
            "anyway to honour storage limitation, but the purge cannot be "
            "attested and `audit verify` will report this day as deleted. "
            "A sealed segment needs its at-rest key available during retention.",
            path, e,
        )
        return "", 0


def _purge_table_by_time(
    db_path: Path,
    table: str,
    time_col: str,
    cutoff_ts: float,
    *,
    dry_run: bool,
) -> int:
    if not db_path.exists():
        return 0
    # Table/column names can't be parameter-bound, so they MUST come from
    # this fixed allow-set — never from caller/user input — to keep the
    # f-string interpolation below injection-free.
    _ALLOWED = {("episodes", "ended_at"), ("goal_events", "ts")}
    if (table, time_col) not in _ALLOWED:
        raise ValueError(
            f"refusing to purge unknown table/column: {table!r}/{time_col!r}"
        )
    conn = sqlite3.connect(str(db_path))
    try:
        # The agent may be writing concurrently; wait rather than fail fast.
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {time_col} IS NOT NULL AND {time_col} < ?",
            (cutoff_ts,),
        )
        (count,) = cur.fetchone()
        if not dry_run and count > 0:
            conn.execute(
                f"DELETE FROM {table} WHERE {time_col} IS NOT NULL AND {time_col} < ?",
                (cutoff_ts,),
            )
            conn.commit()
        return int(count or 0)
    finally:
        conn.close()


def purge_world_episodes(
    *,
    days: int,
    db_path: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Delete ``episodes`` rows ended before ``days`` ago."""
    if days is None or int(days) <= 0:
        return {"deleted": 0, "reason": "disabled"}
    cutoff_ts = _cutoff_for_days(days, now=now)
    if db_path is not None:
        deleted = _purge_table_by_time(
            db_path, "episodes", "ended_at", cutoff_ts, dry_run=dry_run,
        )
    else:
        deleted = _purge_canonical_world(
            "purge_episodes_before", cutoff_ts, dry_run=dry_run,
        )
    log.info("retention: episodes deleted=%d dry_run=%s", deleted, dry_run)
    return {"deleted": deleted, "cutoff_ts": cutoff_ts}


def purge_world_events(
    *,
    days: int,
    db_path: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Delete ``goal_events`` rows older than ``days``."""
    if days is None or int(days) <= 0:
        return {"deleted": 0, "reason": "disabled"}
    cutoff_ts = _cutoff_for_days(days, now=now)
    if db_path is not None:
        deleted = _purge_table_by_time(
            db_path, "goal_events", "ts", cutoff_ts, dry_run=dry_run,
        )
    else:
        deleted = _purge_canonical_world(
            "purge_goal_events_before", cutoff_ts, dry_run=dry_run,
        )
    log.info("retention: goal_events deleted=%d dry_run=%s", deleted, dry_run)
    return {"deleted": deleted, "cutoff_ts": cutoff_ts}


def _purge_canonical_world(
    method_name: str, cutoff_ts: float, *, dry_run: bool,
) -> int:
    """Run a portable retention primitive on the SQLite world backend."""
    from ..world_model import close_world_if_owned, default_db_path, open_world

    # Do not create an empty SQLite database merely because a scheduled
    # retention job ran before the world was initialized.
    if not default_db_path().exists():
        return 0
    world = open_world()
    try:
        method = getattr(world, method_name, None)
        if not callable(method):
            raise RetentionScopeError(
                f"configured world backend lacks retention primitive {method_name}"
            )
        return int(method(float(cutoff_ts), dry_run=dry_run) or 0)
    finally:
        close_world_if_owned(world)


def purge_usage_ledger(
    *,
    days: int,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Prune usage-ledger ``(principal, day)`` buckets older than ``days``."""
    if days is None or int(days) <= 0:
        return {"removed_buckets": 0, "reason": "disabled"}
    from ..quotas import UsageLedger
    ledger = UsageLedger(ledger_path) if ledger_path is not None else UsageLedger()
    result = ledger.prune(int(days), now=now, dry_run=dry_run)
    log.info("retention: usage ledger removed_buckets=%d dry_run=%s",
             result.get("removed_buckets", 0), dry_run)
    return result


def record_retention_marker(report: dict, *, audit_dir: Path | None = None) -> dict | None:
    """Emit a signed audit event recording what a retention run purged.

    Without this, an auditor seeing a gap in the day-files (or a shrunken
    episodes/goal_events table) cannot tell policy-driven retention from
    malicious deletion -- the cross-file anchor ledger even flags a removed
    day-file as a chain break. The marker lands in the LIVE chain (today's
    day-file, which is never itself old enough to be purged), signed by the same
    Ed25519 chain when ``[audit] sign`` is on, so the deletion is itself
    tamper-evidently recorded and reconcilable against the gap.

    Returns the payload written, or None when nothing was actually purged (a
    no-op / dry-run enforce leaves no audit noise). Never raises: a failure to
    write the marker must not undo or block the retention that already ran.
    """
    audit = report.get("audit") or {}
    removed_files = list(audit.get("removed") or [])
    episodes = int((report.get("episodes") or {}).get("deleted") or 0)
    events = int((report.get("goal_events") or {}).get("deleted") or 0)
    usage_buckets = int((report.get("usage_ledger") or {}).get("removed_buckets") or 0)
    if not (removed_files or episodes or events or usage_buckets):
        return None

    payload = {
        "audit_files_removed": len(removed_files),
        "audit_files": removed_files,
        "audit_cutoff_day": audit.get("cutoff_day", ""),
        "episodes_deleted": episodes,
        "goal_events_deleted": events,
        "usage_buckets_removed": usage_buckets,
    }
    try:
        from .events import AuditEvent, EventKind
        if audit_dir is not None:
            # Write into the SAME chain being enforced (tests / explicit tenant
            # dir), not whatever the ambient default resolves to.
            from .writer import AuditLog
            ok = AuditLog(audit_dir).record(
                AuditEvent(
                    ts=time.time(),
                    kind=EventKind.RETENTION_PURGE,
                    agent="system",
                    goal_id=None,
                    payload=payload,
                )
            )
        else:
            from .writer import record as _audit_record
            ok = _audit_record(EventKind.RETENTION_PURGE, **payload)
        return payload if ok else None
    # failure-policy: fail_soft_with_audit
    except Exception as e:  # pragma: no cover - marker must never break retention
        log.warning("retention: could not record retention marker: %s", e)
        return None


def enforce(
    *,
    config: dict | None = None,
    dry_run: bool = False,
    audit_dir: Path | None = None,
    db_path: Path | None = None,
    now: float | None = None,
    _fleet_child: bool = False,
) -> dict:
    """Apply every configured retention rule. Returns a per-rule report.

    ``audit_dir`` defaults to the writer's home/tenant-aware audit dir
    (resolved by :func:`purge_audit_files`), not a frozen path.

    A non-dry-run that actually purged anything also writes a signed
    ``retention_purge`` marker into the audit chain (see
    :func:`record_retention_marker`) so the deletion is tamper-evidently
    recorded.
    """
    cfg = config if config is not None else _config()
    if not cfg:
        return {"status": "disabled", "reason": "no [retention] in config"}

    # An operator-level `retention enforce` must cover every provisioned
    # tenant, not whichever shared SQLite file happens to be the process
    # default.  Explicit paths remain a deliberately single-store maintenance
    # operation (and are heavily used by recovery/tests).
    if not _fleet_child and audit_dir is None and db_path is None:
        from ..paths import current_tenant_id, tenant_scope
        if current_tenant_id() is None:
            from ..tenant.registry import list_tenants

            tenants = list_tenants()
            if tenants:
                reports: dict[str, dict] = {}
                for record in tenants:
                    with tenant_scope(tenant=record.id):
                        reports[record.id] = enforce(
                            config=cfg,
                            dry_run=dry_run,
                            now=now,
                            _fleet_child=True,
                        )
                return {
                    "scope": "tenant_fleet",
                    "dry_run": dry_run,
                    "tenant_count": len(reports),
                    "tenants": reports,
                }

    audit_days = cfg.get("audit_days")
    episodes_days = cfg.get("episodes_days")
    events_days = cfg.get("events_days")
    usage_days = cfg.get("usage_days")

    report: dict = {"dry_run": dry_run}
    if audit_days:
        report["audit"] = purge_audit_files(
            days=audit_days, audit_dir=audit_dir, dry_run=dry_run, now=now,
        )
    if episodes_days:
        report["episodes"] = purge_world_episodes(
            days=episodes_days, db_path=db_path, dry_run=dry_run, now=now,
        )
    if events_days:
        report["goal_events"] = purge_world_events(
            days=events_days, db_path=db_path, dry_run=dry_run, now=now,
        )
    if usage_days:
        report["usage_ledger"] = purge_usage_ledger(
            days=usage_days, dry_run=dry_run, now=now,
        )

    if not dry_run:
        marker = record_retention_marker(report, audit_dir=audit_dir)
        if marker is not None:
            report["marker"] = marker
    return report


__all__ = [
    "RetentionScopeError",
    "enforce",
    "record_retention_marker",
    "purge_audit_files",
    "purge_world_episodes",
    "purge_world_events",
    "purge_usage_ledger",
]
