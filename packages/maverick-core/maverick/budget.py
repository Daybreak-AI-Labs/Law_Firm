"""Budget tracking. Long-horizon agents need hard caps.

v0.2 cost-correctness fix:
  - record_tokens() takes the actual model id (default falls back to
    Sonnet rate for back-compat). Before this, an Opus orchestrator
    call was billed at Sonnet rate, so max_dollars=5 was letting
    ~$25 of real spend through.
  - Cache tokens are priced correctly. Anthropic charges 0.1x for
    cache reads and 1.25x for cache writes. The provider used to
    collapse everything into ``input_tokens`` at full price.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from .pricing import (
    ModelPrice,
    UnverifiedRateError,
    assert_price_evidenced,
    load_pricing_evidence_pack,
)
from .runtime_overrides import budget_override

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class UnpricedModelError(BudgetExceeded):
    """Raised when billing lacks a verified, provenance-carrying model rate.

    Subclassing ``BudgetExceeded`` preserves the existing hard-stop behavior at
    every spend boundary. Planning code can explicitly request estimate-only
    pricing from :func:`_lookup_price`; live accounting cannot silently do so.
    """


# Fallback price (Sonnet 4.6 list, no cache discount) in $/Mtok.
# Used only by the explicit estimate-only fallback for an unknown model.
_FALLBACK_PRICE_IN = 3.0
_FALLBACK_PRICE_OUT = 15.0

# Model ids already warned about (estimate-priced); warn once each, not per call.
_UNPRICED_WARNED: set[str] = set()
_ESTIMATE_MODE_WARNED = False
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _billing_strict() -> bool:
    """Whether live accounting requires a verified rate.

    Billing is fail-closed by default. The environment override has precedence,
    followed by an explicitly configured ``[budget] strict_pricing`` boolean.
    ``false`` is retained as a legacy opt-in to estimate-only accounting for
    custom gateways; malformed values fail closed.
    """

    raw = os.environ.get("MAVERICK_BILLING_STRICT")
    if raw is not None and raw.strip():
        norm = raw.strip().lower()
        if norm in _TRUTHY:
            return True
        if norm in _FALSY:
            return False
        log.warning(
            "MAVERICK_BILLING_STRICT must be true/false; failing closed "
            "with strict pricing"
        )
        return True
    try:
        from .config import get_budget_overrides

        budget = get_budget_overrides() or {}
    except Exception as exc:  # fail closed: config outage cannot weaken billing
        log.warning("pricing config unavailable; failing closed: %s", exc)
        return True
    if "strict_pricing" not in budget:
        return True
    value = budget.get("strict_pricing")
    if isinstance(value, bool):
        return value
    log.warning(
        "budget.strict_pricing must be true or false; failing closed with "
        "strict pricing"
    )
    return True


def _resolve_estimate_only(value: bool | None) -> bool:
    """Resolve an explicit lookup mode or the deployment billing policy."""

    global _ESTIMATE_MODE_WARNED
    if value is not None:
        return bool(value)
    estimate_only = not _billing_strict()
    if estimate_only and not _ESTIMATE_MODE_WARNED:
        _ESTIMATE_MODE_WARNED = True
        log.warning(
            "budget.strict_pricing=false enables legacy estimate-only "
            "accounting: provisional rates may affect dollar totals; every "
            "such rate is labeled unverified in Budget.pricing_evidence and "
            "must not be used for invoices or chargebacks"
        )
    return estimate_only


def _coerce_count(v: object) -> int:
    """Coerce a usage count to a non-negative int, failing closed on bad data.

    Providers occasionally return ``None`` in ``usage`` on streaming refusals;
    that remains a zero count for backwards-compatible null-safety. Non-finite,
    unparseable, or negative counts are invalid accounting data. Raise
    ``BudgetExceeded`` so the run stops instead of silently recording a paid
    call as $0 and bypassing later budget checks.
    """
    if v is None:
        return 0
    try:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("non-finite")
        n = int(v or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BudgetExceeded(f"invalid token usage count: {v!r}") from exc
    if n < 0:
        raise BudgetExceeded(f"invalid token usage count: negative value {n}")
    return n

# Anthropic cache multipliers (over the list input price).
# Two TTLs: 5m default (1.25x write surcharge) and 1h (2.0x write surcharge).
# Wave 12: prior code collapsed all writes to 1.25x; long-TTL writes were
# under-billed by ~40%.
_CACHE_READ_MULT = 0.1     # Anthropic: 90% discount on cache reads (both TTLs)
_CACHE_WRITE_MULT_5M = 1.25
_CACHE_WRITE_MULT_1H = 2.0

# OpenAI / o-series / gpt-5 automatic prompt cache discounts cache reads ~50%
# (0.5x), NOT 90% like Anthropic. Billing those reads at 0.1x under-counted
# cached-input cost ~5x for every non-Anthropic provider with cache hits.
# Callers pass the provider-appropriate multiplier; default stays Anthropic's.
CACHE_READ_MULT_OPENAI = 0.5


def _cache_write_mult_from_ttl(ttl: str | None) -> float:
    """Map Anthropic cache TTL string to the write surcharge multiplier.

    Wave 12 hardening: strip + lowercase before matching so trailing
    whitespace ("1h ") or case variants ("1H") don't silently downgrade
    to the 5m rate. Also: anything >= 5m duration is billed at 2.0x to
    match Anthropic's published surcharge tiers (the SDK accepts more
    TTL strings than the original 3-value whitelist).
    """
    if not ttl:
        return _CACHE_WRITE_MULT_5M
    norm = ttl.strip().lower()
    # Known 1h-tier aliases.
    if norm in ("1h", "60m", "3600s", "1hour", "1 hour"):
        return _CACHE_WRITE_MULT_1H
    # Parse duration suffixes — anything > 5m bills at 1h rate.
    try:
        if norm.endswith("h"):
            return _CACHE_WRITE_MULT_1H if float(norm[:-1]) >= 1 else _CACHE_WRITE_MULT_5M
        if norm.endswith("m"):
            return _CACHE_WRITE_MULT_1H if float(norm[:-1]) > 5 else _CACHE_WRITE_MULT_5M
        if norm.endswith("s"):
            return _CACHE_WRITE_MULT_1H if float(norm[:-1]) > 300 else _CACHE_WRITE_MULT_5M
    except ValueError:
        pass
    return _CACHE_WRITE_MULT_5M


def _tracked_policy_price(
    model_id: str,
    rates: tuple[float, float],
    *,
    evidence_id: str,
) -> ModelPrice:
    """Build and verify a quote from a tracked non-vendor metering policy."""

    evidence_pack = load_pricing_evidence_pack()
    evidence = evidence_pack.entries[evidence_id]
    quote = ModelPrice(
        model_id=model_id,
        input_per_mtok=rates[0],
        output_per_mtok=rates[1],
        source=evidence.source_url,
        as_of=evidence.as_of,
        fetched_at=evidence.retrieved_at,
        currency=evidence.currency,
        confidence=evidence.confidence,
        verified=evidence.verified,
        rate_card_version=evidence_pack.rate_card_version,
        evidence_id=evidence_id,
        pricing_basis=evidence.pricing_basis,
        applicability=evidence.applicability,
    )
    assert_price_evidenced(quote, evidence_pack)
    return quote


def _estimate_policy_price(
    model_id: str,
    rates: tuple[float, float],
    *,
    source: str,
    confidence: float,
    version: str,
    evidence_id: str,
    pricing_basis: str,
    applicability: str,
) -> ModelPrice:
    """Build a clearly provisional quote for a compatibility estimate."""

    return ModelPrice(
        model_id=model_id,
        input_per_mtok=rates[0],
        output_per_mtok=rates[1],
        source=source,
        as_of="2026-07-29",
        fetched_at="2026-07-29T16:00:00Z",
        currency="USD",
        confidence=confidence,
        verified=False,
        rate_card_version=version,
        evidence_id=evidence_id,
        pricing_basis=pricing_basis,
        applicability=applicability,
    )


def _lookup_price_quote(
    model: str | None,
    *,
    estimate_only: bool | None = None,
) -> ModelPrice:
    """Resolve a model rate with its provenance and verification evidence.

    Billing-grade lookup is the default and fails closed for a missing or
    provisional rate. ``estimate_only=True`` opts one planning call into
    estimates; an explicitly configured legacy ``strict_pricing=false`` opts
    live accounting into the same warning-labelled compatibility path.
    """

    from .llm import MODEL_PRICES, MODEL_SONNET, model_price_quote

    estimate_only = _resolve_estimate_only(estimate_only)
    requested = model or MODEL_SONNET

    # Codex CLI is subscription-metered rather than API-token-metered. This is
    # an explicit, verified accounting policy and must win before a bare model
    # id (such as gpt-5.5) resolves to the paid OpenAI API rate.
    if requested.startswith("codex_cli:"):
        return _tracked_policy_price(
            requested,
            (0.0, 0.0),
            evidence_id="codex-cli-subscription-policy-2026-07-29",
        )

    keys = [requested]
    bare = requested.split(":", 1)[1] if ":" in requested else requested
    if bare != requested:
        keys.append(bare)

    for key in keys:
        try:
            quote = model_price_quote(key, estimate_only=estimate_only)
        except UnverifiedRateError as exc:
            raise UnpricedModelError(
                f"{exc}. A legacy/custom gateway may explicitly set "
                "[budget] strict_pricing=false for warning-labelled "
                "estimate-only accounting."
            ) from exc
        legacy = MODEL_PRICES.get(key)
        if quote is not None and (legacy is None or legacy == quote.rates):
            if quote.currency != "USD":
                raise UnpricedModelError(
                    f"price for {requested!r} is in {quote.currency}; "
                    "Budget dollars requires a verified USD rate"
                )
            if not quote.verified and requested not in _UNPRICED_WARNED:
                _UNPRICED_WARNED.add(requested)
                log.warning(
                    "estimate-only accounting is using unverified rate for "
                    "%r (source=%s, as_of=%s, confidence=%.2f); not suitable "
                    "for invoices or chargebacks",
                    requested,
                    quote.source,
                    quote.as_of,
                    quote.confidence,
                )
            return quote
        if legacy is not None:
            # A runtime override of the compatibility tuple map carries no
            # source or verification evidence. Preserve it for planning, but
            # never let it become a bill merely because two floats exist.
            if not estimate_only:
                raise UnpricedModelError(
                    f"price for {requested!r} exists only in the metadata-free "
                    "MODEL_PRICES compatibility view; billing requires a "
                    "verified ModelPrice"
                )
            estimate_quote = _estimate_policy_price(
                requested,
                (float(legacy[0]), float(legacy[1])),
                source="maverick://pricing-policy/legacy-tuple-estimate",
                confidence=0.25,
                version="legacy-tuple-estimate-v1",
                evidence_id="legacy-tuple-estimate-v1",
                pricing_basis="Unverified integration-supplied USD per 1M token estimate",
                applicability=(
                    "Estimate-only compatibility override; no tracked "
                    "source evidence"
                ),
            )
            if requested not in _UNPRICED_WARNED:
                _UNPRICED_WARNED.add(requested)
                log.warning(
                    "estimate-only accounting is using metadata-free rate "
                    "for %r (source=%s, confidence=%.2f); not suitable for "
                    "invoices or chargebacks",
                    requested,
                    estimate_quote.source,
                    estimate_quote.confidence,
                )
            return estimate_quote

    # Unknown self-hosted ids incur no metered API-token charge. This does not
    # claim infrastructure is free; the source makes the accounting scope
    # explicit and auditable.
    provider = requested.split(":", 1)[0] if ":" in requested else requested
    if ":" in requested and provider in ("ollama", "vllm", "tgi"):
        return _tracked_policy_price(
            requested,
            (0.0, 0.0),
            evidence_id="self-hosted-api-metering-policy-2026-07-29",
        )

    if not estimate_only:
        raise UnpricedModelError(
            f"no verified USD price for model {requested!r}; refusing to bill "
            "at an estimate. Add a sourced ModelPrice to the versioned rate "
            "card. Planning code may explicitly request estimate_only=True; "
            "a legacy/custom gateway may explicitly set "
            "[budget] strict_pricing=false for warning-labelled estimates."
        )

    if requested not in _UNPRICED_WARNED:
        _UNPRICED_WARNED.add(requested)
        log.warning(
            "no verified price for model %r; estimate-only calculation is "
            "using the Sonnet fallback ($%.2f/$%.2f per Mtok); this rate must "
            "not be used for billing",
            requested,
            _FALLBACK_PRICE_IN,
            _FALLBACK_PRICE_OUT,
        )
    return _estimate_policy_price(
        requested,
        (_FALLBACK_PRICE_IN, _FALLBACK_PRICE_OUT),
        source="maverick://pricing-policy/unknown-model-estimate",
        confidence=0.1,
        version="unknown-model-estimate-v1",
        evidence_id="unknown-model-estimate-v1",
        pricing_basis="Unverified Sonnet-rate planning fallback",
        applicability=(
            "Estimate-only unknown-model fallback; no vendor-specific "
            "pricing evidence"
        ),
    )


def _lookup_price(
    model: str | None,
    *,
    estimate_only: bool | None = None,
) -> tuple[float, float]:
    """Return ``(input, output)`` rates while enforcing the requested use."""

    return _lookup_price_quote(model, estimate_only=estimate_only).rates


@dataclass
class Budget:
    max_input_tokens: int = 1_000_000
    max_output_tokens: int = 200_000
    max_dollars: float = 5.0
    max_wall_seconds: float = 3600.0
    max_tool_calls: int = 500

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    dollars: float = 0.0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    # Model -> immutable rate-card evidence used by this budget. Kept out of
    # ``__init__`` because callers must not inject provenance independently of
    # the pricing provider.
    pricing_evidence: dict[str, dict[str, object]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    # Legacy defaults; only used when ``model`` isn't passed.
    price_in_per_mtok: float = _FALLBACK_PRICE_IN
    price_out_per_mtok: float = _FALLBACK_PRICE_OUT

    def record_tokens(
        self,
        in_tok: int,
        out_tok: int,
        *,
        model: str | None = None,
        cache_read_tok: int = 0,
        cache_write_tok: int = 0,
        cache_write_ttl: str | None = None,
        cache_read_mult: float | None = None,
    ) -> None:
        """Add usage from one LLM call.

        ``in_tok`` is the number of input tokens billed at full rate
        (i.e. excludes the cache_read_tok and cache_write_tok counts).
        ``cache_read_tok`` is billed at ``cache_read_mult`` x the input rate
        (default 0.1x for Anthropic; pass ``CACHE_READ_MULT_OPENAI`` =0.5 for
        OpenAI/o-series/gpt-5 auto-cache). ``cache_write_tok`` at 1.25x (5m TTL)
        or 2.0x (1h TTL) — pass ``cache_write_ttl="1h"`` for a 1h breakpoint.

        Wave 12 nullsafety: ``None`` usage counts coerce to zero — Anthropic
        occasionally returns ``None`` in ``usage`` on streaming refusals; the
        prior code raised ``TypeError`` and the instance counted as $0 spent.
        Non-finite, unparseable, or negative counts now fail closed with
        ``BudgetExceeded`` instead of silently recording a paid call as zero.

        Council finding: cache reads/writes accumulate in separate
        counters so ``max_input_tokens`` reflects the BILLABLE input
        budget (non-cached only). Caching is a discount, so heavy
        caching should let you DO more work within the same input cap.
        """
        in_tok = _coerce_count(in_tok)
        out_tok = _coerce_count(out_tok)
        cache_read_tok = _coerce_count(cache_read_tok)
        cache_write_tok = _coerce_count(cache_write_tok)
        # Resolve before mutating counters. A missing/unverified billing rate
        # must leave the Budget untouched, not record tokens and then raise.
        quote = _lookup_price_quote(model)
        in_rate, out_rate = quote.rates
        # Wave 12 (council F12b): make the accumulator atomic so a
        # future parallel best-of-N (or multi-agent swarm where two
        # agents share a Budget) can't lose updates. `+=` on float is
        # NOT atomic in CPython under threads — we'd silently undercount.
        # Wave 12 hardening: check() runs INSIDE the lock too — TOCTOU
        # otherwise lets two threads both pass check() with state that
        # the OTHER thread has already invalidated. check() does no I/O
        # and elapsed() is reentrant-safe, so holding the lock through
        # it is fine.
        with self._lock:
            self.input_tokens += in_tok
            self.cache_read_tokens += cache_read_tok
            self.cache_write_tokens += cache_write_tok
            self.output_tokens += out_tok

            self.pricing_evidence[quote.model_id] = quote.metadata()
            write_mult = _cache_write_mult_from_ttl(cache_write_ttl)
            # Provider-aware cache-read discount: Anthropic 0.1x (default),
            # OpenAI/o-series/gpt-5 auto-cache ~0.5x (caller passes it).
            read_mult = _CACHE_READ_MULT if cache_read_mult is None else float(cache_read_mult)
            self.dollars += (in_tok / 1_000_000) * in_rate
            self.dollars += (cache_read_tok / 1_000_000) * in_rate * read_mult
            self.dollars += (cache_write_tok / 1_000_000) * in_rate * write_mult
            self.dollars += (out_tok / 1_000_000) * out_rate
            self.check()

    def record_tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1
            self.check()

    def absorb(self, other: Budget) -> None:
        """Roll another Budget's consumption into this one atomically and
        enforce caps.

        Used when a child/attempt runs on its own Budget (e.g. best-of-N)
        and its spend must count against the parent cap. Replaces the raw
        ``self.dollars += other.dollars`` roll-up, which bypassed both the
        lock (lost updates under concurrency) and ``check()`` (so the
        parent silently busted its cap across attempts).
        """
        with self._lock:
            self.input_tokens += other.input_tokens
            self.output_tokens += other.output_tokens
            self.cache_read_tokens += other.cache_read_tokens
            self.cache_write_tokens += other.cache_write_tokens
            self.dollars += other.dollars
            self.tool_calls += other.tool_calls
            self.pricing_evidence.update(other.pricing_evidence)
            self.check()

    def elapsed(self) -> float:
        # Wave 12: use monotonic so NTP clock-skew doesn't bypass the wall
        # cap. `started_at` is captured in __post_init__ for monotonic.
        try:
            return time.monotonic() - self._started_monotonic
        except AttributeError:
            # Legacy path: dataclass instance created before __post_init__
            # extension landed. Fall back to wall clock.
            return time.time() - self.started_at

    def remaining_wall(self) -> float:
        """Seconds left before the wall-clock cap (<= 0 means already over).

        Lets a caller bound an in-flight async await (e.g. an LLM generation)
        so the wall cap is HARD — interrupting a long generation — rather than
        only checked at turn boundaries."""
        return self.max_wall_seconds - self.elapsed()

    def __post_init__(self) -> None:
        self._started_monotonic = time.monotonic()
        # Wave 12 (F12b): per-instance lock for atomic counter updates.
        self._lock = threading.Lock()
        # Dollar cost of in-flight calls, held by reserve() until release().
        # Lets concurrent sub-agents sharing one budget account for each
        # other's pending spend instead of all passing the same check and
        # then collectively overshooting the cap.
        self._reserved = 0.0
        # A non-finite cap (nan/inf) silently disables enforcement: every
        # `self.dollars > nan` comparison in check() is False, so the cap
        # never trips. TOML 1.0 has native nan/inf and `--max-dollars inf`
        # parses, so a cap could arrive non-finite from config or a flag.
        # Coerce any non-finite dollar/wall/token cap back to a safe default
        # rather than run uncapped. (Budget caps are not optional.)
        for _field, _default in (
            ("max_dollars", 5.0),
            ("max_wall_seconds", 3600.0),
            ("max_input_tokens", 1_000_000),
            ("max_output_tokens", 200_000),
        ):
            _v = getattr(self, _field)
            try:
                if not math.isfinite(float(_v)):
                    setattr(self, _field, _default)
            except (TypeError, ValueError):
                setattr(self, _field, _default)

    def __getstate__(self):
        """Wave 12 hardening: threading.Lock is unpicklable. Drop the
        non-picklable transient fields so a Budget can survive being
        sent to a multiprocessing worker (and the monotonic clock is
        per-process — reset on unpickle to avoid bogus elapsed math)."""
        state = self.__dict__.copy()
        state.pop("_lock", None)
        # Preserve consumed wall time across process boundaries.
        # Monotonic baselines are process-local, so serialize elapsed
        # duration and reconstruct a compatible baseline on restore.
        state["_elapsed_at_pickle"] = self.elapsed()
        state.pop("_started_monotonic", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()
        elapsed = float(self.__dict__.pop("_elapsed_at_pickle", 0.0) or 0.0)
        self._started_monotonic = time.monotonic() - max(0.0, elapsed)

    def check(self) -> None:
        if self.input_tokens > self.max_input_tokens:
            raise BudgetExceeded(f"input tokens {self.input_tokens} > {self.max_input_tokens}")
        if self.output_tokens > self.max_output_tokens:
            raise BudgetExceeded(f"output tokens {self.output_tokens} > {self.max_output_tokens}")
        if self.dollars > self.max_dollars:
            raise BudgetExceeded(f"${self.dollars:.2f} > ${self.max_dollars:.2f}")
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded(f"tool calls {self.tool_calls} > {self.max_tool_calls}")
        elapsed = self.elapsed()
        if elapsed >= self.max_wall_seconds:
            raise BudgetExceeded(
                f"wall time {elapsed:.0f}s >= {self.max_wall_seconds:.0f}s"
            )

    def check_projected(self, est_cost: float) -> None:
        """Raise BEFORE a call whose estimated cost would push spend over the
        dollar cap.

        ``check()`` only fires once ``dollars`` ALREADY exceeds the cap, so a
        single huge-context call -- or several concurrent sub-agent calls on a
        shared budget -- can blow far past ``max_dollars`` before the next check
        trips (observed: a $2.50 cap reached $6.81 with parallel Opus/Sonnet
        researchers on 200k-token contexts). Gating each dispatch on its
        projected cost bounds the overshoot to roughly the in-flight set.
        """
        if est_cost <= 0:
            return
        with self._lock:
            projected = self.dollars + getattr(self, "_reserved", 0.0) + est_cost
            if projected > self.max_dollars:
                raise BudgetExceeded(
                    f"projected ${projected:.2f} > ${self.max_dollars:.2f} "
                    f"(spent ${self.dollars:.2f} + est ${est_cost:.2f} for this call)"
                )

    def reserve(self, est_cost: float) -> float:
        """Atomically hold ``est_cost`` against the cap for an in-flight call;
        return the amount held (0.0 if non-positive). Raises ``BudgetExceeded``
        if spent + already-held + est would exceed the dollar cap -- so N
        concurrent sub-agent calls on a shared budget can't each pass an
        individual check and then collectively blow past it (a $2.50 cap hit
        $6+ with a wide parallel fan-out). Pair every successful reserve() with
        a release() in a ``finally``.
        """
        est = est_cost if (est_cost and est_cost > 0) else 0.0
        if est == 0.0:
            return 0.0
        with self._lock:
            held = getattr(self, "_reserved", 0.0)
            projected = self.dollars + held + est
            if projected > self.max_dollars:
                raise BudgetExceeded(
                    f"projected ${projected:.2f} > ${self.max_dollars:.2f} "
                    f"(spent ${self.dollars:.2f} + in-flight ${held:.2f} "
                    f"+ est ${est:.2f} for this call)"
                )
            self._reserved = held + est
            return est

    def release(self, held: float) -> None:
        """Release a hold taken by reserve(). Safe to call with 0.0."""
        if not held or held <= 0:
            return
        with self._lock:
            self._reserved = max(0.0, getattr(self, "_reserved", 0.0) - held)

    def cache_hit_rate(self) -> float:
        """Fraction of prompt-input tokens served from cache this run, across
        ALL providers (Anthropic ephemeral, OpenAI/Gemini auto-cache).

        ``cache_read / (cache_read + billable_input)`` — the consolidated
        signal there was no single cross-provider view of before (each
        provider's cache was its own ledger). 0.0 when nothing was sent."""
        seen = self.input_tokens + self.cache_read_tokens
        return (self.cache_read_tokens / seen) if seen else 0.0

    def cache_stats(self) -> dict:
        """Read-only cross-provider cache accounting for this run."""
        return {
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "billable_input_tokens": self.input_tokens,
            "hit_rate": self.cache_hit_rate(),
        }

    def pricing_snapshot(self) -> dict[str, dict[str, object]]:
        """Copy of the rate provenance supporting this Budget's dollar total."""

        with self._lock:
            return {
                model_id: dict(evidence)
                for model_id, evidence in self.pricing_evidence.items()
            }

    def summary(self) -> str:
        return (
            f"tokens in={self.input_tokens} out={self.output_tokens} "
            f"cache_read={self.cache_read_tokens} hit_rate={self.cache_hit_rate():.0%} "
            f"$={self.dollars:.3f} tools={self.tool_calls} wall={self.elapsed():.0f}s"
        )


_BUDGET_KEY_TYPES = {
    "max_input_tokens": int,
    "max_output_tokens": int,
    "max_dollars": float,
    "max_wall_seconds": float,
    "max_tool_calls": int,
}


def budget_from_config(*, defaults: dict | None = None,
                       task_class: str | None = None, **overrides) -> Budget:
    """Build a Budget that honors the ``[budget]`` section of config.toml.

    Precedence, lowest to highest:
      learned **self-tuning** suggestion for ``task_class`` (opt-in; only when
      that class has enough history) < ``defaults`` (a caller's own fallback,
      e.g. the background runner's conservative caps) < the ``[budget]`` config
      section < explicit ``overrides`` (e.g. a CLI ``--max-dollars`` flag). A
      ``None`` value in either ``defaults`` or ``overrides`` is treated as
      "unset", so a caller can pass an optional flag straight through.

    The self-tuning layer is the *lowest* precedence: it only fills
    ``max_dollars`` when nothing more explicit set it, so an operator's
    configured cap always wins. Off by default (``[budget] self_tuning``).

    ``config.get_budget_overrides()`` already existed but was never wired,
    so the ``[budget]`` section had no effect on any run. This is the single
    funnel that fixes that; malformed values are skipped (keep prior layer)
    rather than crashing the run.
    """
    kwargs: dict = {}
    # Lowest precedence: a learned per-task-class default cap (opt-in). Seeds
    # max_dollars so `defaults`/config/overrides below still win if they set it.
    if task_class:
        try:
            from .self_tuning_budget import suggested_max_dollars
            learned = suggested_max_dollars(task_class)
            if learned is not None:
                kwargs["max_dollars"] = float(learned)
        except Exception:  # pragma: no cover -- self-tuning never blocks a run
            pass
    if defaults:
        for key, val in defaults.items():
            if key in _BUDGET_KEY_TYPES and val is not None:
                kwargs[key] = val
    try:
        from .config import get_budget_overrides
        cfg = get_budget_overrides() or {}
    except Exception:
        cfg = {}
    for key, caster in _BUDGET_KEY_TYPES.items():
        if cfg.get(key) is not None:
            try:
                cast = caster(cfg[key])
                # Reject non-finite caps from config (TOML nan/inf) -- they
                # would disable the cap rather than set it. Skip -> prior layer.
                if isinstance(cast, float) and not math.isfinite(cast):
                    continue
                kwargs[key] = cast
            except (TypeError, ValueError):
                pass  # malformed config value -> fall back to prior layer
    # Dashboard-set spend cap (settings page; lives in runtime-overrides.toml,
    # never config.toml). A live UI choice sits above [budget] config, below an
    # explicit per-call override / env flag.
    dash_cap = budget_override()
    if dash_cap is not None:
        kwargs["max_dollars"] = dash_cap
    # Env override for the documented MAVERICK_BUDGET_DOLLARS. The cookbooks
    # use `MAVERICK_BUDGET_DOLLARS=0.5 maverick start ...` to cap a single
    # invocation, but nothing read it -- so the cap silently did nothing and
    # users ran at the default. Sits above [budget] config, below an explicit
    # CLI override (--max-dollars). Malformed / non-finite -> keep prior layer.
    env_dollars = os.environ.get("MAVERICK_BUDGET_DOLLARS")
    if env_dollars:
        try:
            cast = float(env_dollars)
            if math.isfinite(cast):
                kwargs["max_dollars"] = cast
        except (TypeError, ValueError):
            pass
    for key, val in overrides.items():
        if val is not None and key in _BUDGET_KEY_TYPES:
            kwargs[key] = val
    reservation_id = _clamp_to_tenant_remainder(kwargs)
    budget = Budget(**kwargs)
    if reservation_id:
        # Handle so the run can release the tenant hold when it records its
        # actual spend: ``quotas.record_usage(..., reservation_id=b._tenant_reservation_id)``.
        budget._tenant_reservation_id = reservation_id
    return budget


def _clamp_to_tenant_remainder(kwargs: dict) -> str | None:
    """Clamp ``kwargs['max_dollars']`` to the active tenant's remaining daily
    allowance (#78) in place, and RESERVE that clamped cap against the tenant so
    concurrent runs coordinate (finding #2). Returns the reservation id, or
    ``None`` when no reservation was made (no active tenant / no cap / clamp 0).

    Highest precedence: the tenant's aggregate daily ceiling is a billing
    guarantee, so it overrides even an explicit ``--max-dollars``. Without it the
    over-quota gate only fires *between* runs, letting one run blow past the
    tenant cap. No active tenant / no cap -> untouched (the default single-tenant
    install is unchanged). Never *raises* a cap, only lowers it.

    Concurrency (finding #2, fixed): the remaining-allowance calculation and
    reservation write are one strict cross-process-locked operation. So N runs
    that START for one tenant before any records spend receive caps whose sum is
    no greater than the tenant's remaining daily allowance. The reservation
    carries a TTL backstop derived from the wall cap, so a crashed run that never
    records spend only dents the cap until the hold expires; the hold is released
    after the run's actual spend is durably recorded.
    """
    try:
        from .paths import current_tenant_id
        tenant = current_tenant_id()
    except Exception as exc:
        # An unreadable tenant identity may hide a cap. Zero the run allowance
        # rather than treating that policy outage as "unlimited".
        log.error("tenant budget policy unavailable; clamping run to zero: %s", exc)
        kwargs["max_dollars"] = 0.0
        return None
    if not tenant:
        return None

    # When no max_dollars was set, the requested amount is Budget's own default
    # (not "unbounded"). The atomic allocator can therefore only lower it.
    current = kwargs.get("max_dollars")
    if current is None:
        current = Budget.__dataclass_fields__["max_dollars"].default
    try:
        requested = float(current)
    except (TypeError, ValueError, OverflowError) as exc:
        log.error("tenant budget request is invalid; clamping run to zero: %s", exc)
        kwargs["max_dollars"] = 0.0
        return None
    if not math.isfinite(requested) or requested <= 0:
        kwargs["max_dollars"] = 0.0
        return None

    try:
        from .tenant.registry import reserve_tenant_budget
        # A run cannot spend past its wall cap, so the wall cap is a tight, safe
        # TTL backstop; bound it so a crashed run can't dent the cap for long.
        wall = kwargs.get("max_wall_seconds")
        try:
            wall = float(wall)
            if not math.isfinite(wall) or wall <= 0:
                raise ValueError
        except (TypeError, ValueError):
            wall = float(Budget.__dataclass_fields__["max_wall_seconds"].default)
        ttl = min(max(wall, 300.0), 6 * 3600.0)
        reservation_id = uuid.uuid4().hex
        granted = reserve_tenant_budget(tenant, requested, ttl, reservation_id)
        if granted is None:
            return None
        kwargs["max_dollars"] = granted
        if granted <= 0:
            return None
        return reservation_id
    except Exception as exc:
        # Running after a failed reservation reopens the concurrent overspend
        # window. Deny this start; a later retry can reserve after state heals.
        log.error("tenant budget reservation failed; clamping run to zero: %s", exc)
        kwargs["max_dollars"] = 0.0
        return None
