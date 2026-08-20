"""Audit round 11: _response_call_cost overpriced OpenAI-family cache hits.

OpenAI-compatible usage folds cached-prompt tokens INTO ``prompt_tokens`` and the
provider doesn't surface them on the LLMResponse, so _response_call_cost (which
feeds provider-health routing + the budget_dollars metric) priced the full,
cache-inclusive prompt at the full input rate -- no discount. The real billing
path (Budget.record_tokens) already splits billable_in = prompt - cached and
prices the cached part at 0.5x; this brings the metrics path in line.

(Anthropic's input_tokens already excludes cache reads, which ride on the
response as cache_read_tokens, so that path is unaffected.)
"""
from __future__ import annotations

import pytest
from maverick.llm import _response_call_cost


class _Details:
    def __init__(self, cached):
        self.cached_tokens = cached


class _UsageOpenAI:
    def __init__(self, prompt, completion, cached):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = _Details(cached)


class _UsageDeepSeek:
    def __init__(self, prompt, completion, cache_hit):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_cache_hit_tokens = cache_hit


class _UsageAnthropic:
    def __init__(self, inp, out):
        self.input_tokens = inp
        self.output_tokens = out


class _Raw:
    def __init__(self, usage):
        self.usage = usage


class _Resp:
    def __init__(self, usage, *, cache_read=0, cache_creation=0):
        self.raw = _Raw(usage)
        self.cache_read_tokens = cache_read
        self.cache_creation_tokens = cache_creation


def test_openai_cache_hit_is_discounted_not_full_rate():
    # gpt-5.4 conservative ceiling = (5.5 in, 24.75 out) per Mtok.
    resp = _Resp(_UsageOpenAI(prompt=100_000, completion=1_000, cached=80_000))
    cost = _response_call_cost("gpt-5.4", resp)
    expected = (
        (20_000 / 1e6) * 5.5
        + (80_000 / 1e6) * 5.5 * 0.5
        + (1_000 / 1e6) * 24.75
    )
    assert cost == pytest.approx(expected, rel=1e-9)
    # And strictly cheaper than the old no-discount pricing of the full prompt.
    no_discount = (100_000 / 1e6) * 5.5 + (1_000 / 1e6) * 24.75
    assert cost < no_discount


def test_deepseek_prompt_cache_hit_tokens_discounted():
    # Current v4 Flash = (0.14, 0.28); cache-hit path has no details object.
    # DeepSeek bills cache hits at 0.1x (see DeepSeekClient.CACHE_READ_MULT, the
    # multiplier the authoritative record_tokens path uses); hard-coding
    # OpenAI's 0.5x here overcounted DeepSeek cached input 5x in the
    # provider-cap ledger and budget_dollars metric.
    from maverick.providers.deepseek_provider import DeepSeekClient
    resp = _Resp(_UsageDeepSeek(prompt=50_000, completion=500, cache_hit=40_000))
    cost = _response_call_cost(
        "deepseek-v4-flash",
        resp,
        DeepSeekClient.CACHE_READ_MULT,
    )
    expected = (
        (10_000 / 1e6) * 0.14
        + (40_000 / 1e6) * 0.14 * 0.1
        + (500 / 1e6) * 0.28
    )
    assert cost == pytest.approx(expected, rel=1e-9)


def test_gemini_cached_tokens_billed_at_provider_mult():
    # Gemini 3.5 Flash = (1.50, 9.00); implicit cache reads bill 0.25x.
    from maverick.providers.gemini_provider import GeminiClient
    resp = _Resp(_UsageOpenAI(prompt=100_000, completion=1_000, cached=80_000))
    cost = _response_call_cost("gemini-3.5-flash", resp, GeminiClient.CACHE_READ_MULT)
    expected = (
        (20_000 / 1e6) * 1.5
        + (80_000 / 1e6) * 1.5 * 0.25
        + (1_000 / 1e6) * 9.0
    )
    assert cost == pytest.approx(expected, rel=1e-9)


