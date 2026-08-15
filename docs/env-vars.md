# Environment variables

Lightwork's primary configuration is `~/.maverick/config.toml` (see
[configuration.md](configuration.md)). The `MAVERICK_*` environment variables
below are a complement: they override the equivalent config keys when set, and
expose a handful of knobs that have no config equivalent. **Env vars win over
config.** Most users never need to set any of these — the defaults are the
out-of-the-box behavior. They're documented here for operators tuning a
deployment.

Boolean vars accept `1`/`true`/`yes`/`on` for true and `0`/`false`/`no`/`off`
for false unless noted otherwise.

## Core / run

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_CONFIG` | `~/.maverick/config.toml` | Path to an alternate config file. |
| `MAVERICK_CODING_MODE` | unset | When `1`/`true`/`yes`, switches the agent into coding mode (set by `maverick start --coding-mode`); affects prompts, fs/shell tool defaults, and cache TTL. |
| `MAVERICK_LANGUAGE` | unset | Primary project language hint (e.g. `python`, `go`). Feeds sandbox/toolchain selection and coding mode. |
| `MAVERICK_MAX_STEPS` | `25` | Global cap on agent loop steps per goal. |
| `MAVERICK_STEP_BUDGET_WARNING` | `3` | When this many tool-using turns remain before `MAVERICK_MAX_STEPS`, the loop nudges the agent to give its FINAL answer (so a long run isn't cut off mid-work). `0` disables. |
| `MAVERICK_MAX_SWARM_FANOUT` | `8` | Max child agents a single spawn call may branch into. |
| `MAVERICK_MAX_TOTAL_SPAWNS` | `64` | Process-wide cap on total spawned agents across a run. |
| `MAVERICK_MAX_CONCURRENT_GOALS` | `16` | Global ceiling on goals running in parallel — a host-overload backstop, sized so normal multi-user load never reaches it. |
| `MAVERICK_MAX_CONCURRENT_GOALS_PER_PRINCIPAL` | `3` | Per-user concurrency lane: one principal can run this many goals at once without blocking other users (fair scheduling). |
| `MAVERICK_GOAL_ACQUIRE_TIMEOUT` | `300` | Seconds to wait for a goal-execution slot (per-user lane, then global) before giving up. |
| `MAVERICK_PARALLEL_TOOLS` | `1` (on) | Set `0` to run tool calls within a turn serially instead of in parallel. |
| `MAVERICK_LOOP_GUARD` | `1` (on) | Set `0` to disable the repeated-failure loop guard (nudges the agent when it re-issues the same failing tool call). |
| `MAVERICK_LOOP_GUARD_THRESHOLD` | `3` | Consecutive identical tool-call failures before the loop guard nudges (min 2). |
| `MAVERICK_HALT_FILE` | `~/.maverick/HALT` | Killswitch path; the run aborts if this file exists. |
| `MAVERICK_NO_CLI` | unset | `1` marks embedded mode: skips third-party plugin auto-discovery and CLI-only paths. |
| `MAVERICK_NO_PROGRESS` | unset | Set to suppress the live progress display. |
| `MAVERICK_DEBUG` | unset | `1` re-raises original exceptions and prints full tracebacks instead of friendly errors. |
| `MAVERICK_NO_WIZARD` | unset | `1` runs the installer non-interactively (used by the unattended install scripts). |

## Budget & limits

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_BUDGET_DOLLARS` | config `[budget]` | Override the dollar budget cap for a run. |
| `MAVERICK_BILLING_STRICT` | `true` | Require verified pricing for live accounting. `false` explicitly enables warning-labelled estimate-only accounting for legacy/custom gateways. |
| `MAVERICK_DEFAULT_MAX_DOLLARS` | `2.0` | Default per-goal dollar ceiling when none is supplied. |
| `MAVERICK_DEFAULT_MAX_WALL_SECONDS` | `1800` | Default per-goal wall-clock ceiling (seconds). |
| `MAVERICK_DEFAULT_MAX_DEPTH` | `3` | Default max recursion depth for spawned sub-goals. |

