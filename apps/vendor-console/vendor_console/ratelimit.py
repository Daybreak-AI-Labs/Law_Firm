"""In-memory login/TOTP throttle — per-process, adequate for a single-worker
internal console. Keyed by identifier (email or staff id): after ``max_fails``
failures the key is locked for ``lockout`` seconds. A multi-process deployment
would move this to shared storage.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_STATE: dict[str, tuple[int, float]] = {}   # key -> (fail_count, locked_until)

MAX_FAILS = 5
LOCKOUT_SECONDS = 300


def locked(key: str, *, now: float | None = None) -> float:
    """Seconds remaining on a lockout for ``key`` (0.0 if not locked)."""
    now = now if now is not None else time.time()
    with _LOCK:
        _, until = _STATE.get(key, (0, 0.0))
    return max(0.0, until - now)


def record_failure(key: str, *, now: float | None = None,
                   max_fails: int = MAX_FAILS, lockout: int = LOCKOUT_SECONDS) -> None:
    now = now if now is not None else time.time()
    with _LOCK:
        count, until = _STATE.get(key, (0, 0.0))
        count += 1
        if count >= max_fails:
            _STATE[key] = (0, now + lockout)     # lock and reset the counter
        else:
            _STATE[key] = (count, until)


def record_success(key: str) -> None:
    with _LOCK:
        _STATE.pop(key, None)


def reset_all() -> None:
    """Clear all state (tests / a fresh process)."""
    with _LOCK:
        _STATE.clear()
