# Changelog

All notable changes to the firm's platform. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed
- **GRC self-certification cluster** — security_ops, the evidence gateway,
  the Model Risk officer, and the soc2/ropa/dpia/ai-act scaffolds. Conflicts
  checks, privilege logs and billing substantiation stay in the audit record.
- **Postgres backend** — the world model, vector store, learning stores,
  governed records and audit retention are SQLite-only; a deployment that
  requires a shared backend now fails closed instead of silently going local.
- **Visual flow designer** — the drag-and-drop authoring UI and its draft/chat
  endpoints; the flow engine, run viewer and analytics remain.
- **External-agent cluster** — agent trust, external identity/gateway,
  fleet memory, memory plane, agent EDR and the framework adapters; gRPC and
  MCP callers authenticate with the shared bearer, fail-closed.
- **Confirmed orphans** — attestation/proof bundles, trajectory donation,
  insight exchange, predictive approvals, perf SLA, the Apple Watch glance
  and offline mobile bundle, plugin CA/telemetry/reliability, the TypeScript
  plugin host, residency pinning, speculative exec, tiered storage, DuckDB
  analytics, and the hardware/media tools (ROS, serial, embedded, sensors,
  audio understanding, iOS sim, Obsidian, Replicate, Vertex, image edit).
- **CLI reduced to the operational surface** — 110 top-level commands down to
  17: launchers (dashboard/mcp/worker), doctor/migrate/config-lint, the audit
  and privacy record, halt/unhalt, knowledge, domains-lint, dream and tax.
  Day-to-day work happens in the dashboard.
- **GitHub composite action and agent-on-PR workflow**, the product proposals
  and RFC folder, and the PyInstaller frozen-binary build.
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

## Earlier history

The platform began as a hard fork of an internal general-purpose agent
platform; the pre-fork release history (0.1.0-alpha through 0.1.6) described
that product, not this one, and has been removed. The fork point is recorded
in the repository history.
