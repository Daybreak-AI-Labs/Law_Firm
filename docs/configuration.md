# Configuration

Lightwork reads `~/.maverick/config.toml`. The installer wizard writes it; you can also edit by hand.

## Full schema

```toml
[deployment]
type = "desktop"       # desktop | docker | vps | phone
# Break-glass only. Exactly one control plane may write a data root: flows,
# A2A claims, and the audit, budget-receipt, fleet-memory and learning ledgers
# are hash-chained and assume a single author. Two writers do not tear a
# record -- they interleave valid ones, so the result verifies clean and is
# unreconstructable. Startup takes an exclusive lock on the data root and a
# second process refuses to serve. Set this only if you have guaranteed
# exclusivity another way; /readyz then reports `replica_safety: unenforced`.
# allow_multiple_control_planes = false   # env: MAVERICK_ALLOW_MULTIPLE_CONTROL_PLANES

[governance]
# The business-shaped governance level picked at onboarding. Every level gets
# the self-learning/self-improvement lifecycle; the level sets the governance
# posture around it:
#   essentials  small business (a gym, a studio) — learning, no ceremony
#   standard    growing company — + signed audit trail, enforced daily quotas
#   regulated   bank/clinic/government — + human sign-off (Ed25519) for
#               code/weights promotions, WORM audit export, long retention
# A label for humans + dashboards: the wizard expands it into the concrete
# knobs ([audit], [quotas], [self_improvement], [retention], ...) below, so
# hand-edits keep working. Re-run `maverick init` to switch levels (the wizard
# defaults to your previously recorded level). The advanced wizard writes
# "custom": the operator hand-picked every knob, no preset applies.
profile = "essentials"   # essentials | standard | regulated | custom

[providers.anthropic]
api_key = "${ANTHROPIC_API_KEY}"   # env var interpolation

[providers.openai]
api_key = "${OPENAI_API_KEY}"

[providers.openrouter]
api_key = "${OPENROUTER_API_KEY}"

[providers.ollama]
base_url = "http://localhost:11434"

[models]
# Per-role model picks. Format: "provider:model-id".
# Any role omitted falls back to maverick.llm.ROLE_MODELS defaults.
orchestrator    = "anthropic:claude-opus-4-8"
researcher      = "anthropic:claude-sonnet-4-6"
coder           = "anthropic:claude-sonnet-4-6"
writer          = "anthropic:claude-sonnet-4-6"
analyst         = "anthropic:claude-sonnet-4-6"
revisor         = "anthropic:claude-opus-4-8"
verifier        = "anthropic:claude-sonnet-4-6"
summarizer      = "anthropic:claude-haiku-4-5"
skill_distiller = "anthropic:claude-sonnet-4-6"

[budget]
max_dollars         = 5.0
max_wall_seconds    = 3600
max_tool_calls      = 500
max_input_tokens    = 1000000
max_output_tokens   = 200000
strict_pricing      = true

[safety]
profile         = "balanced"   # strict | balanced | permissive | off
block_threshold = "high"       # low | medium | high | critical
scan_input      = true
scan_tool_calls = true
scan_output     = true

[sandbox]
backend = "local"                   # local | docker | ssh | podman | devcontainer | firecracker | kubernetes
workdir = "~/maverick-workspace"
timeout = 60

[features]
# Toggle agent-facing behaviors that are otherwise always on. All default true.
skills      = true   # inject distilled/installed skills into agent prompts
                     #   (the MAVERICK_USE_SKILLS env var overrides this when set)
world_model = true   # inject persisted facts (cross-run memory) into runs;
                     #   false = run without prior stored facts. The goal/event/
                     #   checkpoint store (world.db) still works regardless.
streaming   = true   # stream live progress to the terminal during `maverick start`
                     #   (MAVERICK_NO_PROGRESS or non-TTY output still suppress it)
pack_editing = true  # allow editing/overriding agents (domain packs) from the
                     #   dashboard editor at /agents; false = the editor is
                     #   read-only and the mutating /api/v1/agents endpoints 403,
                     #   locking the roster (host-side override TOML still works).
role_editing = true  # allow editing the core roles (orchestrator, coder, ...)
                     #   from the dashboard editor at /roles -- a per-tenant
                     #   system-prompt addendum + model/effort override per role
                     #   (winning over [models]/[effort]); false = read-only and
                     #   /api/v1/roles mutations 403.
scheduling  = true   # allow arming recurring schedules (cron) from the dashboard;
                     #   false = the scheduler editor + /api/v1 schedule routes 403.
triggers    = true   # allow binding a saved template to an inbound webhook;
                     #   false = the /api/v1/triggers editor + /webhook/run 404/403.

[durable]
# Crash-resume: checkpoint a goal's loop state each step so `maverick resume`
# continues from where a crash left off instead of starting over. Off by
# default (a small write per step). keep_last bounds retained checkpoints.
enabled   = false
keep_last = 5

[analytics]
# Consent-gated, OFF by default. When true, the MCP server tallies a coarse
# language bucket from each client's User-Agent (typescript/go/rust/c#/java/
# python) into a local counts file — no request content, no identifiers,
# nothing uploaded. Feeds the language-bindings decision gate.
# The wizard asks for consent in its Analytics step (`maverick init`).
mcp_client_language = false

[channels.telegram]
enabled   = false
bot_token = "${TELEGRAM_BOT_TOKEN}"

[dashboard]
# Optional bearer token. Required for VPS deploys reachable from the open
# internet; harmless to leave unset on a desktop install (localhost-only).
token = "${MAVERICK_DASHBOARD_TOKEN}"

# Department (suite) access for authenticated users with NO explicit grant on
# the Users page (job-function scoping: a finance analyst sees and runs only
# Finance specialists). Unset = unrestricted — scoping is opt-in per user.
# Set a list for deny-by-default: new sign-ins may then use only these
# departments until an admin grants more. Admins are never scoped.
# default_suites = ["finance"]

# SCIM-group mapping: when your IdP (Okta/Entra) pushes Groups via SCIM,
# these tables turn team membership into access — no per-user assignment.
# An explicit Users-page assignment always beats the group-derived value.
# [dashboard.group_roles]
# "Finance Team" = "operator"
# [dashboard.group_suites]
# "Finance Team" = ["finance", "tax"]

[flows]
# The visual flow-automation engine (a deterministic graph of agent/action/
# branch/switch/foreach/while/parallel/approval/delay/wait_event/scope/subflow/
# setvar nodes) + the dashboard designer and triggers. OFF by default.
enable      = false      # or MAVERICK_FLOWS=1
# Autonomous self-improvement of live flows. Both OFF by default and also
# toggleable from the dashboard Learning page.
auto_evolve = false      # revert a node rewrite the loop measures as a regression
                         # (the safety net; or MAVERICK_FLOWS_AUTO=1)
auto_apply  = false      # apply a proven improvement forward without a human
                         # (needs auto_evolve on for the revert safety net)

[governed_records]
# Shared CAS record authority used by assurance products. "auto" keeps
# single-replica installs local and selects configured Postgres when required.
# Postgres requires application encryption plus MAVERICK_ENCRYPTION_KEY and a
# matching MAVERICK_ENCRYPTION_KEY_DIGEST="sha256:..." on every replica.
backend = "auto"          # auto | local | postgres

[evidence_graph]
# Review-gated evidence metadata and cryptographic bindings. Required by the
# Model Risk & AI Assurance Officer.
enable = false

[model_risk_assurance]
# Governed AI inventory, evidence, findings, incidents, decisions, deployment
# lineage, and signed assurance packs. Both switches must be true for the
# fail-closed DGM promotion gate.
enable = false
gate_promotions = false

[evidence_gateway]
# AI delivery disclosures, hash-only interaction receipts, cited regulatory
# impacts, and signed assurance packets. Enabling through the installer also
# enables the companion evidence graph and Model Risk Officer controls. Runtime
# use also requires an explicit authenticated tenant/client binding.
enable = false

[model_improvement]
# Specialist-model tasksets, qualification, training receipts, and optional
# external-backend exports. Inert until enabled. Hosted execution is a second
# opt-in. Cross-tenant training remains structurally refused in v1 even though
# the reserved key is explicit.
enable = false
allow_hosted = false
allow_cross_tenant = false
require_signed_receipt = true
minimum_train_families = 20
minimum_holdout_families = 20
# Signed receipts are mandatory whenever enable = true. With an active tenant,
# global enable/allow_hosted are ceilings and the tenant must opt in separately.
# Family floors combine by maximum. Mutation boundaries reject malformed or
# unknown keys instead of silently falling back.

[finance]
# Regime policies combine strictest-wins. Existing `pci` configurations now
# identify PCI DSS 4.0.1; the key remains backward compatible.
regimes = ["sox", "gaap", "dora", "basel_iii", "ifrs_17", "pci"]

[finance_operations]
# Deterministic regulatory, anomaly, AML/KYC/sanctions, and GRC control-testing
# operations. Off unless explicitly enabled here or through `maverick init`.
enable = false
federal_register_enable = true
# Safe opt-in built-in. The official RSS is issue-level, so every current Texas
# Register issue is routed to the finance review queue with source provenance.
texas_register_enable = false
regulatory_poll_seconds = 3600       # 0 disables polling; otherwise 300..604800
regulatory_domains = ["finance", "money_transmitter", "insurance_producer"]
anomaly_enable = true
sanctions_max_age_hours = 72
control_test_interval_seconds = 86400 # 0 disables; otherwise 300..2678400
evidence_due_days = 7
control_owner = "Finance Control Owner"
# Add up to 50 explicit HTTPS state feeds. Supported formats: json, rss, atom.
# Keep the safe empty default until you have verified the issuing state's URL.
state_feeds = []
# Shape of a domain-specific entry (replace every placeholder with the official source).
# `default_domains` declares that EVERY item in that feed belongs to the domain;
# omit it for a general-purpose state register and rely on item text or field_map.
# state_feeds = [{ key = "state-money-transmitter", name = "Official state money-transmitter notices", jurisdiction = "US-XX", url = "https://<official-state-host>/<domain-specific-feed>", format = "rss", default_domains = ["money_transmitter"] }]

[persona]
# Appended to every agent's system prompt. Optional.
name      = "Lightwork"
style     = "concise"   # concise | thorough | friendly | formal | playful
addendum  = ""           # free-form extra instruction

[mcp_servers.filesystem]
# External MCP servers Lightwork consumes as tools. Each one is spawned as
# a subprocess; their tools appear in the agent's catalog as
# `mcp_<name>__<tool>` and still pass through Shield.
command       = "npx"
args          = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
inherit_env   = false    # default; opt-in to pass the full process env

[mcp_servers.github]
command = "npx"
args    = ["-y", "@modelcontextprotocol/server-github"]
# Pass-through env values must be listed explicitly; secrets live in
# ~/.maverick/.env so they aren't committed by accident.
env     = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
```