## Models & routing

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_MODEL_OVERRIDE` | unset | Global run-wide model override (`provider:model-id`); set by `maverick --model`. Beats config for every role. |
| `MAVERICK_MODEL_OVERRIDE_<ROLE>` | unset | Per-role override, e.g. `MAVERICK_MODEL_OVERRIDE_CODER`. Beats the global override for that role. |
| `MAVERICK_TEMPERATURE` | provider default | Sampling temperature for LLM calls. |
| `MAVERICK_VISION_MODEL` | `anthropic:claude-sonnet-4-6` | Model used by the image/video viewing tools (`provider:model-id`). |
| `MAVERICK_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local embedding model id. |
| `MAVERICK_COST_ROUTING` | config `[routing] cost_aware` (off) | Enable cost-aware model routing. |
| `MAVERICK_CASCADE_ROUTING` | config `[models] cascade` (off) | Enable cascaded routing (cheap model first, escalate on need). |
| `MAVERICK_ESCALATE_BELOW` | `0.6` | Verifier-confidence threshold below which a cascade escalates to a stronger model. |
| `MAVERICK_ESCALATE_TOOL_DEPTH` | `3` | Tool-call depth at which a cascade escalates. |
| `MAVERICK_ANTHROPIC_CACHE_TTL` | `1h` (coding mode: `5m`) | Anthropic prompt-cache TTL; an explicit value always wins. |
| `MAVERICK_CACHE_MESSAGES` | `1` (on) | Set `0` to disable prompt caching of message history. |
| `MAVERICK_LLM_CACHE` | unset (off) | Enable the on-disk LLM response cache. |
| `MAVERICK_LLM_RETRY_ATTEMPTS` | `5` | Max retry attempts for failed LLM calls. |
| `MAVERICK_LLM_RETRY_BASE_DELAY` | `1.0` | Base backoff delay (seconds) between LLM retries. |
| `MAVERICK_LLM_RETRY_MAX_DELAY` | `30.0` | Max backoff delay (seconds) between LLM retries. |
| `MAVERICK_LLM_CONNECT_TIMEOUT` | `15.0` | LLM connect timeout (seconds). |
| `MAVERICK_LLM_READ_TIMEOUT` | `120.0` | LLM read timeout (seconds). |

