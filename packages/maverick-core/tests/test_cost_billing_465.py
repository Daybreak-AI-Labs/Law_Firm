"""Issue #465 — cost/billing accuracy.

Pins two fixes:
  1. Provider-aware cache-read multiplier: Anthropic 0.1x (default) vs
     OpenAI/o-series/gpt-5 auto-cache ~0.5x, while record_tokens stays
     backward-compatible (no new required args).
  2. budget_dollars is a lifetime total that callers inc() by the per-call
     delta, so a second goal can't stomp the running total.
"""
from __future__ import annotations

import pytest

# --- Task 1: provider-aware cache-read multiplier --------------------------

def test_anthropic_cache_read_default_is_0_1x():
    from maverick.budget import Budget
    b = Budget(max_dollars=1000.0)
    # Sonnet input rate is $3/Mtok. 1M cached read tokens at the default
    # Anthropic 0.1x => $0.30.
    b.record_tokens(0, 0, model="claude-sonnet-4-6", cache_read_tok=1_000_000)
    assert b.dollars == pytest.approx(3.0 * 0.1)


def test_openai_cache_read_mult_is_0_5x():
    from maverick.budget import CACHE_READ_MULT_OPENAI, Budget
    assert CACHE_READ_MULT_OPENAI == 0.5
    b = Budget(max_dollars=1000.0)
    # The flat billing card uses the conservative $5.50/Mtok context/region
    # ceiling. One million cached-read tokens at OpenAI 0.5x => $2.75.
    b.record_tokens(
        0, 0, model="gpt-5.4", cache_read_tok=1_000_000,
        cache_read_mult=CACHE_READ_MULT_OPENAI,
    )
    assert b.dollars == pytest.approx(5.5 * 0.5)


def test_record_tokens_backward_compatible():
    """The legacy positional call (no cache_read_mult) must still bill at the
    Anthropic 0.1x default — many callers rely on it."""
    from maverick.budget import Budget
    b = Budget(max_dollars=1000.0)
    b.record_tokens(100, 50, model="claude-sonnet-4-6", cache_read_tok=1000)
    expected = (
        (100 / 1_000_000) * 3.0
        + (1000 / 1_000_000) * 3.0 * 0.1
        + (50 / 1_000_000) * 15.0
    )
    assert b.dollars == pytest.approx(expected)


def test_openai_provider_passes_openai_cache_mult():
    """End-to-end: the OpenAI provider bills cached reads at 0.5x, not 0.1x."""
    from types import SimpleNamespace

    from maverick.budget import Budget
    from maverick.providers.openai_provider import OpenAIClient

    usage = SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1_000_000),
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content="hi", tool_calls=None),
        finish_reason="stop",
    )
    resp = SimpleNamespace(choices=[choice], usage=usage)
    b = Budget(max_dollars=1000.0)
    OpenAIClient._from_response(resp, b, model="gpt-5.4")
    # All 1M prompt tokens are cached -> billable_in=0, cached at 0.5x of the
    # gpt-5.4 conservative $5.50 input ceiling => $2.75.
    assert b.dollars == pytest.approx(5.5 * 0.5)


# --- Task 2: lifetime budget_dollars metric isn't stomped across goals -----

def test_budget_dollars_metric_accumulates_across_goals(monkeypatch):
    import maverick.observability as obs

    recorded: list[tuple[str, float]] = []

    def _fake_metric(name, value=1.0, *, labels=None):
        recorded.append((name, value))

    monkeypatch.setattr(obs, "record_metric", _fake_metric, raising=True)

    from maverick.budget import Budget
    from maverick.llm import LLM, LLMResponse

    def _make_client(spend):
        class _C:
            def complete(self, **kw):
                kw["budget"].record_tokens(
                    int(spend / 3.0 * 1_000_000), 0, model="claude-sonnet-4-6"
                )
                return LLMResponse(text="", thinking=None, tool_calls=[],
                                   stop_reason="end_turn")
        return _C()

    llm = LLM()
    monkeypatch.setattr(llm, "_get_client", lambda provider: _make_client(2.0))

    # Goal A: its own fresh budget reaches $2.
    ba = Budget(max_dollars=100.0)
    llm.complete("s", [], budget=ba)
    # Goal B: a SEPARATE fresh budget that also reaches $2 (starts at $0).
    bb = Budget(max_dollars=100.0)
    llm.complete("s", [], budget=bb)

    deltas = [v for (n, v) in recorded if n == "budget_dollars"]
    assert len(deltas) == 2
    # Each emit is the per-call delta (~$2), NOT the per-goal cumulative; a
    # Counter summing these yields ~$4 lifetime instead of being stomped to $2.
    assert all(d == pytest.approx(2.0, rel=1e-3) for d in deltas)
    assert sum(deltas) == pytest.approx(4.0, rel=1e-3)
