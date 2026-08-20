"""Q3 2026 circuit-breaker tests."""
from __future__ import annotations

import time

# ---------- Circuit breaker ----------

def test_circuit_breaker_starts_closed():
    from maverick.circuit_breaker import CircuitBreaker, CircuitState
    br = CircuitBreaker("test1", failure_threshold=3, cooldown_seconds=1)
    assert br.state is CircuitState.CLOSED
    br.call(lambda: 42)
    assert br.state is CircuitState.CLOSED


def test_circuit_breaker_opens_after_failures():
    from maverick.circuit_breaker import (
        CircuitBreaker,
        CircuitOpen,
        CircuitState,
    )

    def _boom():
        raise RuntimeError("nope")

    br = CircuitBreaker("test2", failure_threshold=2, cooldown_seconds=10)
    for _ in range(2):
        try:
            br.call(_boom)
        except RuntimeError:
            pass
    assert br.state is CircuitState.OPEN
    try:
        br.call(lambda: 1)
    except CircuitOpen:
        return
    raise AssertionError("expected CircuitOpen")


def test_circuit_breaker_half_open_after_cooldown():
    from maverick.circuit_breaker import CircuitBreaker, CircuitState
    br = CircuitBreaker("test3", failure_threshold=1, cooldown_seconds=0.01)
    try:
        br.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    except RuntimeError:
        pass
    assert br.state is CircuitState.OPEN
    time.sleep(0.02)
    assert br.state is CircuitState.HALF_OPEN
    # A successful probe closes the breaker.
    br.call(lambda: 1)
    assert br.state is CircuitState.CLOSED


def test_circuit_breaker_half_open_failure_reopens():
    from maverick.circuit_breaker import CircuitBreaker, CircuitState
    br = CircuitBreaker("test4", failure_threshold=1, cooldown_seconds=0.01)
    try:
        br.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    except RuntimeError:
        pass
    time.sleep(0.02)
    assert br.state is CircuitState.HALF_OPEN
    try:
        br.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    except RuntimeError:
        pass
    assert br.state is CircuitState.OPEN


def test_circuit_breaker_registry_singleton():
    from maverick.circuit_breaker import get, reset_all
    reset_all()
    a = get("shared-key")
    b = get("shared-key")
    assert a is b


def test_circuit_breaker_snapshot():
    from maverick.circuit_breaker import get, reset_all, snapshot
    reset_all()
    br = get("snap-key", failure_threshold=2)
    br.record_success()
    rows = snapshot()
    assert any(r["key"] == "snap-key" for r in rows)
