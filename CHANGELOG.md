# Changelog

All notable changes to the firm's platform. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed
- **Cross-organization agent interop** — `a2a`, `federation`,
  `channel_federation`, the federated audit-log verifier, and
  `grpc_api/federation.proto`. These let *other organizations'* agents discover
  this instance and delegate goals to it; a single firm has no counterparty.
  The `a2a` per-caller bearer surface is gone with them, so
  `[agent_trust] a2a_token`, `maverick trust add --a2a-token` and
  `--surface a2a` no longer exist — an existing `a2a_token` in config is
  ignored rather than rejected. `federation_envelope.py` (the shared Ed25519
  primitive) is retained.
- **Marketplace ecosystem backend** — listing federation, moderation tooling
  and donation links. The pack/connector browser (`storefront.py`) and your own
  goal-template star ratings (`ratings.py`, `stats.py`) are retained.
- **Third-party language SDKs** — the TypeScript, Go, Rust, C# and Java client
  examples, quickstarts and their five CI jobs. MCP remains the surface for
  outside callers.
- **Enterprise product surface** — sales and certification-programme docs, the
  product roadmap and RFCs, translated getting-started guides, and the
  public-docs staging overlay.

### Added
- **AI Evidence-Ready Gateway and operator cockpit** — governed evidence intake,
  review queues, readiness/health visibility, and first-run guidance now share
  one operator-facing workflow with durable audit checkpoints.
- **Governance Frontier benchmark publication** — the public report now ships
  beside its exact measured manifest, separately trusted Ed25519 publisher key,
  and a standalone verifier with tamper/ambiguity rejection.
- **Lockstep release-cohort delivery** — all eight Python distributions are
  validated and resolved together from one manifest across frozen binaries,
  containers, installers, CI integrations, and SBOM generation; release-runtime
  provider support and constrained dependency versions are checked before
  artifacts are published.
- **DPO preference-pair mining from real runs** — the offline DPO fine-tune
  (`maverick.training.rlaif`) now has two objective sources of a quality
  gradient, both built from donated run records (no hand-labels): a
  verifier-**rejected draft** paired against the accepted final within one
  `task_family`, and **best-of-N candidate mining** — `run_goal_best_of_n`
  scores each of N coding attempts by whether the task's real tests pass
  (`--fail-to-pass`/`--pass-to-pass`) and donates the pass-vs-fail spread as a
  preference pair with a ~1.0 reward margin, no verifier rejection needed. New
  `maverick start --repeat N` re-runs the same task N times so its attempts
  share a `task_family`. Trajectory donation is now reachable from the installer
  wizard (Advanced reasoning step, default off, metadata-only), and its
  thresholds are configurable under `[telemetry]` (`donate_min_entropy`,
  `donate_min_confidence`, `donate_text`) so a normal single-agent run is
  captured (the old swarm-only bar donated nothing from typical usage).
  End-to-end recipe in `docs/self-learning-runbook.md`.
- **FinOps & safety CLI + billing view** — `maverick spend` (`--json`) exports
  total + per-goal + per-tag run cost for BI/chargeback scripts; `maverick
  safety` (`--json`) prints the shield / sandbox-backend / egress posture to
  assert deployment safety in CI; and a dashboard `/billing` view shows the
  active tenant's accrued charges, a period-over-period trend, and an itemized
  CSV invoice (`/billing?format=csv&period=YYYY-MM`).
- **Obsidian-style goal graph** — `/graph-editor` now renders a force-directed
  constellation: circular nodes sized by connection count, hover spotlighting a
  node's neighbourhood, drag/pan/zoom, with the layered text tree as the
  accessible fallback. Edits (retitle / reparent / add child) and the cycle
  guard are unchanged.
- Roster & library growth: the specialist roster now stands at **2,020 packs
  across 53 suites**, with a **514-skill** reusable `SKILL.md` library any pack
  can activate by trigger.
- **Primary-source data connectors (37, read-only, low-risk)** — authoritative
  government/public data APIs (SEC EDGAR, FRED, Treasury, World Bank, FDIC,
  Census, BLS, EIA, Alpha Vantage, Finnhub, Polygon, OpenFIGI, Federal Register,
  eCFR, Regulations.gov, CourtListener, GovInfo, USAspending, SAM.gov, Open
  States, PatentsView, GLEIF, OpenCorporates, Companies House, openFDA, NPPES,
  ClinicalTrials, RxNorm, PubMed, NWS, NOAA Climate, OpenWeather, EPA Envirofacts,
  Climatiq, Carbon Interface). Built on three new `make_rest_tool` auth modes:
  `keyless`, `query_auth` (key on the query string), and `default_base_url`
  (zero-config public hosts).
