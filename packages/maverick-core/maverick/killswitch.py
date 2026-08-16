"""Global killswitch for running agents.

Two ways to halt:
  1. **File trigger**: ``touch ~/.maverick/HALT``  (default path; override
     via ``MAVERICK_HALT_FILE``). Polled cheaply at tool-call boundaries.
  2. **In-process trigger**: any thread calls ``halt(reason)`` and every
     ``check()`` call afterward raises ``Halted``.

Agent kernels call ``check()`` at tool-call boundaries and at each
turn. If a halt is active, ``Halted`` is raised, the goal is recorded
as halted in the audit log, and the orchestrator stops cleanly.

The file trigger lets a user (or operator) abort a swarm from outside
the process — handy when you realize the agent is about to do
something expensive or wrong.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .paths import data_dir

log = logging.getLogger(__name__)


def _authority_barrier():
    """Cross-process serialization for halt/clear.

    Inlined from the deleted Ekko control plane: the halt path borrowed Ekko's
    deployment-wide lock so a clear could not return while a write that
    observed the old state was still committing. The semantics stay; only the
    owner changed. Strict: an unavailable lock fails closed on clear.
    """
    from .file_lock import cross_process_lock
    from .paths import data_dir

    return cross_process_lock(data_dir("killswitch", "authority", tenant=None),
                              strict=True)


def _default_halt_file() -> Path:
    """Default HALT path, resolved fresh each call.

    Must NOT be cached at import time: a process that re-homes after import
    (a daemon, an embedder, or the test home-isolation fixture) would
    otherwise keep watching the *old* home's HALT file. Honoring the current
    ``Path.home()`` per call keeps the killswitch trustworthy — the one
    place we can least afford a stale path.
    """
    # One local operator stop must cover every tenant/fleet in the process.
    # Tenant-scoping this path lets a tenant silently watch a different file.
    return data_dir("HALT", tenant=None)


class Halted(Exception):
    """Raised by ``check()`` when a halt is active."""

    def __init__(self, reason: str, source: str):
        super().__init__(f"halted: {reason} (source={source})")
        self.reason = reason
        self.source = source


_state_lock = threading.Lock()
_in_process_halt: tuple[str, str] | None = None  # (reason, source)
_last_file_check_ts: float = 0.0
_last_file_present: bool = False
# Cluster-wide halt (v22): consulted from the shared world store so an emergency
# stop armed on one replica halts the whole fleet. Throttled like the file check;
# only engaged on a shared backend (Postgres).
_last_shared_check_ts: float = 0.0
_last_shared_halt: tuple[str, str] | None = None
_shared_world = None  # cached world handle for the shared-halt consult


def _halt_file_path() -> Path:
    override = os.environ.get("MAVERICK_HALT_FILE")
    return Path(override) if override else _default_halt_file()


def halt(reason: str, source: str = "manual") -> None:
    """Trigger an in-process halt. All subsequent check() calls raise."""
    global _in_process_halt
    with _state_lock:
        _in_process_halt = (reason, source)
    # Set the safety state first, then wait for any Ekko commit that sampled the
    # prior state. A lock outage can never undo the halt; it only weakens the
    # return-time barrier and is logged for operators.
    try:
        with _authority_barrier():
            pass
    except Exception:
        log.exception("killswitch: commit barrier unavailable during halt")
    log.warning("killswitch: halt set (%s, source=%s)", reason, source)
    from .audit import EventKind, audit_event

    # Page the operator even when the configured audit guarantee refuses its
    # row. The halt remains armed either way, then the refusal propagates so the
    # caller cannot mistake an unrecorded stop for a fully recorded one.
    try:
        audit_event(EventKind.HALT, source=source, detail=reason)
    finally:
        # A halt stops all work, so this is the canonical event an SRE must hear
        # about. No-op unless [alerts] is enabled; paging never blocks the halt.
        try:
            from .ops_alert import alert
            alert(
                "killswitch_tripped",
                f"{reason} (source={source})",
                severity="critical",
            )
        except Exception:  # pragma: no cover -- alerting never blocks the halt
            pass


def clear() -> None:
    """Reset the in-process halt. Doesn't delete the HALT file."""
    global _in_process_halt
    # Clearing is expansive, so unlike halt it fails closed if the strict
    # cross-process authority barrier is unavailable.
    with _authority_barrier(), _state_lock:
        _in_process_halt = None