`strict_pricing = true` is the default: a missing or unverified model rate
stops the call before dispatch rather than turning an estimate into billable
accounting. Set it to `false` only for a legacy or custom OpenAI-compatible
gateway whose exact rate is unavailable. That explicit opt-out emits warnings
and records the unverified source, date, confidence, pricing basis, and
applicability in `Budget.pricing_evidence`; those totals are not suitable for
invoices or chargebacks. `MAVERICK_BILLING_STRICT=true|false` overrides the
file setting.

`backend = "local"` runs tools in the same runtime environment as
Lightwork. For untrusted skills, avoid mounting secret-bearing paths into
that runtime and prefer sandbox isolation that does not expose host
state.

## Data residency & zero-data-retention (cloud providers)

When a role is routed to a cloud provider, two `[providers.<name>]` knobs
control where the request goes and what data-handling it asserts:

- **`base_url`** — pin the endpoint. Point it at a regional/EU endpoint or at a
  compliance gateway/proxy you operate, so prompts never leave the chosen
  region. Honored by `anthropic`, `openai`, and the self-hosted/OpenAI-compatible
  clients.
- **`default_headers`** — a `key = value` table of HTTP headers attached to every
  request to that provider, so a gateway can enforce **region pinning** or
  **zero-data-retention** at the edge. Threaded into the two primary cloud
  clients (`anthropic`, `openai`) today. Empty by default.

