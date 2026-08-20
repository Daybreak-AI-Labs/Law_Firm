"""Firm runtime dispatches client matter data to one exact model only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _secure_model(monkeypatch, spec: str | None = "openai:firm-model"):
    from maverick import llm

    monkeypatch.setattr(llm, "_secure_model_policy_enabled", lambda: True)
    monkeypatch.setattr(llm, "_configured_run_model", lambda **_kwargs: spec)
    monkeypatch.setattr(llm, "_allowed_model_specs", set)
    return llm


def _quiet_provider_guards(monkeypatch, llm) -> None:
    from maverick import enterprise, privacy_egress

    monkeypatch.setattr(enterprise, "assert_provider_allowed", lambda _provider: None)
    monkeypatch.setattr(
        privacy_egress,
        "maybe_redact_egress",
        lambda _provider, system, messages: (system, messages),
    )
    for name in (
        "_enforce_circuit",
        "_enforce_provider_cap",
        "_feed_circuit",
        "_record_provider_call",
        "_record_provider_spend",
        "_run_preflight",
    ):
        monkeypatch.setattr(llm, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm, "_estimate_call_cost", lambda *_args, **_kwargs: 0.0)


def test_secure_model_selection_requires_exact_global_pin(monkeypatch):
    llm = _secure_model(monkeypatch, None)
    with pytest.raises(llm.ModelSelectionError, match="one explicit"):
        llm.model_for_role("orchestrator")

    monkeypatch.setattr(llm, "_configured_run_model", lambda **_kwargs: "bare-model")
    with pytest.raises(llm.ModelSelectionError, match="provider:model"):
        llm.model_for_role("orchestrator")


def test_secure_model_selection_rejects_upstream_auto_router(monkeypatch):
    llm = _secure_model(monkeypatch, "openrouter:auto")
    with pytest.raises(llm.ModelSelectionError, match="delegates model choice"):
        llm.model_for_role("orchestrator")


def test_secure_model_selection_ignores_every_per_role_axis(monkeypatch):
    from maverick import llm

    monkeypatch.setattr(llm, "_secure_model_policy_enabled", lambda: True)
    monkeypatch.setattr(llm, "_allowed_model_specs", set)
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE_CODER", "anthropic:role-env")
    config = {
        "models": {
            "default": "openai:firm-model",
            "coder": "anthropic:role-config",
            "verifier": "openrouter:role-config",
        }
    }

    assert llm.offline_model_for_role("coder", config=config) == "openai:firm-model"
    assert llm.offline_model_for_role("verifier", config=config) == "openai:firm-model"


def test_catalog_contains_only_provider_qualified_models():
    from maverick.llm import catalog_specs

    specs = [spec for spec, _label in catalog_specs()]
    assert specs
    assert all(":" in spec and not spec.casefold().endswith(":auto") for spec in specs)


def test_provider_error_is_never_resent_sync_or_async(monkeypatch):
    llm = _secure_model(monkeypatch)
    _quiet_provider_guards(monkeypatch, llm)
    monkeypatch.setenv("MAVERICK_HEDGE_MS", "1")
    monkeypatch.setenv("MAVERICK_COST_ROUTING", "1")
    monkeypatch.setenv("MAVERICK_CROSS_FAMILY_VERIFIER", "anthropic:other")

    calls: list[tuple[str, str]] = []

    class FailingClient:
        def complete(self, **kwargs):
            calls.append(("sync", kwargs["model"]))
            raise RuntimeError("primary failed")

        async def complete_async(self, **kwargs):
            calls.append(("async", kwargs["model"]))
            raise RuntimeError("primary failed")

    dispatcher = llm.LLM("openai:firm-model")
    monkeypatch.setattr(
        dispatcher,
        "_get_client",
        lambda provider: FailingClient()
        if provider == "openai"
        else pytest.fail(f"unexpected provider {provider}"),
    )

    with pytest.raises(RuntimeError, match="primary failed"):
        dispatcher.complete("system", [{"role": "user", "content": "privileged"}])
    with pytest.raises(RuntimeError, match="primary failed"):
        asyncio.run(
            dispatcher.complete_async(
                "system", [{"role": "user", "content": "privileged"}]
            )
        )
    assert calls == [("sync", "firm-model"), ("async", "firm-model")]


@pytest.mark.asyncio
async def test_runtime_verifier_ignores_ensemble_and_cross_family_knobs(monkeypatch):
    from maverick import verifier

    monkeypatch.setenv("MAVERICK_VERIFY_ENSEMBLE", "1")
    monkeypatch.setenv("MAVERICK_CROSS_FAMILY_VERIFIER", "anthropic:other")
    monkeypatch.setattr(verifier, "_structured_verify_enabled", lambda: False)
    monkeypatch.setattr(
        verifier, "model_for_role", lambda _role: "openai:firm-model"
    )
    calls = []

    class OneDestinationLLM:
        async def complete_async(self, **kwargs):
            calls.append(kwargs["model"])
            return SimpleNamespace(
                text=(
                    '{"confidence": 0.9, "accepts": true, '
                    '"critique": "", "issues": []}'
                )
            )

    verdict = await verifier.verify_final(
        "brief",
        "proposal",
        OneDestinationLLM(),
        proposer_model="anthropic:proposer",
    )
    assert verdict.accepts is True
    assert calls == ["openai:firm-model"]
