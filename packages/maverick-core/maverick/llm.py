"""Multi-provider LLM facade.

Dispatches to provider-specific clients based on the ``provider:model-id``
spec. Bare model ids (no colon) default to anthropic for backward
compatibility with the original kernel.

Provider clients (in ``maverick.providers``):
  - anthropic   (claude-*) full impl with caching/thinking/streaming
  - openai      (gpt-*, o1) OpenAI Chat Completions, translates Anthropic format
  - openrouter  (any/model) OpenAI-compatible via openrouter.ai
  - ollama      (llama*, qwen*, phi*, ...) OpenAI-compatible via localhost:11434

The agent kernel only sees the ``LLM`` class; it doesn't know or care
which provider runs a given call. A run can route the orchestrator to
Anthropic Opus, workers to local Ollama, and the summarizer to OpenAI
gpt-4o-mini — all in the same swarm.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .budget import Budget, _cache_write_mult_from_ttl
from .pricing import (
    ModelPrice,
    PriceUse,
    UnverifiedRateError,
    VersionedPricingProvider,
    load_pricing_evidence_pack,
)
from .runtime_overrides import RuntimeOverridesSecurityError

# Latest Claude family as of 2026-05.
MODEL_OPUS = "claude-opus-4-8"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

# Opus 4.8 "fast mode": identical capability, ~2.5x faster output, billed
# at 2x the standard Opus rate ($10/$50). Exposed as a distinct model id so
# callers opt in explicitly; standard Opus stays the default so we never
# silently double a user's bill.
MODEL_OPUS_FAST = "claude-opus-4-8-fast"

DEFAULT_MODEL = MODEL_SONNET


# Per-role default model picks (bare = anthropic). Users override via config.toml.
ROLE_MODELS: dict[str, str] = {
    "orchestrator":    MODEL_OPUS,
    "researcher":      MODEL_SONNET,
    "coder":           MODEL_SONNET,
    "writer":          MODEL_SONNET,
    "analyst":         MODEL_SONNET,
    "revisor":         MODEL_OPUS,
    "verifier":        MODEL_SONNET,
    "summarizer":      MODEL_HAIKU,
    "skill_distiller": MODEL_SONNET,
    "vision":          MODEL_SONNET,
    # Roles resolved by name at their call sites (docs_i18n translator,
    # self_modify_runner coding, review paths). Without an explicit default
    # they silently fell back to DEFAULT_MODEL and were not overridable via
    # [role_models]; pin sensible defaults so they are first-class (kernel rule 2).
    "translator":      MODEL_SONNET,
    "coding":          MODEL_SONNET,
    "reviewer":        MODEL_OPUS,
}


# Immutable rate-card snapshot. A price is not merely two floats: the billing
# boundary needs the source, applicability/fetch dates, currency, confidence,
# and verification status that justify those floats. Every row must exactly
# match the tracked evidence pack. Rows whose current official source could not
# be captured remain available for planning, but cannot cross the billing gate.
MODEL_RATE_CARD_VERSION = "2026-07-29.2"
_PRICING_EVIDENCE = load_pricing_evidence_pack()


def _model_price(
    model_id: str,
    rates: tuple[float, float],
    *,
    evidence_id: str,
) -> ModelPrice:
    evidence = _PRICING_EVIDENCE.entries[evidence_id]
    return ModelPrice(
        model_id=model_id,
        input_per_mtok=rates[0],
        output_per_mtok=rates[1],
        source=evidence.source_url,
        as_of=evidence.as_of,
        fetched_at=evidence.retrieved_at,
        currency=evidence.currency,
        confidence=evidence.confidence,
        verified=evidence.verified,
        rate_card_version=MODEL_RATE_CARD_VERSION,
        evidence_id=evidence_id,
        pricing_basis=evidence.pricing_basis,
        applicability=evidence.applicability,
    )


MODEL_PRICING_PROVIDER = VersionedPricingProvider(
    MODEL_RATE_CARD_VERSION,
    [
        # Anthropic official list pricing.
        _model_price(
            MODEL_OPUS,
            (5.0, 25.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            MODEL_OPUS_FAST,
            (10.0, 50.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            "claude-opus-4-7",
            (5.0, 25.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            "claude-opus-4-6",
            (5.0, 25.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            "claude-opus-4-5",
            (5.0, 25.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            MODEL_SONNET,
            (3.0, 15.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            MODEL_HAIKU,
            (1.0, 5.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        _model_price(
            "claude-haiku-4-5-20251001",
            (1.0, 5.0),
            evidence_id="anthropic-direct-2026-07-29",
        ),
        # OpenAI billing stores a conservative flat ceiling because the
        # published price varies by context length and processing region.
        # The evidence pack records both the direct list prices and the exact
        # ceiling policy, so a flat ModelPrice cannot undercount either tier.
        _model_price(
            "gpt-5.5",
            (11.0, 49.5),
            evidence_id="openai-direct-ceiling-2026-07-29",
        ),
        _model_price(
            "gpt-5.4",
            (5.5, 24.75),
            evidence_id="openai-direct-ceiling-2026-07-29",
        ),
        _model_price(
            "gpt-5.4-pro",
            (66.0, 297.0),
            evidence_id="openai-direct-ceiling-2026-07-29",
        ),
        _model_price(
            "gpt-5.4-mini",
            (0.825, 4.95),
            evidence_id="openai-direct-ceiling-2026-07-29",
        ),
        _model_price(
            "gpt-5.4-nano",
            (0.22, 1.375),
            evidence_id="openai-direct-ceiling-2026-07-29",
        ),
        _model_price(
            "deepseek-v4-flash",
            (0.14, 0.28),
            evidence_id="deepseek-current-2026-07-29",
        ),
        _model_price(
            "deepseek-v4-pro",
            (0.435, 0.87),
            evidence_id="deepseek-current-2026-07-29",
        ),
        _model_price(
            "deepseek-chat",
            (0.27, 1.10),
            evidence_id="deepseek-deprecated-alias-estimates-2026-07-29",
        ),
        _model_price(
            "deepseek-reasoner",
            (0.55, 2.19),
            evidence_id="deepseek-deprecated-alias-estimates-2026-07-29",
        ),
        _model_price(
            "grok-4.5",
            (4.0, 12.0),
            evidence_id="xai-current-2026-07-29",
        ),
        _model_price(
            "grok-4.3",
            (2.5, 5.0),
            evidence_id="xai-current-2026-07-29",
        ),
        _model_price(
            "grok-build-0.1",
            (2.0, 4.0),
            evidence_id="xai-current-2026-07-29",
        ),
        _model_price(
            "grok-code-fast",
            (1.0, 2.0),
            evidence_id="xai-ambiguous-estimates-2026-07-29",
        ),
        _model_price(
            "grok-4-latest",
            (3.0, 15.0),
            evidence_id="xai-ambiguous-estimates-2026-07-29",
        ),
        _model_price(
            "grok-4-mini",
            (0.30, 0.50),
            evidence_id="xai-ambiguous-estimates-2026-07-29",
        ),
        _model_price(
            "grok-3",
            (3.0, 15.0),
            evidence_id="xai-ambiguous-estimates-2026-07-29",
        ),
        _model_price(
            "gemini-3.5-flash",
            (1.5, 9.0),
            evidence_id="google-gemini-flash-2026-07-29",
        ),
        _model_price(
            "gemini-3.5-pro",
            (2.50, 10.0),
            evidence_id="google-unsupported-estimates-2026-07-29",
        ),
        _model_price(
            "gemini-3-pro",
            (2.50, 10.0),
            evidence_id="google-unsupported-estimates-2026-07-29",
        ),
        _model_price(
            "gemini-3-flash",
            (0.15, 0.60),
            evidence_id="google-unsupported-estimates-2026-07-29",
        ),
        *[
            _model_price(
                model_id,
                rates,
                evidence_id="moonshot-uncaptured-estimates-2026-07-29",
            )
            for model_id, rates in (
                ("kimi-k2", (0.60, 2.50)),
                ("kimi-k1.5", (0.20, 2.00)),
                ("moonshot-v1-8k", (0.30, 0.30)),
                ("moonshot-v1-32k", (0.60, 0.60)),
                ("moonshot-v1-128k", (1.20, 1.20)),
            )
        ],
        *[
            _model_price(
                model_id,
                rates,
                evidence_id="openrouter-uncaptured-estimates-2026-07-29",
            )
            for model_id, rates in (
                ("minimax/minimax-m2.5", (0.30, 1.20)),
                ("deepseek/deepseek-v4-pro", (0.14, 0.55)),
                ("qwen/qwen3-coder-next", (0.20, 0.80)),
            )
        ],
        # Zero means no metered external API-token charge, not zero
        # infrastructure cost. The evidence explicitly encodes this scope.
        *[
            _model_price(
                model_id,
                (0.0, 0.0),
                evidence_id="local-api-metering-policy-2026-07-29",
            )
            for model_id in (
                "qwen3-coder-next",
                "qwen3-32b",
                "llama-4-maverick",
            )
        ],
    ],
    evidence_pack=_PRICING_EVIDENCE,
)


def model_price_quote(
    model_id: str,
    *,
    estimate_only: bool = False,
) -> ModelPrice | None:
    """Resolve one canonical quote; billing-grade is the safe default."""

    use = PriceUse.ESTIMATE if estimate_only else PriceUse.BILLING
    return MODEL_PRICING_PROVIDER.quote(model_id, use=use)


# Compatibility view for integrations that only display or compare rates.
# It intentionally includes provisional estimate-only rows. Billing and
# routing decisions must use ``model_price_quote`` / MODEL_PRICING_PROVIDER so
# verification metadata is enforced rather than lost in tuple unpacking.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    model_id: quote.rates
    for model_id, quote in MODEL_PRICING_PROVIDER.rates.items()
}


# Curated model catalog for the dashboard's model pickers: provider -> model
# ids. The dashboard renders these as ``provider:<id>`` specs (bare for
# anthropic, the default provider) and also lets the operator type any other
# id. Admins extend the list via ``[models] catalog`` in config.toml. Prices
# live in MODEL_PRICING_PROVIDER; a model without a verified quote may be shown
# in a picker but cannot be billed.
MODEL_CATALOG: dict[str, list[str]] = {
    "anthropic":  [MODEL_OPUS, MODEL_OPUS_FAST, MODEL_SONNET, MODEL_HAIKU],
    "openai":     ["gpt-5.5", "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano"],
    "gemini":     ["gemini-3.5-pro", "gemini-3.5-flash", "gemini-3-pro", "gemini-3-flash"],
    "xai":        ["grok-4.5", "grok-build-0.1", "grok-4.3", "grok-code-fast", "grok-4-latest", "grok-4-mini", "grok-3"],
    "deepseek":   ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    "moonshot":   ["kimi-k2", "kimi-k1.5", "moonshot-v1-128k"],
    "openrouter": ["minimax/minimax-m2.5", "qwen/qwen3-coder-next"],
    "ollama":     ["qwen3-coder-next", "qwen3-32b", "llama-4-maverick"],
    # ChatGPT/Codex subscription via the local Codex CLI (`codex exec`), not
    # the metered OpenAI API. Ids are whatever the user's Codex plan serves;
    # unknown ids behind the codex_cli: prefix price $0 (subscription-billed).
    "codex_cli":  ["gpt-5.5", "gpt-5"],
}

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic (Claude)", "openai": "OpenAI", "gemini": "Google Gemini",
    "xai": "xAI (Grok)", "deepseek": "DeepSeek", "moonshot": "Moonshot",
    "openrouter": "OpenRouter", "ollama": "Ollama (local)",
    "codex_cli": "Codex CLI (ChatGPT subscription)",
}


def catalog_specs() -> list[tuple[str, str]]:
    """Every built-in model as ``(spec, provider_label)``. Anthropic ids stay
    bare (the default provider); others carry the ``provider:`` prefix the
    resolver expects. The dashboard merges ``[models] catalog`` on top."""
    out: list[tuple[str, str]] = []
    for provider, ids in MODEL_CATALOG.items():
        plabel = PROVIDER_LABELS.get(provider, provider)
        for mid in ids:
            spec = mid if provider == "anthropic" else f"{provider}:{mid}"
            out.append((spec, plabel))
    return out


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    thinking: str | None
    tool_calls: list[ToolCall]
    stop_reason: str
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    raw: Any = None
    # May 26 smoke fix: thinking-block signatures.
    # Anthropic emits one signature per thinking block; when those
    # blocks come back as assistant history, EACH must carry its
    # own original signature. The earlier single-string field
    # `thinking_signature` worked for single-block adaptive runs
    # but corrupts multi-block interleaved thinking (Opus 4.7).
    # Now: store the original (text, signature) pairs so agent.py
    # can reconstruct multiple thinking blocks faithfully.
    thinking_blocks: list[tuple[str, str | None]] = None  # type: ignore
    # Legacy field kept for back-compat with mocks; equals
    # thinking_blocks[0][1] if thinking_blocks present.
    thinking_signature: str | None = None
    # May 28 fix: the model's output blocks in their ORIGINAL order,
    # already in Anthropic content-block dict form (thinking /
    # redacted_thinking / text / tool_use, interleaved as returned).
    # Anthropic forbids rearranging the thinking-block sequence
    # relative to tool_use ("you can't rearrange or modify the
    # sequence of these blocks"), so the bucketed thinking/text/
    # tool_calls fields above cannot rebuild an interleaved Opus 4.7
    # turn faithfully. agent.py replays these verbatim when present;
    # None for providers that don't emit interleaved thinking (they
    # fall back to the bucketed reconstruction).
    content_blocks: list[dict] | None = None

    def __post_init__(self):
        if self.thinking_blocks is None:
            # Back-compat: synthesize from the legacy single fields.
            if self.thinking:
                self.thinking_blocks = [(self.thinking, self.thinking_signature)]
            else:
                self.thinking_blocks = []


class ModelNotAllowedError(PermissionError):
    """A requested model is outside the operator's active hard allow-list."""