def _file_halt_active(
    min_interval: float = 1.0, *, fail_closed: bool = False,
) -> bool:
    """Check the HALT file at most once per ``min_interval`` seconds.

    Avoids stat-ing the filesystem on every tool call. The 1s cache is
    invisible to humans triggering halts but cheap enough to not matter.
    """
    global _last_file_check_ts, _last_file_present
    now = time.monotonic()
    elapsed = now - _last_file_check_ts
    # ``0.0`` is the explicit cache-reset sentinel used by tests and process
    # startup.  Also refuse to reuse a cache entry if a patched/platform clock
    # ever moves backwards.  ``monotonic`` avoids ordinary wall-clock jumps.
    if _last_file_check_ts > 0.0 and 0.0 <= elapsed < min_interval:
        return _last_file_present
    _last_file_check_ts = now
    path = _halt_file_path()
    try:
        path.stat()
        _last_file_present = True
    except FileNotFoundError:
        # An explicitly configured authority whose parent/mount is absent is
        # not equivalent to an absent HALT file. Privileged learning callers
        # must refuse because the configured stop channel cannot be observed.
        if fail_closed and os.environ.get("MAVERICK_HALT_FILE"):
            try:
                path.parent.stat()
            except OSError as parent_exc:
                raise Halted(
                    "HALT file authority unavailable", "file-error",
                ) from parent_exc
        _last_file_present = False
    except (OSError, ValueError) as exc:
        if fail_closed:
            raise Halted("HALT file authority unavailable", "file-error") from exc
        _last_file_present = False
    return _last_file_present


def _shared_halt_active(
    min_interval: float = 2.0, *, fail_closed: bool = False,
) -> tuple[str, str] | None:
    """Read the cluster-wide halt with caller-selected outage posture.

    Only engaged on a shared backend (Postgres): the SQLite single-host path is
    already covered by the local HALT file, and querying a per-host SQLite world
    on every tool call would add hot-path cost for no cluster benefit. Returns
    ``(reason, source)`` when armed, else ``None``. Any error -> keep the last
    known state and drop the cached handle so the next check reconnects -- the
    killswitch must never wedge a run on a shared-store hiccup, but a transient
    read error must not silently drop a real halt either. Privileged callers
    set ``fail_closed`` and receive ``Halted(source="shared-error")`` instead.
    """
    global _last_shared_check_ts, _last_shared_halt, _shared_world
    now = time.monotonic()
    elapsed = now - _last_shared_check_ts
    if _last_shared_check_ts > 0.0 and 0.0 <= elapsed < min_interval:
        return _last_shared_halt
    _last_shared_check_ts = now
    try:
        if fail_closed:
            # ``is_postgres_configured`` deliberately fails soft when config is
            # unreadable. That is appropriate for ordinary availability-first
            # callers, but privileged learning transitions must not interpret
            # a malformed backend policy as "SQLite, so no shared HALT exists".
            # Force all active sources to be read, then inspect the config
            # health seam before deciding whether the cluster authority applies.
            from .config import config_source_errors, load_config

            load_config()
            if config_source_errors():
                raise RuntimeError("shared HALT backend configuration unreadable")
        from .world_model_backends import is_postgres_configured
        if not is_postgres_configured():
            _last_shared_halt = None
            return None
        if _shared_world is None:
            from .world_model import open_world
            _shared_world = open_world()
        state = _shared_world.active_halt()
        _last_shared_halt = (
            (state.get("reason") or "cluster halt", state.get("source") or "shared")
            if state else None
        )
    except Exception as exc:  # reconnect next time; posture is caller-selected
        _shared_world = None
        if fail_closed:
            raise Halted(
                "cluster HALT authority unavailable", "shared-error",
            ) from exc
    return _last_shared_halt


def check(*, force_refresh: bool = False, fail_closed: bool = False) -> None:
    """Raise ``Halted`` if any halt source is active.

    Hot agent boundaries use the normal one/two-second file/shared caches.
    Privileged, comparatively infrequent transitions such as learning
    promotions pass ``force_refresh=True`` so a HALT that lands during an
    evaluation cannot hide behind a stale negative cache entry.  Learning and
    other privileged mutations additionally pass ``fail_closed=True``: an
    unreadable configured HALT authority then refuses work rather than being
    mistaken for an inactive stop.  The ordinary agent hot path keeps its
    historical availability-first behavior by leaving this flag false.
    """
    with _state_lock:
        ip = _in_process_halt
    if ip is not None:
        raise Halted(ip[0], ip[1])
    file_interval = 0.0 if force_refresh else 1.0
    shared_interval = 0.0 if force_refresh else 2.0
    if _file_halt_active(
        min_interval=file_interval, fail_closed=fail_closed,
    ):
        raise Halted(f"HALT file present at {_halt_file_path()}", "file")
    shared = _shared_halt_active(
        min_interval=shared_interval, fail_closed=fail_closed,
    )
    if shared is not None:
        raise Halted(shared[0], shared[1])


def is_active() -> bool:
    """Non-raising query. Useful for UI."""
    try:
        check()
    except Halted:
        return True
    return False
