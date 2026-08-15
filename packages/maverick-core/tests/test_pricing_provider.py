"""Versioned pricing-provider and billing trust-boundary tests."""
from __future__ import annotations

import json
from dataclasses import replace
from importlib import resources

import pytest
from maverick.budget import (
    Budget,
    UnpricedModelError,
    _lookup_price,
    _lookup_price_quote,
)
from maverick.pricing import (
    ModelPrice,
    PriceUse,
    PricingError,
    PricingEvidencePack,
    RateEvidence,
    UnverifiedRateError,
    VersionedPricingProvider,
    assert_price_evidenced,
    load_pricing_evidence_pack,
)


@pytest.fixture(autouse=True)
def _strict_pricing_by_default(monkeypatch):
    import maverick.budget as budget_mod

    monkeypatch.delenv("MAVERICK_BILLING_STRICT", raising=False)
    monkeypatch.setattr("maverick.config.get_budget_overrides", dict)
    budget_mod._ESTIMATE_MODE_WARNED = False
    budget_mod._UNPRICED_WARNED.clear()


def _quote(
    *,
    model_id: str = "vendor/model",
    verified: bool = True,
    currency: str = "USD",
    version: str = "test-card-v1",
) -> ModelPrice:
    return ModelPrice(
        model_id=model_id,
        input_per_mtok=1.25,
        output_per_mtok=4.5,
        source="https://vendor.example/pricing",
        as_of="2026-07-01",
        fetched_at="2026-07-02T03:04:05Z",
        currency=currency,
        confidence=0.9 if verified else 0.4,
        verified=verified,
        rate_card_version=version,
        evidence_id="vendor-price-capture",
        pricing_basis="Direct standard list price",
        applicability="Test fixture",
    )


def _pack_for(quote: ModelPrice) -> PricingEvidencePack:
    evidence = RateEvidence(
        evidence_id=quote.evidence_id,
        source_url=quote.source,
        retrieved_at=quote.fetched_at,
        as_of=quote.as_of,
        currency=quote.currency,
        confidence=quote.confidence,
        verified=quote.verified,
        pricing_basis=quote.pricing_basis,
        applicability=quote.applicability,
        rates={quote.model_id: quote.rates},
    )
    return PricingEvidencePack(
        schema_version=1,
        rate_card_version=quote.rate_card_version,
        entries={evidence.evidence_id: evidence},
    )


def test_builtin_rate_card_is_complete_and_legacy_view_is_derived():
    from maverick.llm import MODEL_PRICES, MODEL_PRICING_PROVIDER

    assert MODEL_PRICING_PROVIDER.version
    evidence_pack = load_pricing_evidence_pack()
    assert evidence_pack.rate_card_version == MODEL_PRICING_PROVIDER.version
    assert set(MODEL_PRICES) == set(MODEL_PRICING_PROVIDER.rates)
    assert MODEL_PRICING_PROVIDER.rates
    for model_id, quote in MODEL_PRICING_PROVIDER.rates.items():
        assert quote.model_id == model_id
        assert quote.source
        assert quote.as_of
        assert quote.fetched_at.endswith("Z")
        assert quote.currency == "USD"
        assert 0.0 <= quote.confidence <= 1.0
        assert isinstance(quote.verified, bool)
        assert quote.rate_card_version == MODEL_PRICING_PROVIDER.version
        assert quote.evidence_id
        assert quote.pricing_basis
        assert quote.applicability
        assert_price_evidenced(quote, evidence_pack)
        assert MODEL_PRICES[model_id] == quote.rates


def test_provider_rejects_unverified_rate_for_billing_but_estimate_is_explicit():
    quote = _quote(verified=False)
    provider = VersionedPricingProvider("test-card-v1", [quote])

    with pytest.raises(UnverifiedRateError, match="PriceUse.ESTIMATE"):
        provider.quote(quote.model_id)
    with pytest.raises(UnverifiedRateError):
        provider.quote(quote.model_id, use="billing")  # type: ignore[arg-type]
    assert provider.quote(quote.model_id, use=PriceUse.ESTIMATE) == quote
    with pytest.raises(PricingError, match="unsupported pricing use"):
        provider.quote(quote.model_id, use="preview")  # type: ignore[arg-type]


def test_provider_snapshot_is_immutable_and_rejects_mixed_versions():
    quote = _quote()
    evidence_pack = _pack_for(quote)
    provider = VersionedPricingProvider(
        "test-card-v1",
        [quote],
        evidence_pack=evidence_pack,
    )

    with pytest.raises(TypeError):
        provider.rates["another"] = quote  # type: ignore[index]
    with pytest.raises(PricingError, match="belongs to rate card"):
        VersionedPricingProvider(
            "test-card-v2",
            [replace(quote, rate_card_version="test-card-v1")],
        )
    with pytest.raises(PricingError, match="duplicate"):
        VersionedPricingProvider(
            "test-card-v1",
            [quote, quote],
            evidence_pack=evidence_pack,
        )