- **Primary-source data grounding** — those connectors are auto-granted to each
  analyst pack by suite (`SUITE_DATA_CONNECTORS`, layered in `domain_capability`),
  so a pack reaches for the right source by default. Additive, low-risk, and
  deferred (no context cost); a host-restricted pack's egress is never silently
  widened. On by default with a kill-switch: `[workforce] data_grounding = false`
  / `MAVERICK_WORKFORCE_DATA_GROUNDING=off`, plus an installer wizard step.
- **Roster-wide governance invariant test suite** — six load-bearing invariants,
  each verified across all 2,020 packs with a non-vacuous fault-injection control
  (property-fuzzed up to 5,000 iterations): (1) tool reachability — no drafting agent can reach
  a state-mutating tool; (2) autonomy dial — an onboarding agent is never
  autonomous and a high-risk action is never autonomous even when graduated;
  (3) capability attenuation — a spawned child can never exceed its parent grant
  (no privilege escalation through the spawn chain); (4) compartment isolation —
  a quarantine seal never bleeds across compartments or suites; (5) hard refusals
  — the universal refusal floor is unstrippable; (6) budget caps — no cap is ever
  silently exceeded. Plus hostile-argument fuzzing of every connector and tool.
- Agent-pack quality & governance sweep across the 1,118-pack roster:
  `[output]` contracts + editable `[[workflow]]` playbooks on every pack;
  reasoning-effort right-sizing (`effort` tier, applied only when `[effort]` is
  on); always-on hard refusals (`domain_refusals.py` — EU AI Act Art-5 for HR,
  safety-critical actuation for the physical suites, autonomous adjudication for
  finance/clinical, MNPI-crossing for strategy, with a `refuse` pack field);
  `maverick domains-audit` (governance-posture inventory, `--json` export) and
  `maverick domains-eval` (behavioral golden cases + deterministic rubric
  scorer); query-based specialist routing (`list_specialists query=<task>`,
  hybrid lexical + sentence-transformer when `fastembed` is installed) with a
  recall@10 benchmark; factory-generated packs brought to parity (intake now
  emits workflow/output/effort/refuse). New `domains-lint` rules: read-only
  deny floor, output/playbook gate consistency, and `effort` validity.
- Closed-loop self-improvement: offline experience consolidation
  (`maverick dream` — replay, consolidate, rehearse, forget, prune),
  department-scoped reflexion/memory, skill distillation with probation and
  retirement, cross-department insight promotion, human-override and
  user-correction ingestion, and verifier-scored rehearsal gated by the
  calibration interlock.
- Learning governance: per-cycle `learning_update` audit rows, `--dry-run`,
  pre-cycle snapshots with `--rollback`, tenant-isolated learned stores, and
  the hindsight engine (`maverick hindsight`) for learning-regression
  detection.
- `maverick proof` — workforce value report (deliverables, cost avoided,
  ROI, improvement curve), with per-vendor `--fleet` breakdown.
- Cognitive Data Engine (`maverick flywheel`, opt-in `[data_engine]`) —
  causal failure triage (stratified ATE + confidence intervals + placebo
  refutation + a trustworthiness gate) → self-retiring guardrails →
  consolidated habits, composed in one pass; observable at
  `GET /api/v1/flywheel`.
- Operations Scientist (opt-in `[operations_scientist]`) — propose a
  harmful→beneficial action swap and validate it in a g-computation
  world-model before spending a real experiment.
- Consequence Engine (`maverick record-outcome`, `POST /api/v1/outcomes`,
  opt-in `[consequence]`) — a real downstream outcome overrides the
  verifier's self-graded proxy so learning is grounded in reality.
- Emergent Substrate (`maverick codebook` / `codec-learn` / `codec-probe`,
  opt-in `[emergent_protocol]` / `[emergent_codec]`) — an auditable
  coordination codec that learns short codes for repeated boilerplate;
  every code decodes EXACTLY back to English (`decode(encode(x)) == x`,
  fuzz-tested). `codec-probe` measures real token (not just byte) savings;
  the token-aware codec measures ~28% in benchmark. Measure-only on the live
  blackboard (`GET /api/v1/codec`); agents reading codes is a separate step.
