# Lightwork

> Enterprise recursive multi-agent swarm. One kernel, every model.

Lightwork is an **agentic enterprise platform**: a governed AI workforce of
2,020 prebuilt specialists across 53 business suites that provably improves
with use — deployed in your own environment. It drives any
LLM (Claude, GPT, Kimi, Grok, Gemini, DeepSeek, Ollama, OpenRouter), and
ships a governed, auditable safety surface built for regulated teams.
Lightwork is proprietary, commercially licensed software (see
[`LICENSE`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/LICENSE)).

## What you can do with it

- **Long-horizon software work**: recursive agent-spawns-agent
  orchestration with shared world model, budgets, and audit log.
- **Use your existing chat subscriptions**: ChatGPT Plus, Claude Pro,
  Kimi, X Premium, Gemini Advanced — drive them from the agent via
  captured browser sessions, no extra API spend. Note: session providers
  have no native function-calling, so Lightwork gives them tools through a
  **simulated** markdown tool-call protocol — it works for tool-using
  roles, but reliability is model-dependent and weaker than an API-key
  provider's native tool use.
- **Computer use & web browser**: Anthropic-spec computer-use tool +
  Playwright-driven browser tool, with kill switches and an audit
  trail for every action.
- **Multi-channel deployment**: Telegram, Discord, Slack, Signal,
- **Primary-source data grounding**: analyst packs are auto-granted 37
  read-only public-data connectors (SEC EDGAR, FRED, Treasury, World Bank,
  FDIC, Census, BLS, EIA, openFDA, NPPES, ClinicalTrials, USAspending,
  SAM.gov, CourtListener, Federal Register, GLEIF, OpenCorporates,
  NWS/NOAA, EPA, Climatiq, …) so they cite authoritative sources instead of
  model recall. On by default; kill-switch via `[workforce] data_grounding
  = false`. Alongside 2,877 write-capable enterprise REST/GraphQL connectors
  and dedicated modules (Salesforce, HubSpot, Stripe, ServiceNow,
  Snowflake, …).
- **Proven governance**: a roster-wide invariant test suite checks six
  governance invariants (tool-reachability, autonomy dial, capability
  attenuation, compartment isolation, unstrippable hard refusals, budget
  caps) across all 2,020 packs with a non-vacuous fault-injection control
  (property-fuzzed up to 5,000 iterations),
  plus hostile-argument fuzzing of every connector and tool.
  Email, Matrix, WhatsApp, SMS, iMessage — one config, all channels.
- **Build an agent from a demonstration**: record a reviewed example of the job,
  then synthesize the agent that does it — `maverick learn-demo <file>`
  ingests that transcript, induces a profile through the same
  intake clamp + review gate, and provisions the skills/tools it needs.
  The factory also improves itself: `maverick factory-learn` mines
  provisioning/approval gaps back into future pack generation
  (off by default, never widens an envelope).

## Quick start

```bash
git clone https://github.com/Daybreak-AI-Labs/Lightwork && cd Lightwork
git checkout --detach <reviewed-full-40-character-commit-sha>
pip install -e ./packages/maverick-core
pip install -e ./apps/installer-cli
maverick init                # interactive wizard (3 minutes)
maverick start "review my latest commit"
```

Or skip the prompts:

```bash
maverick init --fast         # defaults: Anthropic + local sandbox + $5 cap
```

## Watch it work

```bash
maverick monitor             # live plan-tree TUI in another terminal
maverick logs                # audit log
maverick cost                # spend summary
```

## Licensing & access

Lightwork is **proprietary, commercially licensed** software (see
[`LICENSE`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/LICENSE)). It is self-hostable — the runtime executes entirely
in your own environment — and use requires a license. Pricing is handled
per engagement; [contact us](https://github.com/Daybreak-AI-Labs/Lightwork) for
evaluation or enterprise access.

A deliberately stripped-down **open-source "lite" edition** may be released
later as a community on-ramp; the full runtime and the governance/compliance
platform remain proprietary.

## Where to go next

- [Getting started](getting-started.md) — install + first goal
- [Architecture](enterprise/architecture.md) — the governed agent runtime (OS-style primitives)
- [Configuration](configuration.md) — providers, channels, budgets
- [Deployment](deployment.md) — desktop / docker / VPS / phone modes
- [Safety](safety.md) — shield, audit log, kill switches, consent
- [Threat model](security/threat-model.md) — trust boundaries, capabilities, tenancy, and security controls
- [Security & compliance overview](enterprise/security-overview.md) — application egress controls, deployment boundaries, identity, audit/evidence
- [Plugins](plugins.md) — extending the tool / channel / skill surface
- [Governed model improvement](MODEL_IMPROVEMENT_PLATFORM.md) — the learning lifecycle, regression detection, and promotion controls
- [Starter goals](starter-goals.md) — ready-to-run examples for common workflows
- Roadmap details are available during enterprise evaluation.
- [Contributing](CONTRIBUTING.md) — how to send PRs