```toml
[providers.anthropic]
api_key = "${ANTHROPIC_API_KEY}"
base_url = "https://anthropic-eu.gateway.internal"   # region-pinned gateway
[providers.anthropic.default_headers]
anthropic-region = "eu"
x-no-retention = "1"
```

For application-layer default-deny model egress, use the enterprise egress lock
(`[enterprise] mode = true`), which pins every governed role to a self-hosted
provider — see `docs/security-hardening.md`. A hard no-egress boundary also
requires sandbox network isolation and host/OS/VPC egress policy. Outbound PII
can also be stripped before any cloud call with
`[privacy] redact_egress = true`.

## Learning & workforce sections

Governed, in-process learning defaults **on**. Operators can opt out globally
from the dashboard Learning page or disable individual sections below. This
default does not grant higher-authority actuation: generated executable tools,
third-party MCP acquisition, live workflow auto-apply, model-weight adoption,
and DGM code self-modification retain separate controls. If an active config
source is invalid, the learning defaults fail closed instead of enabling.

```toml
[telemetry]                # trajectory donation -> the training corpus (default off)
donate_trajectories = true # write scrubbed run records to ~/.maverick/outbox/
donate_text = false        # ALSO keep raw draft/candidate text -- DPO needs it,
                           #   but it's an egress decision (off => metadata only)
donate_min_entropy = 0.5   # swarm-disagreement floor; a single-agent run has
                           #   entropy 0, so set 0 to capture EVERY successful run
donate_min_confidence = 0.75  # verifier-confidence floor to donate a run
# Feeds `maverick.training.ingest` / `export_texts` -> PRM/DPO. Full recipe
# (incl. best-of-N pair mining) in docs/self-learning-runbook.md.

[dreaming]                 # offline experience consolidation (default on)
enable = true
# min_cluster / insight_ttl_days / retire_skills / rehearse / prune_facts /
# snapshots / promote_shared -- see FEATURES.md "Dreaming".
user_notes = false         # separate privacy opt-in: verbatim cross-chat preferences
trusted_insight_pubkeys = []   # peers for `maverick insights-import`

[data_engine]              # Cognitive Data Engine flywheel (default on)
enable = true              # causal failure triage -> guardrails -> habits
# Reads the trajectory store; mutates nothing until enabled. `maverick flywheel`.

[operations_scientist]     # discover + prove a better process (default on)
enable = true              # propose a swap, validate it in the world-model first

[consequence]              # reality is the reward (default on)
enable = true              # a recorded outcome overrides the verifier proxy
# Feed outcomes via `maverick record-outcome` or POST /api/v1/outcomes.

[emergent_protocol]        # auditable coordination shorthand (default off)
enable = true              # learn short codes for repeated boilerplate; every
                           # code decodes EXACTLY back to English. `maverick codebook`.

[emergent_codec]           # token-aware codec, live measurement (default off)
enable = true              # measure (never apply) the codec's token savings on
                           # the real coordination stream; GET /api/v1/codec.

[reflexion]                # cross-run failure lessons (default on)
enable = true

[self_harness]             # conservative harness learning (default on)
enable = true              # mine failures -> propose -> regression-validate ->
                           #   gate. Promotion ALSO needs [self_improvement]
                           #   enable. Operator commands: `maverick self-harness
                           #   show` (what was learned), `preview` (dry-run of
                           #   what it would propose), `log` (audit trail), and
                           #   `forget` (roll a learned line back).
