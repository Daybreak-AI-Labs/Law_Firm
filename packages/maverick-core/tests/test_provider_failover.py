"""Provider failover (ROADMAP 2027 H1, Performance) — opt-in, default off."""
from __future__ import annotations

import asyncio

import pytest
from maverick import provider_failover as pf
from maverick.llm import LLM

# ---- pure helpers -----------------------------------------------------------

def test_failover_returns_first_success():
    calls = []
    out = pf.failover([
        ("a", lambda: (_ for _ in ()).throw(RuntimeError("a down"))),
        ("b", lambda: calls.append("b") or "B-ok"),
        ("c", lambda: calls.append("c") or "C-ok"),
    ])
    assert out == "B-ok" and calls == ["b"]   # c never tried


def test_failover_all_fail_reraises_last():
    with pytest.raises(RuntimeError, match="second"):
        pf.failover([
            ("a", lambda: (_ for _ in ()).throw(RuntimeError("first"))),
            ("b", lambda: (_ for _ in ()).throw(RuntimeError("second"))),
        ])


def test_failover_should_retry_false_reraises_immediately():
    tried = []
    with pytest.raises(ValueError, match="fatal"):
        pf.failover(
            [("a", lambda: (_ for _ in ()).throw(ValueError("fatal"))),
             ("b", lambda: tried.append("b"))],
            should_retry=lambda e: not isinstance(e, ValueError),
        )
    assert tried == []   # never advanced past the non-retryable error


def test_failover_empty_raises():
    with pytest.raises(ValueError, match="no attempts"):
        pf.failover([])


def test_llm_retry_policy_rejects_control_exceptions():
    from maverick.budget import BudgetExceeded
    from maverick.enterprise import EgressBlocked
    from maverick.preflight import PreflightFailed

    assert pf.should_retry_llm_error(RuntimeError("provider down")) is True
    assert pf.should_retry_llm_error(BudgetExceeded("over budget")) is False
    assert pf.should_retry_llm_error(EgressBlocked("openai")) is False
    assert pf.should_retry_llm_error(PreflightFailed("tiny", 10, 8, 2)) is False


def test_afailover_returns_first_success():
    async def boom():
        raise RuntimeError("down")

    async def ok():
        return "ok"

    out = asyncio.run(pf.afailover([("a", boom), ("b", ok)]))
    assert out == "ok"


# ---- config-driven chain ----------------------------------------------------

def test_fallback_models_reads_config(monkeypatch):
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"provider_failover": {"chains": {"a:m1": ["b:m2", "a:m1", "c:m3"]}}})
    # the primary is filtered out of its own chain
    assert pf.fallback_models("a:m1") == ["b:m2", "c:m3"]
    assert pf.fallback_models("other") == []


def test_fallback_models_off_by_default(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    assert pf.fallback_models("a:m1") == []


# ---- LLM wiring (no real providers) -----------------------------------------

def test_complete_routes_through_failover_when_chain_set(monkeypatch):
    monkeypatch.setattr(pf, "fallback_models", lambda primary: ["b:m2"])
    seen = {}

    def _fake(attempts, **kw):
        seen["labels"] = [a[0] for a in attempts]
        return "ROUTED"

    monkeypatch.setattr(pf, "failover", _fake)
    out = LLM(model="a:m1").complete("sys", [{"role": "user", "content": "hi"}])
    assert out == "ROUTED"
    assert seen["labels"] == ["a:m1", "b:m2"]   # primary first, then the fallback


def test_complete_skips_failover_when_no_chain(monkeypatch):
    monkeypatch.setattr(pf, "fallback_models", lambda primary: [])

    def _boom(*a, **k):
        raise AssertionError("failover must not run when no chain is configured")

    monkeypatch.setattr(pf, "failover", _boom)
    # Original path runs; stub the provider client so we don't hit the network.
    sentinel = object()
    monkeypatch.setattr(LLM, "_get_client",
                        lambda self, provider: type("C", (), {"complete": lambda *a, **k: sentinel})())
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)
    out = LLM(model="anthropic:claude-sonnet-4-6").complete(
        "sys",
        [{"role": "user", "content": "hi"}],
    )
    assert out is sentinel


def test_complete_async_routes_through_afailover_when_chain_set(monkeypatch):
    monkeypatch.setattr(pf, "fallback_models", lambda primary: ["b:m2"])

    async def _fake_afailover(attempts, **kw):
        return [a[0] for a in attempts]

    monkeypatch.setattr(pf, "afailover", _fake_afailover)
    labels = asyncio.run(
        LLM(model="a:m1").complete_async("sys", [{"role": "user", "content": "hi"}]))
    assert labels == ["a:m1", "b:m2"]