## Reasoning & verification

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_BEST_OF_N` | `1` | Generate N candidate solutions and pick the best. |
| `MAVERICK_BON_LADDER` | built-in ladder | Comma-separated best-of-N sample ladder for adaptive escalation. |
| `MAVERICK_BON_EARLY_EXIT` | `1` (on) | Best-of-N stops the ladder after the first candidate whose tests all pass. Set `0` to run all N attempts, so a *failing* candidate is also captured — needed to mine a DPO preference pair (pass vs fail) from one coding task. |
| `MAVERICK_TREE_OF_THOUGHT` | unset (off) | Enable tree-of-thought planning: fork candidate plans, critic selects. |
| `MAVERICK_TOT_CANDIDATES` | built-in | Number of candidate plans tree-of-thought forks. |
| `MAVERICK_REFLEXION` | config `[reflexion] enable` (on) | Override the default-on reflexion self-critique loop; set `0` to opt out. |
| `MAVERICK_DREAMING` | config `[dreaming] enable` (on) | Override default-on offline experience consolidation (`maverick dream`); set `0` to opt out. |
| `MAVERICK_SELF_HARNESS` | config `[self_harness] enable` (on) | Override the default-on conservative self-harness loop (`maverick self-harness`; promotion also needs `[self_improvement] enable`). |
| `MAVERICK_FACTORY_LEARNING` | config `[self_improvement] factory_learning` (on when the master is on) | Override the factory loop directly: `0` disables it and `1` force-enables it even if the self-improvement master is off. |
| `MAVERICK_DATA_ENGINE` | config `[data_engine] enable` (on) | Override the default-on Cognitive Data Engine flywheel: causal failure triage → guardrails → habits (`maverick flywheel`). |
| `MAVERICK_OPERATIONS_SCIENTIST` | config `[operations_scientist] enable` (on) | Override the default-on Operations Scientist: propose + simulate a better process before a real experiment. |
| `MAVERICK_CONSEQUENCE` | config `[consequence] enable` (on) | Override default-on grounding in real downstream outcomes (`maverick record-outcome`). |
| `MAVERICK_SELF_MODIFY` | config `[self_modify] enable` (off) | Arm/disarm the research-only DGM gate. Setting this env var makes the deployment environment authoritative, so the dashboard control is read-only. Arming never starts a cycle or permits live code adoption. |
| `MAVERICK_FLOWS` | config `[flows] enable` (off) | Enable the visual flow-automation engine (deterministic graph of agent/action/branch/… nodes; the dashboard designer + triggers). |
| `MAVERICK_FLOWS_AUTO` | config `[flows] auto_evolve` (off) | Let the flow self-rewrite loop act autonomously: revert a node rewrite it measures as a regression. Pairs with `[flows] auto_apply` (apply an improvement forward) — both are also toggleable from the dashboard Learning page. |
| `MAVERICK_EMERGENT_PROTOCOL` | config `[emergent_protocol] enable` (off) | Enable the auditable coordination codec (sentinel form; `maverick codebook`). |
| `MAVERICK_EMERGENT_CODEC` | config `[emergent_codec] enable` (off) | Measure the token-aware codec on the live coordination stream (telemetry only; `maverick codec-learn`). |
| `MAVERICK_DOMAIN_DISCIPLINE` | config `[domains] discipline` (on) | Append suite operating discipline to specialist personas at spawn. |
| `MAVERICK_FLEET_MEMORY` | config `[fleet_memory] enable` (off) | Allow registered external agents to use the governed memory plane. |
| `MAVERICK_PRM` | `null` | Process reward model: `null`, `heuristic`, `remote`, `learned`, or `linear`. A linear backend reconciles durable verifier-promotion authority before serving and falls back to the heuristic for an in-doubt, unrecognized, or out-of-band artifact. |
| `MAVERICK_PRM_PATH` | unset | Learned-model directory (`learned`) or stable serving artifact (`linear`). Relative linear paths are pinned to an absolute startup path before recovery and serving. |
| `MAVERICK_PRM_BOOTSTRAP_SHA256` | unset | Explicit SHA-256 trust root for an initially provisioned `linear` artifact. Required when `MAVERICK_PRM_PATH` already exists but has no committed promotion chain; later hot reloads must match the durable ledger exactly. |
| `MAVERICK_PRM_ENDPOINT` | unset | Endpoint URL when `MAVERICK_PRM=remote`. |
| `MAVERICK_PRM_API_KEY` | unset | API key for the remote PRM endpoint. |
| `MAVERICK_VERIFY_ENSEMBLE` | config `[routing] verify_ensemble` (off) | Run the multi-model verifier panel (stronger, ~Nx cost). |
| `MAVERICK_VERIFIER_CONFIDENCE` | `0.75` | Confidence threshold at which the verifier accepts a result. |
| `MAVERICK_DISAGREEMENT_HIGH` | `0.5` | Verifier disagreement level treated as high. |
| `MAVERICK_CROSS_FAMILY_VERIFIER` | config-driven | Force the verifier to use a different model family than the generator. |
| `MAVERICK_SPECULATIVE_FINALIZE` | `1` (on) | Set `0` to disable speculative finalization in the orchestrator. |

## Memory & recall

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_MEMORY_DIR` | `~/.maverick/memory` | Root directory for the cross-session `memory` tool (the agent's model-curated long-term notes). |
| `MAVERICK_AUTO_RECALL` | unset (off) | Auto-recall related prior goals/facts into a run. |
| `MAVERICK_AUTO_RECALL_K` | `3` | Number of prior items to recall when auto-recall is on. |
| `MAVERICK_AUTO_DISTILL` | unset (off) | Auto-distill skills from completed runs. |
| `MAVERICK_SELF_LEARNING` | config `[self_learning] enable` (on) | Override governed self-learning; set `0` to opt out. Generated tools and MCP acquisition keep their separate off-by-default controls. |
| `MAVERICK_EKKO` | config `[ekko] enable` (off) | Controls only Ekko's master policy switch. It never enrolls a device, grants an application, starts a collector, or enables provider egress. Invalid values fail closed. |
| `MAVERICK_SKILL_DECAY` | `1` (on) | Set `0` to disable time-decay of skill usefulness stats. |
| `MAVERICK_ALLOW_SKILL_INSTALL` | unset (off) | Opt in to installing skills from free-text URLs. |
| `MAVERICK_VECTOR_STORE` | config `[memory] backend` | Semantic-recall backend: `chroma`, `qdrant`, `weaviate`, `pgvector`, or unset/`none` to disable. |
| `MAVERICK_CHROMA_PATH` | `~/.maverick/...` default | On-disk path for the Chroma vector store. |
| `MAVERICK_QDRANT_URL` | unset | Qdrant server URL (remote mode). |
| `MAVERICK_QDRANT_PATH` | default path | Qdrant local on-disk path (embedded mode). |
| `MAVERICK_QDRANT_API_KEY` | unset | API key for a remote Qdrant server. |
| `MAVERICK_WORLD_BACKEND` | config-driven | Set `postgres` to use the Postgres world-model backend. |
| `MAVERICK_PG_DSN` | unset | Postgres DSN for the Postgres world model (e.g. `postgres://user@host:5432/maverick`; prefer `PGSERVICE`, `~/.pgpass`, peer auth, or a secret manager over embedding passwords). |
| `MAVERICK_ORPHAN_RECLAIM_SECONDS` | code default | Seconds before orphaned world-model goal locks are reclaimed. |
| `MAVERICK_BLACKBOARD_MAX_ENTRIES` | `5000` (min 100) | Max entries retained in the shared blackboard. |

## Hosted control plane & multi-tenancy

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_STRICT_TENANT_ISOLATION` | config `[world_model] strict_tenant_isolation`; **auto-on under enterprise mode** | Postgres reads return ONLY the active tenant's rows (drop NULL-legacy tolerance). Enable after backfilling `tenant_id`. Env wins over config wins over enterprise default. |
| `MAVERICK_PG_RLS` | config `[world_model] rls`; **auto-on under enterprise mode** | DB-native Postgres Row-Level Security on the tenant tables (defense-in-depth over the app predicate). When auto-enabled by enterprise mode, a boot preflight refuses to start on legacy `tenant_id IS NULL` rows (run `maverick tenant backfill`); explicit `=1` keeps the fail-closed opt-in path. |
| `MAVERICK_KMS_KEK` | derived from the at-rest key | The per-tenant-DEK Key Encryption Key (32 bytes, hex/base64) for `tenant/kms.py`. |
| `MAVERICK_KMS_DEK_CACHE_TTL` | config `[kms] dek_cache_ttl` (`0` = process lifetime) | Seconds a tenant DEK stays cached before it must be re-unwrapped by the KMS. A positive TTL bounds how long a *revoked* cloud-KMS key keeps opening data (the next access re-hits the KMS and fails closed). Per-tenant **BYOK** is configured in each tenant's own `tenants/<id>/config.toml` `[kms]` section (provider/key_id/region), resolved deterministically by `get_kms(tenant_id)`. **Rolling the local KEK** across the fleet: `maverick tenant kms-rotate --old-kek-file /run/secrets/old-kek --new-kek-file /run/secrets/new-kek` (re-wrap only, idempotent/resumable, `--dry-run` to preview; omit file options to use hidden prompts). Avoid passing KEKs in command-line arguments; set `MAVERICK_KMS_KEK` to the new value live only after rotation reports 0 failed. Cloud/BYOK rotation uses `tenant.kms.rotate_kek_fleet` with per-tenant resolvers. |
| `MAVERICK_MCP_ANALYTICS` | config `[analytics] mcp_client_language` (off) | Opt-in, consent-gated tally of MCP-client language (feeds the language-bindings gate). |
| `IRC_ALLOWED_ACCOUNTS` | — | Comma-separated allowlist of authenticated IRC account names that may drive the agent over the IRC channel. Requires an IRC server that provides the IRCv3 `account-tag` capability. |
| `GLASSES_ALLOWED_USER_IDS` | — | Allowlist for the glasses/wearable channel. |
| `IRC_SERVER` / `IRC_PASSWORD` | config `[channels.irc]` | IRC server host / password. |

### Fleet queue

`[queue] backend = "arq"` selects authenticated out-of-process goal execution.
Run only the matching hardened worker entry point:

```bash
arq maverick.arq_worker.WorkerSettings
```

| Env var | Config equivalent | Description |
| --- | --- | --- |
| `MAVERICK_QUEUE_SIGNING_KEY` | `[queue] signing_key` | Required shared HMAC key of at least 32 UTF-8 bytes. Supply through a secret manager, not source control. |
| `MAVERICK_QUEUE_NAMESPACE` | `[queue] namespace` | Required non-secret deployment id (1-64 safe characters). Producer and workers must match; distinct fleets must not share it. |
| `MAVERICK_QUEUE_REDIS_DSN` | `[queue] redis_dsn` | Shared `redis://`, `rediss://`, or `unix://` endpoint. Non-loopback TCP requires `rediss://`; hostname and certificate validation are forced on. |
| `MAVERICK_QUEUE_REDIS_CA_CERTS` | `[queue] redis_ca_certs` | Optional private-CA bundle for `rediss://`. |
| `MAVERICK_QUEUE_REDIS_CERTFILE` / `MAVERICK_QUEUE_REDIS_KEYFILE` | `[queue] redis_certfile` / `redis_keyfile` | Optional client mTLS pair; both are required together. |
| `MAVERICK_QUEUE_WORKER_MAX_DOLLARS` | `[queue] worker_max_dollars` | Worker-local per-goal spend ceiling; signed producer requests can only narrow it. |
| `MAVERICK_QUEUE_WORKER_MAX_WALL_SECONDS` | `[queue] worker_max_wall_seconds` | Worker-local wall-time ceiling. |
| `MAVERICK_QUEUE_WORKER_MAX_DEPTH` | `[queue] worker_max_depth` | Worker-local recursion-depth ceiling (1-64). |

Use a dedicated Redis database and ACL identity per Lightwork deployment. ARQ
stores job bodies under global Redis key prefixes even when its ready queue is
namespaced, so a dedicated database/ACL is defense in depth against accidental
cross-fleet access. Network workers also require the same Postgres world-model
backend for durable, tenant-aware replay claims. The worker refuses to poll if
the HMAC key or shared claim store is unavailable. The unsafe
`MAVERICK_ALLOW_INSECURE_QUEUE_REDIS=1` escape hatch is only for isolated local
development networks.

For a codec or signing-key rollout, pause producers, drain (or deliberately
purge) the old deployment queue, deploy workers and producers together, then
resume. Old pickle records and envelopes signed by a retired key are rejected;
there is intentionally no silent compatibility fallback. Generate and rotate
the HMAC value in your secret manager, and keep it out of process arguments,
logs, and repository configuration.

Other config-only knobs: `[billing.plans]` (override plan entitlements), `[egress]` /
`[tenancy.egress.<t>]` (per-tenant egress plane). Tenants are managed with
`maverick tenant …`; invoices/entitlements with `maverick billing …`.

## LLM cost & latency

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_EFFORT_ENABLED` | config `[effort] enabled` (off) | Turn on the built-in per-role reasoning-effort profile (`output_config.effort`): orchestrator/coder/revisor stay `high`, bulk roles (researcher/verifier/writer) drop to `medium`, reflector/distiller to `low`. The biggest cost/latency lever on Opus 4.7/4.8. Model-gated (Opus 4.5+/Sonnet 4.6) so it never 400s. |
| `MAVERICK_EFFORT` | unset | Global effort for **all** roles (`low`/`medium`/`high`/`xhigh`/`max`). Wins over config. |
| `MAVERICK_EFFORT_<ROLE>` | unset | Per-role override, e.g. `MAVERICK_EFFORT_CODER=xhigh`. Highest precedence. |
| `MAVERICK_CACHE_PREWARM` | config `[cache] prewarm` (off) | Pre-warm the prompt cache at orchestrator start (`max_tokens=0` prefill) so the first turn's time-to-first-token doesn't pay the cold cache write. Best for interactive surfaces. |
| `MAVERICK_CACHE_MESSAGES` | `1` | Cache the stable message-history prefix (set `0` to disable). |
| `MAVERICK_LOG_TURNS` | unset | Print per-turn `in/out/cache_read/cache_write` token stats to stderr. The Prometheus `maverick_llm_cache_tokens_total` counter (kind=`read`/`creation`/`uncached`) is the metric form — a hit-rate panel surfaces a silent cache invalidator. |

Config equivalents live under `[effort]` (`enabled`, `default`, `<role>`) and
`[cache]` (`prewarm`).

## Compaction & context

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_COMPACT_HISTORY` | config `[context] compact` (off) | Enable history compaction. |
| `MAVERICK_COMPACT_TIKTOKEN` | `1` | Use a real local BPE tokenizer (tiktoken) for compaction token counts when installed; `0` forces the `len/4` heuristic. Fails open to the heuristic if tiktoken is absent. |
| `MAVERICK_HISTORY_WINDOW` | config `[context]` | Max number of recent turns kept verbatim. |
| `MAVERICK_HISTORY_TOKENS` | config `[context]` | Max history tokens before compaction triggers. |
| `MAVERICK_COMPACT_KEEP_RECENT` | `4` | Recent turns always kept uncompacted. |
| `MAVERICK_COMPACT_DIGEST_EVERY` | `10` | Digest older turns every N turns. |
| `MAVERICK_COMPACT_MAX_TOOL_BYTES` | `2048` | Max bytes of tool output retained before truncation during compaction (applies to results behind the recent window). |
| `MAVERICK_COMPACT_MAX_TOTAL_BYTES` | `200000` | Ceiling on the whole live message window. When a few results at the per-result cap push the window over it, compaction shrinks into the recent window oldest-first (the brief and the newest message are always spared). `0` disables. |
| `MAVERICK_MAX_TOOL_RESULT_BYTES` | `100000` | Hard cap on a single tool result (head+tail kept) before it enters the context window, so one runaway `shell`/query output can't blow tokens/budget in a turn. |
| `MAVERICK_ADAPTIVE_MAX_TOKENS_FLOOR` | `8192` | `max_tokens` headroom floor when Opus 4.7/4.8 auto-injects adaptive thinking for a role with no explicit thinking budget. Thinking bills at the output rate, so this is the worst-case output spend of every non-thinking-role turn on the default model (min 2048). Config `[thinking] budget` sets the orchestrator/revisor base (default 8000). |
| `MAVERICK_SKILL_RENDER_MAX_CHARS` | `4000` | Per-skill body cap when recalled skills are rendered into a system prompt (`0` disables). |
| `MAVERICK_SKILL_RENDER_TOTAL_CHARS` | `10000` | Combined budget across rendered skills; the relevance-ranked tail is dropped once spent (`0` disables). |
| `MAVERICK_FINDING_POST_MAX_CHARS` | `1500` | Excerpt size for a FINAL posted to the blackboard (the full answer still reaches the parent and the run record). |
| `MAVERICK_BB_RENDER_ENTRY_CHARS` | `800` | Per-entry bound when blackboard activity is rendered into a worker's brief. |
| `MAVERICK_SWARM_CHILD_RESULT_CHARS` | `4000` | Per-child answer cap in the `spawn_swarm` result returned to the parent. |
| `MAVERICK_SWARM_RESULTS_TOTAL_CHARS` | `24000` | Total budget for the joined swarm result; trailing children's answers are elided (identity line kept). |
| `MAVERICK_BRIEF_FACTS_MAX` | `50` | Newest facts included in the orchestrator brief's facts block. |
| `MAVERICK_BRIEF_FACT_VALUE_CHARS` | `300` | Per-fact value cap in the brief's facts block (truncation runs after secret redaction). |
| `MAVERICK_ANTHROPIC_MSG_CACHE_TTL` | `5m` | TTL for the message-tier cache breakpoints (re-anchored every turn, so the write surcharge is paid per turn); system/tools keep `MAVERICK_ANTHROPIC_CACHE_TTL` semantics. |
| `MAVERICK_BUDGET_RECEIPTS` | off | Mint a signed budget receipt (tokens, cache buckets, cost) per finished goal into the hash-chained ledger. |
| `MAVERICK_RETRIEVAL_ROUTER` | config `[context] retrieval_router` (off) | Enable the long-context retrieval router: when a single payload (e.g. a pasted document in the goal description) exceeds the token threshold, shard it and keep only the shards relevant to the goal, instead of overflowing the model window. |
| `MAVERICK_ROUTER_THRESHOLD_TOKENS` | config `[context]` (`200000`) | Payload size (approx tokens) above which the retrieval router activates. |
| `MAVERICK_ROUTER_TOP_K` | config `[context]` (`12`) | Number of shards the retrieval router retains. |

## Sandbox & tools

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_SUPPRESS_SANDBOX_WARNING` | unset | `1` silences the "running without an isolated sandbox" warning. |
| `MAVERICK_DEFERRED_TOOLS` | unset (on) | `0` disables deferred tool loading, putting every SaaS-connector schema back in the model's per-turn catalog (~600 tools). On by default: the model sees the core toolset + `find_tools` and activates connectors on demand. Also settable via `[capabilities] deferred_tools`. |
| `MAVERICK_CODE_EXEC` | unset (off) | `1` enables the `code_exec` tool (programmatic tool calling: a sandboxed Python script that orchestrates declared tool calls, keeping raw outputs out of context). Also settable via `[capabilities] code_exec`. |
| `MAVERICK_FIRECRACKER_STRICT` | `1` (on) | Set `0` to allow Firecracker to fall back to a Docker/hardened sandbox instead of failing. |
| `MAVERICK_LONG_CMD_TIMEOUT` | `600` | Timeout (seconds) for long-running shell commands. |
| `MAVERICK_PARALLEL_TOOLS` | `1` (on) | See Core / run. |
| `MAVERICK_BROWSER_DISABLE` | unset | `1` disables the browser tool. |
| `MAVERICK_BROWSER_STATE` | unset | Path to a per-task browser storage-state file; setting it enables persistence. |
| `MAVERICK_BROWSER_NO_PERSIST` | unset | `1` disables browser-state persistence even when a state file is set. |
| `MAVERICK_BROWSER_HEADED` | `0` (headless) | `1` runs the browser headed (visible). |
| `MAVERICK_COMPUTER_DISABLE` | unset | `1` disables the computer-use tool. |
| `MAVERICK_COMPUTER_OCR` | unset (off) | Enable OCR of computer-use screenshots. |
| `MAVERICK_CLIPBOARD_DISABLE` | unset | `1` disables the clipboard tool. |
| `MAVERICK_EMAIL_DISABLE` | unset | `1` blocks the email tool from sending. |
| `MAVERICK_WHISPER_MODEL` | `small` | Whisper model size for the voice tool. |
| `MAVERICK_WHISPER_OPENAI_MODEL` | `whisper-1` | OpenAI speech-to-text model used by the voice tool. |
| `MAVERICK_WHISPER_GROQ_MODEL` | `whisper-large-v3-turbo` | Groq speech-to-text model used by the voice tool. |
| `MAVERICK_TTS_OPENAI_MODEL` | `tts-1` | OpenAI text-to-speech model used by the voice tool. |
| `MAVERICK_TTS_ELEVENLABS_MODEL` | `eleven_turbo_v2_5` | ElevenLabs text-to-speech model used by the voice tool. |
| `MAVERICK_SEARCH_BACKEND` | auto (preference order) | Force a web-search backend: `tavily`, `brave`, `serpapi`, or `ddg`. |
| `MAVERICK_FETCH_ALLOW_PRIVATE` | unset | `1` allows fetching URLs resolving to private/loopback/reserved IPs (SSRF escape hatch). |
| `MAVERICK_FETCH_RESPECT_ROBOTS` | unset | `1` makes the fetch tool honor `robots.txt`. |
| `MAVERICK_FETCH_NO_SCAN` | unset | `1` skips safety scanning of fetched remote content. |
| `MAVERICK_NET_HOST_CONCURRENCY` | `4` | Max concurrent network requests per host. |
| `MAVERICK_ATTACH_MAX_FILE_BYTES` | `26214400` (25 MiB) | Max size of a single attachment file. |
| `MAVERICK_ATTACH_MAX_GOAL_BYTES` | `104857600` (100 MiB) | Max total attachment bytes per goal. |
| `MAVERICK_ALLOW_RAW_MEDIA_ARGS` | unset | `1` passes raw media tool args verbatim (skips sanitization). |
| `MAVERICK_ENABLE_CRED_TOOLS` | unset (off) | Enable credential-handling tools. |
| `MAVERICK_USE_SKILLS` | config `[features] skills` | Override skill injection; `0` disables (e.g. for benchmark runs). |

## Safety & consent

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_CONSENT_MODE` | `auto-approve` | Gating for destructive actions: `auto-approve`, `auto-deny`, `ask`, or `dashboard`. |
| `MAVERICK_CONSENT_DASHBOARD_TIMEOUT` | `300` | Seconds to wait for a dashboard approval before treating as denied. |
| `MAVERICK_MCP_ELICITATION` | `decline` | How the MCP client answers an external server's `elicitation/create`: `decline` (continue without the value), `cancel` (abort the server's op), or `prompt` (collect typed input from an interactive operator, gated through consent). The prompt is shield-scanned either way. |
| `MAVERICK_MCP_ELICITATION_TIMEOUT` | `300` | Seconds the MCP *server* waits for an elicitation response from a stdio client before giving up and leaving the question parked for the async `maverick_answer` flow. |
| `MAVERICK_MCP_MAX_ELICIT_ROUNDS` | `8` | Max elicit→answer→resume rounds the MCP server runs per `maverick_start`/`maverick_resume` call before returning (bounds runaway question loops). |
| `MAVERICK_MCP_TASK_WORKERS` | `4` | Background worker threads for MCP async tasks (concurrent task-augmented tool calls over stdio). |
| `MAVERICK_MCP_MAX_TASKS` | `256` | Max MCP tasks retained in the in-memory registry; the oldest are evicted past this cap. |
| `MAVERICK_MCP_TASK_TTL_MS` | `3600000` | Default task lifetime (ms) when the client doesn't request a `ttl`; the task may be purged after it elapses. |
| `MAVERICK_MCP_TASK_MAX_TTL_MS` | `86400000` | Ceiling (ms) a client-requested task `ttl` is clamped to. |
| `MAVERICK_MCP_TASK_POLL_MS` | `1000` | `pollInterval` (ms) the server suggests to clients in task responses. |
| `MAVERICK_PREFLIGHT` | `warn` | Request preflight mode: `warn` (log only), `strict` (hard-refuse), or `off`. |
| `MAVERICK_AUDIT_SIGN` | config `[audit] sign` (off) | Sign audit-log rows. |
| `MAVERICK_ANON` | config `[privacy] anonymous` (off) | Enable anonymous mode (scrubs home paths and identifying data). |
| `MAVERICK_AI_DISCLOSURE` | config `[compliance] disclosure_text` | AI-disclosure text appended to outputs; empty string opts out. |
| `MAVERICK_STRIPE_ENABLE_REFUNDS` | unset (off) | Required to allow the Stripe tool to issue real refunds. |