def test_verified_provider_requires_exact_tracked_evidence():
    quote = _quote()
    with pytest.raises(PricingError, match="no evidence pack"):
        VersionedPricingProvider("test-card-v1", [quote])
    with pytest.raises(PricingError, match="does not match tracked evidence"):
        VersionedPricingProvider(
            "test-card-v1",
            [replace(quote, output_per_mtok=quote.output_per_mtok + 0.01)],
            evidence_pack=_pack_for(quote),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source", "", "source"),
        ("as_of", "July 1", "as_of"),
        ("fetched_at", "2026-07-02T03:04:05", "timezone"),
        ("currency", "usd", "currency"),
        ("confidence", 1.1, "confidence"),
        ("verified", 1, "verified"),
        ("input_per_mtok", -1.0, "input_per_mtok"),
    ],
)
def test_price_metadata_validation(field, value, message):
    with pytest.raises(PricingError, match=message):
        replace(_quote(), **{field: value})


def test_openrouter_placeholder_is_estimate_only_and_never_mutates_budget():
    model = "openrouter:minimax/minimax-m2.5"
    budget = Budget(
        max_dollars=100.0,
        max_input_tokens=2_000_000,
        max_output_tokens=2_000_000,
    )

    with pytest.raises(UnpricedModelError, match="unverified"):
        budget.record_tokens(100, 20, model=model)
    assert budget.input_tokens == 0
    assert budget.output_tokens == 0
    assert budget.dollars == 0.0
    assert budget.pricing_snapshot() == {}

    quote = _lookup_price_quote(model, estimate_only=True)
    assert quote.rates == (0.30, 1.20)
    assert quote.verified is False
    assert quote.source == "https://openrouter.ai/models"


def test_unknown_rate_fails_billing_and_has_explicit_estimate_fallback():
    with pytest.raises(UnpricedModelError, match="no verified USD price"):
        _lookup_price("vendor:unknown")
    quote = _lookup_price_quote("vendor:unknown", estimate_only=True)
    assert quote.rates == (3.0, 15.0)
    assert quote.verified is False
    assert quote.confidence == 0.1


def test_legacy_strict_false_enables_labeled_estimate_accounting(
    monkeypatch,
    caplog,
):
    import maverick.llm as llm_mod

    monkeypatch.setattr(
        "maverick.config.get_budget_overrides",
        lambda: {"strict_pricing": False},
    )
    monkeypatch.setattr(llm_mod, "MODEL_PRICES", {"vendor-model": (0.2, 0.7)})
    budget = Budget(
        max_dollars=100.0,
        max_input_tokens=2_000_000,
        max_output_tokens=2_000_000,
    )

    with caplog.at_level("WARNING"):
        budget.record_tokens(
            1_000_000,
            1_000_000,
            model="openai_compat:vendor-model",
        )

    assert budget.dollars == pytest.approx(0.9)
    evidence = budget.pricing_snapshot()["openai_compat:vendor-model"]
    assert evidence["verified"] is False
    assert evidence["source"].endswith("legacy-tuple-estimate")
    assert "estimate-only accounting" in caplog.text
    assert "not suitable for invoices or chargebacks" in caplog.text


def test_environment_false_explicitly_overrides_strict_config(monkeypatch):
    monkeypatch.setenv("MAVERICK_BILLING_STRICT", "false")
    monkeypatch.setattr(
        "maverick.config.get_budget_overrides",
        lambda: {"strict_pricing": True},
    )
    quote = _lookup_price_quote("vendor:unknown")
    assert quote.verified is False
    assert quote.rates == (3.0, 15.0)


@pytest.mark.parametrize("configured", ["false", 0, None, []])
def test_malformed_config_pricing_policy_fails_closed(monkeypatch, configured):
    monkeypatch.setattr(
        "maverick.config.get_budget_overrides",
        lambda: {"strict_pricing": configured},
    )
    with pytest.raises(UnpricedModelError):
        _lookup_price("vendor:unknown")


def test_malformed_environment_pricing_policy_fails_closed(monkeypatch, caplog):
    monkeypatch.setenv("MAVERICK_BILLING_STRICT", "sometimes")
    monkeypatch.setattr(
        "maverick.config.get_budget_overrides",
        lambda: {"strict_pricing": False},
    )
    with caplog.at_level("WARNING"), pytest.raises(UnpricedModelError):
        _lookup_price("vendor:unknown")
    assert "failing closed" in caplog.text


def test_metadata_free_legacy_override_is_estimate_only(monkeypatch):
    import maverick.llm as llm_mod

    monkeypatch.setattr(llm_mod, "MODEL_PRICES", {"custom/model": (0.2, 0.7)})
    with pytest.raises(UnpricedModelError, match="metadata-free"):
        _lookup_price("custom/model")
    quote = _lookup_price_quote("custom/model", estimate_only=True)
    assert quote.rates == (0.2, 0.7)
    assert quote.verified is False
    assert quote.source.endswith("legacy-tuple-estimate")


