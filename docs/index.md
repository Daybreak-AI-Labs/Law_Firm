# Bjerken and Day

> The firm's practice platform. One kernel, every model.

A governed roster of 31 legal profiles that
drafts, researches and keeps the file straight, running in the firm's own
environment with an attorney reviewing everything before it leaves the
office. It drives any LLM (Claude, GPT, Kimi, Grok, Gemini, DeepSeek, Ollama,
OpenRouter) behind one governed, auditable safety surface.

Private software for one firm, proprietary and not for distribution (see
[`LICENSE`](https://github.com/Daybreak-AI-Labs/Law_Firm/blob/main/LICENSE)).

## What you can do with it

- **Matter-bound legal work**: reviewed specialist profiles operate only inside
  an exact client matter, with budgets, audit, and attorney release gates.
- **Research and evidence**: matter-scoped attachments, local knowledge search,
  single-vendor guarded web research, citation checking, spreadsheets, and
  permanently read-only bounded SQL.
- **Five legal-system connectors**: GET-only access to Carta, Clio,
  Contractbook, DocuSign, and Ironclad. No connector is granted suite-wide.
- **Local improvement without fleet learning**: matter-scoped reflexion,
  dreaming, rehearsal, and skill distillation feed an offline candidate/eval
  loop; runtime agents cannot acquire tools or auto-promote code.
- **Fail-closed governance**: secure registries require the exact matter/profile
  context, refuse cross-matter reuse and shadow registration, and expose only
  two pure context-free helpers when no matter is bound.

## Quick start

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm && cd Law_Firm
git checkout --detach <reviewed-full-40-character-commit-sha>
pip install -e ./packages/maverick-core
pip install -e ./apps/installer-cli
maverick init                # interactive wizard (3 minutes)
maverick dashboard           # web UI at http://127.0.0.1:8765
```

Or skip the prompts:

```bash
maverick init --fast         # defaults: Anthropic + local sandbox + $5 cap
```

Queue your first goal from the dashboard; `maverick worker` executes queued
goals in the background. The dashboard streams each goal's plan tree live and
shows the audit log and spend.

## Internal use

This repository is private, proprietary software for Bjerken and Day. It is
not a commercial distribution, public package, or community edition. See
[`LICENSE`](https://github.com/Daybreak-AI-Labs/Law_Firm/blob/main/LICENSE).

## Where to go next

- [Getting started](getting-started.md) — install + first goal
- [Architecture](architecture.md) — the exact-matter governed runtime
- [Configuration](configuration.md) — providers, budgets, and firm controls
- [Deployment](deployment.md) — reviewed checkout, Docker, and VPS modes
- [Safety](safety.md) — shield, audit log, kill switches, consent
- [Threat model](security/threat-model.md) — trust boundaries, capabilities, tenancy, and security controls
- [Security hardening](security-hardening.md) — identity, egress, keys, audit, and recovery
- [Tool inventory](specs/tool-inventory.md) — the fixed firm runtime ceiling
- [Working on this repo](CONTRIBUTING.md)
