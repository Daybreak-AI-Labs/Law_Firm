"""Persistent job queue for background goals.

A pure-Python SQLite-backed queue that lets the orchestrator (or
the dashboard) enqueue work that should run *later* without keeping
the request thread alive:

  - "schedule this goal for 3am"
  - "retry this failed goal in 10 minutes"
  - "rate-limit me to 1 of these per minute"

Distinct from ``maverick.runner``, which spawns goals immediately
in a thread pool. The queue is the durable layer underneath: it
survives process restarts and gives the dashboard a single place to
see what's pending.

Schema: ONE table ``jobs(id, kind, payload, run_at, status, attempts,
last_error, created_at, updated_at)``. SQLite WAL mode for concurrent
readers (dashboard) + writer (worker).

Workflow:

  - ``enqueue(kind, payload, run_at=None)`` -> job id
  - ``claim(now, ready_at)``                -> next ready job (atomic UPDATE)
  - ``complete(job_id)``                    -> mark done
  - ``fail(job_id, error, retry_after=60)`` -> bump attempts + reschedule
  - ``list(status='pending')``              -> rows for the dashboard
  - ``purge(older_than_days=7)``            -> sweep terminal rows
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .paths import data_dir

log = logging.getLogger(__name__)


# Compatibility override for callers/tests that historically monkeypatched the
# module constant.  ``None`` means "resolve the active tenant at construction
# time".  Computing ``data_dir("jobs.db")`` here used to freeze whichever
# tenant (usually the shared root) happened to be active when this module was
# first imported, then leak that path into every later tenant scope.
DEFAULT_DB: Path | None = None


def _default_db() -> Path:
    return Path(DEFAULT_DB) if DEFAULT_DB is not None else data_dir("jobs.db")


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    run_at: float
    status: str
    attempts: int = 0
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  run_at      REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, run_at);

-- Durable at-most-once claims for authenticated queue dispatch envelopes.
-- The database itself is tenant-scoped by JobQueue's lazy default path.
CREATE TABLE IF NOT EXISTS dispatch_envelope_claims (
  message_id    TEXT PRIMARY KEY,
  nonce_hash    TEXT NOT NULL,
  expires_at    REAL NOT NULL,
  claimed_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_claim_expiry
  ON dispatch_envelope_claims(expires_at);
"""