# A clean install uses the conservative unattended profile below. Existing
# partial [self_harness] tables retain their explicitly configured lifecycle
# settings during upgrade. Details: docs/proposals/self-harness.md.
risk_limited = true              # require >=8 held-out
#                                cases, 2% effect floor, two-arm 95% confidence
#                                floor, sealed best-of-3, 3 judge votes, recent
#                                calibration, metamorphic checks, one promotion,
#                                operational caps, and canary staging. Requires
#                                an explicitly provisioned holdout ledger and the
#                                exact deployed system prompt. Constituent knobs
#                                may only tighten; fixed search/query
#                                multiplicities cannot be raised. Does NOT turn
#                                either `enable` flag on.
#   holdout_ledger = "/protected/self-harness-holdout.db"
#                                durable cross-cycle query/alpha accounting;
#                                provision with `maverick self-harness holdout
#                                provision --path <path>` before risk-limited use
#   eval_corpus = "/path.json"   {model|domain: [{goal, expected}]} — enables
#                                the auto-built live A/B on scheduled runs
#   eval_budget_dollars = 5.0    spend cap per auto-evaluated cycle (fail-closed)
auto_run = true                  # run the fleet cycle as part of `maverick dream`
#   promote_as_canary = true     stage every promotion on probation
#   relapse_failure_share = 0.5  re-probate a line whose recent outcomes turn bad
#   metamorphic = true           reject lines overfit to exact goal wording
#   judge_samples = 3            self-consistency majority vote for the judge
#   calibrate_judge = true       feed judge verdicts into the drift freeze
#   transfer_auto = true         nightly cross-model transfer of proven lines
#   corpus_harvest = "propose"   bootstrap eval cases from hindsight pairs
#                                ("propose" = human-gated review; "auto" = direct)
#   mine_bucket_by = ["domain"]  scope mining/recall per domain / role / tool
#   store = "world"              learning stores AND the eval-corpus family in the
#                                shared world database so a multi-host fleet learns
#                                as one (default "files"; import a host's files with
#                                `maverick self-harness migrate-store`; hand-edit via
#                                `corpus export` / `corpus import`)