def test_complete_threads_client_cache_mult_into_ledger_spend(monkeypatch):
    # complete() must price the ledger/metrics spend with the dispatching
    # client's CACHE_READ_MULT, not OpenAI's 0.5x (the record_tokens billing
    # path is already per-provider; this keeps the two consistent).
    import maverick.llm as llm_mod
    from maverick.budget import Budget

    class _DeepSeekish:
        CACHE_READ_MULT = 0.1

        def complete(self, **kw):
            return _Resp(_UsageDeepSeek(prompt=50_000, completion=500, cache_hit=40_000))

    recorded = {}
    monkeypatch.setattr(llm_mod, "_record_provider_spend",
                        lambda provider, dollars: recorded.update({provider: dollars}))
    llm = llm_mod.LLM(model="deepseek:deepseek-v4-flash")
    monkeypatch.setattr(llm, "_get_client", lambda provider: _DeepSeekish())
    llm.complete(system="s", messages=[{"role": "user", "content": "hi"}],
                 budget=Budget())
    expected = (
        (10_000 / 1e6) * 0.14
        + (40_000 / 1e6) * 0.14 * 0.1
        + (500 / 1e6) * 0.28
    )
    assert recorded["deepseek"] == pytest.approx(expected, rel=1e-9)


def test_no_cache_is_plain_full_rate():
    resp = _Resp(_UsageOpenAI(prompt=10_000, completion=2_000, cached=0))
    cost = _response_call_cost("gpt-5.4", resp)
    expected = (10_000 / 1e6) * 5.5 + (2_000 / 1e6) * 24.75
    assert cost == pytest.approx(expected, rel=1e-9)


def test_anthropic_path_unaffected():
    # input_tokens already excludes cache reads; cache_read rides on the response
    # at 0.1x. The OpenAI split must NOT engage (cache_read != 0).
    resp = _Resp(_UsageAnthropic(inp=30_000, out=3_000), cache_read=50_000)
    cost = _response_call_cost("claude-sonnet-4-6", resp)  # (3.0, 15.0)
    expected = (30_000 / 1e6) * 3.0 + (50_000 / 1e6) * 3.0 * 0.1 + (3_000 / 1e6) * 15.0
    assert cost == pytest.approx(expected, rel=1e-9)


def test_anthropic_cache_write_priced_at_configured_ttl(monkeypatch):
    # Audit (llm unit): cache_creation_tokens are surfaced only by the Anthropic
    # provider, which writes breakpoints at the configured TTL. The interactive
    # default is "1h" (2.0x write surcharge) -- the authoritative
    # Budget.record_tokens bills it at 2.0x. _response_call_cost previously
    # hardcoded the 5m 1.25x rate, under-counting 1h cache-write spend by ~37.5%
    # in the cross-run provider-cap ledger. Force the interactive default TTL.
    monkeypatch.delenv("MAVERICK_ANTHROPIC_CACHE_TTL", raising=False)
    resp = _Resp(_UsageAnthropic(inp=1_000, out=100), cache_creation=2_000_000)
    cost = _response_call_cost("claude-opus-4-8", resp)  # (5.0, 25.0)
    # 1h TTL bills cache writes at 2.0x, not 1.25x.
    expected = (
        (1_000 / 1e6) * 5.0
        + (2_000_000 / 1e6) * 5.0 * 2.0  # cache write @ 1h surcharge
        + (100 / 1e6) * 25.0
    )
    assert cost == pytest.approx(expected, rel=1e-9)
    # Guard against the old 1.25x-everywhere behaviour.
    wrong = (
        (1_000 / 1e6) * 5.0
        + (2_000_000 / 1e6) * 5.0 * 1.25
        + (100 / 1e6) * 25.0
    )
    assert cost != pytest.approx(wrong, rel=1e-9)


def test_anthropic_cache_write_respects_explicit_5m_ttl(monkeypatch):
    # An explicit 5m TTL override must still price writes at 1.25x.
    monkeypatch.setenv("MAVERICK_ANTHROPIC_CACHE_TTL", "5m")
    resp = _Resp(_UsageAnthropic(inp=1_000, out=100), cache_creation=2_000_000)
    cost = _response_call_cost("claude-opus-4-8", resp)  # (5.0, 25.0)
    expected = (
        (1_000 / 1e6) * 5.0
        + (2_000_000 / 1e6) * 5.0 * 1.25  # cache write @ 5m surcharge
        + (100 / 1e6) * 25.0
    )
    assert cost == pytest.approx(expected, rel=1e-9)
