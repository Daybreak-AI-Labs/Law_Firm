# Pricing governance

Lightwork treats model prices as evidence-bearing accounting inputs, not as
unlabelled constants. The built-in rate card is versioned, immutable at
runtime, and backed by
`maverick/data/pricing-rate-card-2026-07-29.json`. Each row records its source
URL, retrieval timestamp, applicable date, currency, confidence, pricing basis,
scope, and verification status.

Live accounting is fail-closed by default:

- a verified row may affect a budget or spend cap;
- an unverified row is available only for estimates;
- a missing row stops accounting before token counters or dollar totals mutate;
- `Budget.pricing_evidence` retains the exact quote used for each model.

Set `[budget] strict_pricing = false` only when a legacy or custom
OpenAI-compatible gateway has no current sourced price. This explicit
compatibility mode emits warnings and labels its totals as unverified,
estimate-only values. Do not use those totals for invoices or chargebacks.

## Conservative context ceilings

OpenAI publishes context-dependent Standard rates and a regional-processing
uplift. Because the current `ModelPrice` contract stores one flat input/output
pair, rate-card `2026-07-29.2` uses the maximum documented context tier and
then applies the documented 10 percent regional uplift. This can overestimate
some calls, but it cannot undercount a call merely because its context or
processing region is unknown. The evidence pack retains the short and long
source observations separately from the derived billing ceiling.

xAI also publishes short- and long-context standard rates. The same rate card
uses each model's documented 200K-or-more tier as its flat billing ceiling and
retains both source tiers in the evidence pack.

## Verification status as of 2026-07-29

Direct current rows are verified for Anthropic Opus 4.5-4.8, Opus 4.8 fast,
Sonnet 4.6, Haiku 4.5; OpenAI GPT-5.5, GPT-5.4, GPT-5.4 Pro, Mini, and Nano;
DeepSeek v4 Flash and Pro; xAI Grok 4.5, Grok Build 0.1, and Grok 4.3; and
Google Gemini 3.5 Flash.

Deprecated DeepSeek aliases, Grok Code Fast and other ambiguous or redirected
xAI aliases, unsupported older Google rows, Moonshot rows without a captured
current source, and OpenRouter marketplace rows remain unverified and
estimate-only.

The tracked primary sources are:

- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [xAI pricing](https://docs.x.ai/developers/pricing)
- [Google Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Moonshot pricing](https://platform.moonshot.ai/pricing)
- [OpenRouter model catalog](https://openrouter.ai/models)

Zero-dollar Codex CLI and self-hosted rows mean only that Lightwork sees no
metered external API-token charge. Subscription, compute, energy, storage, and
operations costs are outside that accounting scope and are stated explicitly
in each row's applicability field.