[self_learning]            # governed local learning (default on)
enable = true
create_tools = false       # generated executable code remains a separate opt-in
allow_mcp_acquisition = false # starting third-party MCP remains a separate opt-in
allow_provider_egress = false # extra learning-only model calls may cross providers
preflight = true           # default uses a local redacted catalog match; the
                           # LLM needs-analysis call requires provider egress above
distill_local = true
provision_packs = true     # equip a freshly-approved pack with the skills + tools
                           #   its workflow needs at creation time (capability
                           #   provisioning at pack-birth). Default on once
                           #   self-learning is enabled; the wizard sets it.
                           #   Read-only analysis is always safe; applying it is
                           #   gated on the same human approval `save_profile`
                           #   requires and never widens the clamped envelope.
                           #   Wired into `maverick onboard`. See FEATURES.md.

[ekko]                     # client-controlled work discovery (default OFF)
enable = false             # independent of self-learning and DGM
retention_days = 14        # raw local events; runtime clamps to 2..30
enrollment_days = 30       # device consent expiry; runtime clamps 1..90
min_occurrences = 3        # repeated sequences required; runtime clamps 2..100
min_distinct_days = 2      # avoid one-off bursts; 2..retention_days
poll_interval_seconds = 5  # injected collector cadence; runtime clamps 1..300
capture_level = "application_metadata" # or "guided" action labels
allowed_apps = []          # positive allowlist; empty means observe nothing
blocked_apps = ["email", "outlook", "gmail", "chat", "teams", "slack",
                "crm", "salesforce", "erp", "sap", "database"]
provider_egress = false    # reserved/unsupported; true fails closed
# Enabling policy does not enroll a device, start a service, request OS
# monitoring permissions, save a generated flow, or activate automation.
# See ekko-work-discovery.md and `maverick ekko --help`.

[self_improvement]         # promotion ladder for learned guidance (default on)
enable = true
factory_learning = true    # close the loop onto generation quality: attribute
                           #   provisioning/approval gaps to a pack's suite/signal,
                           #   mine them into proposer corrections, promote on the
                           #   `prompt` rung, and fold into future pack generation.
                           #   Default on once self-improvement is enabled; the
                           #   wizard sets it. Force-enable via MAVERICK_FACTORY_LEARNING.
                           #   `maverick factory-learn [--dry-run]`. See FEATURES.md.