## Secrets, residency & audit forwarding

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_SECRETS_BACKEND` | config `[secrets] backend` (`env`) | Where deployment secrets are read from. `env` = process environment (default, unchanged). `file` = mounted secret files (Vault Agent / Secrets Store CSI / Docker/podman secrets), one secret per file, with env fallback. Applies to OIDC client/session secrets, the inbound webhook secret, and the SCIM bearer. |
| `MAVERICK_SECRETS_DIR` | config `[secrets] dir` | Directory the `file` backend reads (`<dir>/MAVERICK_OIDC_CLIENT_SECRET`, etc.; trailing newline trimmed). |
| `MAVERICK_RESIDENCY_STRICT` | config `[residency] strict` (off) | Refuse to boot when the declared data region is missing or outside the allowed set (`require_residency_or_die`). Off = informational only. |
| `MAVERICK_DATA_REGION` | config `[residency] region` | The deployment's declared data region (ISO code or group, e.g. `DE`, `EU`). |
| `MAVERICK_RESIDENCY_ALLOWED` | config `[residency] allowed_regions` | Comma-separated permitted storage regions; `EU`/`EEA` groups expand to members. Empty = region unconstrained. |
| `MAVERICK_SIEM_DEST` | config `[audit] siem_dest` | Destination for `maverick audit forward`: `tcp://host:port` / `udp://host:port` (syslog) or `http(s)://host/path` (Splunk HEC `/raw`, etc.). |
| `MAVERICK_SIEM_TOKEN` | — | Bearer sent on HTTP(S) audit forwarding (read via the secret provider). |