def test_llm_failover_does_not_retry_budget_exceeded(monkeypatch):
    from maverick.budget import BudgetExceeded

    monkeypatch.setattr(
        pf,
        "fallback_models",
        lambda primary: ["openai:gpt-5.4"],
    )
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)
    calls = []

    class _Client:
        def __init__(self, provider):
            self.provider = provider

        def complete(self, **kwargs):
            calls.append(self.provider)
            if self.provider == "anthropic":
                raise BudgetExceeded("input tokens 2 > 1")
            return "fallback should not run"

    monkeypatch.setattr(LLM, "_get_client", lambda self, provider: _Client(provider))

    with pytest.raises(BudgetExceeded, match="input tokens"):
        LLM(model="anthropic:claude-sonnet-4-6").complete(
            "sys",
            [{"role": "user", "content": "hi"}],
        )
    assert calls == ["anthropic"]


def test_llm_failover_does_not_retry_budget_exceeded_async(monkeypatch):
    from maverick.budget import Budget, BudgetExceeded

    monkeypatch.setattr(
        pf,
        "fallback_models",
        lambda primary: ["openai:gpt-5.4"],
    )
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)
    calls = []

    class _Client:
        def __init__(self, provider):
            self.provider = provider

        async def complete_async(self, **kwargs):
            calls.append(self.provider)
            if self.provider == "anthropic":
                raise BudgetExceeded("input tokens 2 > 1")
            return "fallback should not run"

    monkeypatch.setattr(LLM, "_get_client", lambda self, provider: _Client(provider))

    with pytest.raises(BudgetExceeded, match="input tokens"):
        asyncio.run(
            LLM(model="anthropic:claude-sonnet-4-6").complete_async(
                "sys",
                [{"role": "user", "content": "hi"}],
                budget=Budget(max_dollars=100.0),
            )
        )
    assert calls == ["anthropic"]


# ---- circuit-breaker enforcement (finding #9) -------------------------------

def test_open_breaker_short_circuits_dispatch_and_fails_over(monkeypatch):
    """An OPEN provider breaker must fast-fail BEFORE the client is called and
    let the failover chain reach the next model."""
    from maverick import circuit_breaker as cb

    cb.reset_all()
    # Trip the primary provider's breaker OPEN (threshold=1, long cooldown so it
    # can't tick back to HALF_OPEN mid-test).
    br = cb.get("llm:anthropic", failure_threshold=1, cooldown_seconds=999)
    br.record_failure()
    assert br.state is cb.CircuitState.OPEN

    monkeypatch.setattr(
        pf,
        "fallback_models",
        lambda primary: ["openai:gpt-5.4"],
    )
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)
    calls = []

    class _Client:
        def __init__(self, provider):
            self.provider = provider

        def complete(self, **kwargs):
            calls.append(self.provider)
            return f"ok:{self.provider}"

    monkeypatch.setattr(LLM, "_get_client", lambda self, provider: _Client(provider))
    try:
        out = LLM(model="anthropic:claude-sonnet-4-6").complete(
            "sys", [{"role": "user", "content": "hi"}])
        # anthropic's client was NEVER invoked (breaker OPEN); failover reached
        # openai, whose breaker is CLOSED.
        assert calls == ["openai"]
        assert out == "ok:openai"
    finally:
        cb.reset_all()


def test_open_breaker_fast_fails_when_no_chain(monkeypatch):
    """No failover chain: an OPEN breaker surfaces CircuitOpen instead of
    dispatching (fast-fail rather than a dead-provider timeout)."""
    from maverick import circuit_breaker as cb

    cb.reset_all()
    cb.get("llm:anthropic", failure_threshold=1, cooldown_seconds=999).record_failure()

    monkeypatch.setattr(pf, "fallback_models", lambda primary: [])
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)

    def _boom_client(*a, **k):
        raise AssertionError("client must not be called while breaker is OPEN")

    monkeypatch.setattr(
        LLM, "_get_client",
        lambda self, provider: type("C", (), {"complete": _boom_client})())
    try:
        with pytest.raises(cb.CircuitOpen):
            LLM(model="anthropic:m1").complete(
                "sys", [{"role": "user", "content": "hi"}])
    finally:
        cb.reset_all()


def test_breaker_internal_error_does_not_block_healthy_call(monkeypatch):
    """CRITICAL fail-safe: if the breaker object itself errors, the call must
    proceed -- breaker bookkeeping never blocks an otherwise healthy call."""
    from maverick import circuit_breaker as cb

    def _explode(*a, **k):
        raise RuntimeError("breaker registry exploded")

    # Break the breaker lookup used by BOTH the enforcer and the recorder.
    monkeypatch.setattr(cb, "get", _explode)
    monkeypatch.setattr(pf, "fallback_models", lambda primary: [])
    monkeypatch.setattr("maverick.llm._run_preflight", lambda *a, **k: None)
    calls = []

    class _Client:
        def complete(self, **kwargs):
            calls.append("dispatched")
            return "healthy"

    monkeypatch.setattr(LLM, "_get_client", lambda self, provider: _Client())
    out = LLM(model="anthropic:claude-sonnet-4-6").complete(
        "sys", [{"role": "user", "content": "hi"}])
    assert out == "healthy"
    assert calls == ["dispatched"]  # breaker error failed OPEN -> call proceeded
