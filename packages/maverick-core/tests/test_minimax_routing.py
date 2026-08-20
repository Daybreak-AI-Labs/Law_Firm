"""MiniMax M2.5 provisional OpenRouter pricing is planning-only.

These tests pin that:
  - the provisional value remains available for explicit estimates;
  - live billing refuses it until its source is verified;
  - none of this selects a model; the value is estimate-only metadata.

Hermetic: no network, no real config. Environment state is isolated by the shared test fixture.
"""
from __future__ import annotations

import pytest

# OpenRouter `vendor/model` id for MiniMax M2.5. This is the bare model_id
# _lookup_price sees after stripping the `openrouter:` prefix, so it must be
# the MODEL_PRICES key too.
MINIMAX_ID = "minimax/minimax-m2.5"
MINIMAX_SPEC = f"openrouter:{MINIMAX_ID}"


@pytest.fixture
def _clean(monkeypatch):
    for prov in (
        "ANTHROPIC", "OPENAI", "DEEPSEEK", "MOONSHOT",
        "XAI", "GEMINI", "GOOGLE", "OPENROUTER",
    ):
        monkeypatch.delenv(f"{prov}_API_KEY", raising=False)
    monkeypatch.delenv("MAVERICK_MODEL_OVERRIDE", raising=False)
    # Point HOME at a tmp with no config; these tests never resolve a run model.
    monkeypatch.setenv("HOME", "/nonexistent-minimax-routing-test")


def test_minimax_is_priced_in_model_prices():
    from maverick.llm import MODEL_PRICES
    assert MINIMAX_ID in MODEL_PRICES
    in_rate, out_rate = MODEL_PRICES[MINIMAX_ID]
    # Cheap near-frontier: well under flagship rates, sane ordering.
    assert 0 < in_rate < out_rate < 5.0


def test_lookup_price_resolves_openrouter_spec_for_estimate_only():
    # The provider-qualified id resolves without losing the vendor/model suffix,
    # but only after the caller explicitly selects planning mode.
    from maverick.budget import UnpricedModelError, _lookup_price
    from maverick.llm import MODEL_PRICES

    with pytest.raises(UnpricedModelError, match="unverified"):
        _lookup_price(MINIMAX_SPEC)
    priced = _lookup_price(MINIMAX_SPEC, estimate_only=True)
    assert priced == MODEL_PRICES[MINIMAX_ID]