def test_budget_retains_exact_rate_provenance_and_absorb_merges_it():
    child = Budget(max_dollars=100.0)
    child.record_tokens(1000, 100, model="claude-sonnet-4-6")
    evidence = child.pricing_snapshot()["claude-sonnet-4-6"]
    assert evidence["verified"] is True
    assert evidence["currency"] == "USD"
    assert evidence["source"].startswith("https://")
    assert evidence["rate_card_version"] == "2026-07-29.2"
    assert evidence["evidence_id"] == "anthropic-direct-2026-07-29"

    parent = Budget(max_dollars=100.0)
    parent.absorb(child)
    assert parent.pricing_snapshot() == child.pricing_snapshot()


def test_verified_non_usd_rate_is_not_silently_counted_as_dollars(monkeypatch):
    import maverick.llm as llm_mod

    euro = _quote(model_id="euro/model", currency="EUR")
    monkeypatch.setattr(
        llm_mod,
        "MODEL_PRICING_PROVIDER",
        VersionedPricingProvider(
            "test-card-v1",
            [euro],
            evidence_pack=_pack_for(euro),
        ),
    )
    monkeypatch.setattr(llm_mod, "MODEL_PRICES", {"euro/model": euro.rates})

    with pytest.raises(UnpricedModelError, match="requires a verified USD"):
        Budget().record_tokens(100, 10, model="euro/model")


def test_router_excludes_provisional_rates_unless_estimate_only(monkeypatch):
    from maverick.cost import router

    monkeypatch.setenv("MAVERICK_COST_ROUTING", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "XAI_API_KEY",
        "GROK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(router, "_allowed_providers", lambda: {"openrouter"})
    signal = router.signal_for_role("summarizer")

    assert router.pick(signal) is None
    assert router.pick(signal, estimate_only=True).startswith("openrouter:")
    assert router.price_for_model("minimax/minimax-m2.5") is None
    assert router.price_for_model(
        "minimax/minimax-m2.5",
        estimate_only=True,
    ) == (0.30, 1.20)


def test_current_verified_rates_and_unverified_claim_boundaries():
    from maverick.llm import MODEL_PRICING_PROVIDER

    expected_verified = {
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-8-fast": (10.0, 50.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "gpt-5.5": (11.0, 49.5),
        "gpt-5.4": (5.5, 24.75),
        "gpt-5.4-mini": (0.825, 4.95),
        "gpt-5.4-nano": (0.22, 1.375),
        "gpt-5.4-pro": (66.0, 297.0),
        "deepseek-v4-flash": (0.14, 0.28),
        "deepseek-v4-pro": (0.435, 0.87),
        "grok-4.5": (4.0, 12.0),
        "grok-build-0.1": (2.0, 4.0),
        "grok-4.3": (2.5, 5.0),
        "gemini-3.5-flash": (1.5, 9.0),
    }
    for model_id, rates in expected_verified.items():
        quote = MODEL_PRICING_PROVIDER.rates[model_id]
        assert quote.rates == rates
        assert quote.verified is True

    for model_id in (
        "deepseek-chat",
        "deepseek-reasoner",
        "grok-code-fast",
        "grok-4-latest",
        "grok-4-mini",
        "grok-3",
        "gemini-3.5-pro",
        "gemini-3-pro",
        "gemini-3-flash",
        "kimi-k2",
        "moonshot-v1-128k",
        "minimax/minimax-m2.5",
    ):
        assert MODEL_PRICING_PROVIDER.rates[model_id].verified is False

    openai = MODEL_PRICING_PROVIDER.rates["gpt-5.5"]
    assert "context" in openai.applicability.lower()
    assert "10 percent" in openai.pricing_basis


def test_openai_evidence_retains_direct_context_tiers_before_ceiling():
    path = resources.files("maverick").joinpath(
        "data/pricing-rate-card-2026-07-29.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    evidence = next(
        row
        for row in raw["entries"]
        if row["evidence_id"] == "openai-direct-ceiling-2026-07-29"
    )

    assert evidence["source_observations"] == {
        "gpt-5.5": {"short": [5.0, 30.0], "long": [10.0, 45.0]},
        "gpt-5.4": {"short": [2.5, 15.0], "long": [5.0, 22.5]},
        "gpt-5.4-pro": {"short": [30.0, 180.0], "long": [60.0, 270.0]},
        "gpt-5.4-mini": {"short": [0.75, 4.5]},
        "gpt-5.4-nano": {"short": [0.2, 1.25]},
        "regional_processing_uplift": 0.1,
    }


def test_xai_evidence_retains_direct_context_tiers_before_ceiling():
    path = resources.files("maverick").joinpath(
        "data/pricing-rate-card-2026-07-29.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    evidence = next(
        row
        for row in raw["entries"]
        if row["evidence_id"] == "xai-current-2026-07-29"
    )

    assert evidence["source_observations"] == {
        "grok-4.5": {"short": [2.0, 6.0], "long": [4.0, 12.0]},
        "grok-build-0.1": {"short": [1.0, 2.0], "long": [2.0, 4.0]},
        "grok-4.3": {"short": [1.25, 2.5], "long": [2.5, 5.0]},
        "long_context_threshold_tokens": 200000,
    }