- Fleet memory (`maverick fleet-memory`, MCP tools `maverick_fleet_ingest`
  / `maverick_fleet_recall`) — governed, audited memory plane for external
  agents.
- The Operating Record (`maverick record`) — decisions + approvals as a
  queryable system of record with Ed25519-signed, offline-verifiable
  capsule export.
- Federated insight exchange (`maverick insights-export` / `-import`,
  fail-closed signing) and fleet donation aggregation
  (`maverick dream --donations-dir`).
- Specialist portfolio expanded from 338 to 1,000 lint-clean agents across
  25 suites, including customer support, 10 industry verticals, and
  jurisdiction packs; suite operating discipline and department memory
  applied to every pack at spawn; `maverick domains-lint` quality gate.
- Goal rows persist their department (world-model schema v14).
- Tax preparation pipeline (`maverick tax prepare`, `tax_prep.py`): uploaded
  documents → deterministic classification/extraction → workpaper →
  first-pass TY2025 draft 1040 **and resident-state return** (auto-detected
  from W-2 box 15; no-tax/flat states computed, graduated states handed to
  the preparer) → preparer review package, every line cited to its source
  document and out-of-scope items flagged as open items; plus the 19-pack
  `tax_` specialist suite (portfolio now 1,118 packs across 26 suites) and
  CCH Axcess / Thomson Reuters GoSystem connectors (confirm-gated write
  seats + GET-only low-risk read seats for the status packs).
- Signed tax-constants channel (`maverick tax update`, `[tax]` config):
  new tax law arrives as an Ed25519-signed content bundle — fail-closed
  verification, sanity validation, downgrade protection, rollback, audit —
  auto-applied by `maverick tax prepare` when `[tax] auto_update` +
  `update_url` are configured; the `tax_law_watch` pack monitors IRS/state
  guidance (web access, no client-data access) and alerts the firm.


### Fixed
- **Dashboard `/agents` and `/workflows` latency** — the built-in pack catalog
  (2,000+ packs) was re-parsed from disk on every request, making those pages
  ~75× slower than the rest of the dashboard. The built-in catalog is now
  memoised per process (keyed by its directory mtime; tenant overrides still
  load live), cutting both pages from ~700ms to ~10–20ms.
- Goal-graph links at `/graph-editor` now render as a clearly visible
  soft-grey (the previous near-invisible tone read as unconnected nodes).
- Robustness defects surfaced by the 1M-iteration stress sweep (each with a
  regression test): connectors no longer raise on a non-string `op`/`path`/
  `query` — a malformed value now returns an `ERROR:` string instead of an
  `AttributeError` that crashed the call for all ~250 REST + GraphQL connectors;
  `Skill.parse` raises `ValueError` (its documented failure mode), not
  `AttributeError`, on malformed/untrusted frontmatter (a YAML list item under a
  scalar key), so an `except ValueError` guard around skill loading holds;
  `format_money` degrades gracefully on a `None`/empty currency instead of
  crashing on `None.upper()`.
- Dual control: the "requester cannot approve their own request" segregation-of
  -duties rule now fires for real dashboard approvals. The consent gate records
  the executing goal's owner as the approval's requester, so an N-of-M approval
  can no longer be self-approved by its initiator (previously the requester was
  never recorded, leaving the bar unreachable outside tests).
- Best-of-N now sets each attempt's sampling temperature via a per-goal
  ContextVar instead of a process-global `MAVERICK_TEMPERATURE` env var, so two
  goals running concurrently in one process no longer read each other's
  temperature. The env var is still honoured as a process-wide fallback.
- Per-tenant RBAC role assignments are rejected (409) under per-user tenancy
  (`MAVERICK_TENANT_BY_USER`), where every request is pinned to the caller's own
  isolated tenant and a named-tenant role could never take effect — turning a
  silent no-op into an explicit error. Named-tenant deployments are unaffected.
- MCP server returns `-32602` (invalid params), not a scrubbed `-32603`, for a
  non-string `uri` (`resources/read`) or a non-string/non-hashable tool `name`
  (`tools/call`).
- World model search now backfills the `messages_fts` full-text index on
  upgrade (schema v10). The FTS triggers only index future writes, so a
  database whose messages predated the index carried unindexed history that
  message search silently missed; v10 rebuilds it once.
