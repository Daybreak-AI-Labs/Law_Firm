"""Opus 4.8 + Gemini 3.5 model freshness and budget-accounting accuracy.

Regression target: defaults pointed at the prior generation (opus-4-7,
gemini-3) and Opus 4.8 / its fast-mode variant had no MODEL_PRICES entry,
so a run on the newest model silently billed at the Sonnet fallback rate
-- a quiet violation of the "budget caps are not optional" rule.
"""
from __future__ import annotations

import pytest
from maverick.budget import Budget, UnpricedModelError
from maverick.llm import MODEL_OPUS, MODEL_OPUS_FAST, MODEL_PRICES


def test_default_opus_is_4_8():
    assert MODEL_OPUS == "claude-opus-4-8"


def test_opus_48_priced_at_standard_rate():
    # Opus 4.8 standard = $5 in + $25 out per Mtok (unchanged from 4.5-4.7).
    b = Budget(max_dollars=100.0, max_input_tokens=10_000_000,
               max_output_tokens=10_000_000)
    b.record_tokens(1_000_000, 1_000_000, model=MODEL_OPUS)
    assert abs(b.dollars - 30.0) < 0.001


def test_opus_48_fast_billed_at_double_not_fallback():
    # Fast mode = $10 in + $50 out. If it weren't in MODEL_PRICES it would
    # silently bill at the Sonnet $3/$15 fallback ($18) -- under-charging 3x.
    b = Budget(max_dollars=1000.0, max_input_tokens=10_000_000,
               max_output_tokens=10_000_000)
    b.record_tokens(1_000_000, 1_000_000, model=MODEL_OPUS_FAST)
    assert abs(b.dollars - 60.0) < 0.001


def test_prior_opus_47_still_priced():
    # Users can still pin the prior Opus; it must keep its real rate.
    b = Budget(max_dollars=100.0, max_input_tokens=10_000_000,
               max_output_tokens=10_000_000)
    b.record_tokens(1_000_000, 0, model="claude-opus-4-7")
    assert abs(b.dollars - 5.0) < 0.001


def test_gemini_35_flash_priced_and_uncaptured_pro_is_estimate_only():
    # gemini-3.5-flash present so it bills at its own rate, not Sonnet's.
    assert "gemini-3.5-flash" in MODEL_PRICES
    assert "gemini-3.5-pro" in MODEL_PRICES
    b = Budget(max_dollars=100.0, max_input_tokens=10_000_000,
               max_output_tokens=10_000_000)
    b.record_tokens(1_000_000, 1_000_000, model="gemini-3.5-flash")
    assert abs(b.dollars - 10.5) < 0.001
    with pytest.raises(UnpricedModelError, match="unverified"):
        Budget().record_tokens(100, 10, model="gemini-3.5-pro")


# --- provider request shaping: Opus 4.8 was missing from every version
# gate in anthropic_provider, so the default model used the wrong cache
# threshold and the rejected `enabled` thinking mode. ---

def test_opus_48_uses_adaptive_thinking_not_enabled():
    from maverick.providers.anthropic_provider import AnthropicClient
    client = AnthropicClient.__new__(AnthropicClient)
    for mid in ("claude-opus-4-8", "claude-opus-4-8-fast"):
        kwargs = client._build_request(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=4096,
            thinking_budget=8000,
            model=mid,
        )
        thinking = kwargs.get("thinking", {})
        assert thinking.get("type") == "adaptive", (
            f"{mid} must use adaptive thinking, not enabled; got {thinking}"
        )


def test_opus_48_defaults_to_adaptive_without_budget():
    from maverick.providers.anthropic_provider import AnthropicClient
    client = AnthropicClient.__new__(AnthropicClient)
    kwargs = client._build_request(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=4096,
        thinking_budget=None,
        model="claude-opus-4-8",
    )
    assert kwargs.get("thinking", {}).get("type") == "adaptive"


def test_opus_48_uses_4x_min_cache_threshold():
    from maverick.providers.anthropic_provider import (
        _MIN_CACHE_TOKENS_4X,
        _min_cache_tokens,
    )
    assert _min_cache_tokens("claude-opus-4-8") == _MIN_CACHE_TOKENS_4X
    assert _min_cache_tokens("claude-opus-4-8-fast") == _MIN_CACHE_TOKENS_4X
