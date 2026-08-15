<p align="center">
  <img src="Daybreak%20Labs%20Logo.jpg" alt="Daybreak Labs" width="360">
</p>

# Lightwork

> Lightwork — by **Daybreak Labs**.

[![CI](https://github.com/Daybreak-AI-Labs/Lightwork/actions/workflows/ci.yml/badge.svg)](https://github.com/Daybreak-AI-Labs/Lightwork/actions/workflows/ci.yml)
[![License: Proprietary](https://img.shields.io/badge/license-Proprietary-red.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

**The governed, auditable AI agent runtime for regulated enterprises — a long-horizon multi-agent swarm that runs on your data, in your environment, under a hard budget.**

Hand Lightwork a goal. Its orchestrator decomposes it, spawns specialist sub-agents — researcher, coder, writer, verifier — that work in parallel, checks their output, and returns a result. Every step runs under a hard spending cap and through a safety layer, on the models *you* choose.

- 🛡️ **Governed & contained by default.** RBAC, capability tokens, per-tool ACLs, consent gates, and a kill switch bound every agent action — and in Enterprise mode an **egress lock** blocks the agent's outbound network paths (LLM calls and the built-in HTTP tools/connectors), so a prompt-injected agent can't exfiltrate through them (pair it with a network-level egress firewall to also cover raw-shell egress). Agent Shield also screens every prompt, tool call, and output; detector strength depends on the configured backend (see [`docs/safety.md`](./docs/safety.md)).
- 🧾 **Tamper-evident & audit-ready.** A signed, hash-chained, append-only audit log (`lightwork audit verify`) with SIEM export, encryption-at-rest, DSAR, and SOC2-aligned evidence — built to survive a security review.
- 🔒 **Self-host & air-gap.** Runs entirely in your environment — laptop, VPC, Kubernetes, or a disconnected network with no required data egress. No hyperscaler dependency, no telemetry.
- 📈 **A workforce that provably improves.** Closed-loop learning — offline experience consolidation ("dreaming"), per-department memory, skill distillation with safe forgetting — every learned artifact audited, snapshotted, and rollback-able. `lightwork hindsight` detects if learning ever regressed; `lightwork proof` reports deliverables, cost avoided, ROI, and the improvement curve.
- 🔬 **Causal learning, not vibes.** A default-on **Cognitive Data Engine** (`lightwork flywheel`) triages production failures by their *causal* impact on real outcomes — stratified ATE with confidence intervals, placebo refutation, and a trustworthiness gate — then mines self-retiring **guardrails**, consolidates **habits**, and lets an **Operations Scientist** prove a better process in a world-model before spending a real experiment. The **Consequence Engine** (`lightwork record-outcome`) grounds all of it in what *actually* happened — an invoice paid, a ticket reopened — so the workforce learns from reality, not a model grading its own work. Every loop remains independently stoppable.
- 🏢 **2,020 prebuilt specialists across 53 business suites.** Customer support, finance, legal, HR, ops, GTM, marketing, procurement, data, security ops, tax preparation for CPA firms, and 30+ industry verticals (healthcare, insurance, banking, gov contracting, maritime, mining, semiconductors, chemicals, water, renewables, …) — every pack a real agent with a least-privilege tool envelope and risk ceiling, an editable workflow playbook, a declared deliverable, a right-sized reasoning tier, and hard prohibited-use refusals (EU AI Act Art-5 for HR, safety-critical for the physical suites). Prove it: `lightwork domains-lint` (0 errors, 0 warnings), `domains-audit` (0 drafting agents can reach a state-mutator), `domains-eval` (behavioral golden cases). The orchestrator finds the right one via query-based routing. Backed by a library of **514 reusable, validator-compliant skills** (`SKILL.md`) that any pack can activate by trigger, and an **agent factory** that equips every freshly-approved pack with the skills and tools its workflow needs *at birth* — never widening the pack's already-clamped envelope.
- 🏭 **An agent factory that builds agents — and itself.** Describe a job, or demonstrate it: `lightwork learn-demo` consumes a reviewed action transcript and synthesizes the agent that does it through the same intake pipeline — identical envelope clamp and persona shield-scan, with a human review gate always appended. Default-on `lightwork factory-learn` closes the loop onto generation quality, mining provisioning/approval gaps into proposer guidance that improves future pack generation. It remains independently stoppable and never widens any pack's envelope.
- 👁️ **Ekko work discovery.** An independently opt-in, local-first observer learns the structure of repeated work from allowlisted application transitions or explicitly guided semantic actions — never keystrokes, clipboard, screenshots, window titles, URLs, or document content — and turns the evidence into reviewable first-pass Flow and Agent Factory drafts. Capture is visibly start/pause/stop controlled, tenant/owner/device scoped, encrypted at rest, time-bounded, and cannot save, schedule, activate, or run its own recommendations. See [`docs/ekko-work-discovery.md`](./docs/ekko-work-discovery.md).
- 📚 **Primary-source data grounding.** Every analyst pack is auto-granted (by suite) a set of 37 read-only, GET-only public-data connectors — SEC EDGAR, FRED, Treasury, World Bank, FDIC, Census, BLS, EIA, openFDA, NPPES, ClinicalTrials, USAspending, SAM.gov, CourtListener, Federal Register, GLEIF, OpenCorporates, NWS/NOAA weather, EPA, Climatiq, and more — so the workforce grounds its work in authoritative primary sources, not model recall. On by default (low-risk, deferred), with a kill switch (`[workforce] data_grounding = false` / `LIGHTWORK_WORKFORCE_DATA_GROUNDING=off`) and an installer wizard step. These sit alongside 2,877 write-capable long-tail enterprise REST/GraphQL connectors and dedicated modules (Salesforce, HubSpot, Stripe, ServiceNow, Snowflake, …).
- 🧪 **Governance proven across the whole roster.** A roster-wide invariant test suite verifies six governance invariants across all 2,020 packs — tool-reachability (no drafting agent can reach a state-mutating tool), the autonomy dial (onboarding and high-risk actions are never autonomous), capability attenuation (a spawned child can never exceed its parent grant), compartment isolation (a quarantine seal never bleeds across suites), unstrippable hard refusals, and never-silently-exceeded budget caps — each fault-injected with a non-vacuous control across the full roster, plus hostile-argument fuzzing of every connector and tool.
- 🧠 **Long-horizon multi-agent depth.** A recursive orchestrator spawns specialist sub-agents that work for hours under hard dollar / wall-clock / tool-call caps — frontier-agent depth, on the models *you* choose, with the governance and learning layers no coding-agent runtime ships.
- 🔀 **Visual workflow automation that learns (better than Power Automate).** A deterministic flow engine — branch / switch / foreach / while / parallel / approval / delay / scope (try-catch) / sub-flow / setvar — where any step is *either* a fixed tool call *or* a full agentic goal, drawn on a visual designer or drafted from plain English by a chat copilot. Fires from cron (timezone/DST-aware), webhook, email, form, file, RSS, or polled/OAuth'd sources; pauses for human approval; retries with backoff; caps spend and concurrency; pulls credentials with `{{secret('NAME')}}` from the sealed vault; and takes typed, validated manual-run inputs. The differentiator: a **self-rewrite loop** grounds each node's real outcomes and *hardens* a reliably-succeeding agent step into a cheap deterministic action (inferring the very tool it kept calling) — or *softens* a flaky fixed step back to an agent — with measured before/after impact, versioned rollback, and auto-revert. OFF by default (`[flows] enable`).

> **Proprietary software — not open source.** Lightwork is enterprise software; use, redistribution, and derivative works require a license. [Contact us](https://github.com/Daybreak-AI-Labs/Lightwork) for evaluation or commercial access. See [`LICENSE`](./LICENSE) and [`TRADEMARK.md`](./TRADEMARK.md).

```bash
git clone https://github.com/Daybreak-AI-Labs/Lightwork && cd Lightwork
git checkout --detach <reviewed-full-40-character-commit-sha>
pip install -e ./packages/maverick-core   # kernel (with deps)
pip install -e ./apps/installer-cli       # + the setup wizard
lightwork init                        # four questions, safe defaults
lightwork start "Build a CLI that emails me a digest of today's top Hacker News stories — research the API, write it, and verify it runs"
```

> A one-line `pipx install 'maverick-agent[installer]'` is planned after the
> complete eight-package cohort is published. Until the package or an
> asset-bearing GitHub Release exists, use a reviewed source commit as shown
> above. The [Releases page](https://github.com/Daybreak-AI-Labs/Lightwork/releases)
> is the authoritative source for downloadable artifacts.

## Status

Alpha and installable from a reviewed source commit today. Release automation
builds an eight-package Python cohort, container, single-file binaries, four
standalone source archives, and evidence artifacts. A downloadable artifact is
available to users only after it appears on the
[GitHub Releases page](https://github.com/Daybreak-AI-Labs/Lightwork/releases).
PyPI publishing remains disabled until all eight namespaces and trusted
publishers are verified. See [`docs/getting-started.md`](./docs/getting-started.md).

## What works today vs. planned

| Component | v0.1 (today) | Planned (v0.2+) |
|---|---|---|
| Install | Reviewed source commit; bootstrap/native assets when present on a GitHub Release | pipx after the complete cohort is published; code-signed bundles + auto-update |
| GUI | Local web dashboard (`lightwork dashboard`) + chat at `/chat`; authenticated source-bootstrap shell is an internal engineering artifact | Self-contained, signed native installer + iOS/Android |
| Sandbox | Local subprocess, Docker, gVisor, Podman, devcontainer, Kubernetes, SSH, Modal | Firecracker microVM (scaffold — exec path does not yet mount the workspace), Daytona |
| AI providers | Anthropic (full), OpenAI, OpenRouter, Ollama, Gemini, DeepSeek, Bedrock, Azure, xAI, Moonshot, TGI, vLLM (per-role routable) | Cohere |
| Channels | All 17 wired — Telegram, Discord, Slack, Signal, Email, Matrix, Bluesky, Mastodon, Voice, IRC, Threads, RCS, Glasses; WhatsApp (Cloud API + Twilio)/SMS (need Twilio), iMessage (macOS-only) | Push notifications |
| Safety | Shield wired at 3 chokepoints; agent-shield SDK if installed, else a built-in rule set | Agent-shield full ~115 patterns |
| Distribution | Release workflow for GHCR image, binaries, four signed source archives, checksums, and SBOMs; reviewed source install works today | PyPI (8-package lockstep cohort, gated pending namespace verification); self-contained code-signed installer; Homebrew tap |
| Tests | Ruff, security gates, posture ratchet, and pytest on Python 3.10/3.11/3.12 | Broader deployment and browser journey coverage |

**Full list of shipped features → [`docs/FEATURES.md`](./docs/FEATURES.md).** The forward backlog (what isn't built yet) lives in [`docs/ROADMAP.md`](./docs/ROADMAP.md).

## Install

### Download a published single-file binary

Check the **[Releases page ›](https://github.com/Daybreak-AI-Labs/Lightwork/releases)**.
Use this route only when a release includes the binary for your operating
system; a tag alone is not a downloadable build:

| OS | File on the release |
|---|---|
| **Windows** | `maverick-windows-x86_64.exe` |
| **macOS** | `maverick-macos-arm64` |
| **Linux** | `maverick-linux-x86_64` |

Verify the release tag, checksums, and attached Sigstore material before
running a binary. The separate Tauri workflow currently produces an unsigned
authenticated source-bootstrap artifact for authorized repository users. It
clones readable source from a pinned private commit and is not advertised as a
self-contained product installer.

### Terminal install with pipx (coming with the first tagged release)

Once the packages are published to PyPI on the first tagged release, a single pipx command will install everything. **This does not work yet** — the packages aren't on the public index (see [Status](#status)); until then use the [From source](#from-source) steps above. The published command will be:

```bash
pipx install 'maverick-agent[installer]'
lightwork init
```

For source-based desktop bootstrapping, download `deploy/desktop/install.sh` or `deploy/desktop/install.ps1` from a commit you trust, verify it, and set `MAVERICK_REF` to that lowercase, full 40-character commit SHA. The bootstrap scripts reject missing and mutable refs and never fall back to the public index.

The PyPI distribution name is `maverick-agent` — Lightwork's original codename, kept because the `maverick` name is squatted on PyPI. The CLI it installs answers to **both `lightwork` and `maverick`**; every `MAVERICK_*` environment variable can equally be spelled `LIGHTWORK_*`. The `[installer]` extra pulls the wizard into the same pipx environment so `lightwork init` resolves.

If you already installed the kernel without the extra, inject the wizard:

From the reviewed source checkout, inject the local path so pipx never resolves
a first-party name from a public index:

```bash
pipx inject maverick-agent ./apps/installer-cli
```

### From source

```bash
git clone https://github.com/Daybreak-AI-Labs/Lightwork
cd Lightwork
pip install -e ./packages/maverick-core
pip install -e ./apps/installer-cli
# Optional sister packages:
pip install -e ./packages/maverick-shield
pip install -e ./packages/maverick-channels
pip install -e ./packages/maverick-dashboard
pip install -e ./packages/maverick-mcp
pip install -e ./packages/maverick-evolve
pip install -e ./packages/maverick-knowledge

lightwork init                           # interactive wizard
lightwork start "Plan a 2-week trip"      # one-shot goal
lightwork chat                            # interactive REPL
lightwork dashboard                       # web UI at http://127.0.0.1:8765
lightwork serve                           # channel server (Telegram/Discord/...)
lightwork mcp                             # MCP server (Claude Code / Cursor)
lightwork doctor                          # health check
lightwork version                         # installed package versions
```

## CLI reference

The CLI installs under two interchangeable names — `lightwork` (shown below) and `maverick` (the original codename).

| Command | What |
|---|---|
| `lightwork init` | Interactive setup wizard with preflight + API-key validation |
| `lightwork doctor` | Green / yellow / red health check + remediation hints |
| `lightwork version` | Installed package versions + runtime info |
| `lightwork config show / path / edit` | Show / locate / edit `~/.maverick/config.toml` |
| `lightwork start TITLE [--template NAME --param k=v]` | Run a goal once |
| `lightwork chat` | Interactive REPL (each line = a goal) |
| `lightwork serve` | Channel server (reads `[channels.*]` from config) |
| `lightwork dashboard [--host --port --token]` | Local web UI + REST API |
| `lightwork mcp` | MCP server on stdio for Claude Code / Cursor / etc. |
| `lightwork logs / status / answer / resume` | Inspect + control running goals |
| `lightwork schedule goal / add / list / rm` | Schedule recurring autonomous goals via cron |
| `lightwork worker` | Drain the scheduled-job queue (runs the recurring tasks) |
| `lightwork fact / facts` | Get / set persistent facts |
| `lightwork skills` | List installed + distilled skills |
| `lightwork skill install / remove / info` | Manage the skill marketplace |
| `lightwork template list / show` | Goal templates with `{{ var }}` substitution |
| `lightwork learn-demo FILE [--name --no-llm --source --industry --yes]` | Build an agent from a recorded demonstration (parse → induce → approve → save → provision) |
| `lightwork ekko enroll / run / status / pause / resume / stop / discover / erase / forget` | Explicitly control local work discovery and review evidence-backed automation opportunities |
| `lightwork factory-learn [--min-support N] [--dry-run]` | Default-on governed factory learning: mine provisioning/approval gaps into proposer guidance |
| `lightwork self-modify status / archive` | Inspect the off-by-default, research-only DGM gate, readiness blockers, and candidate lineage |
| `lightwork self-modify run --objective TEXT [--cycles N]` | Operator-run DGM research cycle; proposes/evaluates/archives only and never adopts code |
| `lightwork budget` | Total + per-run cost history |
| `lightwork spend [--json --tag-field --top]` | FinOps export: total + per-goal + per-tag run cost (CLI face of `/spend`) |
| `lightwork safety [--json]` | Safety posture: shield / sandbox backend / egress policy (assert in CI) |
| `lightwork flywheel` | Default-on Cognitive Data Engine: causal failure triage → guardrails → habits |
| `lightwork record-outcome GOAL EP VALUE` | Feed a real downstream outcome to a past episode (Consequence Engine) |
| `lightwork codebook / codec-learn` | Learn the swarm's auditable coordination shorthand from real messages |
| `lightwork codec-probe` | Measure the codec's real token (not just byte) savings with the target tokenizer |

## Repository layout

```
packages/
  maverick-core/       Python agent kernel: recursive swarm, persistent world
                       model (SQLite + FTS5, or Postgres; schema v23), 12 LLM providers, 9
                       sandboxes, MCP client, skills, templates, persona,
                       background runner, budget tracking
  maverick-shield/     Agent Shield integration + built-in fallback rule set
  maverick-channels/   17 channel adapters: Telegram, Discord, Slack, Signal,
                       Email, Matrix, Bluesky, Mastodon, Voice, WhatsApp, WhatsApp
                       Cloud, SMS, iMessage, IRC, Threads, RCS, Glasses
                       (WhatsApp/SMS need Twilio; iMessage is macOS-only)
  maverick-dashboard/  Local FastAPI web UI + REST API at /api/v1 + OpenAPI
                       docs at /docs. Live progress streaming via short-poll.
  maverick-mcp/        MCP server (stdio JSON-RPC) -- exposes Lightwork to Claude
                       Code, Cursor, Claude Desktop as a tool. The agent kernel
                       can also CONSUME external MCP servers as its own tools.
apps/
  installer-cli/       Interactive Python TUI wizard (`lightwork init`)
  installer-desktop/   Tauri authenticated source-bootstrap engineering shell;
                       not a self-contained product installer
deploy/
  docker/ vps/ desktop/  Dockerfile, install.sh, systemd unit, Caddyfile
docs/
  getting-started.md     Install + first run
  architecture.md        The governed agent runtime (OS-style primitives)
  configuration.md       Full config schema reference
  deployment.md          Desktop / Docker / VPS / Phone-companion targets
  safety.md              Shield chokepoints and built-in rule set
  security-hardening.md  Enterprise opt-in controls + compliance commands
  api.md                 REST API reference + curl examples
benchmarks/
  longhorizon/           Reproducible long-horizon evaluation tasks
  example-skills/        Curated SKILL.md files for the marketplace
  example-templates/     Reusable goal-template files
```

## Drive Lightwork from another language

Lightwork's kernel is Python, but its **wire surface** is the
[Model Context Protocol](https://modelcontextprotocol.io/). Any
MCP-speaking language can drive the swarm from outside Python:

- **TypeScript / JavaScript** → [docs/clients/typescript-quickstart.md](./docs/clients/typescript-quickstart.md)
- **Go** → [docs/clients/go-quickstart.md](./docs/clients/go-quickstart.md)
- **Rust** → [docs/clients/rust-quickstart.md](./docs/clients/rust-quickstart.md)
- **C# / .NET** → [docs/clients/csharp-quickstart.md](./docs/clients/csharp-quickstart.md)
- **Java / JVM** → [docs/clients/java-quickstart.md](./docs/clients/java-quickstart.md)

Each is a 20-line program: spawn `lightwork mcp`, list tools, call one.
Why this and not a separate `@lightwork/core` port?
[Language Bindings — Council Decision](./docs/ROADMAP.md#language-bindings--council-decision-may-2026).

## Run Lightwork in CI

Run the swarm inside any repo's GitHub Actions — on a PR, a schedule, or on
demand — under a hard spend cap:

```yaml
- uses: Daybreak-AI-Labs/Lightwork/deploy/github-action@<reviewed-full-40-character-commit-sha>
  with:
    goal: "Summarize this PR and flag anything risky."
    max-dollars: "0.50"
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

See [docs/github-action.md](./docs/github-action.md).

## Vision

| Axis | Lightwork |
|---|---|
| **Target user** | Enterprise & technical teams -- self-hosted, governed, auditable |
| **Wedge** | Long-horizon depth + true multi-agent coordination |
| **Safety** | First-class. Every input, tool call, and output passes through Agent Shield. |
| **Control** | You pick the models. Per-role. Multi-provider. |
| **Deploy** | Desktop / Docker / VPS / Phone (17 channels) |
| **Privacy** | All detection runs locally. Your data never leaves your machine unless you choose a cloud LLM. |

## License

Proprietary — commercially licensed. Use, redistribution, and derivative works
require a license. See [`LICENSE`](./LICENSE) and [`TRADEMARK.md`](./TRADEMARK.md);
[contact us](https://github.com/Daybreak-AI-Labs/Lightwork) for access.