evaluator_evolution = true  # promote a BETTER judge instead of only freezing when
                           #   the evaluator drifts: a challenger evaluator replaces
                           #   the incumbent only when its agreement with a fixed,
                           #   checksum-locked ground-truth ANCHOR (by the eps-best-
                           #   belief lower bound) beats it, on a dedicated `evaluator`
                           #   rung. The anchor is immutable (governed by `python -m
                           #   maverick.evaluator_evolution --ci`) so a weak judge
                           #   can't launder drift. An evaluator swap still needs
                           #   human approval until max_auto_rung = "evaluator".
                           #   Override via MAVERICK_EVALUATOR_EVOLUTION. See FEATURES.md.
evaluator_eps = 0.05       # confidence level of the eps-best-belief lower bound used
                           #   for evaluator promotion (lower = more conservative).

[self_modify]              # research-only code DGM (default off)
enable = false             # dashboard/API arming changes only this gate
editable_paths = []        # readiness needs a narrow reviewed allowlist
eval_tests = []            # readiness needs at least two discriminating tests
# Arming does not start a cycle and never authorizes code adoption or deployment.
# Readiness additionally requires [self_improvement] enable, a clean learning
# HALT state, a Git snapshot/budget preflight, and an external sandbox SDK
# backend (`[sandbox] backend = "ep:<name>"`) that enforces no egress/non-root,
# bounded output, and the controller-owned `maverick.test-evidence.v1` protocol.
# Bundled sandboxes are deliberately not DGM-attested; stdout/JUnit files do not
# satisfy this boundary. See `docs/security/dgm-evaluator-evidence.md`.

[domains]                  # specialist-pack behavior (defaults shown)
discipline = true          # suite operating-discipline appended at spawn
memory = true              # department lessons injected at spawn

[workforce]                # treat each agent like a hire (defaults shown)
levels = false             # per-agent autonomy levels (observe/suggest/request/
                           #   auto). OFF -> every agent stages actions for human
                           #   execution. Per-agent overrides: [workforce.agents].
                           #   Env: MAVERICK_WORKFORCE_LEVELS.
data_grounding = true      # auto-grant each analyst pack its suite's primary-
                           #   source data connectors (SEC EDGAR, FRED, openFDA,
                           #   USAspending, weather, ...). GET-only, low-risk,
                           #   deferred (no context cost), inert without each
                           #   source's API key. Set false to withhold them.
                           #   Env: MAVERICK_WORKFORCE_DATA_GROUNDING.

[fleet_memory]             # external agents read/write governed memory
enable = false             # explicit trust decision; roster-gated

