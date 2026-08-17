"""Job-queue worker daemon.

Drains :class:`maverick.job_queue.JobQueue` by claiming pending jobs
and dispatching them to registered handlers. Designed for one or
more worker processes to share the same SQLite DB safely (claim is
atomic).

A handler is just a callable ``(job: Job) -> None``. Raise to fail;
return cleanly to succeed. The worker handles retry / terminal
failure / sleep-when-empty automatically.

Built-in handlers:
  - ``run_goal``  — payload {"goal_id": int} -> runs an EXISTING goal via
    maverick.runner.run_goal_in_thread (with a sync wait).
  - ``start_goal`` — payload {"text": str, "title"?: str} -> creates a FRESH
    goal from the prompt on each run, then runs it. This is the kind to pair
    with cron for a recurring autonomous task (armed from the dashboard's
    schedule endpoints).

Custom handlers are registered via :meth:`Worker.register`.

CLI entry: ``maverick worker [--db PATH] [--idle-sleep 2.0]``
(wired in cli.py when shipped; this module just exposes the loop).
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .job_queue import Job, JobQueue

log = logging.getLogger(__name__)


Handler = Callable[[Job], None]

# Job kinds the worker handles out of the box. Embedders add more at runtime
# via Worker.register(); this is the set the bare ``maverick worker`` knows.
# Exposed so schedule-arming surfaces can warn on a likely-typo'd kind that
# would otherwise sit in the queue and fail terminally only at worker time.
BUILTIN_JOB_KINDS = frozenset({"run_goal", "start_goal"})

# Payload key marking that a cron job's next occurrence has already been armed.
# Durable (persisted on the job row) so a retry or stale-lease reclaim of the
# SAME job re-attempts a *failed* re-arm without ever enqueuing a duplicate
# successor. Stripped from the successor's payload so each occurrence can arm
# its own.
_REARMED_KEY = "__rearmed__"


def _schedule_key(kind: str, payload: dict) -> str:
    """A stable identity for a recurring schedule: kind + payload minus the
    re-arm bookkeeping marker. Two occurrences of ONE schedule share it; two
    DIFFERENT schedules do not -- even two ``run_goal`` jobs on the same cron
    expr differ by ``goal_id`` -- so the idempotency guard never deduplicates
    unrelated schedules."""
    ident = {k: v for k, v in payload.items() if k != _REARMED_KEY}
    return kind + "\x00" + json.dumps(ident, sort_keys=True, default=str)


def _run_identity_kwargs(payload: dict) -> dict[str, object]:
    """Extract optional runner identity context from a queue payload."""
    run_kwargs: dict[str, object] = {}
    channel = str(payload.get("channel") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if channel:
        run_kwargs["channel"] = channel
    if user_id:
        run_kwargs["user_id"] = user_id
    concurrency_principal = str(payload.get("concurrency_principal") or "").strip()
    if concurrency_principal:
        run_kwargs["concurrency_principal"] = concurrency_principal
    allowed_suites = payload.get("allowed_suites")
    if allowed_suites is not None:
        run_kwargs["allowed_suites"] = frozenset(str(s) for s in allowed_suites)
    return run_kwargs


@contextmanager
def _job_tenant_context(payload: dict):
    """Pin a durable job to its captured tenant before any work begins.

    Queue workers are process-global and run outside the HTTP request task that
    originally selected a tenant.  Merely persisting ``payload['tenant']`` is
    therefore not isolation: without restoring the ContextVar, ``run_goal`` and
    custom handlers open the ambient/shared world.  Validate current tenant
    status before re-arming or dispatching so a suspended tenant cannot keep
    executing autonomous work from an old queue row.
    """
    tenant = str(payload.get("tenant") or "").strip()
    if not tenant:
        yield
        return

    from .tenant.registry import assert_tenant_active

    assert_tenant_active(tenant)
    from .paths import reset_tenant, set_tenant

    token = set_tenant(tenant)
    try:
        yield
    finally:
        reset_tenant(token)

class UnknownJobKind(Exception):
    """Raised when no handler is registered for a job.kind."""


class GoalRunFailed(Exception):
    """Raised by the built-in run_goal handler when the goal did not reach
    a successful terminal status, so run_once() routes it through the
    queue's retry/backoff path instead of marking the job done."""