## Observability

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_LOG_LEVEL` | `INFO` | Logging level. |
| `MAVERICK_LOG_FORMAT` | `text` | Log format: `text` or `json`. |
| `MAVERICK_LOG_TURNS` | unset | Set to log full LLM turns (verbose). |
| `MAVERICK_OTEL_EXPORTER` | unset (off) | Set to enable the OpenTelemetry trace exporter. |
| `MAVERICK_OTEL_ENDPOINT` | `http://localhost:4318/v1/traces` | OTLP collector endpoint. |
| `MAVERICK_RESIDENCY_REGION` | config `[residency] region` (unset) | Declare a data-residency requirement (e.g. `eu`). `maverick doctor` then warns about any residency-sensitive feature still defaulting to a US region (`AWS_REGION`→us-east-1, `VERTEX_LOCATION`→us-central1). No effect unset. |
| `MAVERICK_PROMETHEUS_PORT` | unset (off) | Set a port to expose Prometheus metrics. |
| `MAVERICK_PROMETHEUS_ADDR` | `127.0.0.1` | Bind address for the Prometheus metrics server. |
| `MAVERICK_ALERTS` | config `[alerts] enabled` (off) | Enable OPERATIONAL alerts — page the operator (via the configured notification backends) on infrastructure events like a killswitch trip or a deployment-wide provider cost-cap exhaustion. Distinct from agent-task notifications. |