class JobQueue:
    """Thread-safe persistent job queue.

    Multiple ``JobQueue`` instances against the same DB file are
    safe — SQLite WAL handles concurrent writers; the ``claim()``
    method uses a single UPDATE ... WHERE id=(SELECT MIN id) so
    workers don't double-claim.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        selected = db_path if db_path is not None else _default_db()
        self.db_path = Path(selected).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(str(self.db_path), isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.row_factory = sqlite3.Row
            yield c
        finally:
            c.close()

    def _ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        *,
        run_at: float | None = None,
    ) -> int:
        now = time.time()
        rid = run_at if run_at is not None else now
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (kind, payload, run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, json.dumps(payload or {}, default=str), rid, now, now),
            )
            return int(cur.lastrowid)

    def claim_dispatch_envelope(
        self,
        message_id: str,
        nonce: str,
        *,
        expires_at: float,
        now: float | None = None,
    ) -> bool:
        """Atomically consume an authenticated dispatch envelope once.

        The claim is intentionally permanent for the lifetime of the signed
        envelope, including when the worker crashes or execution raises after
        this method returns.  Releasing it would turn a broker retry into a
        second opportunity to spend money or repeat side effects.  Operators
        must submit a fresh signed envelope to retry a post-claim failure.

        Only a SHA-256 digest of the nonce is retained.  Expired rows can be
        pruned safely because the worker rejects expired signatures before it
        reaches this store.
        """
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce must be a non-empty string")
        expiry = float(expires_at)
        if not math.isfinite(expiry) or expiry <= 0:
            raise ValueError("expires_at must be positive")
        claimed_at = time.time() if now is None else float(now)
        if not math.isfinite(claimed_at):
            raise ValueError("now must be finite")
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        try:
            with self._lock, self._conn() as c:
                # Keep the replay table bounded. A worker authenticates expiry
                # before calling us, so removing an already-expired claim
                # cannot make its old envelope executable again.
                c.execute(
                    "DELETE FROM dispatch_envelope_claims WHERE expires_at <= ?",
                    (claimed_at,),
                )
                c.execute(
                    "INSERT INTO dispatch_envelope_claims "
                    "(message_id, nonce_hash, expires_at, claimed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (message_id, nonce_hash, expiry, claimed_at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def prune_dispatch_envelope_claims(self, *, now: float | None = None) -> int:
        """Remove claims whose signed envelope can no longer be accepted."""
        cutoff = time.time() if now is None else float(now)
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM dispatch_envelope_claims WHERE expires_at <= ?",
                (cutoff,),
            )
            return int(cur.rowcount)

    def claim(
        self,
        *,
        now: float | None = None,
        ready_at: float | None = None,
    ) -> Job | None:
        """Atomically pick the next ready pending job + mark it 'running'.

        ``ready_at`` is an optional readiness cutoff for callers that need to
        snapshot queue eligibility, such as one-shot drains. ``now`` remains
        the claim/heartbeat timestamp written to ``updated_at``. Keeping those
        values separate prevents a long-running drain from claiming later jobs
        with a stale heartbeat that another worker could reclaim.
        """
        n = now if now is not None else time.time()
        cutoff = ready_at if ready_at is not None else n
        with self._lock, self._conn() as c:
            # Pick a candidate.
            row = c.execute(
                "SELECT id FROM jobs WHERE status='pending' AND run_at <= ? "
                "ORDER BY run_at ASC, id ASC LIMIT 1",
                (cutoff,),
            ).fetchone()
            if not row:
                return None
            jid = row[0]
            # Try to mark it running atomically (status check guards
            # against races between SELECT and UPDATE).
            cur = c.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, "
                "updated_at=? WHERE id=? AND status='pending'",
                (n, jid),
            )
            if cur.rowcount == 0:
                return None
            row = c.execute(
                "SELECT * FROM jobs WHERE id=?", (jid,),
            ).fetchone()
        return self._row_to_job(row)

    def heartbeat(
        self,
        job_id: int,
        *,
        now: float | None = None,
        expected_attempts: int | None = None,
    ) -> bool:
        """Refresh a running job's lease (bump ``updated_at``) mid-run.

        ``claim()`` writes ``updated_at`` exactly once, so without periodic
        refreshes any handler that outlives ``reclaim_stale``'s lease looks
        crashed to a peer worker and its job is stolen -- requeued and
        re-executed WHILE still running. The worker heartbeats from a small
        thread during dispatch; a real crash stops the heartbeat with the
        process, so orphan recovery is unaffected. Fenced on
        ``status='running'`` (and, when given, the attempt the caller claimed
        at) so a stale heartbeat can't refresh a reclaimed/re-claimed or
        terminal row it no longer owns. Returns True if the lease was extended.
        """
        n = now if now is not None else time.time()
        with self._lock, self._conn() as c:
            if expected_attempts is None:
                cur = c.execute(
                    "UPDATE jobs SET updated_at=? WHERE id=? AND status='running'",
                    (n, job_id),
                )
            else:
                cur = c.execute(
                    "UPDATE jobs SET updated_at=? "
                    "WHERE id=? AND attempts=? AND status='running'",
                    (n, job_id, expected_attempts),
                )
            return cur.rowcount == 1

    def complete(self, job_id: int, *, expected_attempts: int | None = None) -> bool:
        """Mark a job 'done'. ``expected_attempts`` fences a stolen lease: a slow
        worker can have its 'running' job reclaimed (``reclaim_stale``) and
        re-claimed by a peer, which bumps ``attempts``. Passing the attempt count
        the worker claimed at makes a stale completion no-op (rowcount 0) instead
        of clobbering the job the current owner is running."""
        with self._lock, self._conn() as c:
            if expected_attempts is None:
                cur = c.execute(
                    "UPDATE jobs SET status='done', updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            else:
                # Also fence on status='running': reclaim_stale requeues a
                # stale lease to 'pending' WITHOUT bumping attempts, so an
                # attempts-only guard would still match the now-pending row and
                # wrongly flip a job that must be re-executed to 'done'.
                cur = c.execute(
                    "UPDATE jobs SET status='done', updated_at=? "
                    "WHERE id=? AND attempts=? AND status='running'",
                    (time.time(), job_id, expected_attempts),
                )
            return cur.rowcount == 1

    def set_payload(self, job_id: int, payload: dict) -> bool:
        """Replace a job's payload. Lets a handler persist state across retries
        (e.g. a created goal id) so a re-run reuses it instead of redoing work."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                (json.dumps(payload, default=str), time.time(), job_id),
            )
            return cur.rowcount == 1

    def fail(
        self,
        job_id: int,
        error: str,
        *,
        retry_after: float | None = 60.0,
        max_attempts: int = 5,
        expected_attempts: int | None = None,
    ) -> bool:
        """Either reschedule (retry_after seconds) or mark 'failed' permanently.

        ``expected_attempts`` fences a stolen lease the same way ``complete`` does:
        if a peer re-claimed the job (bumping ``attempts``) this stale failure is
        a no-op instead of rescheduling/failing the job its new owner is running."""
        now = time.time()
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT attempts FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
            if not row:
                return False
            attempts = int(row[0])
            if expected_attempts is not None and attempts != expected_attempts:
                return False  # lease lost to a re-claim; the current owner owns it
            # Fence out a self-reclaimed row: reclaim_stale requeues a stale
            # 'running' lease back to 'pending' WITHOUT bumping attempts, so an
            # attempts-only guard would clobber a job another worker is about to
            # own -- rescheduling/killing the wrong occurrence. Guarding on
            # ``status != 'pending'`` blocks that while still allowing a fail()
            # to finalize/annotate a job that is already terminal.
            if retry_after is not None and attempts < max_attempts:
                next_run = now + float(retry_after)
                cur = c.execute(
                    "UPDATE jobs SET status='pending', run_at=?, "
                    "last_error=?, updated_at=? "
                    "WHERE id=? AND attempts=? AND status != 'pending'",
                    (next_run, error[:1000], now, job_id, attempts),
                )
            else:
                cur = c.execute(
                    "UPDATE jobs SET status='failed', last_error=?, "
                    "updated_at=? WHERE id=? AND attempts=? AND status != 'pending'",
                    (error[:1000], now, job_id, attempts),
                )
            return cur.rowcount == 1

    def reclaim_stale(
        self,
        lease_seconds: float,
        *,
        now: float | None = None,
        max_attempts: int = 5,
    ) -> int:
        """Requeue jobs stuck in 'running' past the lease TTL.

        ``claim()`` flips a job to 'running' and bumps ``updated_at``. If the
        worker process then dies before ``complete()``/``fail()`` (a hard
        crash, OOM, or ``kill -9`` — none of which run ``run_once``'s
        ``except`` path), the row stays 'running' forever: ``claim()`` only
        ever looks at 'pending' rows, so no worker re-claims it and the job
        is silently orphaned. Run this on worker start to recover them.

        A job whose ``updated_at`` is older than ``now - lease_seconds`` is
        considered abandoned. Pick a ``lease_seconds`` larger than the
        longest expected job runtime so a still-running job in a live worker
        is not stolen. Abandoned jobs already at/over ``max_attempts`` are
        marked 'failed' (poison-pill guard, mirroring ``fail()``'s terminal
        path) so a job that reliably crashes the *process* can't be requeued
        forever; the rest go back to 'pending' with ``run_at = now`` so
        they're immediately eligible. ``attempts`` is preserved (not reset)
        so the cap is reached. Returns the number of rows transitioned.
        """
        n = now if now is not None else time.time()
        cutoff = n - float(lease_seconds)
        with self._lock, self._conn() as c:
            failed = c.execute(
                "UPDATE jobs SET status='failed', last_error=?, updated_at=? "
                "WHERE status='running' AND updated_at < ? AND attempts >= ?",
                ("lease expired (worker presumed crashed)", n, cutoff, max_attempts),
            ).rowcount
            requeued = c.execute(
                "UPDATE jobs SET status='pending', run_at=?, updated_at=? "
                "WHERE status='running' AND updated_at < ? AND attempts < ?",
                (n, n, cutoff, max_attempts),
            ).rowcount
        return int(failed) + int(requeued)

    def cancel(self, job_id: int) -> bool:
        """Delete a *pending* job (e.g. an armed schedule).

        Only 'pending' rows are removable -- a 'running' job is left for the
        worker to finish, and 'done'/'failed' rows are history. Returns True
        if a row was deleted.
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM jobs WHERE id=? AND status='pending'", (job_id,),
            )
            return cur.rowcount == 1

    def get(self, job_id: int) -> Job | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, *, status: str = "", limit: int = 100) -> list[Job]:
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def counts(self) -> dict[str, int]:
        """``{status: count}`` across the whole queue — backlog + dead-letter
        visibility for metrics/ops (a growing ``pending`` is an alert; ``failed``
        jobs are the dead-letter that ``purge`` would otherwise silently drop)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def purge(self, *, older_than_days: float = 7.0) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM jobs WHERE status IN ('done','failed') AND updated_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    @staticmethod
    def _row_to_job(row) -> Job:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        return Job(
            id=int(row["id"]),
            kind=str(row["kind"]),
            payload=payload if isinstance(payload, dict) else {"_": payload},
            run_at=float(row["run_at"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


__all__ = ["JobQueue", "Job", "DEFAULT_DB"]