- Self-learning data pipeline (donation → ingest → DPO) closed several gaps that
  left the training corpus empty: trajectory records now carry a `goal_id` (so
  `ingest`/`export_texts` can join the world DB for transcripts) and a
  `task_family` derived from the task-brief hash (without it `rlaif` grouped
  everything into one family and mined zero pairs); the swarm-disagreement
  donation gate is now configurable so single-agent runs are no longer silently
  dropped.
- Best-of-N candidate capture now recovers patches an agent applied via tools
  (no diff in its final answer) by diffing the workdir, excludes `__pycache__`/
  `*.pyc` and binary sections (which made `git apply` reject the whole patch and
  scored every candidate 0), and resets the shared workdir to clean HEAD between
  attempts so one attempt's edits can't leak into the next. New
  `MAVERICK_BON_EARLY_EXIT=0` runs all N attempts (instead of stopping at the
  first pass) so a failing candidate is also captured for the preference pair.

## [0.1.6] -- 2026-05-31

Post-0.1.4 hardening, plus the cross-language MCP client surface (the
council's "drive Maverick from any language over MCP" decision) and the
polyglot sandbox / coding-mode work.

### Added
- **Cross-language MCP clients.** Runnable example clients and quickstarts for
  **TypeScript / JavaScript, Go, Rust, C# / .NET, and Java / JVM** -- each one
  exercised in CI against a live `maverick mcp` (initialize -> tools/list -> a
  no-LLM tool call). Maverick stays a single Python kernel; any MCP-speaking
  language drives it over stdio JSON-RPC. See `docs/clients/*-quickstart.md`.
- **Per-language sandbox toolchains.** Container backends now pick their image
  from `[sandbox] language` (rust -> rust:1, go -> golang:1, JS/TS -> node:22,
  Java/Kotlin -> eclipse-temurin:21, ruby -> ruby:3; Python is the default),
  and the installer wizard asks which language you code in -- so `cargo test` /
  `go test` / the JS runner actually run instead of failing on python:3.12-slim.
  coding-mode gained Rust/Go/TypeScript failure-pattern hints, and
  `maverick doctor` reports a missing language toolchain.
- **Agent-to-Agent (A2A) task endpoint** with push-notification config, an
  installer wizard step, and docs.

### Security
- **SMS and WhatsApp now enforce a per-sender allowlist** (default-deny),
  closing a critical auth bypass: in 0.1.4 these two Twilio channels checked
  only the `X-Twilio-Signature` (which proves Twilio relayed the message, not
  that the *sender* is authorized), so any PSTN subscriber who texted the
  number could drive the swarm with host shell access. They now match the
  other channels: a valid signature plus an explicit allowlist member.
- **GDPR erase now re-anchors the signed audit chain.** When `[audit] sign`
  is enabled, scrubbing/tombstoning a user's rows used to break the Ed25519
  hash-chain at the erasure point -- a break `maverick audit verify` could
  not tell apart from tampering, so a routine privacy operation destroyed
  the trust anchor. erase re-chains and re-signs the affected files (the
  signed `erase` marker records the authorized cut) so verification passes
  again. Also de-duplicated a double `scrub_user` call in the erase path.
- Bumped dependency floors past known CVEs on the network-facing surfaces:
  `pillow>=12.2.0` (5 CVEs incl. PYSEC-2026-165), `python-multipart>=0.0.27`
  (CVE-2026-42561), `requests>=2.33.0` (CVE-2026-25645), `starlette>=1.0.1`
  (PYSEC-2026-161), `urllib3>=2.7.0` (PYSEC-2026-142/141). starlette is
  declared directly where the dashboard imports it; requests/urllib3 are
  floored in the Twilio (`sms`/`whatsapp`) extras that pull them in.
- The CI `pip-audit` job is now blocking, so a new advisory fails the build
  and prompts a floor bump instead of shipping silently.

### Changed (breaking)
- The `sms` and `whatsapp` channels **require** an allowlist to start. Set
  `SMS_ALLOWED_USER_IDS` / `WHATSAPP_ALLOWED_USER_IDS` (comma-separated) or
  the `allowed_user_ids` config key, or the channel raises on startup. List
  senders as Twilio delivers them: `+14155551234` for SMS,
  `whatsapp:+14155551234` for WhatsApp. (Slack/Signal/Matrix already required
  this as of 0.1.4 -- SMS/WhatsApp were missed.)

## [0.1.4] -- 2026-05-30

The launch-hardening pass that landed just after the 0.1.3 tag was cut.
0.1.3 shipped without these; 0.1.4 is the first release to include them.

### Added
- Per-sender channel allowlists for Slack/Signal/Matrix/Voice (default-deny);
  the installer wizard now collects `*_ALLOWED_USER_IDS` / allowed callers.
- `compute`/sympy math tool wired up (was dead); restored two
  silently-shadowed CLI commands (GDPR `export`, `logs`).

### Fixed
- **Killswitch is now enforced.** `maverick halt`, the dashboard Halt button,
  and the HALT file are checked at the agent turn boundary and the tool
  boundary -- in 0.1.3 they were read by nothing (a no-op).
- **MCP HTTP transport** no longer crashes with `asyncio.run() ... running
  event loop`; client-supplied budgets are clamped and an arbitrary host-file
  read (`trusted_local`) was closed.
- **VPS installer** references the correct pipx package name (`maverick-agent`)
  and its run-as-user model is coherent.
- GDPR `erase` no longer fails on the foreign-key cascade; audit `scrub_user`
  is wired.
- Cost accounting for router-selected models; best-of-N now rolls all budget
  counters (cache tokens + tool calls) into the parent and enforces its cap.
- CircuitBreaker HALF_OPEN admits exactly one probe (was a retry storm); MCP
  negotiates protocol version by supported set, not a lexicographic downgrade.
- `dep_graph` emits forward-slash paths on every platform; arXiv API uses
  https; Windows POSIX file-mode test assertions are guarded.

## [0.1.3] -- 2026-05-29

### Added
- One-line desktop installers: `deploy/desktop/install.ps1` (Windows,
  `irm ... | iex`) and `deploy/desktop/install.sh` (macOS/Linux,
  `curl ... | bash`). They install Python 3 + git if missing, set up an
  isolated pipx environment, and launch the wizard -- no prerequisites.

### Fixed
- Windows installer could not find a just-installed Python: winget runs
  the python.org installer without adding it to PATH. Detection now
  falls back to the PEP 514 registry and well-known install dirs, and
  probes with `--version` instead of a quoted `python -c "..."` snippet
  (Windows PowerShell 5.1 mangles embedded double quotes, which made
  every probe fail even when the interpreter was fine).
- Release binaries crashed with `No module named 'maverick'` -- the
  build installed packages editable, which PyInstaller can't collect.
  Now installed non-editable, with `collect_submodules`/`copy_metadata`
  in the spec.

### Changed
- The MCP server is published to PyPI as `maverick-mcp-server` (the
  `maverick-mcp` name is taken by an unrelated project). The import
  package (`maverick_mcp`) and the `maverick-mcp` command are unchanged.
- PyPI publishing runs one job per package (`fail-fast: false`), so a
  package without a trusted publisher can't abort the others.
- Repository references updated to the `cdayAI` GitHub account.

## [0.1.1] -- 2026-05-25

### Fixed
- PyInstaller release binaries on all three platforms (Linux x86_64,
  macOS arm64, Windows x86_64) failed at `import sqlite3` -- the
  v0.1.0 build flags missed bundling stdlib `sqlite3`. Switched to a
  `.spec` file with an explicit `hiddenimports` list and pinned
  PyInstaller to `>=6.0,<7.0`. A diagnostic step now verifies
  `sqlite3` is importable on the build host before the bundle runs.
- `maverick version` reported `maverick: not installed` after the
  PyPI rename to `maverick-agent`. The reporter now reads
  `maverick-agent` as the canonical distribution name.

### Added
- Multi-turn conversation state per channel user (schema v3 -> v4).
- File + image input on goals (schema v4 -> v5) with mime allowlist,
  per-file/per-goal quotas, vision-block delivery for images.
- Plugin SDK via `importlib.metadata` entry_points for tools, channels,
  skills, personas. Fault-isolated.
- `benchmarks/harness.py` + RESULTS.md.
- `ask_user` is now scoped to the running goal.
- Council medium-priority polish: docs, schema migration tests,
  orchestrator E2E test, channel adapter smoke tests, /chat/goal
  user-friendly labels, multi-line REPL.

## [0.1.0-alpha]

First public release. Maverick combines [Maverick Agent](https://github.com/cdayAI/research/tree/main/maverick) (recursive multi-agent swarm)
and [Agent Shield](https://github.com/cdayAI/agent-shield) (safety detection)
into a single safest-by-default agent that anyone can install in one
command.

### Added

**Agent kernel** (`maverick-core`)
- Recursive multi-agent swarm: `spawn_subagent` (blocking) and `spawn_swarm` (parallel via `asyncio.gather`)
- Persistent SQLite + FTS5 world model: goals, episodes, facts, questions, messages
- Hard budget caps: tokens, $, wall-clock, tool calls
- Auto-distilled skills: successful trajectories → reusable SKILL.md
- WorldModel schema migrations with version tracking (currently at v2)

**Multi-provider LLM dispatch**
- Anthropic (full impl: prompt caching, extended thinking, streaming)
- OpenAI (Chat Completions + tool-use, with Anthropic format translation)
- OpenRouter (200+ models via single API)
- Ollama (local models, OpenAI-compatible at `localhost:11434`)
- Gemini (Google, via OpenAI-compatible endpoint)
- Per-role model assignment via `[models]` section of config

**Safety** (`maverick-shield`)
- Agent Shield SDK integration when installed (full ~115 patterns)
- Built-in fallback rules (~20 high-impact patterns) when SDK absent
- Three chokepoints: input scan, tool-call scan, output scan
- Profiles: strict / balanced / permissive / off
- Shield is actually wired into the agent loop (not just documented)

**Channels** (`maverick-channels`)
- Telegram (Bot API), Discord (Gateway), Slack (Socket Mode)
- Signal (via signal-cli subprocess)
- Email (IMAP poll + SMTP send, stdlib only)
- Matrix (federated, via matrix-nio)
- WhatsApp + SMS (Twilio, with signature verification)
- iMessage (macOS, parameterized AppleScript, no shell injection)

**Sandboxes**
- Local subprocess
- Docker (throwaway containers, `--network=none` by default)
- SSH (remote host via system ssh binary)

**Installer**
- `maverick init`: interactive CLI wizard with preflight + API-key validation
- Tauri-based native GUI installer scaffold (`apps/installer-desktop/`)
- Per-role model picker, channel picker, safety profile picker
- Channels prompted for required env vars only

**Web dashboard** (`maverick-dashboard`)
- Local FastAPI app at `127.0.0.1:8765` showing goals / skills / facts / spend
- Dark monospace theme, no JS framework, htmx for the live bits

**MCP server** (`maverick-mcp-server`)
- Maverick exposed as a Model Context Protocol server over stdio
- Drives the swarm from Claude Code / Cursor / Claude Desktop / any MCP client
- 8 tools: start / status / resume / answer / skill_install / skills_list / fact_set / facts_get

**CLI commands**
- `maverick init` / `start` / `serve` / `doctor` / `config` / `dashboard` / `mcp`
- `maverick logs` / `status` / `answer` / `resume` / `fact` / `facts`
- `maverick skill install/remove/info` / `skills`

**Distribution**
- Release workflow: GHCR Docker image push on tag
- PyInstaller single-file binaries for Linux x86_64 / macOS arm64 / Windows x86_64
- PyPI publish for all 6 packages (gated on `PYPI_API_TOKEN` secret)
- VPS bootstrap: `install.sh` + systemd unit + Caddyfile

**Tests**
- Smoke tests across all packages (imports, config, budget, blackboard, etc.)
- OpenAI format translator: 17 unit tests covering round-trip fidelity
- Skills: install / remove / parse / safe_name / relevance scoring
- Built-in shield rules: each rule category + profile interactions
- Agent loop tests using FakeLLM fixture: FINAL parsing, ask_user blocking,
  Shield blocking, budget exhaustion, max_steps cap
- MCP server: tool catalog + protocol shapes
- Dashboard: every page renders with empty data

**Documentation**
- `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `CHANGELOG.md`
- `docs/getting-started.md`, `docs/configuration.md`, `docs/deployment.md`, `docs/safety.md`
- 5 example skills (`benchmarks/example-skills/`)
- 3 long-horizon benchmark tasks with budget/criteria

### Known limitations (v0.1.0-alpha)

- No PyPI publication yet (CI workflow is ready; just needs `PYPI_API_TOKEN`)
- No notarized DMG / signed MSIX (Tauri scaffold exists; signing comes next)
- Skill retrieval is lexical (embeddings-based retrieval is v0.2)
- WhatsApp and SMS scaffolds require a public HTTPS endpoint to receive Twilio webhooks
- Agent Shield SDK not yet on PyPI (built-in fallback rules cover ~20 high-impact patterns until then)