class RearmFailed(Exception):
    """Raised by ``_maybe_rearm`` when enqueuing a cron job's next occurrence
    fails (e.g. a transient ``database is locked``). Surfaced through
    ``run_once``'s retry/dead-letter path so a later attempt re-arms the
    schedule -- instead of the old best-effort-once behaviour that logged the
    error and let the recurring schedule die silently."""


class Worker:
    def __init__(
        self,
        queue: JobQueue | None = None,
        *,
        db_path: Path | None = None,
        idle_sleep: float = 2.0,
        max_attempts: int = 5,
        retry_after: float = 60.0,
        reclaim_lease: float = 3600.0,
    ) -> None:
        self.queue = queue or JobQueue(db_path=db_path)
        self.idle_sleep = float(idle_sleep)
        self.max_attempts = int(max_attempts)
        self.retry_after = float(retry_after)
        # Jobs stuck 'running' longer than this with no lease heartbeat (a
        # prior worker crashed mid-job) are requeued on start. A live worker
        # renews its claimed job's lease while the handler runs (run_once),
        # so a job may outlast the lease without being stolen; only a dead
        # worker stops renewing.
        self.reclaim_lease = float(reclaim_lease)
        self._handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._install_builtin_handlers()

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def _install_builtin_handlers(self) -> None:
        def _run_goal(job: Job) -> None:
            goal_id = job.payload.get("goal_id")
            if not goal_id:
                raise ValueError("run_goal payload requires goal_id")
            # Sync run so the queue waits before claiming the next job.
            from .runner import run_goal_in_thread
            status = run_goal_in_thread(int(goal_id), **_run_identity_kwargs(job.payload))
            # Retry only genuinely transient outcomes: couldn't start (None) or
            # an internal crash ('error'/'failed'). A goal that ended 'blocked'
            # is a DELIBERATE stop -- budget cap hit, killswitch armed, or
            # awaiting user input -- and must NOT be retried, or run_once()
            # re-executes the entire swarm and re-spends budget. Let those
            # complete the job normally.
            if status is None or status in ("error", "failed"):
                raise GoalRunFailed(
                    f"goal {goal_id} terminal status={status!r}"
                )
        self._handlers["run_goal"] = _run_goal

        def _start_goal(job: Job) -> None:
            # A recurring autonomous task creates a FRESH goal from the prompt
            # on every fire -- unlike run_goal, which re-runs one fixed goal_id
            # (re-executing the same world-model row). Pair with cron via
            # the dashboard's schedule endpoints; _maybe_rearm carries
            # the prompt forward in the payload so each occurrence is new.
            text = (job.payload.get("text") or "").strip()
            if not text:
                raise ValueError("start_goal payload requires non-empty 'text'")
            # Idempotent across retries: create the fresh goal only once, then
            # persist its id into the job payload. A retry of THIS job reuses it
            # (re-running the existing goal, like run_goal) instead of minting a
            # duplicate goal row on every transient failure. _maybe_rearm runs
            # before dispatch, so the next cron occurrence still gets the
            # original (goal_id-free) payload and creates its own fresh goal.
            goal_id = job.payload.get("goal_id")
            if not goal_id:
                title = (job.payload.get("title") or text).strip()[:80]
                from .world_model import close_world_if_owned, open_world
                world = open_world()  # client/tenant-floored canonical world
                try:
                    owner = str(job.payload.get("owner") or "")
                    goal_id = world.create_goal(title, text, owner=owner)
                    # Provenance: link this run to its schedule so the dashboard
                    # Automations page can show the schedule's run history. Only
                    # on first creation (not retries, which reuse goal_id).
                    schedule_id = job.payload.get("schedule_id")
                    if schedule_id:
                        world.record_goal_origin(goal_id, "schedule", str(schedule_id))
                finally:
                    close_world_if_owned(world)
                job.payload["goal_id"] = goal_id
                self.queue.set_payload(job.id, job.payload)
            # Same retry contract as run_goal: only transient outcomes requeue.
            from .runner import run_goal_in_thread
            status = run_goal_in_thread(int(goal_id), **_run_identity_kwargs(job.payload))
            if status is None or status in ("error", "failed"):
                raise GoalRunFailed(
                    f"scheduled goal {goal_id} terminal status={status!r}"
                )
        self._handlers["start_goal"] = _start_goal

    def stop(self) -> None:
        self._stop.set()

    def _dispatch(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            raise UnknownJobKind(job.kind)
        handler(job)

    def _maybe_rearm(self, job: Job) -> None:
        """Re-arm a recurring (cron) job's next occurrence -- durably.

        Schedule arming stores the cron expression in
        ``payload['__cron__']``. Re-arm is IDEMPOTENT and RETRYABLE, not
        best-effort-once: the old code armed only on the first claim
        (``attempts == 1``) and swallowed any enqueue error, so a single
        transient ``database is locked`` (or a crash between claim-commit and
        re-arm) silently and permanently killed the schedule -- it just ended as
        a ``done`` row with no successor and no signal. Now:

        * skip if this occurrence already armed its successor (a durable
          ``__rearmed__`` marker persisted on the job survives ``fail()``'s
          requeue and a stale-lease reclaim), or if one is already pending (a
          backstop for a marker whose write was lost), so a retry or reclaim
          never enqueues a duplicate future occurrence;
        * otherwise arm the next occurrence, and on enqueue failure RAISE
          ``RearmFailed`` (surfaced by ``run_once`` through ``fail()`` ->
          retry/dead-letter) instead of logging and moving on, so a later
          attempt re-arms and a persistent failure becomes a visible ``failed``
          row rather than a silently-dropped schedule.
        """
        expr = job.payload.get("__cron__")
        if not expr:
            return
        if job.payload.get(_REARMED_KEY):
            return  # this occurrence already armed its successor (durable)
        if self._successor_armed(job):
            # A successor is already pending but our marker was lost (its write
            # failed): record it now and stop, rather than arming a duplicate.
            self._mark_rearmed(job)
            return
        from .scheduler import schedule_cron
        # The successor starts a clean occurrence: strip our bookkeeping marker
        # (it must be free to arm its own successor) but keep the rest of the
        # payload verbatim -- run_goal's fixed goal_id, start_goal's prompt, the
        # cron expr and __tz__ all carry forward unchanged.
        successor = {k: v for k, v in job.payload.items() if k != _REARMED_KEY}
        try:
            _jid, run_at = schedule_cron(self.queue, expr, job.kind, successor)
        except Exception as err:
            raise RearmFailed(
                f"failed to re-arm cron job {job.id} ({expr!r}): {err}"
            ) from err
        log.info("worker: re-armed cron job %d kind=%s next=%.0f",
                 job.id, job.kind, run_at)
        self._mark_rearmed(job)

    def _successor_armed(self, job: Job) -> bool:
        """True if a pending job for the SAME schedule as ``job`` already exists.

        Idempotency backstop for the durable ``__rearmed__`` marker: a schedule
        is keyed on kind + payload (minus the marker), so two distinct schedules
        that share a cron expression -- e.g. two ``run_goal`` jobs with different
        ``goal_id`` -- keep independent successors and never dedup each other,
        while a re-entry for the same schedule (marker lost) is caught. Only
        'pending' rows are scanned, and ``job`` itself is 'running' when
        ``run_once`` calls this, so the claimed job never matches itself. A large
        limit avoids ``JobQueue.list``'s default 100-row cap silently missing an
        armed successor in a big backlog."""
        key = _schedule_key(job.kind, job.payload)
        for other in self.queue.list(status="pending", limit=100_000):
            if _schedule_key(other.kind, other.payload) == key:
                return True
        return False

    def _mark_rearmed(self, job: Job) -> None:
        """Persist the durable 'successor armed' marker on ``job`` (and mirror it
        in ``job.payload`` so a later ``set_payload`` in the same run keeps it).
        A lost write only costs the ``_successor_armed`` backstop a scan, never a
        duplicate, so a failure here is logged, not fatal."""
        job.payload[_REARMED_KEY] = True
        try:
            self.queue.set_payload(job.id, job.payload)
        except Exception:
            log.exception("worker: could not persist re-arm marker for job %d", job.id)

    def run_once(self, *, ready_at: float | None = None) -> bool:
        """Process at most one job. Returns True if a job ran.

        ``ready_at`` bounds which pending rows are considered ready. The
        daemon leaves it unset so each claim uses the current wall clock;
        one-shot drain mode passes its start time so jobs that become due
        while an earlier long-running handler executes wait for the next
        invocation.
        """
        job = self.queue.claim(ready_at=ready_at)
        if job is None:
            return False
        log.info("worker: claimed job %d kind=%s (attempt %d)",
                 job.id, job.kind, job.attempts)
        # Renew the lease while the handler runs: claim() writes updated_at
        # once, so without a heartbeat any job outliving reclaim_lease looked
        # crashed to a peer daemon's periodic reclaim_stale and was requeued +
        # re-executed WHILE still running here (double LLM spend, double side
        # effects). A real crash kills this thread with the process, so
        # orphan recovery is unaffected.
        hb_stop = threading.Event()
        hb = threading.Thread(
            target=self._heartbeat_lease, args=(job, hb_stop),
            name=f"maverick-job-{job.id}-lease", daemon=True,
        )
        hb.start()
        try:
            with _job_tenant_context(job.payload):
                # Re-arm the next cron occurrence INSIDE the tenant + guarded
                # block (before dispatch) so suspended tenants cannot continue
                # autonomous schedules and a re-arm enqueue failure routes
                # through the same retry/dead-letter path as handler failure.
                # It is idempotent, so a retry never duplicates the successor.
                self._maybe_rearm(job)
                self._dispatch(job)
            # Fence the terminal write with the attempt we claimed at: if our
            # lease was reclaimed and re-claimed mid-run, this no-ops instead of
            # clobbering the peer that now owns the job.
            self.queue.complete(job.id, expected_attempts=job.attempts)
            log.info("worker: job %d done", job.id)
        except UnknownJobKind as e:
            # No retry — terminal.
            self.queue.fail(job.id, f"no handler for kind {e}",
                            retry_after=None, max_attempts=0,
                            expected_attempts=job.attempts)
            log.warning("worker: job %d has no handler (%s)", job.id, e)
        except Exception as exc:
            # Put the actionable exception summary first: JobQueue bounds the
            # durable error, and long Windows paths can otherwise consume that
            # budget before the final traceback line. Scrub both the persisted
            # row and log message because provider/tool failures may carry a
            # credential in their exception text.
            raw = (
                f"{type(exc).__name__}: {exc}\n"
                + traceback.format_exc(limit=4)
            )
            try:
                from .secrets import scrub

                err = scrub(raw)
            except Exception:  # never persist an unredacted fallback
                err = f"{type(exc).__name__}: job handler failed (details redacted)"
            self.queue.fail(
                job.id, err,
                retry_after=self.retry_after,
                max_attempts=self.max_attempts,
                expected_attempts=job.attempts,
            )
            log.warning("worker: job %d failed, requeued: %s",
                        job.id, err.splitlines()[0])
        finally:
            hb_stop.set()
            hb.join(timeout=5.0)
        return True

    def _heartbeat_lease(self, job: Job, stop: threading.Event) -> None:
        """Bump the claimed job's ``updated_at`` every ``reclaim_lease/4``
        until ``stop`` is set (dispatch finished). Fenced to the attempt we
        claimed at: a False return means the lease was lost anyway (reclaimed
        and re-claimed by a peer), so stop renewing a row we no longer own."""
        interval = max(self.reclaim_lease / 4.0, 0.05)
        while not stop.wait(interval):
            try:
                if not self.queue.heartbeat(job.id, expected_attempts=job.attempts):
                    return
            except Exception:  # transient DB contention must not kill the run
                log.exception("worker: lease heartbeat failed for job %d", job.id)

    def _reclaim_stale(self) -> None:
        """Requeue 'running' jobs orphaned by a crashed worker; log if any.

        CAVEAT -- at-least-once on the reclaim path: reclaim requeues a job whose
        lease expired so a peer can re-claim and re-run it. A worker that only
        *froze* longer than the lease (GC pause, host stall) rather than crashing
        can therefore have its job re-executed while its own side effects are
        still in flight -- the reclaim path is at-least-once for side effects, not
        exactly-once. Handlers must be idempotent (``start_goal`` reuses its
        goal_id across retries; ``complete``/``fail``/``heartbeat`` are
        attempt-fenced so the frozen worker's terminal writes no-op). The soak's
        ``exactly_once_under_contention`` metric proves ``claim()`` atomicity
        (DB-level exactly-once), NOT reclaim-path side-effect idempotency under
        such a freeze.
        """
        reclaimed = self.queue.reclaim_stale(
            self.reclaim_lease, max_attempts=self.max_attempts,
        )
        if reclaimed:
            log.info("worker: reclaimed %d stale job(s) from a prior crash",
                     reclaimed)

    def run_forever(self) -> None:
        """Loop until ``stop()`` is called or SIGTERM is received."""
        self._wire_signals()
        log.info("worker: started; polling %s every %.1fs",
                 self.queue.db_path, self.idle_sleep)
        # Recover jobs orphaned in 'running' by a previously-crashed worker
        # before draining the queue, so they aren't stuck forever.
        self._reclaim_stale()
        # ...then keep re-running it on an interval: claim() only ever picks
        # 'pending' rows, so a *peer* worker's hard crash (kill -9/OOM, which
        # skip run_once's except path) would otherwise leave its job stuck
        # 'running' until THIS daemon restarts -- which, for a long-lived
        # daemon in a multi-worker cluster, may be never. Re-reclaim every
        # reclaim_lease/2 so a still-running job is never stolen but a crashed
        # peer's orphan is recovered within ~one lease without a restart.
        reclaim_interval = max(self.reclaim_lease / 2.0, 1.0)
        last_reclaim = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_reclaim >= reclaim_interval:
                try:
                    self._reclaim_stale()
                except Exception:
                    log.exception("worker: periodic reclaim failed")
                last_reclaim = now
            ran = False
            try:
                ran = self.run_once()
            except Exception:
                log.exception("worker: unexpected error in loop")
            if not ran:
                # Idle: wait, but wake up cleanly on stop().
                self._stop.wait(self.idle_sleep)
        log.info("worker: stopped")

    def drain(self) -> int:
        """Run every currently-ready job, then return; for cron/systemd timers.

        Reclaims stale jobs once (like ``run_forever``), snapshots the
        drain start time, then processes only jobs whose ``run_at`` was ready
        at that moment. Re-armed cron occurrences, retries, or other jobs
        that become due while a long-running handler is executing wait for
        the next ``--once`` invocation.
        """
        reclaimed = self.queue.reclaim_stale(
            self.reclaim_lease, max_attempts=self.max_attempts,
        )
        if reclaimed:
            log.info("worker: reclaimed %d stale job(s) from a prior crash",
                     reclaimed)
        ready_at = time.time()
        count = 0
        while self.run_once(ready_at=ready_at):
            count += 1
        return count

    def _wire_signals(self) -> None:
        # Only wire signals from the main thread (signal.signal is
        # main-thread-only). Background callers (tests) skip cleanly.
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum, _frame):
            log.info("worker: signal %d -> shutdown", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, OSError):
            # Some hosts disallow signal.signal (e.g. embedded).
            pass


__all__ = ["Worker", "UnknownJobKind", "GoalRunFailed", "RearmFailed"]