[suites]                   # disable whole suites (all on by default)
# healthcare = false
```

## Per-role model choice

This is the *fully control every aspect* knob. Heavy roles benefit from a smart model; cheap roles can use a small one. Mix providers freely — the orchestrator can be a cloud Opus while the summarizer is a local Llama.

Roles available:

| Role | Used for |
|---|---|
| `orchestrator` | Plans, decomposes, verifies. Wants the smartest model. |
| `researcher`   | Searches, gathers info. Workhorse. |
| `coder`        | Writes and tests code. |
| `writer`       | Drafts long prose. |
| `analyst`      | Synthesizes findings. |
| `revisor`      | Second-pass review when verify fails. |
| `verifier`     | Independent final-answer check. |
| `summarizer`   | Cheap distillation. |
| `skill_distiller` | Turns trajectories into reusable skills. |

## Env vars vs config

- **Secrets** (API keys, bot tokens) live in `~/.maverick/.env` (chmod 600) and are referenced via `${VAR}` interpolation.
- **Everything else** lives in `config.toml` and is safe to commit (e.g. to a personal dotfiles repo).

The installer keeps these separated automatically.

> **Config typos are caught, not silently ignored.** `maverick config-lint`
> (also surfaced as advisory `config-lint` rows by `maverick doctor`, and emitted
> as a one-line warning at process startup) walks the loaded config against a
> known-section/key schema
> and flags a mistyped section or an unknown key in a fixed-key section (with
> `difflib` "did you mean" suggestions), plus a few obvious type errors. This
> catches the classic footgun where a typo like `[budget] max_dollarss` would
> otherwise be silently ignored and the run go **uncapped**. The check is
> advisory — the kernel still fails soft on a bad config — so after editing
> `config.toml` by hand, run `maverick config-lint` (or `maverick doctor`) and
> confirm the security/cost-critical values (`[budget]`, `[enterprise]`,
> `[encryption]`, `[audit]`, `[safety]`) read back as you intend.

## Overriding the config path

```bash
MAVERICK_CONFIG=/etc/maverick/config.toml maverick start "..."
```

Useful for VPS deployments where you want the config under `/etc/`.

## Dashboard authentication

For desktop installs the dashboard binds to `127.0.0.1:8765` and bearer
auth is optional. For VPS deploys (reachable from the open internet)
set `MAVERICK_DASHBOARD_TOKEN` — every request to `/api/v1/*` and every
HTML page is then gated. The probe/discovery paths `/healthz`, `/livez`,
`/readyz`, `/openapi.json`, `/docs`, `/redoc`, and the agent-card
well-knowns are exempt (so monitoring + API discovery still works). The
inbound webhook routes (`/webhook/start`, `/webhook/run`, issue webhooks)
and the `/share/`, `/scim/`, `/saml/` prefixes authenticate by their own
mechanism (HMAC signature / share token / SSO) rather than the bearer.

Two ways to authenticate:

- **Header**: `Authorization: Bearer <token>` — for API clients.
- **Query string**: `?token=<token>` — so phone browsers can bookmark a
  page once and not retype the token.

Token comparison is constant-time (`hmac.compare_digest`).

## External MCP servers

Lightwork can consume any MCP server (filesystem, GitHub, Postgres,
browser, etc.) as tools. Add entries under `[mcp_servers.<name>]`:

```toml
# stdio: spawn a local subprocess
[mcp_servers.filesystem]
command = "npx"
args    = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

# remote: connect to a server over Streamable HTTP (set `url` instead of `command`)
[mcp_servers.remote]
url        = "https://mcp.example.com/mcp"
auth_token = "..."                      # optional; sent as `Authorization: Bearer …`
# headers  = { X-Org = "acme" }         # optional extra request headers
```

Behavior:

- A `command` server is spawned as a stdio subprocess on swarm start and
  torn down on goal completion. A `url` server is reached over HTTP
  (Streamable HTTP, spec 2025-11-25) — `tools/list` + `tools/call` over
  JSON or SSE, with session-id continuity; no subprocess. OAuth 2.1 isn't
  wired yet, but a static bearer (`auth_token`) is.
- Every tool it exposes is registered as `mcp_<name>__<tool>` in the
  agent's catalog and passes through `Shield.scan_tool_call` like any
  other tool.
- By default *no* environment is inherited from the parent process —
  only `PATH`, `HOME`, `USER`, `LANG`, `TZ`, `TMPDIR` (see
  `mcp_client.DEFAULT_ENV_ALLOWLIST`). Pass secrets explicitly via the
  `env = { ... }` table or set `inherit_env = true` to pass the full
  environment (only do this for fully-trusted servers).
- A background reader drains stderr to prevent pipe-buffer deadlocks
  when a server logs verbosely.

## Concurrency cap

The dashboard, REST API, and MCP server all share a process-wide
semaphore that bounds the number of swarms running in background
threads simultaneously. Override with:

```bash
MAVERICK_MAX_CONCURRENT_GOALS=4 maverick dashboard
```

Default is 16. Raise on a beefy machine; lower on a Raspberry Pi. (A
separate per-principal cap also limits concurrent goals from any one caller.)

## Environment variables

Most settings live in this file, but many can be overridden (or only set) via
`MAVERICK_*` environment variables — see **[Environment variables](env-vars.md)**
for the full reference.