def _allowed_model_specs() -> set[str]:
    """Canonical active allow-list; empty means unrestricted.

    Runtime-override parsing owns the fail-closed posture for an unreadable
    operator policy. Do not swallow that security error here: treating a broken
    allow-list as empty would silently turn it into unrestricted access.
    """
    from .runtime_overrides import allowed_models

    return {_canonical_spec(spec) for spec in allowed_models()}


def require_model_allowed(spec: str) -> str:
    """Return canonical ``provider:model`` after enforcing the hard allow-list.

    Unlike role routing's convenience fallback, an explicit dispatch choice
    must never be silently replaced: callers that pin a forbidden model receive
    a policy error before provider/client work begins.
    """
    canonical = _canonical_spec(spec)
    allowed = _allowed_model_specs()
    if allowed and canonical not in allowed:
        raise ModelNotAllowedError(
            f"model {canonical!r} is not allowed by [access] allowed_models"
        )
    return canonical


def _explicit_model_for_role(
    role: str,
    *,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Resolve operator-pinned model authority without live routing probes.

    ``config`` is an optional already-loaded snapshot used by deterministic
    preflight.  Normal runtime resolution leaves it unset and therefore uses
    the canonical active-tenant configuration loader.
    """
    override = os.environ.get(f"MAVERICK_MODEL_OVERRIDE_{role.upper()}")
    if override:
        return override
    # Global CLI override (`maverick --model <id>`): an explicit run-wide
    # choice. Beats config so the flag isn't silently ignored by per-role
    # config/defaults (which it was, before this).
    global_override = os.environ.get("MAVERICK_MODEL_OVERRIDE")
    if global_override:
        return global_override
    try:
        from .config import get_role_model

        spec = (
            get_role_model(role)
            if config is None
            else get_role_model(role, config=config)
        )
        if spec:
            return spec
    except Exception:
        pass
    # Dashboard-pinned model (set from the settings page; lives in
    # ~/.maverick/runtime-overrides.toml, never config.toml). A per-role pin
    # wins over the global default pin. Below the user's config.toml [models]
    # above, above the built-in ROLE_MODELS defaults -- an explicit UI choice
    # that still yields to a more specific config [models].<role>.
    try:
        from .runtime_overrides import default_model_override, role_model_override

        pinned = role_model_override(role) or default_model_override()
        if pinned:
            return pinned
    except Exception:  # pragma: no cover -- never let the overlay break resolution
        pass
    return None


def _resolve_model_for_role(role: str) -> str:
    """Return the model spec for a role (may be 'provider:id' or bare id).

    Resolution order:
      1. Per-role env override `MAVERICK_MODEL_OVERRIDE_<ROLE>` (set by
         best-of-N to swap models per attempt).
      2. Global override `MAVERICK_MODEL_OVERRIDE` (set by the CLI's
         `maverick --model <id>` flag) -- an explicit, run-wide choice that
         beats config so the documented flag actually applies to every agent.
      3. ``~/.maverick/config.toml`` -> ``[models]`` -> role
      4. Dashboard role/default pin from ``runtime-overrides.toml``
      5. Local-first router (opt-in)
      6. Cost-aware router (opt-in: `MAVERICK_COST_ROUTING=1` or
         `[routing] cost_aware = true`) -- among the user's configured
         providers, the cheapest one at the role's capability tier.
      7. ``ROLE_MODELS`` defaults, optionally energy-adjusted
      8. ``DEFAULT_MODEL``

    The user's explicit choices (1-4) always win; the router only gets a
    say when no model was pinned, and it returns None (defers to 7) unless
    the operator opted in. This keeps "users own model choice" intact.
    """
    explicit = _explicit_model_for_role(role)
    if explicit:
        return explicit
    # Local-first (opt-in, off by default). When [system] local_first is on and
    # a configured local model's server is reachable, keep the work on-machine;
    # returns None otherwise, so this is a no-op for the default install and
    # gracefully falls through to remote.
    try:
        from .provider_local_first import pick_local
        local = pick_local(role)
        if local:
            return local
    except Exception:  # pragma: no cover -- never let local-first break resolution
        pass
    # Cost-aware routing (opt-in, off by default). pick() returns None when
    # disabled or when no provider is configured, so this is a no-op for the
    # default install.
    try:
        from .cost.router import pick, signal_for_role
        routed = pick(signal_for_role(role))
        if routed:
            return routed
    except Exception:  # pragma: no cover -- never let routing break resolution
        pass
    final = ROLE_MODELS.get(role, DEFAULT_MODEL)
    # Energy-aware downgrade (opt-in, off by default): on a laptop low on
    # battery, step the default-tier model down (Opus->Sonnet->Haiku) to extend
    # runtime, then revert on wall power. No-op unless [routing] energy_aware is
    # on AND battery is low, and only on the default path -- an explicit
    # override/config/router choice above is never downgraded.
    try:
        from .energy_aware_router import route as _energy_route
        cheaper = _cheaper_model(final)
        if cheaper != final:
            final = _energy_route(final, cheaper)
    except Exception:  # pragma: no cover -- never let energy routing break resolution
        pass
    return final


def _apply_allowed_model_policy(final: str) -> str:
    """Apply the dashboard's hard model allow-list to one resolved spec."""
    try:
        from .runtime_overrides import allowed_models

        allow = allowed_models()
        if allow:
            allow_canon = {_canonical_spec(a) for a in allow}
            # Compare canonically: a bare ("claude-sonnet-4-6") and a
            # provider-qualified ("anthropic:claude-sonnet-4-6") spelling of the
            # same model must not read as a mismatch, or an operator-allowed
            # model gets wrongly rejected and silently substituted.
            if _canonical_spec(final) not in allow_canon:
                if _canonical_spec(DEFAULT_MODEL) in allow_canon:
                    final = DEFAULT_MODEL
                else:
                    # Cost-aware fallback: the CHEAPEST allowed model, not
                    # sorted(allow)[0] -- lexicographic-first could force a cheap
                    # bulk role onto the most expensive allowed model.
                    final = _cheapest_allowed(allow)
    except RuntimeOverridesSecurityError:
        raise
    except Exception:  # pragma: no cover -- allow-list never breaks resolution
        pass
    return final


def offline_model_for_role(
    role: str,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """Resolve the deterministic route used by offline readiness checks.

    This shares all explicit operator authority and the final admin allow-list
    with :func:`model_for_role`, but deliberately skips local reachability,
    live cost routing, and battery state.  Those mutable probes belong to
    ``maverick doctor``; preflight must remain deterministic and network-free.
    """
    explicit = _explicit_model_for_role(role, config=config)
    final = explicit or ROLE_MODELS.get(role, DEFAULT_MODEL)
    return _apply_allowed_model_policy(final)


def model_for_role(role: str) -> str:
    """Resolve the live model route and enforce the admin hard allow-list."""
    return _apply_allowed_model_policy(_resolve_model_for_role(role))


def _canonical_spec(spec: str) -> str:
    """``provider:model-id`` canonical form so a bare vs provider-qualified
    spelling of the same model compares equal in the admin allow-list check."""
    provider, model_id = _parse_spec(spec)
    return f"{provider}:{model_id}"


def _cheapest_allowed(allow: list[str]) -> str:
    """The cheapest model in ``allow`` by billable output price (input price as
    a tie-break), name-sorted for determinism. Unknown-priced models rank last,
    but subscription/self-hosted provider prefixes that the budget layer prices
    at zero must win over paid hosted models. The allow-list fallback uses this
    instead of ``sorted(allow)[0]`` (lexicographic, cost-blind)."""

    def rank(spec: str):
        price = _allowlist_rank_price(spec)
        out_in = (price[1], price[0]) if price else (float("inf"), float("inf"))
        return (*out_in, spec)

    return min(allow, key=rank)


def _allowlist_rank_price(spec: str) -> tuple[float, float] | None:
    """Return the price used for allow-list fallback ranking.

    Keep this in step with ``budget._lookup_price`` for billable choices that
    are known to be free or explicitly priced. Genuinely unknown hosted models
    still rank last instead of inheriting the budget fallback estimate, because
    an allow-list fallback should not prefer an unverified price over a known
    cheap model.
    """
    provider, model_id = _parse_spec(spec)
    if provider == "codex_cli":
        return 0.0, 0.0
    for key in (spec, model_id):
        try:
            quote = model_price_quote(key)
        except UnverifiedRateError:
            return None
        if quote is not None:
            return quote.rates
    if provider in ("ollama", "vllm", "tgi"):
        return 0.0, 0.0
    return None


def _allowlist_filter_fallbacks(models: list[str]) -> list[str]:
    """Drop failover fallbacks outside the admin allow-list (no-op when none set).

    ``model_for_role`` enforces the ``[access] allowed_models`` cap on role
    resolution, but provider-failover chains are dispatched straight from config
    without passing back through it -- so a transient error on the primary could
    fail over to a model the operator's "hard cap" forbids (a governance bypass).
    Failover must not introduce a disallowed model the base call wouldn't run, so
    we filter the *fallbacks*; the primary is left untouched (it is what the
    non-failover path runs anyway).
    """
    allow = _allowed_model_specs()
    if not allow:
        return models
    return [m for m in models if _canonical_spec(m) in allow]


def _fallback_chain_for_dispatch(
    raw_primary: str,
    canonical_primary: str,
) -> list[str]:
    """Resolve a configured chain without making aliases a config footgun."""
    from .provider_failover import fallback_models

    chain = fallback_models(raw_primary)
    if not chain and raw_primary != canonical_primary:
        # Preserve exact-key compatibility while also finding chains declared
        # under the canonical spelling of an alias/bare primary pin.
        chain = fallback_models(canonical_primary)
    return _allowlist_filter_fallbacks(chain)


def _record_provider_call(provider: str) -> None:
    """Feed the proactive rate-limit predictor one call timestamp. Cheap
    in-memory ring buffer; powers ``maverick diag ratelimits`` and lets the
    predictor estimate wait-before-429. Never raises into the dispatch path."""
    try:
        from .rate_limit_predictor import record
        record(provider)
    except Exception:  # pragma: no cover -- prediction never blocks a call
        pass


def _feed_circuit(provider: str, error: bool) -> None:
    """Record a provider call's outcome on its circuit breaker so repeated
    failures trip it (observable via ``maverick diag circuits``). This is the
    recorder half; ``_circuit_open`` is the enforcer half that consults the
    state before dispatch. Never raises."""
    try:
        from .circuit_breaker import get
        br = get(f"llm:{provider}")
        br.record_failure() if error else br.record_success()
    except Exception:  # pragma: no cover -- breaker never blocks a call
        pass


def _circuit_open(provider: str) -> bool:
    """True only if this provider's circuit breaker is definitively OPEN.

    Read BEFORE dispatch so an outage window fast-fails instead of hammering a
    dead provider (``complete``/``complete_async``). Fails SAFE: any
    breaker-INTERNAL error (import / registry / state read) returns False, so
    breaker bookkeeping can never block an otherwise healthy call -- only a
    cleanly-observed OPEN state short-circuits. HALF_OPEN returns False so the
    single cooldown probe is allowed through; ``_feed_circuit`` records its
    outcome and resolves the state."""
    try:
        from .circuit_breaker import CircuitState, get
        return get(f"llm:{provider}").state is CircuitState.OPEN
    except Exception:  # pragma: no cover -- breaker never blocks a call
        return False


def _enforce_circuit(provider: str) -> None:
    """Fast-fail BEFORE dispatch when this provider's breaker is OPEN.

    Raises ``CircuitOpen`` -- a transient provider signal the failover chain
    treats as retryable (``should_retry_llm_error`` lets it through since it is
    not a budget/egress/preflight/consent control error, and ``classify_error``
    buckets it ``"other"``, in the default failover set). So a configured chain
    moves to the next model and, chain or not, a dead-provider timeout is
    skipped. No outcome is recorded (the call never ran). Fails safe: a
    breaker-internal error proceeds (see ``_circuit_open``)."""
    if _circuit_open(provider):
        from .circuit_breaker import CircuitOpen
        raise CircuitOpen(f"llm:{provider} breaker OPEN; skipping dispatch")


def _safe_cap_projection(model_id, system, messages, tools, max_tokens,
                         thinking_budget=None) -> float:
    """Estimated cost of the pending call for the provider-cap projection.
    Never raises -- a bad estimate must not block a legitimate call (fails to 0,
    i.e. spend-only enforcement, the prior behaviour)."""
    try:
        return _estimate_call_cost(model_id, system, messages, tools, max_tokens,
                                   thinking_budget)
    except Exception:  # pragma: no cover -- estimate never blocks a call
        return 0.0


def _enforce_provider_cap(provider: str, projected_dollars: float = 0.0) -> None:
    """Deployment-wide provider spend ceiling gate ([budget.provider_caps]).

    Raises ``ProviderCapExceeded`` when the provider's period spend has reached
    its cap -- OR when ``projected_dollars`` (this call's estimated cost) would
    push it over -- so a failover chain moves to the next provider, or (no chain)
    the call fails closed. Projecting the pending call closes the gap where a
    single large call or concurrent calls, each seeing under-cap recorded spend,
    blew past the ceiling. A NO-OP unless a cap is configured for this provider,
    so the default install is unchanged. ProviderCapExceeded is deliberately NOT
    caught here: it must propagate. Only a missing module is swallowed."""
    try:
        from .provider_cost_cap import enforce
    except ImportError:  # pragma: no cover -- module always present
        return
    enforce(provider, projected_dollars=projected_dollars)


def _record_provider_spend(provider: str, dollars: float) -> None:
    """Add one call's spend to the provider's period ledger (the data the cap
    enforces against). Fail-soft -- accounting never crashes the run that
    produced the spend; no-op for non-positive amounts inside record()."""
    try:
        from .provider_cost_cap import record
        record(provider, dollars)
    except Exception:  # pragma: no cover -- accounting never blocks a call
        pass


def _hedge_ms() -> float | None:
    """Tail-latency hedging delay (ms): opt-in, default OFF.

    When set, ``complete_async`` fires a *backup* request this many ms after the
    primary and takes whichever succeeds first, cancelling the laggard — the
    "tail at scale" hedge for tightening p99 on a provider with variable latency.
    It trades extra spend on slow calls for latency, so it is off unless an
    operator opts in via ``MAVERICK_LLM_HEDGE_MS`` or ``[latency] hedge_ms``.
    Returns the delay in ms, or ``None`` (disabled / non-positive / unparseable),
    in which case the single-call path runs unchanged.
    """
    raw: object = os.environ.get("MAVERICK_LLM_HEDGE_MS")
    if raw is None or str(raw).strip() == "":
        try:
            from .config import load_config
            raw = (load_config() or {}).get("latency", {}).get("hedge_ms")
        except Exception:  # pragma: no cover -- config is best-effort here
            raw = None
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _cheaper_model(model: str) -> str:
    """One tier cheaper for energy-aware downgrade: Opus->Sonnet->Haiku."""
    if model == MODEL_OPUS or model == MODEL_OPUS_FAST:
        return MODEL_SONNET
    if model == MODEL_SONNET:
        return MODEL_HAIKU
    return model


def _parse_spec(spec: str) -> tuple[str, str]:
    """Parse ``provider:model-id`` or bare ``model-id`` (= anthropic).

    The provider half is canonicalized (lowercased, alias-resolved via the
    provider registry) so a user-typed ``Anthropic:`` or an advertised alias
    like ``claude:`` resolves the same as ``anthropic:`` -- not just for client
    creation (which already canonicalizes) but for the case-sensitive API-key
    lookup in ``_provider_api_key``, which would otherwise miss the key and
    fail auth at call time.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("model spec must be a non-empty string")
    clean_spec = spec.strip()
    if ":" in clean_spec:
        provider, model_id = clean_spec.split(":", 1)
    else:
        provider, model_id = "anthropic", clean_spec
    model_id = model_id.strip()
    if not model_id:
        raise ValueError("model spec must include a model id")
    from .providers import _canonical
    return _canonical(provider), model_id


def _configured_provider_api_key(provider: str) -> str | None:
    """Return a provider api_key from config, normalized for client use."""
    from .config import get_provider_config

    try:
        key = (get_provider_config(provider) or {}).get("api_key")
    except Exception:
        return None
    if isinstance(key, str):
        return key.strip() or None
    return str(key).strip() if key else None


def _provider_api_key(
    provider: str,
    anthropic_api_key: str | None,
    *,
    provider_config: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Return the explicit API key override for provider-client creation."""
    if provider == "anthropic" and anthropic_api_key:
        return anthropic_api_key.strip() or None
    if provider_config is None:
        key = _configured_provider_api_key(provider)
    else:
        raw_key = provider_config.get("api_key")
        key = (
            raw_key.strip() or None
            if isinstance(raw_key, str)
            else str(raw_key).strip() if raw_key else None
        )
    if key:
        return key
    from .config import PROVIDER_KEY_ENV_MAP
    env = os.environ if environment is None else environment
    for env_key in PROVIDER_KEY_ENV_MAP.get(provider, ()):
        value = env.get(env_key, "").strip()
        if value:
            return value
    return None


_PROVIDER_BASE_URL_ENV_MAP: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "openai": ("OPENAI_BASE_URL",),
    "moonshot": ("MOONSHOT_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
    "xai": ("XAI_BASE_URL",),
    "tgi": ("TGI_BASE_URL",),
    "vllm": ("VLLM_BASE_URL",),
    "azure": ("AZURE_OPENAI_ENDPOINT",),
    "openai_compatible": ("OPENAI_COMPATIBLE_BASE_URL",),
}
_AZURE_CLIENT_ENV_NAMES = (
    "AZURE_OPENAI_AD_TOKEN",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_AUTH",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_TOKEN_SCOPE",
)
_PROVIDER_SNAPSHOT_RETRIES = 3
_CLIENT_CACHE_MAX = 32


@dataclass(frozen=True, repr=False)
class _ProviderClientConfig:
    """One immutable provider-client admission snapshot.

    The secret-bearing values intentionally do not appear in ``repr``.  Cache
    identity uses only ``fingerprint`` so credentials and auth headers never
    become dict keys that diagnostics might print.
    """

    api_key: str | None
    base_url: str | None
    default_headers: tuple[tuple[str, str], ...]
    auth_mode: str | None
    provider_environment: tuple[tuple[str, str], ...]
    fingerprint: str

    def headers_dict(self) -> dict[str, str] | None:
        return dict(self.default_headers) if self.default_headers else None

    def environment_dict(self) -> dict[str, str]:
        return dict(self.provider_environment)


def _provider_config_snapshot(
    provider: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read provider config against one stable environment generation.

    ``load_config`` may populate wizard-managed environment values and performs
    live ``${VAR}`` interpolation.  A seqlock-style retry prevents a rotation
    during that read from pairing the old config table with the new credential
    environment (or vice versa).
    """
    from .config import get_provider_config

    for _ in range(_PROVIDER_SNAPSHOT_RETRIES):
        before = dict(os.environ)
        try:
            raw = get_provider_config(provider) or {}
            provider_config = dict(raw) if isinstance(raw, dict) else {}
        except Exception:  # pragma: no cover -- provider config remains fail-soft
            provider_config = {}
        after = dict(os.environ)
        if before == after:
            return provider_config, before
    # Never manufacture a mixed generation merely to make progress. A caller
    # can retry once the configuration rotation settles.
    raise RuntimeError(
        f"provider configuration changed during {provider!r} client admission"
    )


def _canonical_headers(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if name:
            headers[name] = str(raw_value)
    return tuple(sorted(headers.items(), key=lambda item: (item[0].casefold(), item[0])))


def _provider_config_fingerprint(
    *,
    api_key: str | None,
    base_url: str | None,
    default_headers: tuple[tuple[str, str], ...],
    auth_mode: str | None,
    provider_environment: tuple[tuple[str, str], ...],
) -> str:
    canonical = json.dumps(
        {
            "api_key": api_key,
            "base_url": base_url,
            # HTTP names are case-insensitive. Preserve configured casing when
            # constructing the client, but hash equivalent spellings equally.
            "default_headers": sorted(
                (name.casefold(), value) for name, value in default_headers
            ),
            "auth_mode": auth_mode,
            "provider_environment": provider_environment,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_preflight(model_id, system, messages, tools, max_tokens) -> None:
    """Token preflight before an LLM dispatch (roadmap 'token preflight v1').

    Mode via the ``MAVERICK_PREFLIGHT`` env var:
      - ``warn`` (default): log a warning if the estimated request won't fit
        the model's context, but still dispatch;
      - ``strict``: raise ``PreflightFailed`` so the caller can hard-refuse
        before burning tokens on a doomed call;
      - ``off``: skip entirely.

    Default is ``warn`` so wiring preflight onto the live path can't turn a
    borderline-but-valid request into a false refusal (the estimate is a
    cheap chars/token heuristic); operators opt into hard-refuse explicitly.
    """
    mode = os.environ.get("MAVERICK_PREFLIGHT", "warn").strip().lower()
    if mode not in ("warn", "strict"):
        return  # 'off' / unrecognized -> skip
    try:
        from .preflight import preflight
    except ImportError:  # pragma: no cover
        return
    # strict=True makes preflight() raise PreflightFailed (propagates to the
    # caller); warn mode only logs.
    preflight(
        model=model_id, system=system, messages=messages,
        tools=tools, max_tokens=max_tokens, strict=(mode == "strict"),
    )


def _release_budget_hold(budget: Budget | None, held: float) -> None:
    if budget is not None and held:
        budget.release(held)


def _estimate_call_cost(model_id, system, messages, tools, max_tokens,
                        thinking_budget=None) -> float:
    """Rough $ for one call BEFORE dispatch: estimated input tokens at the
    model's input rate + max_tokens at the output rate (chars/4 heuristic,
    matching the token preflight). Input dominates -- a 200k-token context is
    the cost driver -- so output using the full max_tokens is acceptably
    conservative."""
    from .budget import _lookup_price
    try:
        # Price the max_tokens the provider will actually send: the Anthropic
        # request builder bumps it for thinking headroom (explicit budget, or
        # the adaptive floor on Opus 4.7/4.8), and a hold priced at the
        # pre-bump value under-reserves the real worst-case output spend.
        from .providers.anthropic_provider import effective_max_tokens
        max_tokens = effective_max_tokens(str(model_id or ""), max_tokens,
                                          thinking_budget)
    except Exception:  # pragma: no cover -- estimate must never block a call
        pass
    in_rate, out_rate = _lookup_price(model_id)
    chars = len(system or "")
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else m
        chars += len(c) if isinstance(c, str) else len(str(c))
    for t in tools or []:
        chars += len(str(t))
    return (chars / 4 / 1_000_000) * in_rate + (max_tokens / 1_000_000) * out_rate


def _openai_cached_tokens(usage) -> int:
    """Cached-prompt token count from an OpenAI-family usage object.

    Mirrors the OpenAI provider's billing read: ``prompt_tokens_details.
    cached_tokens`` (OpenAI / Gemini OpenAI-compat) or ``prompt_cache_hit_tokens``
    (DeepSeek). Returns 0 for Anthropic usage (which has neither). These tokens
    are INCLUDED in ``prompt_tokens``, so the caller subtracts them before
    pricing the remainder at the full input rate.
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    n = getattr(details, "cached_tokens", 0) if details is not None else 0
    if not n:
        n = getattr(usage, "prompt_cache_hit_tokens", 0)
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def _response_call_cost(model_id, resp, cache_read_mult: float | None = None) -> float | None:
    """Per-call $ derived from THIS response's own token usage, or ``None`` when
    the response carries no usage to price from.

    provider-health spend used to be diffed off the shared ``budget.dollars``
    counter (``dollars - _d0``), which races other concurrent sub-agents on the
    same budget: their spend lands inside the window and inflates this call's
    recorded dollars. Pricing the response's own usage is call-local, so a wide
    parallel fan-out records each call's real cost. Returns ``None`` when no
    usage is exposed so the caller can fall back to the (sequentially-correct)
    budget diff rather than recording a phantom $0.

    ``cache_read_mult`` is the OpenAI-family cached-prompt discount to bill at
    -- the dispatching client's ``CACHE_READ_MULT`` (DeepSeek 0.1x, Gemini
    0.25x, OpenAI 0.5x); ``None`` falls back to OpenAI's 0.5x. Anthropic cache
    reads always bill 0.1x (they arrive via ``cache_read_tokens`` instead).
    """
    if resp is None:
        return None
    from .budget import (
        _CACHE_READ_MULT,
        CACHE_READ_MULT_OPENAI,
        _cache_write_mult_from_ttl,
        _lookup_price,
    )
    usage = getattr(getattr(resp, "raw", None), "usage", None)

    def _u(*names) -> int:
        for n in names:
            v = getattr(usage, n, None) if usage is not None else None
            if v is not None:
                try:
                    return max(0, int(v))
                except (TypeError, ValueError):
                    return 0
        return 0

    # Anthropic uses input/output_tokens; OpenAI-compat uses prompt/completion.
    # Anthropic's ``input_tokens`` already EXCLUDES cache reads (they ride on the
    # LLMResponse as cache_read_tokens). OpenAI-family ``prompt_tokens`` FOLDS the
    # cached tokens IN and the provider doesn't surface them on the response, so
    # without splitting them out here a cache hit is priced at the full input rate
    # -- overstating OpenAI spend (~2x on the input side) in provider-health and
    # the budget_dollars metric. Mirror the provider's billing split.
    in_tok = _u("input_tokens", "prompt_tokens")
    out_tok = _u("output_tokens", "completion_tokens")
    cache_read = int(getattr(resp, "cache_read_tokens", 0) or 0)
    cache_write = int(getattr(resp, "cache_creation_tokens", 0) or 0)
    if not (in_tok or out_tok or cache_read or cache_write):
        return None
    # OpenAI/DeepSeek cached-prompt split (only when the response didn't already
    # surface cache_read, i.e. the OpenAI-family path; in_tok is cache-inclusive).
    openai_cached = 0
    if not cache_read:
        openai_cached = min(_openai_cached_tokens(usage), in_tok)
        in_tok -= openai_cached
    in_rate, out_rate = _lookup_price(model_id)
    # ``cache_write`` (cache_creation_tokens) is only ever surfaced by the
    # Anthropic provider, which writes its breakpoints at the configured TTL
    # (``_default_cache_ttl()`` -- "1h" in interactive mode, billed at 2.0x).
    # Hardcoding ``None`` here priced every write at the 5m 1.25x rate, so this
    # cross-run provider-cap ledger under-counted 1h cache-write spend by ~37.5%
    # vs the authoritative Budget.record_tokens (which receives the real TTL),
    # letting a deployment overshoot its configured provider cap. Match the TTL
    # the real per-run path bills at.
    if cache_write:
        from .providers.anthropic_provider import _default_cache_ttl
        write_mult = _cache_write_mult_from_ttl(_default_cache_ttl())
    else:
        write_mult = _cache_write_mult_from_ttl(None)
    # Per-provider cached-prompt discount, mirroring the authoritative
    # Budget.record_tokens path (openai_provider passes its class
    # CACHE_READ_MULT -- DeepSeek 0.1x, Gemini 0.25x). Hard-coding OpenAI's
    # 0.5x here overcounted DeepSeek cached input 5x (Gemini 2x) in the
    # provider-cap ledger, provider_health, and the budget_dollars metric.
    openai_mult = CACHE_READ_MULT_OPENAI if cache_read_mult is None else float(cache_read_mult)
    cost = (in_tok / 1_000_000) * in_rate
    cost += (cache_read / 1_000_000) * in_rate * _CACHE_READ_MULT   # Anthropic 0.1x
    cost += (openai_cached / 1_000_000) * in_rate * openai_mult
    cost += (cache_write / 1_000_000) * in_rate * write_mult
    cost += (out_tok / 1_000_000) * out_rate
    return cost


def _call_spend(model_id, resp, budget, dollars_baseline, cache_read_mult=None) -> float:
    """Dollars to attribute to one LLM call for provider-health/metrics.

    Prefers the response's own usage (call-local, race-free under a shared
    budget); falls back to the budget diff when the response exposes no usage.
    ``cache_read_mult`` is threaded to ``_response_call_cost`` (the dispatching
    client's per-provider OpenAI-family cache discount).
    """
    if not budget:
        return 0.0
    call_cost = _response_call_cost(model_id, resp, cache_read_mult)
    return call_cost if call_cost is not None else (budget.dollars - dollars_baseline)


class LLM:
    """Multi-provider LLM dispatcher.

    Holds a cache of provider-specific client instances. Each call routes
    to the right one based on the model spec (defaults to ``self.model``).

    Drop-in replacement for the previous anthropic-only LLM class.
    """

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self.model = model
        self._anthropic_api_key = api_key  # legacy back-compat
        # Compatibility/debug view: latest client created for each provider.
        self._clients: dict[str, Any] = {}
        # Bounded LRU keyed by tenant + a one-way fingerprint of every
        # client-affecting field. Raw credentials/headers never become a
        # diagnostic-visible key, and a rotation cannot reuse the prior client.
        self._client_cache: OrderedDict[
            tuple[str, str | None, str], Any
        ] = OrderedDict()
        # Wave 12 (council F12a): lock the provider cache so two
        # concurrent calls don't double-init httpx connection pools.
        import threading as _threading
        self._clients_lock = _threading.Lock()

    def _provider_client_config(
        self, provider: str
    ) -> _ProviderClientConfig:
        # Resolve ALL client-affecting values from the same admitted
        # config+environment generation. Previously api_key and endpoint were
        # separate get_provider_config() reads, so a rotation between them could
        # pair old credentials with a new gateway.
        pcfg, environment = _provider_config_snapshot(provider)
        key = _provider_api_key(
            provider,
            self._anthropic_api_key,
            provider_config=pcfg,
            environment=environment,
        )

        base_url = None
        raw_base_url = pcfg.get("base_url")
        if isinstance(raw_base_url, str) and raw_base_url.strip():
            base_url = raw_base_url.strip()
        if base_url is None:
            for env_name in _PROVIDER_BASE_URL_ENV_MAP.get(provider, ()):
                value = environment.get(env_name, "").strip()
                if value:
                    base_url = value
                    break
        # Bedrock derives its endpoint from region rather than a BASE_URL env.
        # Resolve that derivation here so cache identity rotates with AWS_REGION.
        if provider == "bedrock" and base_url is None:
            region = environment.get("AWS_REGION", "").strip()
            if region:
                base_url = (
                    f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
                )

        default_headers = _canonical_headers(pcfg.get("default_headers"))
        raw_auth_mode = pcfg.get("auth_mode")
        auth_mode = None
        if isinstance(raw_auth_mode, str) and raw_auth_mode.strip():
            auth_mode = raw_auth_mode.strip().lower()
        elif provider == "azure":
            auth_mode = (
                environment.get("AZURE_OPENAI_AUTH", "").strip().lower() or None
            )

        provider_environment: tuple[tuple[str, str], ...] = ()
        if provider == "azure":
            azure_environment = {
                name: environment.get(name, "")
                for name in _AZURE_CLIENT_ENV_NAMES
            }
            if auth_mode is not None:
                azure_environment["AZURE_OPENAI_AUTH"] = auth_mode
            provider_environment = tuple(sorted(azure_environment.items()))

        fingerprint = _provider_config_fingerprint(
            api_key=key,
            base_url=base_url,
            default_headers=default_headers,
            auth_mode=auth_mode,
            provider_environment=provider_environment,
        )
        return _ProviderClientConfig(
            api_key=key,
            base_url=base_url,
            default_headers=default_headers,
            auth_mode=auth_mode,
            provider_environment=provider_environment,
            fingerprint=fingerprint,
        )

    def _get_client(self, provider: str):
        from .paths import current_tenant_id
        tenant_id = current_tenant_id()
        with self._clients_lock:
            # Snapshot and admission share the cache lock. This prevents an
            # older, slow resolution from being inserted after a newer one.
            config = self._provider_client_config(provider)
            cache_key = (provider, tenant_id, config.fingerprint)
            cached = self._client_cache.get(cache_key)
            if cached is not None:
                self._client_cache.move_to_end(cache_key)
                return cached

            from .providers import get_provider_client

            factory_kwargs: dict[str, Any] = {
                "api_key": config.api_key,
                "base_url": config.base_url,
                "default_headers": config.headers_dict(),
            }
            if provider == "azure":
                # Azure's key-vs-Entra selection, static token, deployment, and
                # API version must consume the same environment snapshot too.
                factory_kwargs["environment"] = config.environment_dict()
            client = get_provider_client(provider, **factory_kwargs)

            # A provider/tenant has exactly one current client in this LLM.
            # Drop stale rotations immediately, then enforce a global LRU cap
            # for long-lived multi-tenant servers.
            stale_keys = [
                key
                for key in self._client_cache
                if key[:2] == (provider, tenant_id) and key != cache_key
            ]
            for stale_key in stale_keys:
                self._client_cache.pop(stale_key, None)
            self._client_cache[cache_key] = client
            self._client_cache.move_to_end(cache_key)
            while len(self._client_cache) > _CLIENT_CACHE_MAX:
                self._client_cache.popitem(last=False)
            self._clients[provider] = client
            return client

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        budget: Budget | None = None,
        max_tokens: int = 4096,
        thinking_budget: int | None = None,
        model: str | None = None,
        on_delta=None,
        effort: str | None = None,
        _no_failover: bool = False,
    ) -> LLMResponse:
        raw_requested_model = model or self.model
        requested_model = require_model_allowed(raw_requested_model)
        # Provider failover (opt-in, default off): when a fallback chain is
        # configured for this model, try each in turn. No chain -> this block is
        # skipped and the original single-call path below runs unchanged.
        if not _no_failover:
            from .failover_policy import order_chain, policy_should_retry
            from .provider_failover import failover
            _chain = _fallback_chain_for_dispatch(
                raw_requested_model, requested_model
            )
            _chain = [require_model_allowed(candidate) for candidate in _chain]
            if _chain:
                # The policy engine narrows WHICH errors fail over and skips
                # cooling-down models; with no [provider_failover.policy] both
                # collapse to the v1 behavior.
                return failover([
                    (m, (lambda m=m: self.complete(
                        system, messages, tools=tools, budget=budget,
                        max_tokens=max_tokens, thinking_budget=thinking_budget,
                        model=m, on_delta=on_delta, effort=effort, _no_failover=True)))
                    for m in order_chain([requested_model, *_chain])
                ], should_retry=policy_should_retry)
        provider, model_id = _parse_spec(requested_model)
        # Provider-qualified spec for the pricing/ledger paths
        # (_estimate_call_cost / _call_spend): _lookup_price's self-hosted $0
        # rule keys off the "ollama:"/"vllm:"/"tgi:" prefix that _parse_spec
        # strips, so pricing the bare id billed free local models at the
        # Sonnet fallback (and strict pricing refused them before dispatch).
        # Known table ids resolve identically -- _lookup_price strips a
        # "provider:" prefix for table lookups.
        _price_spec = f"{provider}:{model_id}"
        # Egress lock (no-op unless enterprise mode is on): refuse to send data to a
        # non-local provider so sensitive data never leaves the boundary. Raises
        # EgressBlocked before any prompt is dispatched.
        from .enterprise import assert_provider_allowed
        assert_provider_allowed(provider)
        # Outbound data-minimization (opt-in, default off): strip detectable
        # PII/secrets from the prompt before it leaves the box to a cloud
        # provider. No-op unless [privacy] redact_egress is on; skipped for
        # local providers. Rewrites the outbound copy only.
        from .privacy_egress import maybe_redact_egress
        system, messages = maybe_redact_egress(provider, system, messages)
        _record_provider_call(provider)
        # Project this call's cost into the deployment-wide $ ceiling so a single
        # large call (or concurrent calls seeing the same under-cap spend) can't
        # blow past it.
        _enforce_provider_cap(provider, _safe_cap_projection(
            model_id, system, messages, tools, max_tokens, thinking_budget))
        _run_preflight(model_id, system, messages, tools, max_tokens)
        client = self._get_client(provider)
        # Circuit-breaker enforcement (the breaker was observe-only): if this
        # provider's breaker is OPEN, fast-fail before dispatch. _feed_circuit
        # below still records the real outcome; this only short-circuits.
        _enforce_circuit(provider)
        kwargs: dict[str, Any] = dict(
            system=system, messages=messages, tools=tools, budget=budget,
            max_tokens=max_tokens, thinking_budget=thinking_budget, model=model_id,
        )
        if on_delta is not None and provider == "anthropic":
            kwargs["on_delta"] = on_delta
        # Per-role effort is an Anthropic-only output_config knob; other
        # providers don't accept it, so only thread it to the anthropic provider.
        if effort and provider == "anthropic":
            from .effort import effort_for_model
            model_effort = effort_for_model(effort, model_id)
            if model_effort:
                kwargs["effort"] = model_effort
        import time as _time
        try:
            from .chaos import maybe_fail
            maybe_fail("llm_call", message=f"chaos: llm_call provider={provider}")
        except ImportError:
            pass
        try:
            from .observability import (
                gen_ai_attributes as _gen_ai_attributes,
            )
            from .observability import (
                gen_ai_span_name as _gen_ai_span_name,
            )
            from .observability import (
                trace_span as _trace_span,
            )
        except ImportError:  # pragma: no cover
            import contextlib
            def _trace_span(*a, **kw):  # type: ignore
                return contextlib.nullcontext()
            def _gen_ai_span_name(op, model):  # type: ignore
                return f"{op} {model}"
            def _gen_ai_attributes(*a, **kw):  # type: ignore
                return {}
        _t0 = _time.monotonic()
        _d0 = budget.dollars if budget else 0.0
        _err = False
        _resp = None
        # Hold this call's projected cost against the cap BEFORE dispatching, so
        # concurrent callers on a shared budget can't each pass an individual
        # check() and then collectively overshoot -- the same defense the async
        # path uses. reserve() raises BudgetExceeded if the call won't fit;
        # released in `finally` once the actual spend lands.
        _est_cost = _estimate_call_cost(_price_spec, system, messages, tools,
                                        max_tokens, thinking_budget=thinking_budget)
        _held = budget.reserve(_est_cost) if budget is not None else 0.0
        try:
            with _trace_span(
                _gen_ai_span_name("chat", model_id),
                attributes={
                    "llm.provider": provider, "llm.model": model_id,
                    **_gen_ai_attributes(provider, model_id),
                },
            ):
                _resp = client.complete(**kwargs)
                return _resp
        except Exception:
            _err = True
            raise
        finally:
            if _held:
                budget.release(_held)
            _dt_ms = (_time.monotonic() - _t0) * 1000.0
            # Price THIS call's own usage rather than diffing the shared
            # budget.dollars counter, which races concurrent sub-agents.
            # The dispatching client's CACHE_READ_MULT keeps the OpenAI-family
            # cached-prompt discount per-provider (DeepSeek 0.1x, Gemini 0.25x).
            _spent = _call_spend(_price_spec, _resp, budget, _d0,
                                 getattr(client, "CACHE_READ_MULT", None))
            _record_provider_spend(provider, _spent)  # feed the $ ceiling ledger
            try:
                from .provider_health import get as _h
                _h().record(provider, model_id,
                            latency_ms=_dt_ms, dollars=_spent, error=_err)
            except Exception:  # pragma: no cover -- never fail on stats
                pass
            _feed_circuit(provider, _err)
            try:
                from .observability import record_metric as _rm
                _rm("llm_calls", labels={"provider": provider, "model": model_id})
                _rm("llm_latency", _dt_ms / 1000.0,
                    labels={"provider": provider, "model": model_id})
                if budget is not None:
                    # inc() by THIS call's delta, not the per-goal cumulative
                    # total: budget_dollars is a lifetime counter, and passing
                    # the per-goal accumulator let a fresh goal stomp it.
                    _rm("budget_dollars", _spent)
            except Exception:  # pragma: no cover
                pass

    async def complete_async(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        budget: Budget | None = None,
        max_tokens: int = 4096,
        thinking_budget: int | None = None,
        model: str | None = None,
        effort: str | None = None,
        _no_failover: bool = False,
    ) -> LLMResponse:
        raw_requested_model = model or self.model
        requested_model = require_model_allowed(raw_requested_model)
        # Provider failover (opt-in, default off) — see complete(). No configured
        # chain -> skipped, and the original single-call path below is unchanged.
        if not _no_failover:
            from .failover_policy import order_chain, policy_should_retry
            from .provider_failover import afailover
            _chain = _fallback_chain_for_dispatch(
                raw_requested_model, requested_model
            )
            _chain = [require_model_allowed(candidate) for candidate in _chain]
            if _chain:
                return await afailover([
                    (m, (lambda m=m: self.complete_async(
                        system, messages, tools=tools, budget=budget,
                        max_tokens=max_tokens, thinking_budget=thinking_budget,
                        model=m, effort=effort, _no_failover=True)))
                    for m in order_chain([requested_model, *_chain])
                ], should_retry=policy_should_retry)
        provider, model_id = _parse_spec(requested_model)
        # Provider-qualified spec for the pricing/ledger paths -- see complete().
        _price_spec = f"{provider}:{model_id}"
        # Egress lock (no-op unless enterprise mode is on): see complete().
        from .enterprise import assert_provider_allowed
        assert_provider_allowed(provider)
        # Outbound data-minimization (opt-in, default off): strip detectable
        # PII/secrets from the prompt before it leaves the box to a cloud
        # provider. No-op unless [privacy] redact_egress is on; skipped for
        # local providers. Rewrites the outbound copy only.
        from .privacy_egress import maybe_redact_egress
        system, messages = maybe_redact_egress(provider, system, messages)
        _record_provider_call(provider)
        # Project this call's cost into the deployment-wide $ ceiling so a single
        # large call (or concurrent calls seeing the same under-cap spend) can't
        # blow past it.
        _enforce_provider_cap(provider, _safe_cap_projection(
            model_id, system, messages, tools, max_tokens, thinking_budget))
        _run_preflight(model_id, system, messages, tools, max_tokens)
        client = self._get_client(provider)
        # Circuit-breaker enforcement -- see complete(). OPEN fast-fails with a
        # failover-retryable CircuitOpen before dispatch; fails safe.
        _enforce_circuit(provider)
        import time as _time
        # complete_async is the PRIMARY agent-loop path; the sync complete()
        # had the chaos hook + trace span but this one didn't, so chaos
        # injection and OTLP LLM spans never fired on the live path.
        try:
            from .chaos import maybe_fail
            maybe_fail("llm_call", message=f"chaos: llm_call provider={provider}")
        except ImportError:
            pass
        try:
            from .observability import (
                gen_ai_attributes as _gen_ai_attributes,
            )
            from .observability import (
                gen_ai_span_name as _gen_ai_span_name,
            )
            from .observability import (
                trace_span as _trace_span,
            )
        except ImportError:  # pragma: no cover
            import contextlib
            def _trace_span(*a, **kw):  # type: ignore
                return contextlib.nullcontext()
            def _gen_ai_span_name(op, model):  # type: ignore
                return f"{op} {model}"
            def _gen_ai_attributes(*a, **kw):  # type: ignore
                return {}
        _t0 = _time.monotonic()
        _d0 = budget.dollars if budget else 0.0
        _err = False
        _resp = None
        # Hold this call's projected cost against the cap BEFORE dispatching, so
        # concurrent sub-agents on a shared budget can't each pass an individual
        # check and then collectively overshoot (a $2.50 cap reached $6+ with a
        # wide parallel fan-out). reserve() raises BudgetExceeded here if the
        # call won't fit; released in `finally` once the actual spend lands.
        _est_cost = _estimate_call_cost(_price_spec, system, messages, tools,
                                        max_tokens, thinking_budget=thinking_budget)
        _held = budget.reserve(_est_cost) if budget is not None else 0.0
        try:
            with _trace_span(
                _gen_ai_span_name("chat", model_id),
                attributes={
                    "llm.provider": provider, "llm.model": model_id,
                    **_gen_ai_attributes(provider, model_id),
                },
            ):
                _ekw = {}
                if effort and provider == "anthropic":
                    from .effort import effort_for_model
                    model_effort = effort_for_model(effort, model_id)
                    if model_effort:
                        _ekw["effort"] = model_effort

                def _call():
                    return client.complete_async(
                        system=system, messages=messages, tools=tools, budget=budget,
                        max_tokens=max_tokens, thinking_budget=thinking_budget,
                        model=model_id, **_ekw,
                    )

                hedge = _hedge_ms()
                if hedge is None:
                    _resp = await _call()
                    return _resp
                # Tail-latency hedge (opt-in): race the primary against a backup
                # fired `hedge` ms later; first success wins, the laggard is
                # cancelled. The race is bounded by the remaining wall budget via
                # a SpanBudget so a hedge can never run past the goal's wall cap,
                # and the remaining budget is stamped on the current trace span.
                import asyncio as _asyncio

                from .latency_best_of_n import AllAttemptsFailed, race_first_success
                from .latency_span_budget import SpanBudget, tag_span_budget

                race_budget_ms: float | None = None
                if budget is not None and budget.max_wall_seconds:
                    span = SpanBudget(
                        max(0.0, (budget.max_wall_seconds - budget.elapsed()) * 1000.0)
                    )
                    tag_span_budget(span)
                    # An already-spent wall (remaining() == 0.0) must bound the
                    # race at zero -- it is not "no budget" (None = unbounded).
                    race_budget_ms = span.remaining()

                async def _backup():
                    await _asyncio.sleep(hedge / 1000.0)
                    _backup_held = (
                        budget.reserve(_est_cost) if budget is not None else 0.0
                    )
                    try:
                        return await _call()
                    finally:
                        _release_budget_hold(budget, _backup_held)

                try:
                    _resp = await race_first_success(
                        [_call, _backup], budget_ms=race_budget_ms
                    )
                    return _resp
                except AllAttemptsFailed as e:
                    # Both the primary and the hedge failed: surface the real
                    # provider error (chained as __cause__) so failover/retry
                    # classification upstream sees the provider's exception, not
                    # the race wrapper.
                    raise (e.__cause__ or e) from None
        except Exception:
            _err = True
            raise
        finally:
            if _held:
                budget.release(_held)
            _dt_ms = (_time.monotonic() - _t0) * 1000.0
            # Price THIS call's own usage rather than diffing the shared
            # budget.dollars counter, which races concurrent sub-agents.
            # The dispatching client's CACHE_READ_MULT keeps the OpenAI-family
            # cached-prompt discount per-provider (DeepSeek 0.1x, Gemini 0.25x).
            _spent = _call_spend(_price_spec, _resp, budget, _d0,
                                 getattr(client, "CACHE_READ_MULT", None))
            _record_provider_spend(provider, _spent)  # feed the $ ceiling ledger
            try:
                from .provider_health import get as _h
                _h().record(provider, model_id,
                            latency_ms=_dt_ms, dollars=_spent, error=_err)
            except Exception:  # pragma: no cover
                pass
            _feed_circuit(provider, _err)
            try:
                from .observability import record_metric as _rm
                _rm("llm_calls", labels={"provider": provider, "model": model_id})
                _rm("llm_latency", _dt_ms / 1000.0,
                    labels={"provider": provider, "model": model_id})
                if budget is not None:
                    # inc() by THIS call's delta, not the per-goal cumulative
                    # total: budget_dollars is a lifetime counter, and passing
                    # the per-goal accumulator let a fresh goal stomp it.
                    _rm("budget_dollars", _spent)
            except Exception:  # pragma: no cover
                pass

    def prewarm(
        self,
        system: str,
        tools: list[dict] | None = None,
        model: str | None = None,
        *,
        budget: Budget | None = None,
    ) -> bool:
        """Pre-warm the prompt cache for ``(system, tools, model)``.

        Anthropic-only (other providers cache implicitly with no warm hook), and
        a no-op unless caching is on. When a budget is provided, reserve an
        estimated cache-write cost before sending and let the provider record
        returned usage. Returns whether a warm request was sent; never raises."""
        if os.environ.get("MAVERICK_CACHE_MESSAGES", "1") == "0":
            return False
        provider, model_id = _parse_spec(
            require_model_allowed(model or self.model)
        )
        if provider != "anthropic":
            return False
        try:
            from .enterprise import assert_provider_allowed
            assert_provider_allowed(provider)
            client = self._get_client(provider)
            warm = getattr(client, "prewarm", None)
            if not callable(warm):
                return False
            messages = [{"role": "user", "content": "warmup"}]
            # A prewarm can bill prompt-cache creation/read tokens even though
            # max_tokens=0. Reserve the worst-case Anthropic cache-write input
            # multiplier before dispatch so zero/low dollar caps cannot be
            # bypassed by opt-in prewarming. The provider records exact usage.
            # Estimate on the provider-qualified spec -- see complete().
            _held = budget.reserve(
                _estimate_call_cost(f"{provider}:{model_id}", system, messages, tools, 0)
                * _cache_write_mult_from_ttl("1h")
            ) if budget is not None else 0.0
            try:
                if budget is not None:
                    return bool(warm(system, tools, model_id, budget=budget))
                return bool(warm(system, tools, model_id))
            finally:
                if _held:
                    budget.release(_held)
        except Exception:  # pragma: no cover -- prewarm is best-effort
            return False


def cache_prewarm_enabled() -> bool:
    """Opt-in, default-OFF. ``MAVERICK_CACHE_PREWARM=1`` or ``[cache] prewarm =
    true`` pre-warms the prompt cache at orchestrator start so the first turn's
    time-to-first-token doesn't pay the cold cache write."""
    _true = {"1", "true", "yes", "on"}
    if os.environ.get("MAVERICK_CACHE_PREWARM", "").strip().lower() in _true:
        return True
    try:
        from .config import load_config
        v = (load_config() or {}).get("cache", {}).get("prewarm")
        return str(v).strip().lower() in _true if isinstance(v, str) else bool(v)
    except Exception:  # pragma: no cover
        return False