## Plugins

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_PLUGINS_ALLOW` | config `[plugins] enabled` | Comma-separated plugin allowlist; `*` enables all. |
| `MAVERICK_PLUGINS_ENFORCE` | config `[plugins] enforce_permissions` (off) | Enforce plugin permission declarations. |

## Channels & integrations

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_WEBHOOK_SECRET` | unset | HMAC secret for inbound webhooks. **Inbound receivers fail closed (401) without it** — set it before relying on inbound webhook channels (e.g. Twilio SMS / WhatsApp), or they will reject every request. |
| `MAVERICK_WEBHOOK_WORKERS` | config `[webhooks] workers` (4) | Outbound webhook dispatch thread-pool size. |
| `MAVERICK_WEBHOOK_MAX_INFLIGHT` | config `[webhooks] max_inflight` (16×workers) | Cap on queued+running dispatches; excess is dropped + logged (bounds memory under a burst against a slow receiver). |
| `MAVERICK_GRPC_MAX_WORKERS` | config `[grpc] max_workers` (8) | gRPC goal-API server thread-pool size. |
| `MAVERICK_GRPC_MAX_CONCURRENT` | config `[grpc] max_concurrent_rpcs` (worker count) | Max in-flight gRPC RPCs (RESOURCE_EXHAUSTED backpressure past the cap). |
| `MAVERICK_GRPC_MAX_DEPTH` | config `[grpc] max_depth` (runner default, hard max 64) | Worker-owned recursion ceiling for remote `RunGoal` calls. |
| `MAVERICK_GRPC_STREAM_MAX_SECONDS` | config `[grpc] stream_max_seconds` (300, hard max 3600) | Per-call episode-stream deadline; clients resume with `since_id`. |
| `MAVERICK_ALLOW_INSECURE_GRPC` | unset (off) | Explicitly permit non-loopback plaintext gRPC binds and client dials. Keep unset in production; configure `[grpc]` TLS instead. |
| `MAVERICK_GH_APP_WEBHOOK_SECRET` | unset | Webhook secret for the GitHub App; requests are rejected if unset. |
| `MAVERICK_TRIGGER_LABELS` / `MAVERICK_GH_TRIGGER_LABELS` | built-in default | Comma-separated issue labels that trigger a GitHub-App run. |
| `MAVERICK_BOT_LINEAR_ID` | unset | Linear user id identifying "the bot" for issue webhooks. |
| `MAVERICK_BOT_JIRA_ACCOUNT_ID` | unset | Jira accountId (or bot email) identifying "the bot" for issue webhooks. |
| `MAVERICK_NTFY_TOPIC` | config `[notifications]` | ntfy topic for push notifications. |

