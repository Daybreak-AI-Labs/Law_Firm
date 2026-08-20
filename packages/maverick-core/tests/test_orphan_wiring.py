"""Wiring of retained support capabilities into live chokepoints."""
from __future__ import annotations

import pytest

# ---- rate-limit predictor + circuit breaker fed from dispatch helpers --------

def test_record_provider_call_feeds_predictor():
    from maverick import rate_limit_predictor as rlp
    from maverick.llm import _record_provider_call

    rlp.reset()
    _record_provider_call("anthropic")
    _record_provider_call("anthropic")
    rows = {r["provider"]: r["recorded"] for r in rlp.report()}
    assert rows.get("anthropic") == 2


def test_feed_circuit_trips_breaker_on_failures():
    from maverick import circuit_breaker as cb
    from maverick.llm import _feed_circuit
    cb.reset_all()
    for _ in range(10):  # well past the default threshold
        _feed_circuit("flaky-provider", error=True)
    states = {s["key"]: s["state"] for s in cb.snapshot()}
    assert states.get("llm:flaky-provider") == "open"


def test_feed_circuit_success_keeps_closed():
    from maverick import circuit_breaker as cb
    from maverick.llm import _feed_circuit
    cb.reset_all()
    _feed_circuit("good-provider", error=False)
    states = {s["key"]: s["state"] for s in cb.snapshot()}
    assert states.get("llm:good-provider") == "closed"


# ---- diag CLI surfaces the read-only utilities ------------------------------



# ---- approval delegation reachable from the consent path --------------------

def test_approval_delegation_route_importable_and_noop_without_rules():
    from maverick.approval_delegation import route
    # No rules configured -> None (default queue), so consent wiring is a no-op.
    assert route({"risk": "high", "tool": "shell"}, rules=[]) is None


# ---- queue dispatcher install reachable -------------------------------------

def test_queue_install_noop_without_backend(monkeypatch):
    import maverick.config as cfg
    import maverick.queue_dispatcher as qd
    import maverick.runner as runner
    monkeypatch.setattr(cfg, "load_config", dict)
    original = runner.get_dispatcher()
    try:
        assert qd.install_from_config() is False
        assert runner.get_dispatcher() is original
    finally:
        runner.set_dispatcher(original)


@pytest.mark.parametrize("enabled", [False, True])
def test_skill_distillation_local_gate(monkeypatch, enabled):
    import maverick.skill.distillation_local as sdl
    monkeypatch.setenv("MAVERICK_DISTILL_LOCAL", "1" if enabled else "0")
    assert sdl.enabled() is enabled