## Connectors

Each enterprise connector reads its own credentials from named environment
variables — by convention `<SYSTEM>_BASE_URL` + `<SYSTEM>_TOKEN`, with a few
system-specific shapes (e.g. `SERVICENOW_INSTANCE_URL`, `SNOWFLAKE_ACCOUNT`,
`DATABASE_URL`). The full list of connectors and their exact variables lives in
[connectors.md](connectors.md); `maverick init` can collect them for you.

| Env var | Default | Description |
| --- | --- | --- |
| `<SYSTEM>_BASE_URL` / `<SYSTEM>_TOKEN` | unset | Per-connector endpoint + credential; see [connectors.md](connectors.md). Writes stay confirm-gated regardless. |
| `DATABASE_URL` | unset | SQLAlchemy URL for the `database` tool (Postgres / MySQL / SQL Server / Oracle / Redshift / ...). |
| `MAVERICK_ENABLE_CRED_TOOLS` | unset (off) | `1`/`true` registers connectors that can use ambient host credentials (AWS Lambda/DynamoDB, Google Drive, Airtable, Asana, ClickUp, Vercel). Off by default. |
| `MAVERICK_WORKFORCE_DATA_GROUNDING` | config `[workforce] data_grounding` (on) | Kill-switch for primary-source data grounding. When on, each analyst pack is auto-granted its suite's 37 read-only public-data connectors (SEC EDGAR, FRED, openFDA, USAspending, NWS/NOAA weather, ...) — GET-only, low-risk, deferred. Set `off`/`0` to withhold them. See [connectors.md](connectors.md). |
| `MAVERICK_WORKFORCE_LEVELS` | config `[workforce] levels` (off) | Enable per-agent autonomy levels (observe/suggest/request/auto). Off = every agent stages actions for human execution. |

## Durable execution

| Env var | Default | Description |
| --- | --- | --- |
| `MAVERICK_DURABLE` | config `[durable] enabled` (off) | Enable durable execution (checkpoint/resume). |
| `MAVERICK_WORLD_SYNCHRONOUS` | config `[world_model] synchronous` (NORMAL) | World-DB PRAGMA synchronous level. `FULL`/`EXTRA` make every commit durable on OS crash/power loss (no acked-row loss) at a write-latency cost — for deployments treating the world DB as the billed Operating Record. |
