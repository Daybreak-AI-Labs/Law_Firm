# Lightwork — Shipped Features

What Lightwork **does today**, grounded in the code on `main`. This is the
catalogue of built features and tools; the forward backlog (what's *not* done
yet) lives in [`ROADMAP.md`](./ROADMAP.md). When a roadmap item ships, it moves
here.

> Conventions: capabilities are grouped by concern. Module paths are relative to
> `packages/maverick-core/maverick/` unless noted. CLI verbs are shown as
> `maverick <verb>`.

## Agent kernel & orchestration

- **Recursive multi-agent swarm** — orchestrator decomposes a goal and spawns
  specialist sub-agents (researcher / coder / writer / verifier / revisor /
  reflector), run in parallel (`orchestrator.py`, `agent.py`, `swarm.py`).
- **Durable, resumable execution** — checkpoint / rewind / `maverick resume`
  (`checkpoint.py`), opt-in via `[durable]`.
- **Session forking + run tree** (`session_tree.py`, default-on
  `[session_tree] enable`, depth-capped by `max_depth`) — branch a run at one
  decision point so Oversight can read what the agent chose *beside* the
  alternative instead of only the branch that shipped. `fork(goal_id,
  at_event=…)` creates a new goal, copies the parent's event trail up to an
  absolute `goal_events` id (ids, not offsets — there is no per-goal sequence
  column), appends a marker event naming its origin, and audits
  `session_forked`; `lineage` / `tree` / `roots` read it back. **Nothing is
  re-executed** — a replayed event is a record row, so forking calls no tool,
  spends no token, and never touches the sandbox. Lineage lives in a
  tenant-scoped `session_tree.json` sidecar (released migrations are
  immutable) and deliberately does *not* set `goals.parent_id`, so a
  counterfactual never appears as a decomposition sub-goal; a corrupt sidecar
  degrades to "no known forks" rather than taking a review page down. Read-only
  **Run Tree** dashboard page (`/run-tree`, owner-scoped) +
  `GET /api/v1/run-tree/{goal_id}` (both ends access-checked).
- **Kernel lifecycle hooks** — `PreToolUse` / `PostToolUse` / `UserPromptSubmit`
  (`hooks.py`), registrable from plugins.
- **Budget caps** — hard dollar + wall-clock + tool-call ceilings the kernel
  refuses to exceed (`budget.py`).
- **Killswitch** — `~/.maverick/HALT` aborts all running goals (`killswitch.py`).
- **Long-horizon review checkpoint** — opt-in `[safety] review_checkpoint`
  (`review_checkpoint.py`): the root agent fires a human-review heartbeat every
  N dollars / M tool calls / T wall-seconds; continuation is gated through the
  consent/approval path with silent auto-approval disabled, so a reviewer denial
  or missing explicit approval stops the run cleanly. Distinct from the hard
  budget cap; inert and behavior-identical when unconfigured.
- **Verifier default-on** across goal types (`verifier.py`); **reflexion** retry
  loop with cross-session failure memory (`reflexion.py`); graded **critic** for
  structured accept/revise/reject feedback (`critic.py`).
- **Planning topologies** — tree-of-thought (`tree_of_thought.py`), debate
  (`debate.py`), **plan-execute-reflect** loop (`plan_execute_reflect.py`,
  `maverick plan-reflect GOAL`): a planner decomposes the goal, an executor runs
  each step, a reflector decides done/revise/continue and loops until done, the
  iteration cap, or the budget runs out — speculative decode/finalize
  (`speculative.py`), latency-aware best-of-N that cancels laggards
  (`latency_best_of_n.py`), shared-scratchpad blackboard (`blackboard.py`),
  cross-agent bus (`agent_bus.py`), and a read-only **observation channel**
  (`observation_channel.py`) — a live push/subscribe broadcast of the swarm's
  event stream for an external observer (monitoring agent, dashboard,
  supervisor) that doesn't join the control flow; `blackboard.post` tees into
  it, a slow observer drops its oldest events rather than stalling the swarm,
  and it's a no-op (lock-free subscriber check) when nobody is watching.
- **Context lifecycle** — deferred tool loading + `find_tools`, cross-session
  `memory` tool (`tools/memory.py`), programmatic tool calling
  (`tools/code_exec.py`), structural/retrieval-augmented compaction
  (`compaction/__init__.py`, `context_compactor.py`), and a **long-context retrieval
  router** (`long_context_router.py`) that shards an oversized payload (e.g. a
  document pasted into a goal) and keeps only the query-relevant shards instead
  of overflowing the model window — zero-dep lexical ranking by default, an
  injected Chroma/Qdrant store for embedding-quality retrieval; opt-in via
  `[context] retrieval_router`.
- **Model-scaled context** (`context_scaling.py`, on by default; `[context]
  model_scaled = false` pins the legacy fixed bounds) — history windows,
  per-turn caps, compaction targets, the retrieval-router threshold, tool-result
  caps, and per-turn `max_tokens` all derive from the **driving model's real
  context window** (`preflight.context_limit`) instead of constants tuned for a
  200k model: a 1M-window model gets 1M-window bounds, a 32k model routes
  before overflowing. Legacy values are the floors (nothing shrinks), explicit
  `[context]` keys/env vars still win, and unknown/self-hosted models declare
  their window via `[context.model_windows]` or
  `MAVERICK_MODEL_CONTEXT_WINDOW`.
- **Compaction plug-in API** (`compaction/plugins.py`) — register a custom
  context-compaction strategy (graph-structured, domain summarizer, a learned
  model) under a name and select it via `[context] compaction_strategy`; the
  shipping heuristic registers as the default `"heuristic"`, and `compact_with`
  **fails safe** to it when a configured strategy is unknown (a typo degrades to
  working compaction, never none) — so the kernel's compaction is extensible
  without a fork. Four strategies ship registered in this one dispatcher (and
  are reachable from `agent.py`'s per-turn compaction): **`learned`** (LLM
  summary with a self-tuning prompt picker scored by an outcome ledger),
  **`multimodal`** (replaces heavy image/audio blocks with text stubs that keep
  the media fact + dimensions), **`streaming`** (an incremental running summary
  with a persisted per-conversation cursor — folds only new turns), and
  **`graph`** (an entity-relation digest); each degrades deterministically
  without an llm and is selected by name via `[context] compaction_strategy`.
- **Governed session kernel** (`governed_repl.py`, opt-in `[repl] enable` /
  `MAVERICK_REPL=1`, OFF by default) — an agent writes **Python** against a
  live namespace instead of composing fixed tool schemas, and the code is the
  audited artifact: every statement is SHA-256 hashed, injection-screened
  (`memory_guard`, fail-closed on a broken screen), receipted on the
  hash-chained lineage ledger PREPARE-before/COMMIT-after (`strict=True`, so a
  tampered chain refuses rather than extends — no receipt, no execution),
  audited as `repl_executed` with the digest and never the source, and
  appended to a per-session `ledger.jsonl` that outlives the session. Runs
  through `sandbox.exec` like every other shell, so the configured backend is
  the containment boundary; per-statement wall/output/state/statement-count
  caps come from `[repl]`, and output is secret-redacted host-side. Continuity
  is an explicit **JSON namespace carry** (a stdlib driver re-loads and
  re-dumps the serializable globals), so a side-effecting statement runs
  exactly once — no prelude replay — and values that cannot cross that
  boundary (modules, callables, arbitrary objects) are dropped and **named**
  in the result and the ledger. No tool bridge in v1, deliberately: a kernel
  that reached the registry would dispatch outside `tool_authz.authorize`.
  The module API (`open_session` / `execute` / `transcript` / `close_session`)
  is the entry point in v1 — no CLI verb, no dashboard page. Documented in
  [`governed-execution.md`](./governed-execution.md).
- **Governed learning defaults on** — the in-process learning loops are enabled
  on a clean install and can be disabled individually or together from the
  dashboard Learning page. Higher-authority actions do not inherit that default:
  generated executable tools, third-party MCP acquisition, extra task/result-
  bearing provider calls, verbatim cross-conversation user notes, live flow
  auto-apply, model-weight adoption, and DGM code self-modification retain
  separate gates.
- **Local continuous learning** — distill successful run trajectories into a
  reusable, validator-compliant `SKILL.md` under `~/.maverick/learned-skills`
  (`skill_distillation_local.py`), default-on via `[self_learning] distill_local`.
  **v2** (`skill_distillation_v2.py`) adds two quality gates so the loop stays
  useful: it won't distill from fewer than N successful trajectories (a one-off
  success is noise), and it dedups a candidate against the learned-skills store
  by lexical containment so near-duplicate lessons don't accumulate — the
  orchestrator uses the gated path.
- **DPO preference-pair mining from real runs** — the offline DPO fine-tune
  (`maverick.training.rlaif`, the L3 rung) is trained on preference pairs built
  from donated run records, not hand-labels. Two objective gradient sources:
  (1) a verifier-**rejected draft** paired against the accepted final within one
  `task_family`, and (2) **best-of-N candidate mining** — `run_goal_best_of_n`
  scores each of N coding attempts by whether the task's real tests pass
  (`--fail-to-pass`/`--pass-to-pass`) and donates the pass-vs-fail spread as a
  pair with a ~1.0 reward margin (no verifier rejection needed; a strong
  proposer + strong verifier rarely rejects, so the objective signal is the
  robust one). A pair is written only when the attempts *diverge*; an all-tie
  run logs `best-of-N: no DPO pair donated` and writes nothing. Candidate
  patches an agent applied via tools (no diff in the final answer) are recovered
  from the workdir (`_capture_workdir_diff`, `__pycache__`/binary noise
  excluded) and each attempt is isolated by a reset to clean HEAD. Donation is
  opt-in and metadata-only unless `donate_text` is set (`[telemetry]
  donate_trajectories` / `donate_text`, thresholds `donate_min_entropy` /
  `donate_min_confidence`); `maverick start --repeat N` re-runs one task N times
  so its attempts share a `task_family`. End-to-end recipe in
  [`self-learning-runbook.md`](./self-learning-runbook.md).
- **Dreaming (offline consolidation)** — `maverick dream` (`dreaming.py`,
  default-on `[dreaming] enable`) replays recent successes + failure reflexions
  while the swarm is idle, attributes them to departments (domain packs),
  distills recurring wins into learned skills per department (via the gated
  v2 distiller), clusters recurring failures into *dream insights*
  (`~/.maverick/dreams/insights.ndjson`) that the orchestrator recalls on the
  next similar goal — a domain run is boosted toward its own department's
  insights — and prunes stale near-duplicate reflexions. Deterministic and
  LLM-free, so consolidation can't be steered by injected trajectory text.
  Reflexions also carry the recording run's department (`reflexion.py`
  `domain` field), and same-department lessons outrank equally-similar
  generic ones at recall time.
- **Self-harness (model-specific harness learning)** — `maverick self-harness`
  (`self_harness.py`, default-on under the conservative `risk_limited` profile)
  mines one model's
  recurring failure reflexions into weakness *signatures*, proposes a *minimal*
  operating-guidance line for each, and regression-validates it on held-in AND
  held-out cases (rejecting an edit that overfits its own examples). Survivors
  are gated through the self-improvement ladder on the `prompt` rung — so
  promotion inherits the evidence floor, the calibration-freeze interlock,
  reversibility, and the signed audit, and **requires `[self_improvement]
  enable`**. An accepted line is recalled into the system prompt for *that model
  only* (`recall_addendum`, keyed on the agent's resolved model), never a
  kernel-template mutation. Reflexions now carry a `model_id` for the per-model
  mining. After *Self-Harness: Harnesses That Improve Themselves* (arXiv
  2606.09498); design in [`docs/proposals/self-harness.md`](./proposals/self-harness.md).
- **Governed harness self-refinement** (`harness_refine.py`, opt-in
  `[harness_refine] enable` / `MAVERICK_HARNESS_REFINE=1`, OFF by default) —
  an agent **proposes** a change to its own operating instructions from an
  observed failure; a human **applies** it. `propose` accepts one observation
  (`failure/target/name/change/rationale`) against a closed target set
  (`prompt` / `skill` / `memory`, so a proposal cannot widen its own blast
  radius), screens every free-text field for injection tripwires (a hit
  **refuses** — untrusted text is never queued for a write into the agent's
  own instructions), secret-redacts it, and parks a real world-model approval
  at `dual_control.required_approvals("high")` bound to that one target and
  name. `require_approval` defaults ON and fails closed on a malformed value.
  `apply` refuses unless the proposal is pending, its change still matches the
  approved digest, the killswitch is clear, the evaluator anchors verify, and
  the approval is approved and unspent (one-shot, burned before the effect);
  it snapshots the overlay through the learned-state machinery first, so a
  mid-way failure restores rather than half-writes, and emits
  `harness_refinement_applied` **plus** `learning_update` so learned-state
  verification covers it like a dream cycle. `revert` restores the snapshot
  wholesale and is deliberately not gated on `enable` (turning the capability
  off must never strand an applied refinement); `refinements()` is the read
  seam. Stores are tenant-scoped and 0600, with their own snapshot base — never
  the shared dream store. The module API is the entry point in v1; the human
  half runs in the existing dashboard approval queue. Documented in
  [`governed-execution.md`](./governed-execution.md).
- **Evaluator co-evolution (promote a better judge, don't just freeze)** —
  `maverick.evaluator_evolution` (default-on
  `[self_improvement] evaluator_evolution`)
  extends the calibration interlock from *freeze-on-drift* to *promote-on-drift*:
  a challenger evaluator replaces the incumbent only when its agreement with a
  fixed ground-truth **anchor**, measured by the conservative epsilon-best-belief
  lower bound (the eps-quantile of a `Beta(1+S,1+F)` posterior), beats it. Routed
  through the self-improvement ladder on a dedicated `evaluator` rung, so a swap
  inherits the evidence floor, the calibration freeze, reversibility, human
  approval (it sits above the default `max_auto_rung`), and the signed audit; an
  evaluator only scores, so it carries no capability-escalation surface. Each
  swap advances the slot's *epoch* and applies **selective erasure** — only the
  displaced judge's learning records are discarded, keeping the within-epoch
  signal stationary. The anchor is the guardrail, so released anchors are
  **immutable**: checksum-pinned in `evaluator_anchors.lock.json` and enforced by
  `python -m maverick.evaluator_evolution --ci` (a weak or mutable anchor would
  launder drift). The loop is enabled by default, but the evaluator rung remains
  above the default auto-promotion ceiling and therefore requires human approval.
  After *The Red Queen Gödel Machine:
  Co-Evolving Agents and Their Evaluators* (arXiv 2606.26294); design in
  [`docs/proposals/evaluator-co-evolution.md`](./proposals/evaluator-co-evolution.md).
- **Shared generic insight promotion** — when explicitly enabled with
  `[dreaming] promote_shared`, only recurring *unscoped* failures can become
  shared insights. Department-scoped failures remain compartment-local and are
  never synthesized into globally recallable `domain=None` insights.
- **Skill retirement (the forgetting loop)** — a dream phase moves learned
  skills with a decayed track record (`skill_stats.evictable`: enough uses,
  win rate under the floor) to `learned-skills/retired/` with a logged
  reason — out of the recall glob, reversible by moving the file back
  (`[dreaming] retire_skills / retire_min_uses / retire_below`).
- **Dream-time rehearsal** — dream cycles queue the biggest recurring
  failure patterns as practice cases (`[dreaming] rehearse`,
  `~/.maverick/dreams/rehearsals.ndjson`); `maverick dream --rehearse` runs
  them as budgeted `[rehearsal]`-titled goals and reports how many
  previously-failing patterns now complete. Refused while verifier
  calibration is frozen (the same interlock that gates maverick-evolve), so
  the system never practices against a distrusted grader.
- **Department-scoped routing memory** — a domain swarm's counterfactual
  credit is recorded both globally and per department
  (`role_stats.py` `<domain>::<role>` keys); a domain run's routing guidance
  prefers its own department's track record and falls back to the global
  signal when history is thin.
- **Counterfactual promotion** — the self-improvement controller can judge a
  learned change (tool/prompt/policy) on its confounder-adjusted *causal*
  effect on outcomes, not a correlation that merely co-occurred with success
  (`promotion_effect.py`: stratified/subclassification ATE with a confidence
  interval over the logged trajectory population). The evidence gate then
  requires the effect's lower confidence bound to clear the margin, and every
  promotion records the effect, its CI, the naive (confounded) number for
  contrast, and the confounders adjusted for. Fail-closed by calibration: an
  estimate with too little overlap, or one that leaks a non-zero effect under a
  within-stratum placebo permutation, is refused. Distinct from per-swarm CSCA
  (`credit.py`) — this is offline, corpus-level, for promotion decisions.
  Default-on via `[self_improvement] causal_promotion`; applies only when
  self-improvement is enabled.
- **Capability grading on the promotion receipt** — the capability
  non-escalation gate always refused a widening change, but the durable receipt
  did not record *what the gate concluded*, so a third party reading the ledger
  afterwards could not tell "proven bounded" from "never checked". Each
  `PromotionRecord` now carries `capability_evidence` —
  `probed_bounded` (the capability algebra was walked over N tools, with N
  recorded), `declared_bounded` (the caller asserted it), or `unproven` (the rung
  required no proof and none was supplied) — which is what makes the portable
  attestation's bounded-self-improvement claim checkable rather than asserted.
  Additive and backward-compatible: an absent grading is emitted as an absent
  field, so receipts written before it existed re-serialize byte-identically and
  their hash-chained journal entries still verify. A verifier must read that
  absence as *unknown*, never as bounded; an unrecognised grading fails closed
  rather than being downgraded to a weaker one.
- **Model-based counterfactual rollouts** — when confounding is so severe that no
  stratum has both arms (zero overlap), stratification is blind; g-computation
  over a tabular transition model fit from the logged `(state, action) ->
  next_state` records (`counterfactual_rollout.py`) recovers the effect anyway by
  re-simulating each context with the decision forced to treated vs control and
  rolling forward to a terminal outcome. Returns the same `EffectEstimate`, so
  the gate is unchanged. Fail-closed by calibration: trustworthy only when the
  model predicts held-out one-step transitions, both actions have support, and a
  null-action placebo reads ~0. The tabular learning half of the Operating Twin
  (a generative transition model is a drop-in behind the same interface); same
  `causal_promotion` knob.
- **Pre-execution rehearsal** — the governance half of the Operating Twin: before
  a risky plan runs, simulate it against the same learned world-model and gate on
  the prediction (`rehearsal.py`). Proceed when the model has support and
  confidently predicts a good outcome (let the agent be bold), **block** a
  confidently-poor outcome, and **escalate** to a human/canary when the rollout is
  too uncertain or — crucially — when the model has never seen the move (a
  simulator that bluffs about the unknown is worse than none). Governance as a
  capability, not just a brake. Default-on (`[rehearsal] enable`); it fails
  toward caution (unknown / over-uncertain / error all
  escalate). The verdict maps onto the existing consent / autonomy-gate surfaces.
- **Human-override ingestion** — when a human declines an Art-14 approval
  gate, the refusal is persisted as a recallable lesson
  (`reflexion.record_human_override`, failure class `human_override`,
  department-tagged) so the next similar goal proposes an alternative or
  seeks approval earlier — and dreaming consolidates repeated refusals into
  department insights. Default-on via `[reflexion]`; the audit record is
  unchanged.
- **Signal capture across the run lifecycle** — goal rows persist their
  department (schema v14 `domain` column; resumes inherit it); a stall on a
  user question records WHAT was missing (`blocked_on_user`); a loop-guard
  tool-failure streak persists as `tool_flaky` (and `find_tools` demotes
  repeat offenders at discovery time); an explicit user correction of the
  prior answer becomes a `user_correction` lesson (`corrections.py`,
  deterministic phrase match over the triggering turn only); verifier
  critiques are mined out of donated trajectories into dream fodder.
- **Insight lifecycle management** — a recurring pattern *refreshes* its
  standing insight (ts + evidence) instead of duplicating; insights
  unconfirmed for `[dreaming] insight_ttl_days` age out; a failure insight
  contradicted by newer similar successes retires; opt-in fact pruning
  (`[dreaming] prune_facts`, default off — the only phase touching operator
  data) expires stale facts and caps the table.
- **Per-user preference notes** (`user_notes.py`) — explicit, deterministic
  preference statements ("I prefer tables", "call me Sam") distilled from
  recent conversations into briefing notes injected ONLY for their exact
  (channel, user) scope; the store rewrites every cycle, so deleted
  conversations stop feeding notes.
- **Learned behavior selection** — `[planning] mode = "auto"` picks
  tree-of-thought per task class from a learned outcome record
  (`planning_stats.py`, bandit-lite + deterministic); budget task classes
  scope by department (`finance_sox::reconcile` learns finance-shaped
  caps); the learned compaction ledger scopes by department
  (`scope|kind` rows).
- **Verifier-scored rehearsal + evolve bridge** — `maverick dream
  --rehearse` grades each practiced case with the calibrated verifier, and
  `maverick-evolve --live --rehearsals` consumes the rehearsal queue as
  weighted eval cases so config evolution optimizes against the operator's
  own recurring failures. `maverick-evolve --adopt` overlays the archive's
  best config onto a domain pack (persona/description/models only —
  capability scopes are refused), diff-shown, `--yes`-gated, `.bak`-backed.
- **Learning-side canary** — probation retirement (a new skill that loses
  its first 3 decided uses outright is retired early) and a benchmark gate:
  while `continuous_benchmark` history shows a regression, a dream cycle's
  NEW skills are quarantined (reversibly) instead of going live.
- **Learning governance** — every dream cycle writes one tamper-evident
  `learning_update` audit row; `maverick dream --dry-run` runs the full
  cycle against temp copies and reports exact would-be changes; each CLI
  cycle snapshots all learned stores first (`--list-snapshots`,
  `--rollback latest|<name>` restore wholesale); with an active tenant,
  every learned store resolves under the tenant's data dir so one tenant's
  memory never feeds another's runs.
- **Client-controlled research DGM** — `[self_modify] enable` remains off by
  default and outside the blanket learning control. A deployment-global admin
  can arm it from the dashboard Learning page or `POST /api/v1/learning/dgm`
  after acknowledging that it is research-only. Arming does not run a cycle and
  never authorizes code adoption or deployment. A ready state requires a narrow
  editable surface, at least two discriminating `eval_tests`, and an external
  attested `ep:<name>` sandbox; every operator-run cycle repeats Git, budget,
  HALT, containment, and evaluator checks. If `MAVERICK_SELF_MODIFY` is set, the
  environment owns the setting and the dashboard control is read-only. An
  operator-owned `MAVERICK_CONFIG_OVERLAY` that sets the DGM bit likewise locks
  the dashboard control. Tenant overlays cannot alter the repo-global gate,
  editable surface, challenge corpus, or evaluator policy.
- **Governed Actions** (`governed_actions.py`, `governed_connectors.py`;
  opt-in, additive) — a consequential operation can be a typed `ActionSpec`
  rather than a free-form call: **simulated** before commit (effect preview,
  no side effects), **gated** on risk/approval (`[actions] require_approval_at`,
  default `high`), and **lineage-tracked** (a tamper-evident hash chain from
  outcome → action → inputs/sources/skills; `verify_lineage` / `trace`).
  `Connector`s expose a system of record as `<sys>.read` (low risk) /
  `<sys>.write` (high) governed Actions. `governed_rest.py` adapts the LIVE
  enterprise REST connectors (Salesforce, ServiceNow) into this surface — the
  write previews its effect without a network call, hits the approval floor,
  commits through the same SSRF-safe / egress-guarded path the tool form uses,
  and records lineage — so a real system-of-record write is governed, not just
  a confirm-gated tool call. Opt-in via `[governed_connectors] enable` +
  `connectors` (`MAVERICK_GOVERNED_CONNECTORS`; wizard step). Palantir-style
  action governance, for self-improving agents (see `docs/palantir-playbook.md`).
- **Governed learning at scale** (`access_policy.py`, `learning_rollout.py`;
  opt-in, additive) — **PBAC**: a skill declares `purposes:` and
  `relevant_skills` recalls it only under a matching run purpose
  (`purpose_scope` / `MAVERICK_PURPOSE`; default-open). On the run path, per-goal
  action **lineage** is recorded when `[actions] enable` is set (tenant-scoped),
  and `impact_of` answers "revoke X → what did it touch?". Apollo-style
  `run_rollout` promotes a learned skill across the fleet only behind eval/health
  constraints, staged with auto-rollback. Inspect with
  `maverick governance lineage|impact`.
- **Specialist operating discipline** (`domain_discipline.py`, on by
  default; `[domains] discipline = false` opts out) — every domain pack's
  persona is augmented at spawn with a universal verification/escalation
  discipline plus its suite's professional guardrails (finance
  maker-checker + SoD, legal privilege, HR PII-minimization, IT/GRC
  chain-of-custody, ops safety interlocks, engineering tests-first, GTM
  no-overpromising, strategy source-grounding). One implementation point
  upgrades all 2,020 built-in packs AND operator/intake-generated packs;
  prompts only — hard limits stay with capabilities/governance.
- **Hard refusals** (`domain_refusals.py`, always on — a prohibited use is not
  an operator preference) — every pack's prompt carries a non-negotiable
  refusal block with **no approval path**: the EU AI Act Art-5 prohibitions for
  HR (workplace emotion inference, biometric categorization, social scoring),
  safety-critical actuation/interlock-override refusals for ops/manufacturing/
  utilities/logistics, autonomous clinical/insurance/banking adjudication,
  MNPI-crossing for capital-markets/strategy, and counsel-of-record for legal,
  plus a universal "never disable your own controls / impersonate a human".
  Packs add their own via a `refuse = [...]` field; the rails (capability /
  governance / Shield) still enforce independently — this makes the agent
  refuse *before* a rail is tested.
- **Pack consumption surface** — every one of the 2,020 packs now declares an
  `[output]` contract (deliverable shape, consumers, cadence, sign-off gate) and
  an editable `[[workflow]]` playbook (the ordered procedure, each step naming
  only the pack's own tools and ending in the human gate). Rendered into the
  spawn prompt and the dashboard; intake-generated packs get them too.
- **Reasoning-effort right-sizing** (`effort` pack field, applied only when
  `[effort]` is enabled) — high-stakes judgment packs (SOX, AML/SAR, valuation,
  prior-auth, litigation, incident response, pharma) run deep; clerical
  high-throughput packs (status pages, chasers, hygiene) run light; the rest
  inherit the operator default. A pack tier beats the global default but defers
  to any per-role/env override, and never turns the feature on.
- **Department memory at every spawn depth** — `agent_from_profile` appends
  the department's recalled lessons (same-department reflexions + dream
  insights) to a specialist's brief, so a `spawn_specialist` child starts
  with its department's memory instead of blank (`[domains] memory`;
  no-op unless those loops are enabled).
- **Pack quality gate** — `maverick domains-lint [--ci --warnings]`
  (`domain.lint_profile`): errors for envelope holes (empty tool allowlist
  = ALL tools, missing/unknown `max_risk`, invalid `effort` tier), warnings
  for quality gaps (thin persona, allow∩deny overlap, no knowledge sources, a
  read-only pack not explicitly denying the `shell`/`write_file` floor, an
  `output.gate` lighter than the playbook's final sign-off). All 2,020 built-in
  packs lint clean (0 errors, 0 warnings); every pack carries at least a
  suite-level `knowledge_sources` grounding fallback.
- **Governance-posture audit** — `maverick domains-audit [--json <path>
  --suite <name>]` (`domain_audit.py`): the auditable inventory of "what can
  these agents do, and what stops them?" — per pack the compartment seal, risk
  ceiling, whether any state-mutating tool is *reachable* (0 across the drafting
  roster), which irreversible actions it denies (segregation of duties), its
  refusals, and the human sign-off on its deliverable. Flags + exits non-zero if
  a drafting pack could reach a mutator; `--json` exports for a GRC system.
- **Safety-posture & FinOps export** — `maverick safety [--json]` prints the
  live posture (shield status, sandbox backend + container isolation, egress
  policy) to assert deployment safety in CI; `maverick spend [--json]` exports
  total + per-goal + per-tag run cost for BI/chargeback; and the dashboard
  `/billing` view shows a tenant's accrued charges, period-over-period trend,
  and an itemized CSV invoice.
- **Per-pack behavioral evals** — `maverick domains-eval [--check]`
- **Roster-wide governance invariant suite** (verified across ALL 2,020 packs,
  fault-injected with a non-vacuous control) — six invariants proven non-vacuously
  (each carries a fault-injection control that fails when the guarantee is
  removed): (1) **tool-reachability** — no drafting/non-builder agent can reach
  a state-mutating tool; (2) **autonomy dial** — an onboarding agent is never
  autonomous, and a high-risk action is never autonomous even once a pack is
  graduated; (3) **capability attenuation** — a spawned child can never exceed
  its parent's grant (no privilege escalation); (4) **compartment isolation** —
  a quarantine seal never bleeds across compartments/suites; (5) **hard
  refusals** — the universal refusal floor is unstrippable; (6) **budget caps**
  — no cap is ever silently exceeded. Plus hostile-argument fuzzing of every
  connector and tool.
- **Robustness hardening** (bugs found by the stress sweep and fixed) —
  connectors no longer raise on a non-string op/path/query (they return an
  ERROR string); `Skill.parse` raises `ValueError` (not `AttributeError`) on
  malformed/untrusted frontmatter; `format_money` degrades gracefully on a
  None/empty currency.
  (`domain_eval.py`): golden cases that test a specialist's load-bearing
  behavior (AP catches a duplicate and never pays; legal cites or marks
  unverified; HR refuses emotion inference; ops refuses an interlock override).
  A deterministic rubric scorer (include/exclude/refuse/cite) plus an injected
  runner; `--check` lints the suite against the roster (key-free CI gate) while
  `run_eval(cases, runner)` scores live when a provider key is present.
- **Specialist routing** (`domain_router.py`, wired into `list_specialists
  query=<task>`) — a pre-filter that ranks the whole 2,020-pack roster for a
  task so the orchestrator picks from a shortlist, not a haystack: lexical
  TF-IDF blended with sentence-transformer cosine when `fastembed` is installed
  (paraphrase-aware), graceful lexical-only fallback otherwise. A 25-case
  benchmark floors recall@10 ≥ 80% so a pack/persona edit can't silently degrade
  routing.
- **Hindsight engine** (`hindsight.py`, `maverick hindsight [--strict
  --ledger]`) — replays past goals against learned-state snapshots to detect
  when the forgetting loops silently cost coverage: gained / regressed /
  unchanged, deterministically, with no agent re-runs. `--strict` is a
  learning-regression CI gate; `--ledger` appends a tamper-evident row.
- **Workforce value report** (`workforce_value.py`, `maverick proof
  [--fleet]`) — deliverables completed, agent cost vs human baseline →
  cost avoided + ROI, the capability improvement curve from the hindsight
  ledger, and governance evidence, per department (and per external vendor
  with `--fleet`). Read-only; the POC-closing artifact.
- **Assessment doc discovery** (`doc_discovery.py`, `POST /api/v1/docs/
  discover`, `POST /api/v1/goals/{id}/attachments/from-source`) — when
  someone is filling an assessment, searches the CONNECTED sources
  (Microsoft Graph/SharePoint/OneDrive, Slack files, Google Drive) for the
  subject's SOW/contract/DPA/security paperwork and attaches it as real
  goal evidence in one click. Creds resolve env-first then named sealed
  connections; fetched bytes pass the same validation as a direct upload
  (size cap, mime allowlist, executable deny). Fail-open per source;
  `[assessments] doc_discovery` + sources list; inert until a source is
  connected.
- **Assessment memory** (`assessment_memory.py`, `GET /api/v1/assessment-
  memory/similar`, agent tool `similar_assessments`) — the assessment flow
  learns from the org's own past assessments: similar-subject precedents
  with risk ratings, advisory per-question answer suggestions (majority
  vote with confidence + provenance; ties dropped), and semantic lessons
  via the knowledge plane (`assessments` collection, ingested on every
  `save_session`) when enabled. Advisory only — the human review gate is
  untouched. `[assessments] learn`.
- **Department workspaces** (dashboard `/privacy` + `/finance`, shared
  chassis partials) — one workspace pattern per department over the same
  assessment engine: worklist with the inherent→residual risk pair, aging,
  follow-ups, client-side filters, hero stats, precedent memory (rows open
  the full record in the review pop-out), and the framework catalog. Built
  for program volume: actionable rows first (open work + due re-reviews),
  capped table with an honest "showing X of N", stats always count
  everything. Operate floor — records carry answer bodies.
- **Assessment lifecycle** (`assessment.decide_assessment`, `POST /api/v1/
  assess/sessions/{id}/decide`) — reviewer decisions (approved/rejected,
  attributed, audited as `ASSESSMENT_DECIDED`) with a review cadence:
  approval schedules `next_review_at` (90d/180d/1yr/none from the shared
  pop-out) and the record comes due — "due for re-review" stat + filter +
  countdown column in both workspaces. Re-engagement is the existing
  follow-up loop (needs_more → answered → pending_review → re-decide).
- **Editable questionnaire templates** (`assessment.save_custom_template`,
  `GET/PUT/DELETE /api/v1/assess/templates/{type}`, catalog editor on the
  workspaces) — templates are data an operator edits, not code: a custom
  template (JSON under the tenant home, department-tagged privacy|finance)
  WINS over the built-in of the same type; deleting the override restores
  the built-in, which is never modified. Scoring identical; strict
  human-readable validation; saves audited (`TEMPLATE_SAVED`).
- **Privacy framework coverage** — built-in questionnaires now span the
  assessments a program actually runs: PIA (lightweight screen) plus the
  formal **DPIA** (GDPR Art. 35, distinct and deeper), **LIA** (Art. 6(1)(f)
  three-part legitimate-interest test), **CCPA/CPRA** (California), the
  **TIA** (Schrems II), AIRA, vendor risk, HIPAA/SOC 2/PCI DSS.
- **Assessment lifecycle depth** — beyond approve/reject: **risk acceptance**
  (`accept_risk`, `POST /assess/sessions/{id}/accept-risk`) records a named
  owner, rationale, and expiry, and comes due again when it lapses;
  **re-review triggers** (`trigger_review` / `set_renewal`,
  `/trigger-review`) force a record due now for an external event (contract
  renewal, new sub-processor) or on a renewal date; the worklist's
  `review_due_reason` says why. **Vendor risk trend** (`risk_trend`,
  `/assess/trend`) shows residual risk moving up/down across successive
  reviews in the pop-out. **Bulk import** (`/assess/bulk-import`) queues a
  CSV of vendors as pending assessments. All revision-checked and audited
  (`RISK_ACCEPTED`, `REVIEW_TRIGGERED`, `RENEWAL_SET`,
  `ASSESSMENTS_BULK_IMPORTED`).
- **Program insights** on `/privacy` — a **DSAR SLA aging** heatmap
  (`/privacy/dsar/aging`: open requests bucketed by days to the statutory
  deadline, by kind) and a **cross-border transfer map**
  (`/privacy/transfer-map`: flows drawn from the Art. 30 register and TIAs
  with a Chapter V safeguard flag).
- **Audit binder** (`/audit/binder`, `GET /api/v1/audit/binder`,
  audit-gated) — the regulator-grade evidence pack assembled from the
  records themselves: per-day signed-chain verification over the audit log,
  the event summary, approvals with identity + quorum, the assessment
  register with CAS revisions and template digests, the privacy registers,
  and the acceptance-learning KPIs. Print-friendly; the product generates
  its own workpapers.
- **Partner fleet console** (`/partner`, `/api/v1/partner/*`) — the
  multi-tenant view for a partner operating client deployments: registry
  (admin-gated writes; tokens never echoed), live `/health` +
  `/value.json` probes per tenant, white-label theme per row, and fleet
  rollups of the agents' own counted value ledgers.
- **Learning KPI** (`acceptance_metrics`, on the command center and the
  binder) — first-pass acceptance (approved with no follow-up thread = the
  reviewer took the agent's draft as-is), qualified-after-follow-ups,
  rejections, median days to decision, and the 12-month trend. The
  measurable learning loop: evidence the agent is getting better.
- **Question ROI** (`question_roi`, `GET /api/v1/assess/question-roi`,
  command-center panel) — per-question evidence across every saved
  assessment: fire rate, rating impact (re-rolled on the scorer's own
  rollup), and a verdict (load-bearing / informative / inert / unproven).
  The evidence-based questionnaire prune list.
- **Signed license keys** (`maverick.licensing` + each SKU's
  `license_kit.py`) — Ed25519 tokens (`LW1.payload.sig`) minted by the
  vendor CLI, verified agent-side from `LIGHTWORK_LICENSE` +
  `LIGHTWORK_LICENSE_PUBKEY`. Unlicensed standalone agents run in
  evaluation mode (fully functional, open-case cap, honest banner); an
  upsell or renewal is a key swap, not a reinstall.
- **DSAR Concierge** (`demo/dsar-concierge/`) — the second sellable
  standalone agent on the capability seam: subject-facing intake + a
  deterministic message detector, the emailed identity-verification loop,
  the statutory clock with SLA aging, access/portability packages built
  ONLY from operator-provided extracts, the deliberately non-destructive
  erasure handoff, `/value.json` for the fleet console, licensing, and a
  full deployment kit (Dockerfile, launcher, env reference). With
  Lightwork the same agent mirrors into the privacy workspace registers
  and the signed audit chain.
- **Verified OneTrust wire protocol** (`demo/pia-concierge/
  onetrust_client.py`, reference: `demo/pia-concierge/
  ONETRUST-INTEGRATION.md`) — the concierge files assessments through the
  protocol a live OneTrust tenant actually accepted: launch (v3, org group +
  respondent), export-first response writes (option answers with optionId +
  responseKey; justifications as entries in the responses array — the
  top-level field is silently dropped; PERSONAL_DATA as catalog triples
  whose write replaces the row set), submit with locally-computed
  completeness (the server under-reports), stage self-check by re-read,
  two-step attachments onto the vendor record (assessment-level ops are
  session-gated), typo-tolerant inventory dedup (vendors created only on
  true no-match; entities never), the Enterprise Policy notices API, and a
  deterministic notice cross-check (contradictions with published promises
  escalate the filed rating) + jurisdiction-driven contract instrument
  guard + per-work-product value ledger. The mock tenant (`ot_mock.py`)
  enforces every trap, so the test suite proves wire compatibility offline;
  autonomy stops at Under Review — the human gate.
- **Privacy command center** (`/privacy/board`, `GET /api/v1/privacy/board`,
  operate-gated) — the whole program on one live executive board, every
  number from the operating record: KPI tiles with 30-day delta chips and
  sparklines; a residual-risk donut and per-framework bars that
  **cross-filter** the board on click (plus a 30d/90d/12m time slicer); the
  **risk burn-down** (inherent → residual — the risk the program removed);
  12 months of opened-vs-decided throughput; the DSAR SLA runway stacked by
  kind; transfer safeguard coverage; AI Act tiers; **posture gauges** that
  are real ratios out of the registers (DSAR SLA, review cadence, transfer
  safeguards, incident 72h clock, DPA clause coverage — never a synthetic
  score); and a drill-through of the records behind the numbers. One
  aggregate round trip; hand-rolled SVG (no chart deps); auto-refreshes
  every 60s; Present mode for a full-screen pitch.
- **Privacy ops record types** (`privacy_ops.py`, `/api/v1/privacy/*`,
  tabs on `/privacy`; `[privacy_ops] enable`, wizard step) — the "nimble
  OneTrust" data plane, every engine deterministic and explainable:
  - *DPA reviews* — clause-by-clause GDPR Art. 28(3) verdicts
    (present/unclear/missing) where every call quotes its matched excerpt;
    inherent risk from the document's declared exposure, residual falls as
    clauses are found. Reviews from pasted text, from CONNECTED sources
    (`/privacy/dpa-documents` search + review-in-place via doc discovery),
    or from digital PDFs (stdlib extractor: FlateDecode, Tj/TJ/hex
    operators; scanned PDFs refuse honestly).
  - *Vendor-paper review + tracked-changes redline*
    (`paper_review.py`, `docx_redline.py`, the **Vendor paper** tab on
    `/privacy` + `/api/v1/privacy/paper-reviews`, and the concierge's
    reviewer desk; `[paper_review] use_model`, wizard step) — when a vendor
    insists on THEIR paper: classify the instrument (GDPR DPA vs CCPA/CPRA privacy
    addendum, scored from the document and filename, ties resolving to the
    stronger instrument), compare it clause-by-clause against our standard
    positions, and emit two deliverables — an analysis memo and the vendor's
    own document marked up with real Word revisions (`w:ins`/`w:del`,
    authored and dated), filed onto the vendor's OneTrust record under the
    next sequential version for that vendor+instrument. The comparison goes
    beyond presence detection: adverse language ("may engage sub-processors
    at its sole discretion") is reported as *conflicting* and escalated to
    high severity, because a paper that bargains for the opposite of our
    position would otherwise score as satisfied. Gap findings are always
    deterministic — a model can neither create nor clear one; it only
    re-words OUR clause to match their drafting, and a draft that drops a
    load-bearing term is discarded for the template text. Redlines edit an
    uploaded `.docx` in place (every other package part preserved) or
    reconstruct from a digital PDF, saying so explicitly. Stdlib OOXML — no
    python-docx.
    **Setting it up:** the shipped clauses are *our* standard positions; a
    deployment's are its own. `maverick clause-playbook init` writes an
    editable playbook (`show` reports what is in force and what you have
    customised), `[paper_review] playbook_path` relocates it, and the
    **Your clause playbook** editor on the Vendor paper tab
    (`GET`/`POST /api/v1/privacy/clause-playbook`, admin to write) does the
    same without a terminal. Clauses left alone keep the shipped position, so
    a partial playbook is a valid one, and a missing or malformed one degrades
    to the shipped language rather than emitting a blank demand.
  - *Drafting OUR paper* (`paper_review.draft_our_paper`,
    `GET /api/v1/privacy/our-paper`, the **Draft our paper** button on the
    Vendor paper tab, `maverick clause-playbook draft <vendor>`;
    `[paper_review] org_name`, wizard question) — the other half of the
    paper-source question: when the vendor signs OUR paper, generate our
    template DPA or CCPA/CPRA addendum as a real `.docx` whose clause body is
    the operator's playbook and whose vendor-specific values (party names,
    effective date) are **rendered in red (`FF0000`)** and listed in a cover
    note, so counsel verifies exactly what the machine inserted. Entirely
    deterministic — no model touches it; a missing org name or date stays a
    red bracketed placeholder rather than a guess, and the generated draft
    carries zero fabricated tracked changes (`revision_count == (0, 0)`).
  - *The negotiation round-trip* — a renegotiated vendor draft (v2+ for the
    same vendor+instrument) is automatically diffed against the previous
    round's demands: the saved record and memo gain a **NEGOTIATION
    PROGRESS** section naming which demanded changes the counter-party
    accepted (`closed_from_previous`) and how many remain open, so a
    negotiation reads as converging (or not) instead of as unrelated
    reviews.
- **The entity graph** (`entity_graph.py`, `/api/v1/graph/*`, the **Lineage**
  tab on `/privacy`, `maverick graph rebuild|dossier|why|blast`;
  `[entity_graph] enable`, wizard step) — one resolved identity per vendor /
  system / person / document / clause across every record silo, connected by
  typed, **time-bounded** edges derived from the governed stores (assessments,
  DPA + vendor-paper reviews, security vendor reviews, AI registry, DSARs,
  incidents). Three rules make it trustworthy: it is an **index, never a
  source of truth** (rebuilt from records at any time, every edge cites the
  record id it derives from, so graph and records cannot disagree); linking is
  **exact-canonical only** ("Acme Corp." = "acme corp"), never fuzzy — a
  fabricated edge is worse than a missing one; and **every edge carries
  `valid_from`/`valid_to`**, with a superseded review's edge closed at its
  successor's birth. That buys the three questions a regulator actually asks,
  each answered with citable record ids: *why did we approve this vendor?*
  (`why()` walks decision → reviewer → reviews in force at decision time →
  clause findings → documents), *what did we know when we signed?*
  (`dossier(as_of=…)` reconstructs the point-in-time picture), and *this
  clause turned out bad — what relied on it?* (`blast_radius()` reverse
  reachability to records, vendors, and decisions). The **episodic plane**
  joins on exact keys only — episode→goal (foreign key), goal→agent
  (`goal.domain`), goal→owner — and the **procedural plane** joins through
  `source_goal_ids`, the goal ids the local distiller now stamps into each
  learned skill's frontmatter. A goal's *title* is prose and is never matched
  against vendors or systems; a skill distilled before provenance stamping
  reads as provenance-unknown rather than guessed. Together they close the
  memory-integrity loop: *"this run turned out to be tainted — which learned
  skills depend on it?"* is `blast_radius("goal", id)`, the compliance
  version of a product recall applied to the platform's own memory.
- **Assessment assignment** (`assessment.assign_assessment`,
  `POST /api/v1/assess/sessions/{id}/assign`, the **owner** column plus
  *Mine* / *Unassigned* filters on every department worklist) — every other
  field on an assessment is post-hoc attribution (who decided, who answered);
  this is the only one that says whose job it is NEXT, so a reviewer can ask
  "what is on my desk?". Revision-guarded and audited like every other
  assessment mutation. Routing only: it never gates who may decide.
  - *AI systems registry* — EU AI Act tier screening
    (prohibited/high/limited/minimal) using the Act's own tests (Art. 5,
    Annex III, Art. 50) with matched signals + tier obligations; a
    completed AIRA registers its system (`/privacy/ai-systems/
    from-assessment/{id}`) and attestations ratchet the tier UPWARD only.
  - *RoPA / data inventory* — Art. 30(1) records; a completed PIA drafts
    an entry with provenance (`draft_ropa_from_assessment`), OneTrust's
    RoPA/Data-Mapping CSV export imports with `source="onetrust"`
    (tolerant of their column drift), `export.csv` produces the register
    an authority asks for.
  - *DSAR tracker* — statutory 30-day clock over the REAL machinery:
    access/portability run `dsar.export_subject_data`; erasure only ever
    prepares the exact `maverick erase` command (never destructive from
    the API). Intake from inbound messages: `detect_dsar` classifies the
    email a subject actually sent (Art. 15/17/20 phrasing), extracts the
    address, and keeps the matched phrases as provenance.
  - *Incident register* — Art. 33 72-hour clock from discovery ("past
    72h" badge while undecided); the notification call stays a documented
    HUMAN decision both ways (rationale required — Art. 33(5) covers the
    "no"), attributed and audited. The register never notifies anyone
    itself.
- **Security & GRC workspace** (`security_ops.py`, dashboard `/security` +
  print-friendly `/security/report`, `[security_ops] enable`) — a governed
  control-program data plane on the shared department chassis. Security
  questionnaires cover SOC 2, ISO/IEC 27001:2022, NIST CSF 2.0, NIST SP
  800-53 Rev. 5, CIS Controls v8, PCI DSS 4.0, the HIPAA Security Rule,
  CMMC 2.0 Level 2, and FedRAMP Rev. 5 Class C readiness (the 2026
  transition mapping for legacy Moderate authorizations) using the existing
  yes/no/na/unknown and inherent→residual scoring contract. Pinned granular
  catalogs provide 106 CSF subcategories, 287 NIST 800-53 Moderate selections,
  153 CIS safeguard IDs with minimum IG assignments, 110 CMMC L2 practices,
  and 322 current FedRAMP Class C selections; the CIS prompts omit licensed
  safeguard titles/text and require an authorized CIS copy for commercial use.
  Durable record
  types cover the control register / SoA, quoted-evidence verdicts, risk and
  expiring exceptions, POA&M, vendor posture, policy/attestation lifecycle,
  security incidents, and audit engagements. Mutations are revision-CAS and
  audit-outbox backed; program insights aggregate framework coverage, control
  status, open work, exception expiry, and crosswalk reuse. Framework prompts
  are readiness aids, not bundled licensed standards text or certification.
- **Agent Security Plane — agent-EDR: detect, contain, prove** (`agent_edr.py`,
  `maverick security posture|detections|contain|release|incident`) — the
  defense stack surfaced as one operator surface instead of six modules with
  six vocabularies. **Detect**: every security row on the signed audit chain
  (`shield_block`, `capability_denied`, `governance_denied`, `egress_blocked`,
  `agent_trust_denied`, `secret_redacted`) normalized into one `Detection`
  shape with an attack class and a severity **derived from the event, never
  self-declared** — an audit kind the table does not model produces no
  detection at all, because a plane that invents threats from rows it does not
  understand teaches its operator to ignore it. **Respond**: `contain()` seals
  the agent mid-run and revokes its capability subtree in one act, reporting
  each separately — a compartment seal is **run-scoped and in-memory**, a
  revocation is **durable**, and `contained` is True only when every requested
  action actually landed (a revocation is confirmed by re-reading the registry,
  never assumed). **Prove**: every containment lands on the signed chain and
  `incident_report()` renders the forensic timeline. Every command reports the
  deployment's **posture** alongside its findings: an empty detection list from
  a deployment with the shield off means "not watched", not "not attacked", and
  an unsigned chain is flagged as not tamper-evident rather than presented as
  evidence.
- **Internal platform threat hunter** (`platform_hunt/`, dashboard
  `/security/threats`, opt-in `[threat_hunt] enable`) — a deterministic,
  model-free detector whose production verdict authority is the verified signed
  audit chain plus the independently verified budget-receipt ledger. Mutable
  WorldModel approval/goal/episode snapshots cannot become finding evidence by
  being supplied beside an intact chain. Stable MITRE ATT&CK-tagged findings
  cover shield bypass, injection,
  novel tools, privilege/approval abuse, exfiltration-shaped sequences,
  unsanctioned self-modification, budget/rate and operator anomalies, quorum
  abuse, and goal anomalies; every verdict cites exact event references plus a
  normalized-event SHA-256 commitment. Audit-chain verification is a separate
  input and can raise a critical finding. Findings/investigations use revision
  CAS + a durable outbox; containment is proposal-only, never autonomous.
- **Customer-environment threat hunter** (`env_hunt/`, dashboard
  `/security/soc`, opt-in `[env_hunt] enable`) — read-only telemetry adapters,
  deterministic and customer-Sigma detections, exact-event evidence,
  bounded related-event pivots, allowlisted enrichment, an ATT&CK heatmap, and
  defensive response proposals. Raw events
  stay ephemeral; durable data is derived findings, investigations, proposals,
  and receipts. `[env_hunt] response_execution` is a separate default-off
  switch and does not bypass exact proposal-bound human approval or executor
  scope. The offline Ed25519 approval binds the exact queue row, decision
  identity, proposal digest, and executor; a durable one-shot claim blocks
  automatic retries after an ambiguous external outcome. Missing Shield keeps
  deterministic detection available but suppresses pivots, enrichment,
  automatic proposals, and execution as reduced-autonomy mode.
  The read-only `ConnectorRegistry` is the ingestion extension seam; the
  mutating `ResponseExecutorRegistry` ships empty and requires explicit client
  registration. The reduced standalone's generic JSON ingestion is the
  compatibility floor for a client source without a named adapter.
- **Model Risk & AI Assurance Officer** (`model_risk_assurance.py`, dashboard
  `/security/assurance`, opt-in `[model_risk_assurance] enable`) - governed
  inventory, declarations, review-gated evidence, deterministic findings,
  incidents, time-bounded risk acceptance, exact DGM promotion authority,
  deployment lineage, and signed assurance packs. The required opt-in
  `[evidence_graph] enable` projects approved evidence without treating raw
  collection as proof. Framework and legal mappings are advisory; applicability
  and every consequential decision remain human-owned. See
  [`MODEL_RISK_ASSURANCE.md`](./MODEL_RISK_ASSURANCE.md).
- **AI Evidence-Ready Gateway** (`ai_evidence_gateway.py`, the existing
  `/security/assurance` cockpit, opt-in `[evidence_gateway] enable`) -
  deterministic text-delivery disclosures, machine-readable markings,
  idempotent hash-only interaction receipts, policy/model/context bindings,
  cited regulatory-impact review, and exact signed JSON assurance packets.
  It stores no raw prompt or generated response in its receipt ledger and makes
  no certification claim. Packet issuance is a bounded, admin-only mutation
  with a completeness manifest and source-snapshot recheck. The synthetic
  dashboard seed performs no network access, requires an empty/dedicated demo
  tenant, and forces a visible readiness gap. See
  [`AI_EVIDENCE_GATEWAY.md`](./AI_EVIDENCE_GATEWAY.md) and the
  [governance benchmark report](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/benchmarks/GOVERNANCE_FRONTIER_RESULTS.md).
- **Four reduced standalone security SKUs** (`demo/grc-concierge/`,
  `demo/platform-threat-hunter/`, `demo/environment-threat-hunter/`,
  `demo/model-risk-ai-assurance-officer/`) — each has
  an explicit standalone capability layer, backend seam, vendored
  engine that imports no `maverick` code in forced mode, launcher, accessible
  UI, `/about` capability matrix, and focused isolation tests. GRC keeps a
  local unsigned starter catalog/risk/POA&M store; both hunters persist derived
  records only; Model Risk keeps declared metadata, local human decisions, and
  non-executing DGM readiness reports. They do not claim the platform's signed
  audit, governed
  approvals, managed connectors, or cross-run learning. Both hunter SKUs are
  proposal-only; response execution is exclusive to the integrated platform's
  exact approval-bound, explicitly enabled executor seam.
- **Workspace history seeder** (`demo/pia-concierge/seed_workspace.py`) —
  seeds an enterprise year through the real engines: ~436 assessments over
  14 months across both departments (weighted statuses, cadences with a
  due-for-re-review queue, varied risk), DPA reviews, the AI registry, a
  populated Art. 30 register, a DSAR queue with history, and incidents in
  three states. Reproducible (seeded random), <1s, marker-guarded.
- **Savings dashboard** (`savings.py`, `maverick savings`, dashboard
  `/savings` + `/api/v1/savings`) — money saved vs the typical human cost,
  from real completed work and the CLIENT's own inputs: fully-loaded human
  hourly rate × human hours per task (global + per-department overrides,
  `[value]` in config), editable right on the page and persisted via the
  dashboard overlay. Savings = human cost − actual agent spend, plus ROI
  and hours given back. Read-only over the same episode ledger as Spend;
  zeros (never invented numbers) until goals have run.
- **2,020-agent specialist portfolio across 53 suites** — the original 8
  business suites plus customer experience, marketing, procurement,
  data & analytics, security ops, executive office, facilities/EHS, tax
  preparation, and 30+ industry verticals (healthcare, insurance, banking,
  retail, manufacturing, construction, logistics, professional services,
  government contracting, education/nonprofit), with jurisdiction packs
  (country employment law, GDPR/CCPA/PIPL/EU-AI-Act regimes, indirect-tax
  regimes). Quality gate: `maverick domains-lint [--ci]` — every pack has
  a bounded persona, least-privilege allow list, explicit deny list, and
  risk ceiling; 0 errors, 0 warnings across all 2,020.
- **Tax preparation pipeline** (`tax_prep.py`, `maverick tax prepare
  <docs-dir>`; the 19-pack `tax_` suite) — a CPA firm's docs-to-draft
  workflow: uploaded client documents are classified and extracted
  deterministically (W-2 / the 1099 family incl. R/B/G/SSA / K-1 /
  1098/-T/-E / prior returns), assembled into a standardized workpaper, and
  computed into a first-pass TY2025 draft 1040 **plus the resident-state
  return** (auto-detected from W-2 box 15; no-tax and flat-rate states
  computed, graduated states explicitly handed to the preparer/tax engine)
  where **every line cites its source document** and everything out of
  computed scope (Schedule C/D/E, itemizing, benefit taxability) is an
  explicit OPEN ITEM. Agents do the unstructured labor (chasing,
  extraction, client comms — all drafts); the module does the provable
  math; the credentialed preparer reviews, completes, and signs in the
  firm's professional tax engine — reachable via the **CCH Axcess and
  Thomson Reuters GoSystem connectors** (write seats confirm-gated and
  high-risk; GET-only low-risk read seats wired into the status packs).
  Never files anything.
- **Signed tax-constants channel** (`tax_constants.py`, `maverick tax
  update [--file|--status|--rollback]`; `[tax]` config) — new tax law
  ships as a **content release, not a code release**: Ed25519-signed
  constants bundles verified fail-closed against `[tax]
  trusted_constants_pubkeys` (no TOFU), sanity-validated before they can
  replace the tables (rates in range, ascending brackets, real state
  codes), downgrade-protected, applied atomically with the previous
  bundle kept for `--rollback`, and audited. With `auto_update` +
  `update_url` configured, `maverick tax prepare` picks up a published
  law change automatically (throttled); air-gapped firms apply the same
  bundle from a file. Every review package states the constants revision
  that computed it, and the `tax_law_watch` pack (web access, NO
  client-data access — the inverse seal of the rest of the suite)
  monitors IRS/state guidance and alerts the firm; agents never edit
  constants directly.
- **Institutional Memory attestation — is it really compounding, and really
  yours?** (`memory_plane.py`, `maverick memory-plane compounding|export|verify`)
  — the `attest` half of Bet 2's ingest/recall/attest API, riding the **same
  signed bundle spine as the Bet 1 attestation** (one implementation of the
  crypto, the out-of-band trust anchor, and the
  HOLDS/FAILS/INDETERMINATE/NOT_APPLICABLE vocabulary). Three claims:
  **cross_vendor** — how many vendors actually deposited through the governed
  plane, graded `NOT_APPLICABLE` below two because one vendor has demonstrated
  nothing, and `FAILS` outright if any ingest cannot be attributed to a
  `vendor:agent_id`; **compounding** — the cold-vs-warm cost and reliability
  curve *per department* (`compounding_by_department`), `INDETERMINATE` below
  the run floor because a short curve is noise, and an honest `FAILS` when the
  plane is running and has not yet paid off; **tenant_isolation** — deliberately
  `NOT_APPLICABLE`, because every record in a bundle is under that tenant's root
  by construction, so isolation is established by comparing two tenants' bundles
  rather than by reading one. Activity counts come from the **signed audit
  chain**, not the inbox directory: `ingest` routes a lesson into the reflexion
  store and only success/failure records into the inbox, so an inbox count would
  miss exactly the cross-vendor lessons the plane exists for.
- **Fleet memory — the Learning System of Record** (`fleet_memory.py`,
  opt-in `[fleet_memory] enable`; MCP tools `maverick_fleet_ingest` /
  `maverick_fleet_recall`; CLI `maverick fleet-memory`) — ANY external agent
  (Agentforce, Copilot, custom, OSS runtimes) deposits experience into and
  recalls from Lightwork's governed memory: roster-gated (fail-closed),
  Shield-scanned, provenance-tagged (`vendor:agent_id`), tenant-isolated,
  every read audited. Successes/failures consolidate through the dream
  cycle; `maverick proof --fleet` breaks value out per vendor.
- **The Operating Record** (`operating_record.py`, `maverick record
  stats|search|export|verify`) — the firm's decisions as a system of
  record: goals (with departments, outcomes, spend) + human approvals
  (with deciders) threaded into one queryable spine, exportable as an
  Ed25519-signed portable **capsule** (decision spine + learned state)
  verifiable offline; tampering fails verification.
- **Portable attestation — provable to somebody who trusts nobody**
  (`attestation.py`, `attestation_verify.py`, `maverick attest
  key|export|verify|export-verifier`) — the capsule proves *integrity* but is
  self-certifying: `verify_capsule` checks the signature with the public key
  stored inside the capsule, so it establishes that nobody edited the file and
  nothing about who signed it. An attestation bundle closes that gap. It binds
  three claims to evidence a third party can re-derive — every recorded action
  stayed inside the declared governance envelope; self-improvement never widened
  its own authority; the decision history is intact and complete (per-day content
  digests, chain tips, the cross-file anchor ledger) — and **verification
  requires the publisher's key obtained out of band**, failing closed without
  one. Each claim reports `HOLDS` / `FAILS` / `INDETERMINATE` /
  `NOT_APPLICABLE`, and the last two are never softened into the first: an empty
  governance policy forbids nothing, so "no violations" is reported as
  inapplicable rather than as a pass, and a sealed-at-rest day-file is
  `INDETERMINATE` because nobody outside the tenant can walk its chain. Two
  depths: *signed* (authentic, claims as the issuer recorded them) and
  *corroborated* (`--evidence <audit-dir>`, claims re-derived from the raw
  files). A day-file still open at issuance is committed as a **prefix**, so
  ordinary later activity does not invalidate a bundle while an edit to the
  attested rows still does. `maverick attest export-verifier` writes the
  verifier out as a standalone program that imports nothing from maverick and
  needs only Python plus `cryptography` — the "hand a regulator a USB stick"
  path.
- **Memory Guard — governed reads/writes (OWASP ASI06)** (`memory_guard.py`,
  opt-in `[memory_guard] enable` / `MAVERICK_MEMORY_GUARD`) — the screen
  between an agent and its memory store. Every fact write is stamped with
  **provenance** (`source` + a `TrustTier`: first-party / learned / tool /
  external + a sensitivity label) and low-trust writes are run through an
  **injection/poisoning tripwire** (and the Shield, fail-open) so smuggled
  instructions are quarantined, not stored. **Trust-aware retrieval**
  (`filter_facts`) keeps memory below `[memory_guard] min_recall_trust` out of
  the agent's standing brief, with a stricter gate available for memory
  consulted right before an irreversible action (`filter_facts(high_risk=True)`,
  which requires at least learned-loop trust). Every decision lands in the
  signed audit chain (`EventKind.MEMORY_GUARD`). Provenance columns are recorded
  even when the guard is off, so turning it on governs existing memory
  immediately.
- **Temporal memory — non-destructive fact evolution** (`fact_history` in
  `world_model.py`, opt-in `[memory] temporal` / `MAVERICK_TEMPORAL_MEMORY`) —
  a changed fact no longer overwrites the old value; the prior value is kept
  with the validity window it was believed in (`valid_from`..`valid_to`), so
  `world.get_fact(key, as_of=t)` and `world.fact_history(key)` answer "what did
  we believe on date X, and why" for the Operating Record. Bitemporal metadata
  is plaintext (queryable) while the value stays sealed at rest; sqlite-first.
- **Federated insight exchange** (`insight_exchange.py`,
  `maverick insights-export` / `insights-import`) — consolidated lessons
  (never raw trajectories) cross instance boundaries as Ed25519-signed
  bundles; imports are fail-closed against `[dreaming]
  trusted_insight_pubkeys`, Shield-scanned, provenance-tagged, and merged
  through the normal dedup gate. Transport is deliberately operator-managed
  (a file), never a network call. **Fleet aggregation:** a central
  `maverick dream --donations-dir` replays the whole fleet's donated
  trajectory records through the same consolidation.
- **Vector-store cross-run memory** — opt-in `[memory] backend` routes
  cross-run recall through a persistent **Chroma / Qdrant / Weaviate** store
  (`vector_store/`, `semantic_recall.py`) so similarity search is indexed and
  incremental instead of a linear re-embed scan; fail-open and
  dependency-injectable — the kernel never *requires* a vector store. The
  **Weaviate** adapter (`weaviate_store.py`, `[weaviate]` extra) targets a
  local or cloud v4 cluster with a server-side vectorizer (`near_text`). The
  **pgvector** adapter (`pgvector_store.py`, `[postgres]` extra) keeps vectors
  in the same Postgres database as the world-model backend (cosine `<=>`
  search); it takes an injected embedder rather than embedding itself, so
  recall reuses the local fastembed model.
- **Cognitive Data Engine — the causal improvement flywheel** (`data_engine.py`
  + `flywheel.py`, `maverick flywheel`, default-on `[data_engine] enable` /
  `MAVERICK_DATA_ENGINE`) — the Tesla-style data engine for a governed
  workforce: production failures are triaged by their **causal** impact on real
  outcomes (fix what moves reality most, not what's merely frequent), the worst
  actions are mined into **guardrails** (`negative_knowledge.py` — kept only
  when the causal harm is trustworthy and self-retired when it's gone),
  beneficial actions consolidate into **habits** (`procedural_memory.py`, a
  reinforce/forget curve), and the whole loop composes in one pass. It reads the
  trajectory store and can be disabled independently. Observable
  read-only at `GET /api/v1/flywheel` (mined guardrails + consolidated habits).
- **Causal estimators — the rigor under the flywheel** (`promotion_effect.py`,
  `counterfactual_rollout.py`) — effects are estimated, not guessed: a
  stratified/subclassification ATE with a confidence interval, a placebo
  refutation, and a `trustworthy` calibration gate (`estimate_effect`), plus a
  g-computation rollout over a learned tabular world-model
  (`estimate_effect_via_rollout`). An untrustworthy estimate never promotes a
  change. Validated to recover a known effect under confounding while rejecting
  the naive (confounded) contrast.
- **Operations Scientist — discover a better process and prove it**
  (`operations_scientist.py`, default-on `[operations_scientist] enable` /
  `MAVERICK_OPERATIONS_SCIENTIST`) — pairs a causally-harmful action with the
  beneficial habit that should replace it ("stop A; do B"), validates the swap
  in the world-model **before** spending a real experiment, and only a
  trustworthy positive lift is promoted to a live trial. Discovery, not just
  labour; it can be disabled independently.
- **Consequence Engine — reality is the reward** (`consequence.py`, `maverick
  record-outcome`, dashboard `POST /api/v1/outcomes`, default-on `[consequence]
  enable` / `MAVERICK_CONSEQUENCE`) — when a real downstream result lands (an
  invoice paid, a ticket reopened), it **overrides the verifier's self-graded
  proxy** so the flywheel learns from what actually happened, not from a model
  grading its own work. Newest outcome wins, clamped to [0,1]; when disabled the
  verifier proxy passes through unchanged. The HTTP entry point a CRM/ERP/ticketing
  connector calls once reality reports back.
- **Earned Autonomy — consequence-proven trust** (`earned_autonomy.py`,
  `maverick earned-autonomy`, opt-in `[earned_autonomy] enable` /
  `MAVERICK_EARNED_AUTONOMY`; graduation additionally requires arming
  `auto_graduate`, strict-parsed) — agents earn the right to act, action type
  by action type, by proving they predict consequences correctly. Each
  rehearsed high-stakes action that PROCEEDS pins a **consequence card**
  (hash-chained, append-only, multi-writer safe) with the predicted outcome
  BEFORE it runs; when reality reports back through the Consequence Engine,
  `reconcile` scores predicted-vs-actual **per action type** — the same unit
  the enforcement surface grants at, so authority is never wider than its
  evidence — with the acting agent kept on every event for provenance, and
  same-episode duplicates collapsed so one observed outcome grades one
  prediction. A proven streak (default 10 consecutive accurate, ≥90% overall)
  **graduates** the action from "a human approves" to "policy auto-approves"
  — implemented as a revocable standing consent-ledger grant the existing
  approval path already honours — and one miss by ANY agent **demotes
  instantly** (revoke-before-record, retried on every later miss plus a
  stale-grant sweep each reconcile, so authority never outruns evidence).
  Interlocks fail toward the human: no graduation while `calibration` is
  frozen or a learning HALT is active, `max_auto_risk` defaults to `medium`
  (graduating irreversible high-risk types is an explicit operator decision;
  the action's risk is recomputed live, never trusted from the card, and an
  unknown level ranks above every ceiling), and `require_reversible` demands
  every card declare an inverse. Disabling the feature stops evidence and
  demotion but not grants already minted — `maverick earned-autonomy --revoke
  ACTION` is the incident-response path and works while disabled. `run_saga`
  is the compensating half: steps ship their undo, an irreversible step
  refuses to start, and a mid-saga failure rolls the completed prefix back in
  reverse. **Shadow Mode** (`shadow_execute`) is the centerpiece that ties it
  together: a `ConsequencePreview` from a connector's `preview_write` ("this
  would move $240k, touch these records") is gated — auto-approved if the
  action type has *earned* it, otherwise routed to a human — then executed
  inside the compensating saga, with a consequence card pinned **only when the
  effect commits** (a denied, refused, or rolled-back action never becomes a
  mis-gradeable prediction), and the whole sim → approve → execute chain
  signed. Every card, hit/miss, graduation, demotion, and shadow execution is
  a signed audit event (`consequence_card`, `autonomy_graduation`,
  `shadow_execution`).
- **Connector consequence adapters — reversibility that is earned, not
  asserted** (`connector_previews.py`, `maverick connectors plan`, gated by
  `[governed_connectors] restore_points` / `MAVERICK_GOVERNED_RESTORE_POINTS`)
  — the governed REST connectors' `preview_write` returns a *sentence*
  ("would PATCH salesforce/… with fields ['Amount']"); Shadow Mode needs the
  dollars, the entities, and above all a real `undo`. `plan_write` supplies
  them, and enforces one rule: **an action is reversible only when we are
  holding the thing that inverts it.** A PATCH earns its undo by capturing the
  record's prior values for exactly the fields being changed; a POST earns its
  undo from the id the create response returns — but only when the path
  addresses a collection the dialect actually models, because the same verb
  drives RPC endpoints (`/actions/standard/emailSimple`) that no DELETE
  recalls, and appending an id to an unmodelled path guesses at another
  vendor's URL grammar. A full-replace PUT and a DELETE earn nothing, because
  replaying system fields fails and a recreated record has a new id and
  dangling references; a write path carrying a **query string** earns nothing
  either — for any verb, creates included, since a parameter like
  `sysparm_input_display_value` changes how the service reads the values being
  submitted (so replaying a raw prior writes something other than what was
  captured) and parameters like `sysparm_fields` reshape what the write
  *answers* with, which is the create's only evidence. Every gap fails closed to
  `undo=None`, which makes `run_saga` refuse **before any effect** and routes
  the action to a human. Per-service knowledge lives in a `RestDialect`
  (`SalesforceDialect`, `ServiceNowDialect`; an unknown connector gets a
  deliberately incapable generic one that earns nothing): which fields carry
  money, how a path names a record, which paths are create-into collections
  (matched **anchored**, since a suffix match would claim any prefix at all as
  this vendor's and earn a compensation aimed at a URL the dialect never
  modelled), what a record id must look like before the response's id is
  concatenated into a compensating DELETE address, and three facts that decide
  whether a restore can actually
  restore — **system-managed fields** (audit stamps, auto-numbers, formulas,
  roll-ups) whose priors read back perfectly and then silently fail to apply,
  so touching one forfeits the inverse; the **optimistic-concurrency token**
  (`LastModifiedDate` / `sys_mod_count`), which guards *both* windows a
  stranger's edit can land in — re-read immediately before the write, so an
  edit made while the card sat in front of a human aborts with no effect at
  all rather than being quietly reverted later, and re-read again before
  restoring, so an undo refuses rather than overwriting somebody else's edit.
  The token is necessary but not sufficient — Salesforce stamps
  `LastModifiedDate` only to the second, so a colleague saving inside the same
  second as our own write carries an identical token forever — so the undo
  also compares the **values** it is about to overwrite against the ones our
  write left behind. Third: the **raw-value capture** ServiceNow requires
  (display values are not writable back). The post-write snapshot that becomes the undo's reference is
  adopted only if it still carries the values we just wrote, field by field —
  a fallback GET can otherwise hand us a stranger's state and license the undo
  to overwrite it, while a stranger touching some *other* field is no reason
  to refuse, because the undo only ever replays ours. That comparison has to
  survive two shapes of the same value: the capture GET is *narrowed* (raw,
  dereferenced) but a write's echo is whatever the service volunteers, so a
  ServiceNow reference field is bare from the read and
  `{"link": …, "value": …}` from the echo — the dialect normalizes it, and an
  echo that still does not corroborate is treated as no echo at all rather than
  silently forfeiting the undo. The comparison is deliberately *not* lossy in
  the other direction: `"USD;100"` and `"EUR;100"` are different values, and
  reading them as equal would let the undo destroy an edit while reporting the
  field untouched. The approved params are
  snapshotted at planning time, so a caller mutating its dict while the card
  awaits approval cannot change the write that executes. Exposure is the
  *delta* where a prior was captured ($100k → $120k risks $20k) and the whole
  figure where it was not — overstating keeps a human in the loop,
  understating would not. `preview_write` keeps its no-network guarantee; the
  read-only GETs that earn and re-confirm the undo live in this separate seam
  and are operator-gated.
- **Emergent Substrate — an auditable coordination shorthand** (`emergent_protocol.py`
  + `emergent_tokens.py`, `maverick codebook` / `codec-learn` / `codec-probe`,
  opt-in `[emergent_protocol]` / `[emergent_codec]` / `MAVERICK_EMERGENT_CODEC`) —
  swarms coordinate in English, most of which is boilerplate they repeat. The
  codec learns short codes for that boilerplate from the swarm's *actual*
  messages, and — unlike opaque emergent languages — **every code decodes
  EXACTLY back to English** (`decode(encode(x)) == x`, fuzz-tested against
  adversarial content), so the Shield and a human always read plain text while
  agents move the compressed form. `maverick codec-probe` measures the real
  token (not just byte) savings with the target tokenizer; the token-aware codec
  measures **~28% token savings** on realistic coordination in benchmark. OFF by
  default and currently **measure-only** when wired into the live blackboard
  (`GET /api/v1/codec` reports what it would save on real traffic) — agents
  reading the codes to *realize* the savings is a separate, gated step.

- **Ekko — client-controlled work discovery** (`work_discovery.py`,
  `work_discovery_store.py`, `ekko_daemon.py`, `maverick ekko`, independently
  gated by default-off `[ekko]` / `MAVERICK_EKKO`) — turns repeated application
  transitions into evidence-backed automation candidates and review-only Agent
  Factory + Flow drafts. Enrollment is owner/device scoped, observation uses a
  positive application allowlist with a winning sensitive-app blocklist, and
  raw local events expire after at most 30 days. The bundled foreground daemon
  accepts an explicitly supplied observer; it never chooses an ambient OS
  monitor, requests endpoint permissions, starts a login service, or captures
  screen/window/clipboard/keystroke/document content. Pausing, revoking,
  erasing, saving a draft, and activating automation remain distinct
  human-controlled actions. Provider
  egress is reserved and unsupported in this release; it must remain false.
  See
  [`ekko-work-discovery.md`](./ekko-work-discovery.md).

- **Flow engine & visual automation** (`maverick.flow.*`, gated on `[flows]`) — a
  deterministic graph of typed nodes (`ir.py`: agent / action / branch / switch /
  foreach / while / parallel / approval / delay / wait_event / scope (try-catch) /
  subflow / setvar) run by a pure interpreter (`runner.py`)
  with data threading, safe expression rendering (`{{fn(...)}}` incl. arithmetic,
  list ops, `and`/`or` conditions with numeric-or-lexical ordering so date/string
  comparisons work; action params may additionally use `{{secret('NAME')}}` to
  pull a connector credential from the governed vault at run time so the raw
  value never enters the flow definition or persisted run data), a whole-flow wall-clock deadline
  (`Flow.max_seconds`), a per-run **aggregate agent-spend ceiling**
  (`Flow.max_dollars`, enforced in `execution.py` so a wide parallel / long
  foreach can't fan out unbounded cost), a per-flow **concurrency cap**
  (`Flow.max_concurrent`, a lock-guarded slot claim at dispatch that defers an
  overlapping run so a singleton "drain/sync" flow never double-fires its side
  effects), a per-flow **schedule timezone** (`Flow.timezone`, an IANA zone so
  `0 9 * * *` fires at 9am local and follows DST; `scheduler.next_run(tz=...)`),
  per-node retries with exponential **backoff** (`retry_backoff`, and a raising
  executor is retried too) / timeout / on-error routing, and **real intra-flow
  parallel concurrency** (parallel branches AND `foreach concurrent: true`
  iterations run on a bounded thread pool with per-branch cycle detection).
  `switch` routes n-way on a data key; `while` loops a body with a runaway cap;
  `setvar` computes flow-data keys from expressions (a loop counter, a composed
  field); `wait_event` pauses the run until an external event resumes it; `scope`
  is try/catch (a body failure routes to `on_error` with `{{_error}}` readable in
  the catch arm). A flow may declare **typed manual-run inputs** (`Flow.inputs`:
  `{key, type: text|number|bool|date, required, default}`) — the designer's Run
  button prompts for them and `POST /flows/{id}/run` validates + coerces them
  (`ir.coerce_inputs`, 400 on a missing-required or bad value), and that endpoint
  takes an optional `idempotency_key` so a double-submit dedups to one run.
  Reusable **sub-flows take mapped inputs** (`subflow_inputs`) to run isolated —
  the child sees only what's passed, can't clobber parent data, and returns via
  its `output` key. Approvals support **choice verdicts** beyond approve/reject
  (recorded to `output` for downstream routing) and an **expiry** (`timeout` — a
  lapsed approval resolves as rejected via the sweep, fail-closed). The live
  driver (`execution.py`) persists a resumable run (`store.py`) with per-node
  trace **including each work node's measured wall-clock seconds**, dry-run
  sandbox executors, and secret redaction (`redact.py`). A **self-rewrite loop**
  (`evolve.py` + `evolution_log.py`) swaps a node kind, measures grounded
  before/after outcomes, versions the definition, and autonomously reverts a
  regression. It grounds which tool a reliable agent node keeps calling
  (`node_tools.py`, sourced from the trajectory store) so a **hardening**
  proposal (agent → deterministic action) can name — and, under `[flows]
  auto_apply`, autonomously apply — the inferred tool instead of waiting for a
  human to pick one. Both autonomy knobs (`auto_evolve` revert, `auto_apply`
  forward) are toggled from the Learning page, deliberately kept outside the
  blanket learning control. `evolution_log.improvement_count()` reports the
  self-rewrites that stuck (reverted ones netted out) as the moat number. A failed run records WHICH node failed (`FlowRun.cursor`) and can
  **resume from the failed step** with the data as of the failure
  (`execution.execute(from_failure=True)`, `POST /flows/runs/{id}/retry?from_failure=1`)
  — pre-failure side effects are never re-driven. The server-rendered **visual
  designer** (`flow_designer*.js`, pure logic in `flow_designer_core.js`) drafts
  a flow from natural language, edits nested foreach bodies / parallel branches
  on their own canvas, shows live run + measured-impact overlays (💡 badges mark
  nodes with learned self-rewrite proposals), and carries the editor
  fundamentals: undo/redo (Ctrl+Z/Y), duplicate (Ctrl+D), copy/paste
  (Ctrl+C/V), click-to-cut edges, insert-a-node-into-a-selected-edge, 16px
  grid snapping, zoom-to-fit, client-side structural validation before save
  (`FDCore.validateFlow`), and **data pills** — one-click `{{key}}` insertion
  from the outputs a node can actually see upstream (`FDCore.upstreamOutputs`).
  The action picker searches the FULL live tool registry
  (`GET /flows/tools?q=`) and groups its ~3,300 connectors into coarse,
  browsable **categories** (Communication, Dev & Code, CRM, Finance, … +
  Other — `tool_categories.py`, keyword-classified) so a user can filter to one
  area instead of scrolling a flat list; `?category=` narrows the registry and
  `?q=`+`?category=` intersect, a **⚡ Triggers panel** shows everything that starts
  the flow (cron / webhooks / event triggers) in one place, a **gallery**
  (`GET /flows/gallery`) loads curated starter graphs onto the canvas, flows
  export/import as JSON files, and `?from_template=` opens any saved template
  as its single-agent flow (the text-builder → designer bridge). Shift-drag
  **marquee multi-select** moves/deletes node groups together, a corner
  **minimap** gives click-to-jump orientation, and a failed run surfaces a
  one-click **🔧 Fix it** that opens the copilot pre-armed with a
  diagnose-and-patch turn grounded in that run's trace. Live status streams
  over **SSE** (`GET /flows/runs/{id}/events`, slot-capped like the goal
  stream) with automatic fallback to backoff polling. A dedicated
  **run viewer** (`/flows/{flow_id}/runs/{run_id}`) shows the node-by-node
  timeline with per-step durations, redacted run data, and the recovery
  actions.

- **Flow copilot (chat that builds the flow)** — a conversational assistant
  docked in the designer (`maverick.flow.chat` + `POST /api/v1/flows/chat`).
  It edits a populated canvas via small **validated patches**
  (`maverick.flow.patch`: add/remove node, set field, rewire — applied
  copy-on-write and `Flow.validate()`d, so the model can never leave the canvas
  broken), drafts a full graph only on an empty canvas, answers "what does this
  flow do?", and — grounded in a real run trace via `run_id` — diagnoses "why
  did it fail?" and proposes the fix. Budget-capped per turn like the drafter;
  every applied edit is one designer undo step and a normal flow version.

- **Triggers for flows** — a flow fires from a cron schedule (`Flow.schedule`, a
  per-flow `flow_cron` job), any polled event source (`automation_events.py`:
  `http_json` / `rss` / `oauth_http_json` / `github_issues` / `imap_email` /
  `file_dir` / `form`), an **inbound webhook** (`/webhook/run` with a
  flow-target trigger — the signed payload's `data` becomes the run data and the
  delivery `id` doubles as the idempotency key, so a re-delivered event never
  fires twice), or a manual/dry run. A run paused on a **wait_event** node
  resumes from the same webhook (`{"resume": "<run_id>", "data": {...}}` — the
  event payload merges into the flow data). A paused **approval** node can post
  signed Approve/Reject links to a channel (`approvals.py` — a stateless HMAC
  token; `POST /flow/approve` resumes) so a human clears it without a dashboard
  session. A hosted **form** submission (`form_store.py`, public `POST /form/{token}`)
  fires a flow with the submitted fields as data.

- **Migration fidelity** (`automation_import.to_flow`) — a per-source
  `_STRUCTURAL_LOWERERS` registry (register a source in one line): n8n IF/Filter,
  Power Automate (WDL) `If` / `Foreach` / `Switch` / `Until` / `Scope`, Workato
  recipe `if` / `repeat`, UiPath exported activity trees
  (`Sequence` / `If` / `While` / `ForEach` / `TryCatch`), and Make (Integromat)
  `BasicRouter` routes are all captured **structurally** (branch/foreach/switch/
  while/scope nodes, arms wired to rejoin, bodies as nested flows, Until's exit
  condition negated, and a Make router lowered to parallel fan-out route flows
  guarded by its route filters); graphless sources approximate each step as an agent
  node that still runs. A UiPath Release/Schedule with no exposed activity tree
  stays a correct one-step *invocation* import — honestly reported
  `approximated` (the Orchestrator API returns no .xaml, so the process's
  internal control flow isn't visible), never a false `preserved`.
  `to_flow_with_report` returns the per-step fidelity log
  (`preserved` / `approximated` + why), and the dashboard import can save the
  graphs directly (`as_flows: true` on `POST /import/run`) with the fidelity
  summary rendered per automation — an import never silently pretends to be
  lossless.

- **Rich human tasks** — an `approval` node carries `choices` (verdicts beyond
  approve/reject, recorded to `output` for a downstream switch), an `assignee`,
  an `expires_after` expiry (its own field, not the execution `timeout`) that
  routes to an `on_expire` escalation lane on lapse (else fails closed), and a
  `form` of fields collected on sign-off (merged into flow data, filtered to the
  declared field names). The run viewer renders the choice buttons + form inputs;
  the expiry sweep resumes the run with an out-of-band `expired` flag (never a
  human-verdict string, so no resume caller can forge it) so expiry escalates via
  `on_expire` rather than only reject.

- **Data experience** — the `{{ }}` transform set spans text (`split` `join`
  `slice` `regex` …), data (`index` `keys` …), number (`abs` `int` …), and time
  (`now` `datefmt`). Real runs record per-output-key **schema hints** (types +
  field names, never values; `maverick.flow.schema_infer`, `GET /flows/{id}/schema`)
  so the designer's data pills offer nested keys (`order.total`). Branch/while
  conditions have a **structured builder** (key / op / value dropdowns over a
  data-key datalist) with a raw-text advanced mode for `and`/`or`.

- **Named connections** (`maverick.connections`, gated on `[connections]`) —
  store a connector's base URL + API token under a name, sealed at rest with
  the tenant KMS key like the OAuth vault; a connector resolves a saved
  connection as a fallback only when `<NAME>_TOKEN` is unset (env always wins).
  CRUD + a reachability `test` at `/api/v1/connections`; a Connections page
  wires a connector without shell access — the citizen-developer path.

- **Flow analytics** (`GET /flows/analytics`) — per-flow volume, success rate,
  failure count, duration p50/p95 (from per-node `seconds`), and top recent
  error messages, on a Flow Analytics page. The Workflows index unifies flow
  graphs alongside templates and playbooks (one surface, not two).

## Tools

286 built-in tool modules. Highlights by group (all under `tools/`):

- **Code & files** — `fs`, `str_edit`, `ast_edit` (tree-sitter), `apply_patch`
  (atomic multi-file), `repo_map`, `dep_graph`, `test_impact` (coverage-guided),
  `reviewer` (diff review), `file_watcher`, `notebook_exec` (run a .ipynb's code
  cells in the sandbox), `self_edit` (human-gated, path-confined edits to
  Lightwork's own code/config), `html_to_app` (scaffold a starter app from an HTML
  mockup).
- **Data** — `sql_query` (read-only by default), `pandas_query`, `spreadsheet`
  (CSV/XLSX, write-capable), `compute` (SymPy), `embeddings`.
- **Web & research** — `web_search` (Tavily/Brave/DDG/SerpAPI), `http_fetch`,
  `browser` (navigate/click/type/`fill_form`), `browser_device` (device-emulation
  presets), `browser_auth_vault` (Fernet-encrypted session store), `websocket`
  (ws/wss connect-send-recv), `dom_diff` (structural before/after HTML diff),
  `arxiv`, `semantic_scholar`, `wikipedia`, `hackernews`, `youtube`.
- **Media** — `view_image`, `view_video`, `pdf_reader`, `ocr`, `voice`
  (transcribe/speak — STT backends: OpenAI/Groq Whisper keys, local
  faster-whisper, and a **built-in offline path**: the pywhispercpp engine
  (whisper.cpp wheels, a maverick-dashboard dependency — dashboard installs
  transcribe out of the box) or a local whisper.cpp binary, both loading the
  checksum-pinned GGML model managed by `voice_models.py`
  (`maverick voice setup|status|transcribe`; auto-fetch ON by default and
  OFF under the egress lock, override via `[voice] auto_fetch_model`;
  deployment-wide routing via `[voice]
  stt_backend`, where `local` keeps audio on-machine even with keys set) —
  with **voice persona presets** (`[voice.personas]`:
  named backend+voice bundles selected per call) and **multi-language voice**
  (`[voice.languages]`: per-language voice map, BCP-47 prefix match); explicit
  args always win, unknown presets degrade to defaults — `voice_personas.py`), `ffmpeg_tool`, `imagemagick_tool`, `pandoc_tool`,
  `office_convert` (LibreOffice headless: the binary office formats pandoc
  can't take — Word/Excel/PowerPoint/OpenDocument → PDF/text/HTML/CSV, all
  sandbox-mediated with workdir-confined paths),
  `replicate_tool` (image/video/audio gen), `latex` (math→MathML + document→PDF),
  `diagram` (Graphviz / Mermaid render).
- **Robotics & hardware** — `ros` (drive a ROS stack over **rosbridge** via
  `roslibpy`, `[ros]` extra): publish a command to a topic (e.g. `/cmd_vel`) or
  call a service; auth `ROS_BRIDGE_URL`, no native ROS in the agent process;
  disabled by default and only registered when `[capabilities].ros = true`.
  `serial` (embedded device over **UART**/serial via `pyserial`, `[serial]`
  extra): list_ports / write / read / query a microcontroller or board, with a
  device-path guard so it can't be turned into an arbitrary-file opener.
- **Knowledge** — `knowledge_search` (per-domain RAG over collected docs),
  `recall`, `kv_memory`.
- **Productivity & SaaS connectors (~47)** — GitHub Actions, GitLab,
  Bitbucket (issues / PRs / pipelines), Jira, Linear,
  Asana, Trello, ClickUp, Confluence, Notion, Obsidian, Slack, Discord, Gmail,
  Google Drive, Dropbox, Calendar, Salesforce, HubSpot, Stripe, Shopify, Plaid,
  TrueLayer (EU/UK open banking),
  Twilio, Zoom, S3, DynamoDB, MongoDB, Redis, Elasticsearch, Datadog, Sentry,
  PagerDuty, Mixpanel, PostHog, Plausible, GA4, Home Assistant, Cloudflare,
  Vercel, AWS Lambda/SES/SNS, Microsoft Graph, and more — plus a long-tail of
  2,877 write-capable token-authed REST/GraphQL connectors
  (`enterprise_connectors.py`, built on `make_rest_tool`) covering nearly
  every category of enterprise and SMB software: CRM, ERP, ITSM, HRIS,
  security, data/BI/CMS, automation/iPaaS platforms (Zapier, n8n, Power
  Automate, Make, Workato), e-commerce, healthcare, real estate, legal,
  insurance, manufacturing, education, nonprofit/government, hospitality,
  media, telecom, no-code/AI, fintech/crypto, and region-specific SaaS.
- **Primary-source data connectors (37, read-only, low-risk)** — authoritative
  government and public data APIs that ground the analyst-style packs in
  primary sources instead of model memory: SEC EDGAR, FRED, U.S. Treasury,
  World Bank, IMF, FDIC, BEA, Census, BLS, EIA, Alpha Vantage, Finnhub,
  Polygon, OpenFIGI (finance/markets); Federal Register, eCFR, Regulations.gov,
  CourtListener, GovInfo, USAspending, SAM.gov, Open States, PatentsView
  (legal/regulatory/gov); GLEIF, OpenCorporates, UK Companies House (entity
  registries); openFDA, NPPES/NPI, ClinicalTrials.gov, RxNorm, PubMed (health);
  NWS, NOAA Climate, OpenWeather, EPA Envirofacts, Climatiq, Carbon Interface
  (weather/energy/ESG). All GET-only and confirm-free — they read public
  reference data, mutate nothing, and carry no tenant secrets. Most are keyless
  (a fixed public host, zero config); the rest take a free API key from env,
  delivered as a header or query param (never from the prompt). Built on three
  new `make_rest_tool` auth modes: `keyless`, `query_auth`, `default_base_url`.
  **Auto-wired by suite** (`SUITE_DATA_CONNECTORS`): each analyst pack is granted
  its suite's relevant sources in `domain_capability` (e.g. FDIC/FRED → banking,
  openFDA/NPI → healthcare, USAspending/SAM.gov → gov-contracting, EIA/EPA →
  utilities, NWS/NOAA → insurance & agriculture), so the agent reaches for the
  right primary source by default. Additive and deferred (no context cost); a
  host-restricted pack's egress is never silently widened. On by default with a
  kill-switch — `[workforce] data_grounding = false` /
  `MAVERICK_WORKFORCE_DATA_GROUNDING=off` (installer wizard step).
- **System** — `shell` (sandbox-mediated), `wasm_run` (**WASM sandbox**:
  execute a WebAssembly/WASI module under wasmtime — capability-grant
  isolation where the module sees ONLY the preopened dirs/env/args given;
  workdir-confined paths, validated env keys, sandbox-mediated invocation),
  `git_advanced`, `compute`,
  `dns_lookup`, `openapi_runner`, `clipboard`, `notify`, `attachments`,
  `android` / `ios_sim`, `a11y`, `task_graph` (persistent dependency-DAG of
  tasks — add/status/ready/order/list plus **`critical`**: the longest
  dependency chain, weighted by optional per-task cost, that bounds completion
  no matter the parallelism — the tasks a critical-path-aware scheduler runs
  first), `workspace_snapshot` (snapshot/restore a working dir), **attachments
  of all kinds** (`attachments.py`: text, images, audio, video, PDFs, and
  Office/OpenDocument/epub files — document ZIP containers are structurally
  sniffed past the executable/archive magic-byte deny, which no setting
  bypasses; extend the mime allowlist with `[attachments]
  extra_mime_prefixes` or accept any declared type with `allow_any_mime`;
  images auto-embed as vision blocks and PDFs as native document blocks
  budgeted by the driving model's context window, `[attachments]
  embed_documents` to opt out), **attachment understanding**
  (`generate_companions`: incoming audio/video auto-transcribes via the
  kernel STT backends and Office/OpenDocument/RTF files auto-extract text
  via the knowledge parsers into `<name>.transcript.txt` /
  `<name>.extracted.txt` companion attachments, which embed as text blocks
  on the first message under a window-scaled budget — user text files stay
  tool-reachable only; off via `[attachments] transcribe_media` /
  `extract_text`), **channel inbound attachments** (files shared over
  email/Telegram/Slack download in the adapter — auth first — travel as
  `IncomingMessage.attachments`, and the server stores them as goal
  attachments under the same validation as a dashboard upload, then runs
  companion generation; attachment-only messages get a "Process the
  attached file(s)." brief), **attachments panel** (the goal page lists a
  goal's files with companion labels, uploads more via `POST
  /api/v1/goals/{id}/attachments`, and downloads each via `GET
  .../attachments/{aid}/download` — always `Content-Disposition:
  attachment` + `nosniff`, so a crafted HTML/SVG upload can't execute in
  the dashboard origin; the chat composer also takes drag-and-drop and
  pasted files), **S3-backed
  attachments** (`[attachments] s3_bucket`: mirror every stored attachment to
  any S3-compatible bucket + `s3_fetch` pulls it down on another worker host;
  local disk stays the source tools read, mirror is fail-open), `license_scan`
  (classify deps + flag copyleft), `self_capability` (report the run's capability
  grant), `oidc` (OIDC authorization-code client), `oauth_helper` (generic OAuth2 for
  any provider — PKCE authorize URL / code exchange / refresh; token responses
  summarised with a sha fingerprint and never echoed into context; full tokens
  seal into the per-tenant OAuth vault — `oauth_vault.py`, AES-256-GCM under the
  tenant DEK, refresh-aware via a caller-supplied refresher — when `[oauth] vault`
  is on, else a 0600 MAVERICK_OAUTH_OUT file; the Automations page's **Connected
  accounts** panel lists each provider's token-free health (expiry, granted
  scope, auto-refresh) and a **Test** action that forces a live refresh through
  the preset's token endpoint so a stale connection surfaces before a flow hits
  it — `oauth_vault.status()` and `POST /oauth/{provider}/test`), `cost_curve` (per-provider cost
  model), `bench_track` (record benchmark scores + flag regressions), `teams`
  (Microsoft Teams webhook), `knowledge_graph` (extract/query/render
  subject-relation-object triples; no external graph DB), `cross_repo_deps`
  (cross-repo Python package import graph + cycle detection via `ast`),
  `citation_verifier` (check cited quotes against their source text), `anki`
  (flashcards via the local AnkiConnect add-on — decks/models/find/add_note/
  sync, writes gated by confirm, loopback-only by default), `test_gen`
  (generate a Hypothesis property-test scaffold from a function's signature),
  `semantic_code_search` (rank functions/classes by intent via ast + lexical
  scoring), `lsp_bridge` (cross-language code intelligence over the Language
  Server Protocol — symbols/definition/references/hover/diagnostics against
  host-installed servers: pyright/gopls/rust-analyzer/tsserver/clangd;
  one-shot session per call, deadline-gated stdio), `mutation_test` (plan source mutants a strong suite should catch),
  `constrained_output` (validate/coerce a value to a typed/enum/range/regex
  shape — the guard half of constrained generation), `model3d_inspect`
  (headless 3D-mesh stats — triangle/vertex counts + bounding box for STL/OBJ),
  `synthetic_data` (deterministic synthetic rows from a field spec, json/csv),
  `web_recorder` (generate a runnable Playwright script from a list of browser
  steps — deterministic codegen with escaped literals), `web_archive` (save a
  URL's content locally so research stays reproducible — SSRF-pinned per-hop
  redirect revalidation, 5 MiB cap, sha-dated snapshot ids, list/get over the
  archive), `github_search` (GitHub repos/code/issues search — explicit token
  only, clamped pages, readable rate-limit errors with Retry-After), `a11y_tree` (distill raw
  HTML into a compact accessibility tree — landmarks/headings/links/controls —
  for a 5-10x token cut), `cache_admin` (inspect/purge the
  tool-output cache — stats or targeted purge), `error_patterns` (cluster noisy
  error/log lines into ranked patterns by normalised signature), `container_build` (build a
  container image from a Dockerfile via sandbox-mediated `docker build`), `ai_act_classifier` (EU AI Act
  risk-tier screening for a described AI use-case — prohibited/high/limited/
  minimal + obligations; heuristic, not legal advice), `geofence` (region
  allow/deny policy check — ISO codes or groups EU/EEA/FIVE_EYES, deny-precedence), `two_person_rule` (validate
  dual-control sign-off — distinct approvers, separation of duties, optional roles), `differential_privacy` (Laplace
  mechanism for (epsilon)-DP noisy counts/sums on published stats), `watermark_detector` (find hidden
  text watermarks/steganography — zero-width, tag chars, variation selectors, homoglyphs), `privacy_budget` (stateless differential-privacy budget calculator — remaining epsilon +
  fit/deny estimate from trusted cumulative spend; not an enforcement ledger),
  `collusion_detector` (flag collusion between independent swarm agents —
  op=scan: echoed reasoning + rubber-stamping in messages; op=detect:
  voting-collusion blocs whose agreement defeats independent-quorum
  guarantees), `coordinated_disclosure` (run a CVD process offline —
  op=status over a record set flags EMBARGOED/DUE_SOON/OVERDUE/PATCHED/
  DISCLOSED per report with per-severity policy, or checks one report's
  OPEN/EXPIRED window; op=advisory renders the advisory block),
  `capability_delegation` (validate a
  delegation graph for privilege escalation — fixpoint from root capabilities),
  `capability_delegation_graph`
  (static analysis over capability delegations — cycles, privilege escalation,
  transitive holders), `agent_identity` (per-agent stable id + HMAC sign/verify),
  `risk_tier` (score an agent goal LOW/MEDIUM/HIGH from operational signals —
  shell/secrets/PII/spend/irreversibility — for gating), `bias_eval` (group-
  fairness metrics — four-fifths rule, demographic-parity and equal-opportunity
  differences from per-group outcome counts), `decision_explainer` (per-factor
  contribution breakdown for an additive/scorecard decision — right-to-explanation),
  `governance_explainer` (explain a governance ALLOW/DENY/REQUIRE_HUMAN decision —
  the rule that fired + plain reason + the counterfactual that would change it;
  GDPR Art. 22 / AI Act Art. 14, re-runs the real policy evaluator),
  `voice_command_grammar` (match a transcribed utterance to an intent + slots
  from a {slot}-template grammar — no model round-trip for high-frequency
  commands), `what_changed_digest` (added/removed/changed digest between two
  snapshots, optional signed numeric deltas), `gui_element_memory` (offline
  store of GUI element locators keyed by app/screen/name for computer-use),
  `adversarial_eval` (score a red-team batch — confusion matrix, recall/
  precision, and the missed-attack list that gates red-team CI),
  `trace_compare` (diff two replay traces step by step — first divergence,
  matched prefix, per-step field diffs), `latency_heatmap` (tool × latency-band
  shaded grid + p50/p95 per tool), `tool_call_inspector` (per-tool call count,
  error rate, avg/max latency, HIGH-ERROR flags from a tool-call log),
  `rectification` (validate/apply GDPR Art. 16 field corrections under a
  mutability policy — auditable diff + corrected record), `anomaly_scan` (flag
  cross-run metric outliers via the robust median/MAD modified z-score),
  `k_anonymity` (check a released dataset for k-anonymity + optional l-diversity
  — quasi-identifier group sizes and sensitive-value diversity), `retention_check`
  (audit records against a data-retention policy — flag over-retained and
  no-policy records by category/age; GDPR storage limitation), `redact`
  (**provable redaction**, `provable_redaction.py`: redact secrets/PII to a
  fixpoint then re-scan to *prove* the output carries none — composes the
  secret + PII detectors, and reports the residual gap instead of a false
  guarantee when a bound is hit), `breach_notification`
  (GDPR Art. 33/34 72h breach-notification timer — DUE/OVERDUE/ON_TIME/LATE +
  Art. 34 high-risk reminder), `data_minimization` (flag fields collected beyond
  a purpose's allowlist + missing required fields; GDPR Art. 5(1)(c)),
  `consent_check` (evaluate consent records for active validity — granted /
  withdrawn / expired per purpose, latest grant governs; GDPR Art. 7),
  `kv_cache_offload` (LRU KV-cache keep/offload plan under a memory budget),
  `otel_semconv` (map span attributes to OpenTelemetry semantic-convention
  keys), `payload_compress` (zlib compress/round-trip ratio helper),
  `compaction_classifier` (rule-based compaction-strategy picker),
  `capability_revocation` (transitive revocation over a delegation graph),
  `memory_safe_parse` (size/depth/item-bounded JSON/CSV parse that never raises
  on hostile input), `misuse_removal` (remove flagged leaderboard entries +
  tombstones), `consent_ergonomics` (minimal plain-language consent prompt +
  risk badge), `skill_distill_v2` (extract a reusable skill spec from a
  successful trace), `observation_channel` (merge multi-agent observations into
  a time-ordered feed), `marketplace_moderation` (APPROVE/REVIEW/REJECT listing
  scan), `channel_autoroute` (pick the best channel for a message by rules),
  `jwt_inspect` (decode + validate a JWT offline — claims, exp/nbf, and
  HS256/384/512 HMAC signature verification; flags alg=none), `rbac_check`
  (evaluate an RBAC authorization decision — role inheritance + '*'/'prefix:*'
  wildcards, ALLOW/DENY with the granting role), `cidr_check` (firewall-style
  ordered CIDR access-control for an IPv4/IPv6 address — first match wins),
  `semver_check` (does a semver version satisfy a constraint — comparator sets,
  caret/tilde ranges, prerelease ordering, and prerelease exclusion at
  final-release upper bounds unless explicitly named).
- **Extensibility** — `@tool` decorator (`tools/decorator.py`): turn a typed
  function into a registered Tool with a signature-derived JSON Schema, no
  boilerplate. **Language-neutral subprocess plugins** use the versioned NDJSON stdio protocol
  (`maverick-plugin/1`: `--describe` manifest, `{id,tool,args}` →
  `{id,result|error}`); the host (`ts_plugin_host.py`, `[plugins] ts =
  [["node", "/path/plugin.js"]]`, wizard step included) loads the manifest
  into regular Tools with a persistent scrubbed-env child, per-call timeout,
  one crash-restart, and the no-shadowing rule built-ins enjoy. A **gRPC
  plugin host** (`grpc_plugin_host.py`, proto `grpc_api/plugin_host.proto`,
  `[plugins] grpc = [{target, command}]`) carries the same contract over gRPC
  for any language: Describe → Tools, Call with a deadline, scrubbed-env spawn,
  reconnect/respawn-once.
  **Retrospective generators (time-gated runs)** — the 2-/36-month
  retrospectives ship as period generators the operator runs at the mark:
  `safety_report` (safety), `benchmark_retrospective` (perf), and
  **`ux_retrospective.py`** (`python -m maverick.ux_retrospective`): goal
  volume/outcomes, top task verbs, channel mix, approval friction over a
  window, plus a **reset worksheet** whose questions are answered from the
  data rows (zero-use surfaces to cut, friction concentrations, dominant
  verbs); empty sections say so.
  **AI Act conformance package** (`ai_act_package.py`, `python -m
  maverick.ai_act_package [-o out.md]`): assembles the Art. 11 / Annex IV
  technical-documentation skeleton from the deployment's *recorded* posture —
  the Annex III risk self-assessment, Art. 14 oversight measures (consent
  mode, capability enforcement, delegation, killswitch), Art. 12 logging
  (audit signing, retention, day-files present), Art. 15 evidence (red-team
  gate, shield calibration, reliability cert when present), Art. 50
  transparency wiring — sections without evidence say so, and the items only a
  provider can complete (intended purpose, conformity route) are an explicit
  checklist, not fabricated prose.
  **Adversarial-prompt corpus release** (`maverick_shield/corpus_release.py`,
  `python -m maverick_shield.corpus_release`): turns the CI red-team corpus
  into a versioned, validated, integrity-pinned artifact — content-hash
  version, SHA-256 over canonical rows, license + intended-use ("NOT a
  training set for attack generation"), and a provenance gate that REFUSES a
  release containing secret-shaped content or identity PII (fixture IPs in
  attack samples are allowed and disclosed); writes corpus + MANIFEST +
  README.
  **Security backports + LTS machinery**
  ([`docs/security-backports.md`](security-backports.md) +
  `backport_tool.py`, `python -m maverick.backport_tool scan|plan|check`):
  the policy (what qualifies, the `lts/<v>` 2-year safety-fix branch, 7-day
  SLA) made executable — `scan` finds security-marked commits, `plan` lists
  the ones not yet on the LTS branch (patch-id matched, so a cherry-picked
  twin isn't re-flagged), and `check` exits non-zero when an eligible fix is
  past the SLA — read-only; cherry-picks/pushes stay maintainer acts.
  **Formal verification of the sandbox interface (TLA+)**
  ([`docs/specs/tla/`](specs/tla/README.md)): `SandboxInterface.tla`
  models the chokepoint as a state machine and TLC-verifies — for all
  interleavings — no silent downgrade to host exec under a container backend,
  scrubbed child env always, refused-never-ran, bounded execution budget, and
  every command eventually terminal (checking the liveness property surfaced a
  real modelling subtlety: dispatch fairness, now explicit). Verified: 982
  states, no errors; reproduction steps in the README.
  **Sigstore keyless signing** (`sigstore_signing.py`, `[sigstore]` extra,
  `python -m maverick.sigstore_signing sign|verify`): sign skill/plugin
  artifacts with sigstore's keyless flow (OIDC identity) into a
  `.sigstore.json` bundle; verification pins the identity+issuer pair and
  fails CLOSED on a missing bundle, wrong identity, or absent install —
  identity-based signing alongside the key-based skill signing and the
  self-hosted plugin CA.
  **Federated shield rule updates** (`shield_updates.py`, opt-in `[shield]
  federated_updates` + `update_url`/`update_pubkey`, wizard step included):
  pull-based publisher-signed rules bundles (Ed25519 over canonical JSON);
  unsigned, mis-signed, tampered, downgraded, or un-anchored bundles are
  refused, and a verified bundle stages `shield_rules.json` (0600) atomically —
  the kernel never imports the shield itself (rule 1).
  **Shield decode/defang pre-pass** (`maverick_shield/deobfuscate.py`): on every
  scan surface — input, tool-call arguments, and tool output — the shield decodes
  base64/hex/percent encodings and folds Unicode homoglyphs (Cyrillic/Greek
  lookalikes NFKC leaves alone), then re-scans every de-obfuscated variant — so an
  encoded `rm -rf /` is blocked even though the literal surface form hid it.
  Monotonic (only upgrades an allowed verdict to a block), bounded against decode
  bombs, and fail-open; escape hatch `MAVERICK_SHIELD_NO_DECODE=1`.
  **Trained cheap-probe seam + offline trainer** (`probe_model.probe_features`
  n-gram extension + `maverick_shield/probe_train.py`, `python -m
  maverick_shield.probe_train --corpus … --out model.json`): `probe_features`
  now additively emits deterministic hashed char n-gram features (`ng:<bucket>`,
  blake2b — stable across processes) when `ngram_buckets > 0`, so a model can
  carry lexical content the 7 hand-named signals can't; `ngram_buckets = 0` keeps
  the original contract, so every existing artifact is byte-for-byte unaffected.
  The pure-stdlib trainer (no numpy/sklearn) fits an L2 logistic regression over
  the SAME extractor inference uses (train/serve parity), selects a threshold at a
  configurable benign false-positive ceiling (`--max-fp`, default 1%), and exports
  the `{bias, weights, threshold, ngram_buckets, ngram_sizes}` JSON the shield
  already loads. Still OFF by default and MAX-ensembled with the heuristic (can
  only raise recall). Recommended datasets/model and the ship-gate are in
  [`docs/research/shield-model-recommendation.md`](research/shield-model-recommendation.md).
  **Annual safety report generator** (`safety_report.py`, `python -m
  maverick.safety_report --since --until`): aggregates what the deployment
  actually recorded — shield blocks, capability denials, killswitch
  activations, consent decisions, erasure requests, red-team/calibration
  results when present — into a markdown report with explicit reporting-period
  and data-available sections; empty sections say so, nothing fabricated.
  **eBPF syscall monitor** (`ebpf_monitor.py`, opt-in `[ebpf_monitor] enable`,
  wizard step included, `python -m maverick.ebpf_monitor program|run`):
  generates a bpftrace program tracing execve/connect/openat for the agent's
  PID tree with a validated suspicious-syscall watchlist, supervises it via an
  injected runner, parses events, and alerts on watchlist hits — generator/
  parser/supervisor fully offline-tested; the live attach needs root +
  bpftrace and refuses politely otherwise.
  **Memory-safe parsing of untrusted bytes** (`parser_isolation.py`, opt-in
  `[security] isolate_parsers`): the parsers fed attacker-controllable bytes
  (PDF via pdfplumber/pypdf, images via Pillow) are C-extension-backed — a
  memory-safety bug there is an in-process foothold. The whitelisted-parser
  inventory (`PARSERS`, the policy in code) routes them through a
  secret-scrubbed child process: a segfault on hostile bytes kills the child,
  never the kernel, and an exploited child holds no provider keys; size caps
  enforced before the child sees data, hard timeout, and on child death the
  consumer REFUSES rather than re-parsing the same bytes in-process (wired
  into `read_pdf`). Off by default — in-process behavior unchanged.
  **Plugin signing CA** (`plugin_ca.py`): a self-hostable Ed25519 certificate
  authority for plugin/skill artifacts — the in-house counterpart to sigstore's
  keyless flow. An org runs its own root (`init_root`, keys 0600 under
  `keys/plugin_ca/`), issues publisher certs (`issue`, expiring, serial'd),
  maintains a CA-signed revocation list, and every install verifies the
  two-link chain offline (artifact sig → publisher key; publisher cert → root)
  **fail-closed**: tampered artifact/cert, wrong root, expired or revoked cert,
  or a missing piece all refuse; an unverifiable CRL never silently
  un-revokes.
  **Plugin compatibility matrix** (`plugin_matrix.py`, `python -m
  maverick.plugin_matrix [--ci]`, wired as a CI lint step): one table per
  installed entry point — dist, declared API major, loadable/deprecated/
  refused, allowlisted, permissions granted — with a CI gate that fails when
  any *enabled* plugin is API-incompatible, so an upgrade dropping an API
  major can't ship silently against plugins still pinned to it. Pure
  inspection (nothing imported or executed).
  **Plugin API v2 (released)** — `MAVERICK_API_VERSION = "2"` with
  `SUPPORTED_API_MAJORS = (1, 2)`: v1 plugins keep loading through a
  deprecation window (warned in manifest validation), declared v3+ is
  refused; release notes in [`docs/plugin-api-v2.md`](./plugin-api-v2.md)
  (structured channel `Reply`, enforced manifest permissions, lockfile,
  isolation, TS plugins).
  **Plugin sandboxing** — opt-in
  `[plugins] isolation = "subprocess" | "subinterpreter"`
  (`plugin_isolation.py`): discovered plugin tools keep their schema but their
  *calls* run in a fresh CPython subinterpreter (fault/state isolation — a
  plugin that pollutes globals or leaks can't touch the host) or a
  secret-scrubbed child process (stronger: separate address space, survives a
  segfaulting plugin, no host env secrets); values pass by baked literals,
  never argv. **Plugin telemetry (opt-in, local-only)** —
  `[plugins] telemetry = true` counts plugin-tool invocations to a local JSON
  tally (nothing leaves the machine); `maverick plugin stats` shows
  calls/last-used per tool for allowlist pruning; composes with isolation so
  isolated calls count too. **Plugin version-pinning lockfile** —
  `maverick plugin lock` records each plugin distribution's version to
  `plugins.lock.json`; discovery verifies against it per `[plugins]
  lock_policy = "off"|"warn"|"enforce"` (`plugin_lock.py`: enforce refuses a
  drifted or unpinned dist — that plugin only — warn logs once per dist;
  `maverick plugin verify` reports drift/missing/unpinned and exits 1 on
  failure). **Hot plugin reload** — `maverick plugin reload <dist>`
  (`plugins.reload_plugin`): drop a plugin distribution's modules from the
  import cache so the next discovery pass re-imports the current code on disk;
  the edit-reload-retry loop for plugin authors, same allowlist/permission
  gates on re-import.

## Channels

17 wired channels (`packages/maverick-channels/`): Telegram, Discord, Slack,
Signal, Email, Matrix, Bluesky, Mastodon, Voice (Twilio), WhatsApp (Twilio),
**WhatsApp Cloud API** (`whatsapp_cloud.py`: Meta's first-party Graph API —
GET verification handshake, constant-time `X-Hub-Signature-256` HMAC,
sender allowlist, atomic message-id dedup claim, chunked outbound; no Twilio
middleman), SMS, iMessage
(macOS), **IRC** (channels + DMs, TLS), **Threads** (`threads.py`: Meta's
Threads API — polling adapter by design since webhooks are partner-gated;
author allowlist, claim-first dedup that fails CLOSED because a polling
adapter re-sees replies, two-step publish with 500-char chunking),
**RCS** (`rcs.py`: Google RCS Business Messaging for approved RBM agents —
Pub/Sub or direct envelopes, constant-time clientToken verify, MSISDN
allowlist, service-account Bearer auth with cached refresh), and a
**glasses/wearable** adapter
(Even Realities G2 "bring your own agent" bridge: the ack-then-run pattern that
answers quick utterances on the HUD within the device deadline and runs long
tasks in the background, delivering the result to a secondary channel). Rich
formatting + dedup + per-channel authz. **Reply threading** — inbound messages
carry their platform `message_id` and adapters expose `send_threaded`
(Slack `thread_ts` behind opt-in `[channels.slack] thread_replies`; Telegram
`reply_to_message_id`; base falls back to a plain send) so long-running
answers land under the message that asked. **Email v2** adds IMAP IDLE (push
instead of poll) + conversation threading from Message-ID/In-Reply-To/References
(`email_v2.py`). **Discord Stages voice v2** (`discord_stages.py`): drive Lightwork from a
Stage channel — per-speaker utterance assembly over an injected transcriber,
the same `DISCORD_ALLOWED_USER_IDS` speaker allowlist as Discord text,
optional wake-word gating, replies spoken when the bot holds a speaker slot
and degraded to stage-chat text when it doesn't (or TTS fails), and stage
etiquette built in: the bot only *requests* a speaker slot, never
self-promotes (a human moderator approves). Every Discord interaction sits
behind an injected seam so the session logic is fully offline-tested; the
heavy voice binding (discord.py voice + PyNaCl) plugs into the same seam.
**KaTeX/Mermaid rich render** (`rich_render.py`, opt-in
`[channels] rich_render`): replies carrying display math or ```mermaid fences
are rendered into a standalone HTML artifact (KaTeX/Mermaid in-page, escaped
`<pre>` source as the no-JS fallback) under `data_dir("rich_render/")`;
`RichRenderChannel` wraps any adapter — an injectable `deliver` hook ships the
file on platforms that can, otherwise the path is appended — and plain
messages pass through byte-identical. **Channel SDK v2** (RFC 0001 C2,
`base.py`): handlers may return a structured `Reply` (text + attachments +
thread_ref) instead of bare `str` — `as_reply` is the v1 shim (bare `str`
accepted through the deprecation window), `Channel.dispatch`/`dispatch_text`
normalize either contract, and all 18 in-tree adapters route through the
dispatch path so a v2 handler works everywhere unchanged.

## Sandboxes

8 run-to-completion backends (`sandbox/`): local subprocess, Docker, gVisor, SSH,
Podman, devcontainer, Kubernetes, and Modal cloud (below), plus an **experimental
Firecracker microVM scaffold** (the exec path does not yet mount the workspace).
Selected via `[sandbox] backend`.
**Modal sandbox backend** (`sandbox/modal_backend.py`, `[sandbox] backend =
"modal"`, `[modal]` extra): run agent shell in ephemeral Modal cloud sandboxes
(per-exec container, image/cpu/memory/timeout plumbed, torn down after the
command) — burstable remote compute without running a cluster; infra errors
surface as a failed command, never a kernel crash. The Cloudflare-Workers half
of the roadmap pair was declined for shell semantics (Workers run JS/WASM
request handlers, not processes; the honest Workers story is the self-hosted
relay reference + `wasm_run`).
**Sandbox SDK v2** (`sandbox/sdk.py`, `SDK_VERSION = 2`): the formal backend
contract — a `runtime_checkable` `SandboxV2` protocol (`workdir` +
`exec(cmd, timeout=None)`), declared optional capabilities
(`capabilities()`), a static `conformance()` checker, and **entry-point
loading** (`[sandbox] backend = "ep:<name>"` resolves the
`maverick.sandboxes` group, instantiates with `[sandbox] options`, and
refuses a non-conformant backend rather than falling through to unsandboxed
local exec) — so third parties ship backends without forking. All in-tree
backends conform (the check surfaced and fixed a real gap: devcontainer
lacked `workdir`, crashing path-confined tools).
**gVisor** is offered as a backend (`backend = "gvisor"`): Docker with the
`runsc` runtime (`--runtime=runsc`), interposing a userspace application kernel
between a possibly prompt-injected agent and the host — stronger isolation than
seccomp + dropped capabilities alone. It reuses every Docker knob (image,
network, memory/pids/cpu caps, non-root); `[sandbox] runtime` may select only
an approved runsc registration whose Docker metadata declares an exact runsc
`path`/`runtimeArgs` or the official `io.containerd.runsc.v1` runtime type.
Ambiguous aliases and literal `runc` fail closed in construction, health, and
diagnosis. This catches misconfiguration; it does not cryptographically attest
the runtime binary, so the Docker daemon administrator remains a trust boundary.
**Warm-container reuse** (`[sandbox] reuse_container`, default off): instead
of a fresh `docker run --rm` per command (a cold start each time), keep one
container alive and `docker exec` into it, so the 2nd..Nth command in a run
skip container startup; torn down on `close()`.

## LLM providers & routing

13 providers, routable per role (`llm.py`): Anthropic, OpenAI, OpenRouter,
Ollama, Gemini, DeepSeek, Bedrock, Azure, xAI, Moonshot, TGI, vLLM (generic
OpenAI-compatible via `base_url`), and **Codex CLI**
(`providers/codex_cli_provider.py`: ChatGPT/Codex *subscription* via a local
`codex exec` subprocess — auth from `CODEX_ACCESS_TOKEN` or `codex login`,
token fed on STDIN only; tool calls emulated via a fenced-JSON protocol;
spend is subscription-metered so `codex_cli:` ids price $0 while token /
wall-clock / tool-call budget caps still enforce). Cost-aware routing (`cost_router.py`) with **per-role
policies** (`[routing.roles.<role>]`: provider allow/deny, cost ceiling, tier
floor) and provider failover (`provider_failover.py`) with a **policy engine**
(`failover_policy.py`: error-class gating — auth fails fast, 429/timeout/5xx
fail over — plus per-model cooldowns), all opt-in. **Local-first routing**
prefers a reachable local model before remote (`provider_local_first.py`);
**energy-aware routing** downgrades to a cheaper model on low battery
(`energy_aware_router.py`); both opt-in and default-OFF. **Cost-aware routing
v3** (`cost_router_v3.py`) layers a contextual **epsilon-greedy bandit** on top
of v2: it learns reward-per-dollar per coarse task class (role + tier) and
reorders *only within* the healthy/affordable arm set v2 already produced —
never routing somewhere v2 rejected, falling back to v2 on a cold context. The
learned table persists atomically (`router_bandit.json`, 0600); opt-in via
`[routing] bandit` and default-OFF.

**Public perf dashboard** (`GET /perf` + `GET /api/v1/perf` on the
dashboard): one page/JSON face for the perf story — the perf-SLA checks
measured live on the host (in a worker thread, against the published
thresholds), recorded benchmark history with short-window regression
verdicts, and the longitudinal era retrospective; sections with no recorded
data say so. **Longitudinal benchmark retrospective** (`benchmark_retrospective.py`,
`python -m maverick.benchmark_retrospective`): the multi-year companion to
continuous benchmarking — slices the FULL recorded score history into calendar-
quarter eras and reports per-era medians, era-over-era movement, best/worst
eras, net first→last change, and a least-squares trend verdict per benchmark;
the report states its actual coverage span (the intended cadence is the 3-year
mark, run over whatever the deployment recorded). **Public performance SLA** ([`docs/perf-sla.md`](./perf-sla.md) +
`perf_sla.py`, `python -m maverick.perf_sla --ci`): the published, measurable
performance properties each release certifies — tool-dispatch overhead,
compaction latency, world-model hot-path read/write p95 — measured against the
REAL code paths and compared to the published thresholds (changing a threshold
is changing the SLA); rows that need concurrency/fault drills delegate to the
reliability cert. **Reliability certification** (`reliability_cert.py`,
`python -m maverick.reliability_cert`): a reproducible, evidence-backed
self-certification composing the shipped drills — chaos game-day, the plugin
reliability drill, a 16-writer WAL contention probe — into a certificate JSON
(environment fingerprint + per-check verdicts), Ed25519-signed with the audit
key when available and only issued for a passing run.
**Deprecation registry + sunset gate** (`deprecations.py`, `python -m
maverick.deprecations [--ci]`, wired into CI): every deprecated path is
declared in one registry (target, replacement, deprecated_in, **remove_in**);
`warn_once` gives call sites a once-per-process DeprecationWarning,
`check_config` lints a loaded config for deprecated keys, and the **sunset
gate** fails CI once the package version reaches an entry's removal version
until the old path and its registry entry are deleted together — so
deprecations can't rot. Seeded with the two live windows (plugin API v1
manifests; bare-`str` channel handlers). **Cache-aware prompt assembly DSL** (`prompt_dsl.py`): a `PromptBuilder` that
tags each segment STABLE (cacheable — system, tool catalog, exemplars) or
VOLATILE (per-request); `assemble()` orders them stable-first and marks the
**cache breakpoint** at the end of the stable prefix so a provider adapter
places `cache_control` correctly by construction (a volatile token early in a
hand-built prompt silently busts the cache for everything after it).
`cache_fingerprint()` hashes only the stable prefix, and `lint_segments` flags
anti-patterns (timestamp/nonce in a "stable" block, volatile-before-stable).
**Critical-path-aware scheduling** (`task_graph.py`):
`remaining_critical_weight()` gives each task its heaviest tail of not-yet-done
work, and `ready_prioritized()` orders the runnable frontier longest-tail-first
(the standard critical-path heuristic — start the work bounding the finish
time before short-tail work); exposed as the `task_graph` tool's `schedule` op.
**Speculative best-of-N with early pruning** (`speculative_best_of_n.py`):
run N attempts but prune at the **first reasoning checkpoint** — each attempt
emits a cheap partial (its plan / first step), an injected scorer ranks the
partials, and only the top `keep` run to completion; the rest are cancelled
before they finish, so the budget concentrates on the strongest candidates
rather than N full runs. Distinct from latency best-of-N (the kill signal is
early *quality*, not time); the scorer only ever sees the cheap partials.
**Fast JSON seam** (`fastjson.py`, opt-in `[perf-fastjson]` extra): a
stdlib-compatible `dumps`/`loads` that prefers **orjson** (~5-10x faster) when
installed and falls back to stdlib `json` otherwise — `dumps` returns `str`,
honors `sort_keys`, and degrades on any value orjson rejects, so it's a safe
drop-in for round-trip/transport paths (wired into the tool-output cache
snapshot). Deliberately NOT used for cache keys/signatures, where exact bytes
must stay backend-stable. **Self-tuning budgets — online auto-apply**
(`self_tuning_budget.py`, default-on `[budget] self_tuning`; the auto-applying
companion to the advisory `maverick budget tune` / `budget_tuner.py`, which
only *recommends* a cap for a human to set): learns a default spend cap *per
coarse task class*
(e.g. the goal's leading verb) from how much past runs of that class actually
cost — a bounded reservoir per class, a high-quantile × margin suggestion
clamped to [floor, ceiling]. Wired as the **lowest-precedence** layer of
`budget_from_config` (an operator's configured `max_dollars` always wins) and
fed by the orchestrator's per-run cost recording; returns nothing until a
class has enough samples, so it never lowers safety on a guess. Off by
default.

**Local-runtime launcher + autoscaler** (`local_runtime.py`, opt-in
`[local_runtime]`, `maverick local-runtime plan`): composes the correct
engine flags for **vLLM / TGI / llama.cpp** from config — continuous
batching (`max_concurrent`/`max_batch_tokens` → `--max-num-seqs` /
`--max-batch-total-tokens` / `--parallel --cont-batching`), **persistent
KV-cache** (`kv_cache = "persistent"` → `--enable-prefix-caching` /
`PREFIX_CACHING=1` / `--prompt-cache FILE --prompt-cache-all`), **KV offload
to disk** (`kv_offload_dir`; llama.cpp persists, vLLM gets `--swap-space`,
others warned honestly), and **mixed precision** (`precision =
fp16|bf16|int8|int4` → `--dtype`/`--quantization`/quant-GGUF guidance) — plus
a queue-depth **autoscaler** (min/max replicas, hysteresis, injectable
spawn/stop/probe/clock, round-robin `endpoints()` for the router). Default
OFF; the launcher refuses to start until `[local_runtime] enabled = true`
(wizard step included); no model is ever defaulted.

**Per-role reasoning effort** (`effort.py`) — the biggest cost/latency lever on
Opus 4.7/4.8: model-gated `output_config.effort` tiered by role (critical roles
`high`, bulk roles `medium`/`low`), opt-in via `[effort] enabled`.

**Prompt caching** (`providers/anthropic_provider.py`) — frozen system prompt +
name-sorted tool catalog + a stable-history-prefix breakpoint (with a secondary
breakpoint on long turns for the 20-block lookback), 1h TTL; opt-in **cache
pre-warming** (`max_tokens=0` prefill at orchestrator start) and a
`maverick_llm_cache_tokens_total` hit-rate metric.

## MCP & agent interop

- **MCP server** (`packages/maverick-mcp/`) — stdio JSON-RPC **and** Streamable
  HTTP transport; tool `outputSchema`, resource subscriptions.
- **Registry publishing** (`maverick_mcp.publish`, `python -m
  maverick_mcp.publish [--validate]`) — emit the reverse-DNS-namespaced
  `server.json` an operator submits to an MCP registry (name / version /
  source repo / pypi package + stdio transport), built from the server's own
  `SERVER_NAME`/`SERVER_VERSION` with a `validate` lint; tools stay
  runtime-discovered (`tools/list`), never frozen into the manifest.
- **Elicitation** — client inbound (policy + shield); server outbound **form
  mode** and **URL mode** (https-only, shield-screened prompt, action-only
  response so secrets never transit the model).
- **MCP Tasks (2025-11-25)** — task-augmented `tools/call` → `CreateTaskResult`,
  background worker, `tasks/get|result|cancel|list`, status notifications.
- **MCP client** (`mcp_client.py`) — consume remote HTTP servers; **OAuth 2.1
  client-credentials and authorization-code + PKCE** grants (`mcp_oauth.py`).
- **MCP registry** (`mcp_registry.py`) — `maverick mcp-registry browse/add/...`.
- **Federated marketplace indexes** — `[catalogs] indexes` takes any number of
  index base URLs; catalogs merge across them (earlier indexes win on name
  collision, malformed entries skipped per-entry) — run your own index next
  to the community one (`catalog.py`, pinned by test).
- **A2A** (`a2a.py`, `a2a_tasks.py`) — Agent Card discovery + delegation, with
  the **interop consuming half** (`validate_agent_card` spec-shape lint,
  `parse_remote_card` normalization that refuses a non-conformant card before
  anything delegates against it) proven both ways by interop tests: Lightwork's
  own card passes its own validator, and third-party-shaped fixture cards
  (rich + minimal) parse correctly. The mounted task engine claims every
  `messageId` in a tenant-and-principal-scoped SQLite ledger before execution,
  replays terminal results across workers/restarts, and refuses crash-left
  indeterminate claims rather than risking duplicate side effects.
- **Swarm federation** (`federation.py` + `grpc_api/federation.proto`,
  protocol `maverick-federation/1`, opt-in `[federation] enabled` + `peers`):
  delegate goals across *sovereign* swarms (each with its own world DB —
  distinct from `RunGoal`'s shared-DB offload). `Hello` exchanges A2A agent
  cards (non-conformant peers refused), `DelegateGoal` carries a correlation
  id + required tools resolved **narrow-only** via capability boot negotiation
  (an ungrantable requirement refuses the delegation), auth is a constant-time
  shared token that *identifies* the caller from local config (wire names
  never trusted; fail-closed), and **both halves record reciprocal audit rows**
  in exactly the convention `audit/federation.py` cross-verifies — a dropped
  half is detectable. The protocol layer runs over any `call(method, payload)`
  transport; the gRPC binding is a thin `[grpc]` adapter (live-smoked).
- **Agent Trust Plane** (`agent_trust.py`, engaged by enterprise mode or
  `[agent_trust] enforce = true` / `MAVERICK_AGENT_TRUST=1`) — the *single*
  registry + decision point for talking to **external** agents, unifying what
  was scattered across `[federation] peers`, `[a2a]`, the fleet-memory roster,
  and the channel/marketplace pinned-key lists. One `[agent_trust] agents` list
  names each trusted outside agent by its **pinned Ed25519 public key** (reusing
  `federation_envelope`'s asymmetric identity), with a **direction**
  (inbound/outbound/both), a tool/risk **capability ceiling**, a dollar+wall
  **budget ceiling**, and **data_scopes** (which memory domains it may read).
  `decide_inbound` / `decide_outbound` / `decide_memory_access` are the gate
  every transport consults: **default-deny at the company boundary** when
  engaged (an unregistered agent is refused even with a valid shared token), a
  strict **no-op when disengaged** (kernel rule 1 preserved). Entries carry a
  **key lifecycle** (`not_before` / `expires_at` / `revoked`, propagated onto
  the issued capability). Wired into federation (inbound + outbound + `hello` /
  `status`, registry ceiling intersected into capability boot, **wall-clock AND
  dollar budget clamped down**, and goal text **secret-redacted + shield-screened
  in BOTH directions**, fail-toward-gate); A2A (**default-deny admission** when
  engaged, plus ceiling tightening); and fleet memory (**both recall AND ingest**
  gated by `data_scopes`, with recall **hard-filtering** returned content to the
  declared scope — unscoped reads denied). Engagement + registry are read from a
  **single config snapshot** per operation; `maverick doctor` warns when the
  plane is engaged with an empty registry. Denials record an `agent_trust_denied`
  audit row. In-process peer messaging (`agent_bus`) is internal and never gated.
  **Signed-request identity** makes the pinned key *load-bearing* on federation
  delegation: the caller signs the canonical delegation envelope
  (`maverick-federation-delegate/1`) with its audit Ed25519 key and the receiver
  verifies it against the pinned key — so a leaked shared token alone can no
  longer impersonate a peer that has a pinned key. Freshness + a replay-nonce
  cache reject captured signatures; `[agent_trust] require_signed = true` refuses
  even shared-token-only peers (peers without a pinned key remain a documented
  migration path). **Per-caller A2A identity**: an `[agent_trust] a2a_token`
  resolves an A2A caller to principal `agent:<id>`, so the registry governs
  individual A2A callers (admission + per-caller tool ceiling) rather than one
  shared surface. **Channel and marketplace federation** are gated by the
  registered (signature-verified) origin. **Org governance**: accepting a
  federation delegation routes through `governance.evaluate` when engaged —
  `DENY` refuses and `REQUIRE_HUMAN` refuses fail-closed (no silent
  auto-accept), a no-op without a `[governance]` policy. The **gRPC goal API**
  and **MCP server** are likewise gated: each accepts a per-caller
  `[agent_trust] grpc_token` / `mcp_token` (distinct per surface) resolving to
  `agent:<id>`, and when engaged a caller must be a permitted inbound agent
  (per-caller entry, or the surface-wide `"grpc"` / `"mcp"` entry for a
  shared-bearer caller) — so every external ingress (federation, A2A, fleet,
  channel, marketplace, gRPC, MCP) is default-denied at the boundary.
- **Bring-your-own-agent gateway** (`external_agents.py`, opt-in
  `[external_agents] enable` / `MAVERICK_EXTERNAL_AGENTS=1`, gold-tier
  `external_agents` entitlement) — agents built on *other* runtimes
  (Agentforce, Bedrock Agents, Copilot Studio, OpenAI, LangChain/LangGraph,
  custom) enroll into the governed workforce: they run on their runtime,
  governed on ours. Four verbs: **enroll** (one call writes the inbound Agent
  Trust entry — tool/risk/budget ceilings, expiry — plus the fleet-memory
  roster and platform/ownership provenance), **credential** (per-surface
  bearer tokens, rest/a2a/grpc/mcp, shown exactly once; rotate = re-mint,
  revoke everywhere at once), **screen** (pre-action admission: trust
  ceilings, cumulative budget cutoff, Shield input scan — a scanner error
  **denies** — and the `[actions] require_approval_at` floor, which parks a
  real dashboard approval the agent polls), and **account** (completed runs
  ingest as Operating Record goals owned by `agent:<id>` with step trails and
  costed episodes, so Overview/Spend/Workforce/Savings and the audit binder
  count foreign agents like native ones). Above screening sits **governed
  execution** (`[external_agents] connectors` / `MAVERICK_EXTERNAL_CONNECTORS`,
  empty = screen-only): Lightwork performs the action *itself* through the
  governed-REST connector path (SSRF host-pinning, enterprise egress
  allowlists, PREPARE/COMMIT lineage receipts) — reads run immediately, writes
  always park a digest-bound human approval that commits only on the
  byte-identical request (a mismatch voids the parked execution and counts
  toward containment; 24h TTL, 20-pending backlog cap) via
  `/api/v1/external/execute` + `/executions/{id}` + `/executions/{id}/commit`,
  audited as `external_action_executed`. Fail-closed at the boundary: text
  hygiene rejects poisoned ingest, over-budget agents are refused further
  actions, and the registry binds here even with the global trust plane
  disengaged. Agent-facing HTTP surface `/api/v1/external/runs|screen|
  approvals` + an importable `/api/v1/external/openapi.json` (drops into
  Agentforce External Services / Bedrock action groups); audit kinds
  `external_agent_enrolled` / `external_credential_minted` /
  `external_run_ingested` / `external_action_screened`. **Platform-native
  identity** (`external_identity.py`) layers on the minted bearers: a trust
  entry can pin a connected-app JWT (`jwt_issuer` / `jwt_audience` /
  `jwks_file`, verified offline against a local PEM/JWKS with `sub` = agent
  id), a webhook-format HMAC secret *reference* (`hmac_secret_ref`, resolved
  through the secret provider at verify time), and/or Ed25519 signed requests
  against the entry's `pubkey` (domain-separated `lightwork-external-request-v1`
  message, single-use nonce, fail-closed replay defence). `[external_agents]
  require_signed` then refuses the bearer for agents holding a strong
  credential, and `mint_approval` step-up gates every credential mint behind
  a one-shot, dual-control dashboard approval. See
  `docs/external-agents.md`.
- **gRPC API v1 — stable** (`grpc_api/maverick.proto`, package `maverick.v1`;
  contract gate `grpc_api/contract.py` + committed golden
  `maverick_v1_contract.json`, wired into CI): additive changes pass; removing/
  renaming a service/rpc/message/field, renumbering or retyping a field,
  changing an rpc's streaming shape, or reusing a removed field number fails
  the gate — a breaking change requires a `maverick.v2` package. The gate is a
  dependency-free proto parser, so it runs in CI without grpcio.
- **gRPC dispatch** (`grpc_dispatcher.py`, opt-in `[grpc_dispatch] target`)
  — execute goals on a remote Lightwork worker over gRPC: a `RunGoal` RPC runs
  an existing goal row to completion (API and worker share the Postgres world
  DB, same contract as the arq queue), and `GrpcDispatcher` plugs into the
  runner's Dispatcher seam with no caller changes; queue backend wins when
  both are configured; unreachable worker degrades to could-not-start, never
  an exception.
- **gRPC API** (`grpc_api/`) — typed, streaming surface for driving the runtime
  from any language: `StartGoal` / `StreamEpisode` (server-stream of episode
  events) / `Cancel` / `GetStatus`. Behaviour lives in a transport-agnostic
  `GoalService`; the gRPC shim compiles stubs on demand from the bundled
  `maverick.proto`. Behind the `[grpc]` extra; run via `python -m maverick.grpc_api`.
- **LangChain / LangGraph interop** (`langchain_adapter.py`, `[langchain]` extra)
  — expose the Lightwork swarm as a LangChain `StructuredTool`, and wrap any
  LangChain `BaseTool` as a Lightwork tool. **AutoGen + CrewAI adapters**
  (`agent_framework_adapters.py`): the same two directions for both frameworks
  — Lightwork as an AutoGen `FunctionTool` (or a dependency-free typed
  callable) and as a CrewAI `BaseTool`; `wrap_autogen_tool` /
  `wrap_crewai_tool` adapt their tools into Lightwork `Tool`s (duck-typed,
  lazy imports, actionable install hints).
- **MCP-client language analytics** (`mcp_analytics.py`) — opt-in, consent-gated
  tally of client language (from the User-Agent) that feeds the language-bindings
  decision gate (`non_python_share()`); off by default, consent step in the
  installer wizard (`maverick init` → Analytics).

## Safety & security

- **Secure by default** (`security_defaults.py`, `MAVERICK_SECURE_DEFAULT` /
  `[security] secure_defaults`) — the protective controls that don't break the
  happy path ship **on**: at-rest encryption (auto-key), audit-log signing
  (auto-key), fail-closed consent for high/critical-risk actions, and a `high`
  tool-risk ceiling (CRITICAL needs an explicit raise). Precedence: compliance
  floor > explicit arg > env > config > the secure-by-default switch; opt out per
  control or flip the whole posture off. OIDC, the egress lock, and Postgres RLS
  stay opt-in; the Shield stays fail-open. See `docs/security-hardening.md`.
- **Shield** at 3 chokepoints (input / tool-call / output); built-in rule set
  fail-open if the SDK isn't installed.
- **Floors** — secret detector, PII detector, jailbreak heuristics, unicode /
  zero-width filter, remote-content scan, output-policy classifier
  (regurgitation + refusal-leak), **phishing-content detector** (credential-harvest
  + deceptive-link heuristics, composed into `Shield.scan_output`),
  **operator-defined constitutional rules** (custom regex policy via `[safety]
  constitution`, `maverick_shield/constitutional.py`),
  Constitutional-Classifier-v2 cascade (`safety/`, `maverick_shield/`) — whose
  heuristic cheap probe can now ensemble a **trained classifier**
  (`maverick_shield/probe_model.py`): plain-JSON linear weights over the probe's
  named features (no pickle → loading an operator model can't execute code),
  combined by MAX so the model only raises recall, opt-in via `[shield]
  probe_model` / `MAVERICK_SHIELD_PROBE_MODEL`,
  **voice safety pass** (`safety/voice_safety.py`): transcript screen for
  wake-word stuffing + spoken role-switch before an utterance drives the
  agent, and redact-before-speak (secrets/PII never read aloud) wired into
  the `speak` tool, **image-content classifier**
  (`tools/image_content_classifier.py`): model-free pixel heuristics — skin-
  tone ratio (NSFW pre-filter routes to human review), brightness extremes,
  photo-vs-graphic, dimension sanity — file decode via Pillow or raw pixels
  with no imaging dep.
- **Confidential-compute detection** (`confidential_compute.py`, `maverick
  confidential-compute`) — detects whether the process runs inside a hardware
  confidential VM (AMD **SEV-SNP** / Intel **TDX**) from standard guest
  indicators (`/dev/{tdx,sev}-guest`, TDX firmware sysfs, TDX guest CPU flag;
  AMD SEV CPU capability flags alone are not treated as guest proof), so a regulated
  deployment can verify (and gate on) hardware memory encryption; exits non-zero
  when not confidential.
- **Air-gap preflight** (`air_gap.py`, `maverick airgap check`) — verifies a
  deployment has no outbound path in *Lightwork's own config*: a remote model
  provider, a non-deny-all egress policy, or a sandbox with network access — and
  exits non-zero on any finding so it can gate a deployment. (OS-level air-gap
  is the operator's job; this catches the application-layer leaks.)
- **Shield ensemble** (`shield_ensemble.py`) — a **deny-wins detector ensemble
  with explainable reason codes** (the Shield-v3 framework): pluggable members
  screen a blob (injection via the jailbreak heuristics, exfil via the secret
  detector, PII via the PII detector) and any one firing blocks, with a
  structured `reason_codes` list saying *which* detector objected and *why*
  rather than an opaque refusal. A member is a small pluggable unit, so a
  trained small-model classifier drops in behind the same interface later.
- **Access control** — tool ACLs, consent prompts + a persistent **consent
  ledger** (`safety/consent.py`; `MAVERICK_CONSENT_MODE` =
  auto-approve / auto-deny / ask / dashboard), capability tokens
  (`capability.py`), **per-call token exchange** (`tool_token.py`, opt-in
  `[capabilities] per_call_tokens` / `MAVERICK_TOOL_TOKENS=1`): each tool call
  *exchanges* the run-long grant for a freshly minted, single-tool-scoped,
  short-lived (default 30s), single-use, Ed25519-signed token — verified before
  dispatch and recorded as a `token_exchange` audit row — so a mid-run
  compromise can only ever wield one tool for a few seconds, not the whole
  grant (zero-trust "token exchange for every tool call", mapped onto our own
  capability + audit-signing primitives — no new deps; fail-open and a no-op
  unless enabled), role-based access control over capabilities, the
  `self_capability` self-report tool, **capability boot negotiation**
  (`capability_boot.py`): a spawned child may declare a narrower requested
  scope (tools/max_risk/paths/hosts), and `negotiate_boot` resolves it against
  the parent grant narrow-only (never gaining authority the parent lacked),
  records the handshake, and fails the spawn when a *required* capability
  isn't grantable, **capability revocation**
  (`revocation.py`, `maverick capability revoke/unrevoke/revocations`): kill a
  still-valid grant before its TTL — the tool chokepoint denies a revoked
  principal's next call, and the list is re-read on change so a revoke in
  another process reaches agents already mid-run; `revoke_subtree` walks the
  delegation graph to revoke a principal and every descendant it spawned
  (fail-open, like the opt-in capability layer), **approval delegation rules**
  (risk/scope-based routing, `approval_delegation.py`), per-tool network egress
  policy (`sandbox/network_policy.py`), `maverick whoami`.
- **Out-of-process model proxy** — `model_proxy.py` (`python -m
  maverick.model_proxy`, `[model_proxy] upstream/auth_style/client_token`): a
  separate process holds the provider key (from its **own** env,
  `MAVERICK_PROXY_KEY`) and the agent points a provider's `base_url` at it. The
  agent process never holds the provider credential — a prompt-injected agent
  can't exfiltrate a key it doesn't have. The listener requires the agent to
  present a separate proxy client token (`MAVERICK_PROXY_CLIENT_TOKEN` or
  `[model_proxy] client_token`), strips that client auth + hop-by-hop headers,
  injects the real key in the upstream's scheme (bearer / `x-api-key`), and
  forwards only to its single configured upstream host plus model-inference
  routes by default (override with `[model_proxy] allowed_routes` or
  `MAVERICK_PROXY_ALLOWED_ROUTES`).
- **Audit & compliance** — signed append-only audit log (`maverick audit verify`), **federated
  audit-log verification** (`audit/federation.py`) — over a set of nodes/tenants
  whose signed logs reference each other (delegation, A2A handoff), confirms
  every cross-node reference is *reciprocated* (a node can't drop its half to
  hide an action) on top of each node's own chain/anchor check; an
  unreciprocated or forged link is reported with the missing counterpart,
  date-windowed **SIEM export**, **WORM export** (`audit/worm.py`, `maverick audit
  worm push`) — ships closed day-files to S3 Object-Lock (COMPLIANCE/GOVERNANCE)
  or a local read-only mirror with a retention lock, so the historical trail is
  *immutable*, not just tamper-evident; `maverick audit worm verify` proves every
  closed file is durably shipped, encryption-at-rest (`crypto_at_rest.py`,
  `maverick encryption migrate`), SOC2 readiness (`soc2.py`), DSAR (`dsar.py`),
  **differential erasure verification** (`erasure_verify.py`, `maverick
  erase-verify`) — a right-to-erasure *proof*: combines exact post-delete
  counts with an immutable signed pre-delete scope receipt and a bound signed
  completion record for attachment files, user notes, the disposable LLM
  cache, and audit-chain maintenance. Any missing, failed, or tampered proof
  surface yields `INDETERMINATE`, never a false clean certificate. The
  before/after `differential` also confirms the erase actually removed data,
  **data-retention enforcement** (`audit/retention.py`, opt-in `[retention]`
  config, `maverick retention enforce [--dry-run]`) — prunes audit files,
  `episodes`/`goal_events` rows, **and the usage-ledger cost buckets**
  (`usage_days`: the per-principal `(principal, day)` chargeback tally accrues
  forever otherwise),
  per-run file-write + tool quotas, `maverick compliance --strict`, CycloneDX
  SBOM in CI.
- **Compliance mode profiles** — `[compliance] profiles = ["hipaa"]` turns on
  a cross-domain runtime posture (`compliance_profiles.py`): **HIPAA mode**
  asserts the 45 CFR Part 164 safeguards, names the protection floors it
  requires (PII redaction, encryption-at-rest, egress lock, audit), and folds
  a require-human-on-high-risk policy into the live governance policy
  (strictest-wins, via the same union the finance regimes use). Inert when
  unset — default behavior is unchanged.
- **Refusal calibration** (`safety/refusal_calibration.py`) — score
  {prompt, should_refuse, refused} samples into over/under-refusal rates with
  configurable ceilings and CALIBRATED/OVER/UNDER verdicts; deterministic
  `is_refusal` completion detector.
- **Shield call rate-limit per goal** (`safety/shield_rate_limit.py`) — opt-in
  `[safety] shield_rate_limit = "100/60"` sliding-window token bucket per goal;
  throttling SKIPS the scan fail-open (the shield never blocks the agent by
  being busy), with once-per-window suppressed-call alerts.
- **Model cards per LLM** (`model_cards.py`) — aggregate the deployment's own
  usage ledger into per-model cards (roles, calls, tokens, dollars) rendered
  as markdown with a no-vendor-claims disclaimer; duck-typed world adapter.
- **Behavioral diff on upgrades** (`behavioral_diff.py`) — replay a fixed probe
  set before/after a model/prompt change; classify per-probe
  unchanged/minor/major/refusal-flip, PASS verdict gated on flips + major-change
  fraction.
- **Goal risk-tier auto-classifier** (`safety/goal_risk.py`) — deterministic
  low/medium/high scoring of a goal before it runs (money/infra/credential/
  bulk-comms/PII/irreversibility signals, read-only de-escalators, documented
  weights), config floor + require-human mapping for the approval path.
- **Containment mode** (`containment.py`, opt-in `[containment]`) — lock a
  run into no-egress + ephemeral 0700 workspace: composes the registry ACL
  (denies the exfil tools; config *extends*, never replaces the default deny
  set), black-holed proxy env for subprocesses (advisory; the load-bearing
  layer is the ACL + container backends' network deny), cleanup handle.
- **Cryptographic budget receipts** (`budget_receipts.py`) — HMAC-signed,
  hash-chained spend receipts per goal (prev-hash inside the signed payload
  so deletion/reorder is unforgeable; append-only 0600 ledger; refuses to
  mint unsigned), verify + chain verification with break index.
- **Quorum approval for config changes** (`quorum.py`) — N-distinct-approver
  gate over protected config keys (fnmatch patterns; self-approval and
  duplicate approvers refused; required count snapshotted per proposal so
  policy edits can't shrink a pending quorum; TTL-pruned proposals).
- **Capability-leak fuzzer** (`capability_fuzzer.py`) — seeded adversarial
  probes (case/homoglyph/prefix/separator/NUL/glob/long-name) against
  Capability.permits; CI `python -m maverick.capability_fuzzer` exits 1 on
  any leak. Run against the real implementation: **0 leaks in ~2000 probes**.
- **Provider-level cost caps** (`provider_cost_cap.py`, `[budget.provider_caps]`)
  — per-provider dollar ceilings across ALL runs per UTC day/month (the
  Budget caps one run; this caps the provider), atomic ledger, enforce()
  raising ProviderCapExceeded for the LLM path.
- **Supply-chain pinning** (`supply_chain.py`) — pin the deployment's Python
  dependency tree (`write_pins` → 0600 JSON), verify drift/missing/unpinned
  (`verify`/`render` PASS-FAIL), opt-in startup warning via
  `[safety] supply_chain_pinning` (never raises).
- **Crash-only logging** (`crash_only_log.py`) — append-only JSONL safe to
  kill -9 at any byte: one fsync'd `os.write` per record, seq resumes after
  reopen, replay tolerates (and counts) the torn tail vs mid-file corruption,
  gap detection; fsync policy knob for test/throughput mode.
- **Right-to-rectification** (`rectification.py`) — **built, not yet wired:
  no production code path reaches this module** (see
  `maverick.reachability`), so it is available to call but is not part of any
  shipped flow. GDPR Art. 16 counterpart to DSAR/erasure: find a subject's
  occurrences across goals/turns/facts (snippets), rectify with dry-run
  default inside one write transaction, and a subject-digest audit trail
  (`rectifications.jsonl`) that never carries the old or new value.
- **Honeytoken planting** (`safety/honeytokens.py`) — mint decoy credentials
  (AWS-key-shaped, API-key, passphrase), plant a realistic 0600 secrets file,
  and alert (once per fingerprint) when a decoy value appears in text — alerts
  carry sha-fingerprints, never the live decoy.
- **Public safety bulletin RSS** (`safety_bulletins.py`) — render
  frontmattered bulletin markdown into a standards-shaped RSS 2.0 feed
  (newest-first, malformed bulletins skipped loudly); self-host first: the
  feed is a file you serve, not a hosted service.
- **Tamper-evident screenshots** (`screenshot_seal.py`) — every capture is
  sealed into a per-directory hash-chained, HMAC-signed ledger (replace /
  edit / delete / reorder all detectable; re-capture legitimately
  supersedes); wired into the computer tool's screenshot path, opt-in purely
  by key presence (`[safety] screenshot_key`), best-effort so evidence
  capture never breaks the screenshot the model is waiting on.
- **Red-team CI** — a named CI job (`redteam` in `ci.yml`) runs the labelled
  adversarial corpus (`maverick_shield/redteam_corpus.jsonl`, grow-by-PR)
  through the shield's built-in detector via `python -m maverick_shield.redteam`
  and fails the build on any missed attack or over-blocked benign case.
- **Shield calibration dashboard** — the same runner swept across every block
  threshold yields the operating curve (recall/precision/fp-rate per
  threshold) + per-rule hit counts: `--calibrate` CLI and
  `GET /api/v1/shield/calibration` on the dashboard (auth-gated; operator
  corpus via `MAVERICK_REDTEAM_CORPUS`).
- **Sandbox-escape canaries**, per-tool rate limiter, killswitch.

## Governed agent runtime & onboarding

- **Conversational intake** — interviews a user, collects docs, and proposes a
  domain configuration (intake agent + LLM proposer + `run_intake`).
- **Capability provisioning** (`provision.py`) — the agent factory equips a
  pack *at birth*, not reactively mid-run: `analyze_profile` diffs a draft's
  workflow + declared `allow_tools` against the installed skills and live tool
  registry (`tools.base_tool_names()`) and surfaces the gaps at the approval gate;
  on approval, `apply_plan` installs the matching catalog skills
  (`self_learning.acquire_skill`) and synthesizes any missing declared tools
  (`self_learning.write_generated_tool` — stdlib-only, import-validated
  out-of-host, consent-gated). Analysis is read-only and always safe; applying
  it is gated on `[self_learning] enable` + the new `provision_packs` sub-knob
  (wizard step) + the same human approval `save_profile` requires, and never
  widens the pack's already-clamped envelope.
- **Programming by demonstration** (`demonstration.py`) — the factory's second
  front door: watch a person do their job, then build the agent that does it. A
  `Demonstration` is an ordered record of observed actions + narration (from any
  capture front-end), ingested by `parse_demonstration`/`load_demonstration`
  (JSONL or prefixed text like `ACTION[email]: send digest -> ops@`;
  secret-redacted at the door, byte/step-bounded, and fail-soft — a malformed
  or oversized capture drops bad lines rather than raising).
  `induce_profile` turns it into a `DomainProfile`
  by reusing the intake pipeline wholesale — it builds the same
  `propose(spec) -> dict` and routes through `generate_profile` →
  `validate_profile`, so a demonstrated pack inherits the identical envelope
  clamp + persona shield-scan as a described one. LLM path (model proposes from
  the transcript) and deterministic path (workflow mirrors the steps, tools =
  what the person used) both supported; a human review gate is always appended.
  CLI: `maverick learn-demo <file>` → induce → approve → save → provision.
- **Self-improving factory** (`factory_learning.py`) — closes the loop back onto
  *generation quality*. Provisioning/approval gaps (a tool a draft kept omitting,
  a skill its workflow kept needing) are attributed to the pack's suite and
  mined into proposer **corrections**; each is promoted through the existing
  `SelfImprovementController` on the `prompt` rung (guidance text widens no
  capability, so no escalation proof or human sign-off is required — only the
  evidence + calibration gates), and promoted guidance is folded into future
  pack generation (`augment_system_prompt`, scope-matched per suite). Default-on
  and byte-identical to before while disabled; gated by `[self_improvement]
  enable` + the `factory_learning` sub-knob (wizard step) or
  `MAVERICK_FACTORY_LEARNING`. Live promotion requires self-digested v2 paired
  evidence with immutable data/split/evaluator/model/prompt provenance and uses
  the shared PREPARE/CAS/COMMIT promotion ledger; v1 evidence is preview-only.
  CLI: `maverick factory-learn [--dry-run]`.
- **Governed adapter rung — in-tenant weights** (`adapter_rung.py`,
  `[adapter_rung]`) — LoRA adapters for a LOCAL open-weights base, trained on
  tenant-provenance data and promoted through the existing `weights` rung.
  Training data passes a provenance boundary (frontier-model output refused by
  default — the distillation-ToS guard; synthetic opt-in; unknown always
  refused) and is content-hashed into the manifest. The payload passes a
  hygiene boundary BEFORE any eval (safetensors/gguf/json/text only; code and
  pickle-bearing formats refused structurally, so weights can never smuggle
  executable authority). Fitness is held-in/held-out with the code rung's
  overfit refusal; the Ed25519 approval payload EMBEDS the weights digest
  (a post-signature weights swap stops verifying); promotion lands in the
  append-only ledger; activation is an atomic pointer swap whose previous
  pointer is the byte-identical one-step rollback handle. Serving: a rendered
  Ollama Modelfile (`ADAPTER`) + emitted `ollama create` command; the Ollama
  provider client resolves base → tuned at request-build time
  (`adapter_rung.effective_wire_model`, mtime-cached, fail-open), so spec
  parsing, admin allow-lists, pricing, and telemetry keep the stable base
  model id. Trainers:
  `stub` (deterministic; proofs/tests) and `dpo-lora` (delegates to
  `training/rlaif.py`, `[training]` extra). OFF by default AND inert until
  `base_model` is set (never hard-coded; wizard suggests an Apache-2.0 base).
  Proof: `python proof/adapter_rung_proof.py` — 6 guarantees.
- **Governed specialist-model improvement** (`training/environments.py`,
  `training/specialist_models.py`, `training/qualification.py`,
  `training/receipts.py`, `[model_improvement]`) — locked deterministic PIA,
  DSAR, and Article 28 environments; structured tenant consent and redaction
  evidence; strict source-span citation rewards; immutable-revision candidate
  research; non-weakenable exact runtime/hardware qualification; and
  dual-signed, hash-chained, tenant-private training receipts verified against
  server-owned trust policy. Public seed packs are bakeoff fixtures and
  deliberately fail promotion readiness because their holdout answers are
   published. Cross-tenant training is refused. Optional Verifiers/prime-rl
   integration uses separate pinned Prime v0.7.0/Verifiers v0.2.0 and standalone
   Verifiers v0.2.1 profiles, is operator-provisioned, and never auto-installs or
   uploads. Prime v0.7 specs use the Verifiers v1 taskset shape, replace inherited
   `PYTHONPATH` with the exact verified bundle source, and verify the bundle's
   digest-bound, self-contained scorer before execution. Live pointers bind the
   effective policy and resolve approver
  fingerprints through global deployment trust. The complete design, model
  matrix, and rollout gates are in
  [`MODEL_IMPROVEMENT_PLATFORM.md`](./MODEL_IMPROVEMENT_PLATFORM.md).
- **Domain packs** — spawn domain agents from profiles (legal / privacy / generic
  packs) on the sector-seal foundation.
- **Pack output contract** — a pack's optional `[output]` block declares the
  *consumption* side of the deliverable: its render `shape` (prose / report /
  table / forecast), a human `deliverable` label, the `consumers` (persona
  roles who receive it), a `cadence`, and the `gate` (review / approval) it
  needs before it is acted on. Additive and lint-checked (`OutputContract` in
  `domain.py`); absent means today's behaviour — a free-text prose result with
  no declared consumer. The dashboard reads it to render, route, and gate a
  result by domain rather than showing every output as the same text box.
- **Deliverable rendering** — the goal page (`/chat/goal/<id>`) renders a
  result as the artifact its pack declares: a `forecast`/`table`/`report` shape
  whose result carries a grid becomes a real titled, gated `<table>` (the FP&A
  13-week cash forecast lands as a week-by-week grid, not a wall of monospace),
  while `prose` and contract-less goals keep the plain text result. Server-side
  and dependency-free (`deliverable.py` parses the pipe table agents emit; cells
  go through template autoescaping), so a malformed or table-less result
  degrades gracefully to prose.
- **Artifacts** — a goal can produce versioned, kind-tagged artifacts
  (markdown / code / table / text) stored apart from the single `goal.result`
  blob (v18 `artifacts` table; `add_artifact` / `latest_artifacts`, title
  plaintext for version-keying, content encrypted at rest). The goal page shows
  the latest version of each, rendered by kind — a `table` artifact as a grid
  (reusing the deliverable renderer), others as text — with a per-title version
  count; `GET /api/v1/goals/<id>/artifacts` lists them. Governed take on
  Claude-style artifacts: rich *rendering*, never arbitrary HTML/JS execution in
  the operator's browser. **Agents emit them automatically**: on goal completion
  the orchestrator records a structured deliverable as an artifact (best-effort,
  byte-identical re-finalizes are deduped), so re-runs accrue version history.
  The goal page grows a **version/diff viewer** — a per-artifact history
  disclosure that fetches each version with a server-computed unified diff
  (`/artifacts/history`), rendered with add/del coloring (textContent only, no
  innerHTML).
- **Projects / matter workspaces (`/projects`)** — group related goals into a
  persistent workspace (a close cycle, an audit, a deal). v19 `projects` table
  (name/description encrypted; owner/domain plaintext) + a nullable
  `goals.project_id`; `create_project` / `list_projects` (with goal counts) /
  `set_goal_project` / `list_goals(project_id=...)` / `project_status_counts`.
  The page lists + creates projects; a detail page shows member goals and a
  status rollup; the goal page gains a Project selector to file a run (or clear
  it). Owner-scoped like goals (a project you don't own 404s). The maverick take
  on Claude Projects: a governed, attributable workspace, not just a chat folder.
- **Output styles (`/styles`)** — a user-selectable response style (concise /
  explanatory / formal / executive / technical / bullet) appended to every
  agent's system prompt — tone and format only, never capabilities or the safety
  surface (the Claude "styles" analog; sibling to the operator `[persona]`
  block). `styles.py` is the registry + renderer; the active selection is a
  dashboard runtime overlay (`styles.active`, like plugin toggles — not
  config.toml), set on the page (operator role, validated against the registry).
  Injected in `agent.py` beside the persona block, additive and fail-open.
  Per-conversation selection + custom styles are the planned follow-ons.
- **Share links (`/share/<token>`)** — a revocable, expiring, read-only link to a
  goal's deliverable for someone without a dashboard login. v20 `share_links`
  stores only the token's SHA-256 (like a password-reset token), so the DB never
  holds anything that grants access and the clear token is shown exactly once;
  `create_share_link` / `resolve_share_link` (rejects unknown / revoked /
  expired) / `revoke_share_link` (goal-scoped) / `share_links_for_goal`. The
  public view is auth-exempt (the token IS the credential — bearer + OIDC
  middleware both skip `/share/`), renders only title + deliverable + artifacts
  (never the worklog, spend, controls, or nav; `noindex`), and 404s a bad token
  with no detail. Create/revoke are operator-role + goal-access-gated; the goal
  page grows a Sharing panel (create → copy-once link, list, revoke). 7-day default.
  the deliverable their pack declares and scoped to the consumer role, so an
  FP&A analyst sees "my forecasts" and a risk officer sees "assessments awaiting
  my sign-off" instead of the flat `/goals` stream. Filter chips per consumer
  role; a gated deliverable whose run has finished surfaces in an "Awaiting
  sign-off" queue whose Review action opens the rendered deliverable. Keyed off
  the existing `goals.domain` attribution (`list_goals(domain=...)`) and the
  pack output contract; the model is shaped by a pure `deliverables.build_inbox`.
  **Persona identity:** with a `[personas]` binding (principal → consumer roles,
  with a single-user `default`; a wizard step), the inbox defaults to the
  signed-in user's own deliverables — a risk officer lands on their assessments,
  an analyst on their forecasts — with a "Mine / All" toggle. Distinct from the
  RBAC role (admin/operator/viewer, which gates *actions*); this says which
  deliverables are *yours*. No binding = the full list, as before.
  The finance suite declares output contracts across its towers (FP&A forecasts,
  controllership reconciliations, treasury schedules, tax provisions, assurance
  memos, reporting drafts, risk/credit), so the inbox is populated with ~29
  deliverables across 11 consumer roles, not just the one proof pack. The
  insurance suite extends this with ~38 more (claims files, underwriting files,
  reserve indications, reinsurance reconciliations, statutory filings) across
  its own 11-role vocabulary (underwriter, actuary, claims_adjuster/manager,
  reinsurance_analyst, compliance_officer, ...). The banking suite adds ~37
  (CECL allowance, SAR/CTR filings, AML queues, loan files, call/liquidity
  reports, ALM sensitivity) across bsa_officer, credit_officer, loan_officer,
  treasurer, operations_manager, ...; and the IT-GRC / risk suite adds ~55
  (enterprise risk register, DPIA/RoPA, control-test results, incident/breach
  reports, vendor TPRM, audit evidence) across risk_officer, ciso,
  privacy_officer, internal_auditor, security_analyst, ... -- so the inbox spans
  ~159 deliverables across ~30 consumer roles.
- **Governed hand-off (deliverable sign-off + export)** — closes the loop the
  output gate opens: a human certifies a finished, gated deliverable
  (`POST /api/v1/goals/<id>/signoff`, recorded in the v16 `signoffs` table with
  who/when and an encrypted review note), and an approved deliverable can be
  pulled downstream as CSV (`GET /api/v1/goals/<id>/deliverable.csv`) instead of
  re-keyed by hand. The goal page grows a sign-off panel (Approve / Reject + note,
  then the decision + hand-off download); a signed-off deliverable drops out of
  the persona inbox's "awaiting sign-off" queue. Agents draft; humans certify.
- **System-of-record routing** — when a deliverable is approved, Lightwork POSTs
  it to a configured downstream endpoint (treasury / GL / Jira), so "approved in
  Lightwork" lands in the system of record automatically instead of being
  re-keyed. Opt-in via `[deliverables] handoff_webhook` (a wizard step;
  env-referenceable URL), signed with the existing `[webhooks]` HMAC secret and
  delivered over the SSRF-safe webhook path. Best-effort and never blocks the
  sign-off; the payload carries the parsed deliverable (table rows) and
  attribution. Fires only on `approved` (`webhooks.fire_deliverable_handoff`).
- **In-dashboard skill authoring** — the `/skills` page can now *create* a skill,
  not just install one: a form (name, trigger phrases, tools, instructions)
  composes a `SKILL.md` and writes it via the same validate + secret/shield-scan
  path as install (`skills.create_skill` / `build_skill_md`). Kebab-cased id,
  injected into matching agents on the next run. Shares the
  `MAVERICK_ALLOW_SKILL_INSTALL` opt-in (skills land in agent prompts), so it's
  off by default in a locked deployment. Skills are instructions, not code —
  the safe, governed equivalent of an in-app extension.
- **Allowlisted plugin install** — the `/plugins` page can one-click `pip
  install` a *code* plugin package (tools / channels / skills / personas entry
  points), but only one the operator pre-approved in `[plugins] installable`.
  Fail-closed and quad-gated: same-origin, the `MAVERICK_ALLOW_PLUGIN_INSTALL`
  opt-in, the admin role, and the install allowlist — and pip runs as argv (no
  shell), so nothing is injectable and a compromised token can't pull arbitrary
  code, only pre-vetted packages (`plugins.install_plugin` / `installable_plugins`).
  Distinct from the *load* allowlist (`enabled`); installed slots appear in the
  toggle list to enable.
- **Finance suite (Office of the CFO)** — 31 domain packs across 7 towers
  (Controllership, FP&A, Treasury, Tax, Assurance, Procurement, Reporting) + a
  Finance Controller, each a sealed read-only/draft-by-default compartment with
  the "never move money without a human" guardrail. The governance wrapper:
  amount-aware authorization (`[governance] deny_above`/`require_human_above`
  dollar tiers), a segregation-of-duties linter (`maverick finance lint-sod`),
  OFAC/SDN sanctions screening (`screen_sanctions`), finance assessment templates
  (sox_control / fraud_risk / itgc / credit_risk / close_readiness),
  compliance-regime packs (SOX/COSO/GAAP/PCI DSS 4.0.1/GLBA/AML/SEC/IRS plus
  DORA, Basel III final reforms, and IFRS 17; strictest-wins), and the
  `maverick finance status` posture report (`maverick/finance/`). The opt-in
  `[finance_operations]` control plane adds: SSRF-guarded Federal Register,
  opt-in official Texas Register RSS, and configured state-register polling;
  atomic deterministic content versioning,
  enabled-scope matching, field diffs, and a citation-bearing review queue;
  versioned 50-state money-transmitter and insurance-producer source-routing
  packs persisted into the same register; duplicate-payment, Benford-eligibility,
  approval-threshold/split-payment, and off-hours anomaly rules with exact,
  bounded evidence; governed AML/KYC/sanctions list versions, fail-closed
  completeness, transparent typo-tolerant matching, and four-eyes case
  disposition; and crash-resumable scheduled finance observations routed into the existing
  Security/GRC audit and evidence-request APIs as `needs_review` tests, with
  evidence-backed human dispositions reconciled to finance cycles. The
  finance-suite-gated `/finance` workspace and 24 permission-tiered
  `/api/v1/finance-operations/*` routes expose the queues. No LLM makes a
  regulatory, anomaly, sanctions, or
  audit decision. See [`FINANCE_OPERATIONS.md`](./FINANCE_OPERATIONS.md).
- **maverick-knowledge** — per-domain vector RAG package backing
  `knowledge_search`; config-selected embedders (`embed.py`): hosted **Voyage**
  (or any OpenAI-compatible endpoint), **Cohere** (`/v2/embed`, typed
  `embedding_types`), local, or deterministic — fails loud rather than silently
  degrading.
- **Role-based page visibility** (`maverick_dashboard/ui_visibility.py`) — one
  page registry drives the sidebar nav, a per-role visibility policy, and the
  Settings "Page visibility by role" matrix. Each RBAC role (admin/operator/
  auditor/viewer) sees only the pages its work needs by default (a viewer gets
  read-only pages, an auditor the audit surfaces, an operator no admin
  plumbing), and an admin can show/hide any page per role from Settings.
  Hidden pages leave the nav AND 403 on direct access (an app-level dependency
  after `require_principal`); overrides can never show a page past the role's
  enforced permission floor, admins can never lose Settings/Users (lockout
  guard), and /api routes are untouched. Auth off = full nav, nothing
  enforced (single-user mode unchanged). Store: diffs-only
  `~/.maverick/dashboard-ui-visibility.json` (0600), global control-plane.
  The registry also declutters the sidebar to one entry per job: secondary
  composers/detail surfaces (`in_nav: False` — Quick Goal, Goal Map, Workflow
  Builder, Flow Designer, Billing, Learned, Redaction) stay governed but are
  reached from their primary page's own links; Walkthroughs/Facts live under
  Observe and Response Styles under Extend, matching their real audience. The
  shell's design system + behaviors ship as static assets
  (`/static/lightwork.css`, `/static/lightwork-ui.js`) rather than inline in
  `base.html`, and the /settings + /users routes live on their own router
  (`maverick_dashboard/admin_pages.py`).
- **Reverse-proxy SSO** — trusted forwarded-identity header for enterprise auth.
- **SAML 2.0 SSO** (`maverick_dashboard/saml.py`, the `[saml]` extra / pysaml2) —
  a SAML SP browser-login front-end (`/saml/metadata`/`login`/`acs`) for IdPs that
  mandate SAML over OIDC. The IdP's signed assertion is verified by pysaml2 (no
  hand-rolled XML-dsig) and mints the SAME `mvk_session` cookie as the OIDC login,
  so a SAML user flows through RBAC / `require_principal` identically
  (`user:<NameID>`); open-redirect-safe RelayState; off by default (404s until
  `[auth.saml]` is set). pysaml2 calls are isolated + unit-tested; certify a live
  IdP round-trip before production.
- **SCIM 2.0 provisioning** (`maverick_dashboard/scim.py`) — the RFC 7643/7644
  `/scim/v2` surface (Users CRUD + `ServiceProviderConfig`/`ResourceTypes`, the
  `userName eq` filter, Okta/Azure PATCH-deprovision forms) so an enterprise IdP
  drives user lifecycle automatically: a SCIM user provisions a backing tenant,
  `active=false`/DELETE suspends/removes it. Carries its own static IdP bearer
  (`MAVERICK_SCIM_TOKEN`, constant-time compare), exempt from the dashboard-token
  middleware and OIDC gate; 404s when unset, so it's inert off by default.
  Deprovision also **force-revokes live sessions**, not just future logins: a
  per-principal revocation epoch (`session_revocation.py`, fail-closed on a
  damaged store) rejects any credential issued before it, and a login-time
  **subject directory** (`subject_directory.py`, sha256-keyed for privacy)
  reaches a session even when the IdP issues a pairwise/per-app OIDC `sub`
  (Entra) that's in no SCIM attribute. "Log out everywhere" bumps the same epoch.
- **Tenant-aware persistence** — workspaces wall each tenant into
  `~/.maverick/tenants/<t>/` (`workspace.py`, `paths.py`), with a per-tenant
  world DB and `data_dir()`-routed audit / quotas / DSAR / fleets. The shared
  **Postgres** backend (`[world_model] backend = "postgres"`) carries a
  **versioned migration runner** (the world-model `MIGRATIONS` ledger), a
  `tenant_id` on every root table (write-stamped, read-scoped), **tenant-aware
  UNIQUE constraints**, a **strict-isolation mode** (`[world_model]
  strict_tenant_isolation`), **database-native Row-Level Security**
  (`[world_model] rls` / `MAVERICK_PG_RLS`, **auto-on under enterprise mode**
  with a boot preflight that refuses to start on legacy NULL-tenant rows: a
  FORCE-RLS policy on every
  tenant-scoped table keyed on a transaction-local `maverick.tenant` GUC, so
  the database — not just the app-layer predicate — enforces the boundary; the
  policy fails closed when the tenant GUC is unset/empty and startup fails if
  the requested policy cannot be installed or verified; applied by the table
  owner, enforced for non-superuser connections, validated
  against a live Postgres under a non-superuser role), and an opt-in
  **`psycopg_pool` connection pool** (`[world_model] pool_size` /
  `MAVERICK_PG_POOL_SIZE`) that hands each transaction its own pooled
  connection for horizontal scale (default 0 = the original single-connection
  model, unchanged).
- **Per-tenant KMS at fleet scale** — envelope encryption with a per-tenant DEK
  wrapped by a KEK in a pluggable `KMS` (`tenant/kms.py`; LocalKMS or AWS/GCP/
  Vault BYOK via `kms_backends.py`). **Deterministic per-tenant BYOK**:
  `get_kms(tenant_id)` reads each tenant's own `[kms]` overlay independent of the
  active context. A **DEK-cache TTL** (`MAVERICK_KMS_DEK_CACHE_TTL`) bounds how
  long a revoked cloud key keeps opening data. **Fleet KEK rotation** is operable
  and resumable: `maverick tenant kms-rotate --old-kek --new-kek [--dry-run]`
  skips tenants already on the new KEK (re-run finishes an interrupted rotation)
  and refuses success while any tenant is still on the old KEK (re-wrap only — no
  data re-encrypted).
- **Online-migration preflight + governance** — `schema_migrations.py` + `maverick
  schema-plan`: the *operations* view over that ledger. It classifies each
  pending statement `online` (cheap/non-blocking: `ADD COLUMN`, `CREATE INDEX IF
  NOT EXISTS`, FTS rebuild) or `offline` (table rewrite / long write lock),
  `plan(current, target)` lists the pending steps, and `online_only()` gates a
  hot deploy — failing **closed** on any unclassifiable statement so an unknown
  migration is reviewed before it runs against a live, high-traffic world. On top
  of it, **`migration_governance.py`** is the integrity ratchet (CI-gated):
  per-version sha256 checksums pinned in `migrations.lock.json` make released
  migrations immutable, new ones must be additive (no `DROP`/`RENAME`), and both
  backend ladders (SQLite + Postgres) must stay at the same declared head.
- **Proven control/data-plane split** — the dispatcher seam hands goal execution
  to a separate worker (`queue_dispatcher.py` arq/Redis, `grpc_dispatcher.py`
  gRPC, both installed at startup). Two CI-gated proofs back the claim:
  `control_data_plane_e2e.py` (one goal provably runs out-of-process, with a JSON
  evidence artifact) and `control_data_plane_soak.py` (many goals under
  concurrent workers reach `done` zero-loss and **exactly-once** — the contention
  test of `JobQueue.claim`'s `WHERE status='pending'` guard).
- **Process table** — `maverick ps` (unified view of runs/workers).
- **Scheduling** — recurring autonomous goals from a prompt; `worker --once`
  cron-friendly drain (`scheduler.py`, `job_queue.py`, `worker.py`).

## Hosted control plane & multi-tenancy

The backend for running Lightwork as a governed, multi-tenant platform (each piece
opt-in; single-tenant/self-hosted deployments are unaffected):

- **Tenant lifecycle / provisioning** — `tenant/registry.py` + `maverick tenant
  create/list/suspend/resume/quota/delete`: a roster of tenants with status,
  plan, and per-tenant daily spend quota; `assert_tenant_active` refuses a
  suspended tenant.
- **Metering → billing & entitlements** — `billing.py` + `maverick billing
  invoice/entitlements`: rate the usage ledger (pass-through+markup or
  token-priced) into per-period invoices; plan → feature/limit entitlements
  (`tenant_entitled`).
- **Per-tenant envelope encryption** — `tenant/kms.py` (fleet KEK rotation in
  `tenant/kms_fleet.py`): a DEK per tenant, wrapped by a KMS KEK (LocalKMS
  default; cloud KMS is a drop-in `wrap`/`unwrap`); one tenant's DEK can't open
  another's data; instant KEK rotation.
- **Per-tenant egress plane** — `tenant/egress.py`: a per-tenant allow/deny
  egress policy composed (AND) with the per-tool policy at the egress chokepoint.
- **Multi-tenant `maverick serve`** — the channel server enforces the tenant
  roster at the door: with per-user tenancy on and tenants provisioned, a
  suspended/unknown tenant's message is refused before any goal exists, and a
  tenant over its provisioned `max_daily_dollars` (`maverick tenant quota`;
  `tenant_over_quota` sums today's tenant-scoped usage ledger across
  principals) is refused until the UTC day rolls. No roster = no-op; registry
  read errors fail soft.
- **Out-of-process execution** — a swappable goal **Dispatcher** (`runner.py`)
  with a **QueueDispatcher** (`queue_dispatcher.py`) that enqueues goals for a
  worker pool (arq adapter behind `[queue]`; `install_from_config` wires it).
- **Isolation test suite** — `tests/test_multitenant_isolation.py` proves the
  tenant walls across the primitives that actually carry tenant data: `data_dir`
  path routing (distinct ids never collide onto one segment), `world_for_tenant`
  DB separation (A's goal invisible to B), per-tenant KMS (a DEK wrapped for A
  does not unwrap under B's AEAD context), and clean `set_tenant`/`reset_tenant`
  scope discipline.

## Evaluation & benchmarks

**Long-running plugin reliability drill** (`plugin_reliability.py`, `python -m
maverick.plugin_reliability`): the plugin counterpart to the chaos game-day —
a host-agnostic sustained-load drill (give it any `call(payload) -> str`)
that injects crashes/timeouts/errors at seeded rates over thousands of calls
and asserts the reliability properties a host must hold: **recovery** (a crash
is followed by a later success — no permanent wedge), **isolation** (a faulted
call never poisons the next), bounded **error rate**, and **no monotonic
memory growth** (a leaking plugin, via an injected sampler). Deterministic;
exits non-zero in `--ci` on any property failure. **Chaos game-day drill** (`chaos_gameday.py`,
`python -m maverick.chaos_gameday`): scripted fault scenarios against the
real retry layer — 20% tool flakes must be absorbed (≤5% surfaced), a total
outage must exhaust retries in bounded attempts (backoff virtualized so the
drill runs in milliseconds), plus a no-chaos control; exits 1 when a
resilience property fails. Standalone drill, not for serving processes.

**Cost/perf release canary** (`release_canary.py`, `maverick canary
record/compare`): snapshot a release's cost/latency/success-rate metrics and,
before shipping the next, compare against the recorded baseline — a
**direction-aware** relative check (lower-is-better for cost/latency,
higher-is-better for success-rate/throughput) that exits non-zero on a
regression beyond tolerance, gating a release the way tests do. Deterministic;
the snapshot store is an atomic JSON keyed by release tag.

**Reproducible benchmark v2** (`maverick.benchmarks.reproducible_v2`, `python
-m maverick.benchmarks.reproducible_v2 run|--verify`): runs a suite under
pinned conditions (seed, model id, prompt-template hash, tool-set hash) and
emits an HMAC-signed `{suite, seed, env_fingerprint, results, aggregate}`
manifest; `--verify baseline current` diffs two runs and names the exact
diverged task on non-determinism. **Marketplace moderation**
(`marketplace_moderation.py`, `python -m maverick.marketplace_moderation
<path>`): static pre-publication checks over a submitted skill/plugin —
manifest completeness, permission-escalation (declared vs used), secret scan
(reuses the secret detector), prohibited patterns, license — with a
strictest-wins approve/flag/reject verdict. **Skill search engine**
(`skill_search.py`, `python -m maverick.skill_search`): zero-dep BM25-lite
ranked search over the local skill library with HF-dataset export/import
(`skills.jsonl`, network via an injected fetcher; pulled skills re-validated
through the skill validator). **Self-hosted relay reference**
(`relay_reference.py`, [`docs/self-hosted-relay.md`](./self-hosted-relay.md)):
the self-hostable inbound-webhook relay (quick-vs-ack-then-run classification,
deadline enforcement, secondary-channel delivery) as a framework-agnostic,
fully-injected core that runs as a Worker or a local service.

`benchmarks/`: GAIA, τ²-bench-style stateful harness, terminal-bench-style
harness, SWE-bench harness, moat suite, and an **adversarial-cost suite**
(`eval_adversarial_cost.py`): scripted money-wasting scenarios — tool loops,
token bombs, runaway iterations — each asserted CLAMPED by the cache /
output-cap / Budget ceilings; `main()` exits 1 on any unclamped scenario. All
CI-runnable on shipped fixtures.

## Observability & reliability

OpenTelemetry traces (spans carry the **full OTel GenAI semantic-convention
attribute set** — `gen_ai.operation.name` / `system` / `request.model` /
`request.{max_tokens,temperature,top_p,frequency_penalty,presence_penalty}` /
`response.{model,id,finish_reasons}` / `usage.{input,output}_tokens`, plus the
`execute_tool` tool-span attributes AND the `invoke_agent` agent-span leg
(`gen_ai.agent.name/id` around every `Agent.run`) with semconv `error.type`
stamped on failed spans — so any OTel-native backend reads them
with no custom mapping), Prometheus `/metrics`, and a **Sentry performance
tab** (all opt-in) (`observability.py`): `MAVERICK_SENTRY_DSN` (or
`[observability] sentry_dsn`) initializes Sentry tracing and every existing
`trace_span` call feeds it — a transaction at the root (episodes), child spans
inside (tools) — sample rate via `MAVERICK_SENTRY_TRACES_SAMPLE_RATE`, PII off,
`[sentry]` extra; per-tool latency profiles + extended stats
(`tool_latency.py`); **tail-latency hunting** (`tail_latency.py`, `GET
/api/v1/diag/tail-latency`): flags tools with a fat tail (p99/p50 ≥ ratio) —
usually fast, occasionally terrible — which is where the bug hides, not just
the slowest by p95; opt-in per-tool **latency budget** (`latency_budget.py`) and
cross-span **budget propagation** (`latency_span_budget.py`); **tiered storage**
(`tiered_storage.py`, opt-in `[world_model] cold_dir` + `archive_after_days`):
archive old episodes/goal_events to cold parquet (pyarrow when present, gzip
JSONL always, or **zstd** JSONL via `[world_model] cold_codec = "zstd"` + the
`[zstd]` extra — smaller/faster, with graceful gzip fallback) with
write-before-delete safety, fact-pinned rows kept hot, and `read_cold`
(every codec, mixed dirs OK) so archives stay queryable; **speculative tool execution**
(`speculative_tools.py`, opt-in `[tools] speculative`): pre-execute predicted
read-only (`parallel_safe`) tool calls concurrently into the tool-output cache
— `predict_from_history` warms only calls repeated across turns; **async
compaction** (`async_compaction.py`, opt-in `[context] async_compaction`): the
expensive prefix of a conversation's history is compacted in the background
between turns and the hot path pays only a cheap tail-merge — single daemon
worker, last-write-wins, fingerprint-validated so a changed prefix never mixes
stale summaries; **cost projection at plan time** (`cost_projection.py`): token/dollar
estimates per plan step from the role's model + MODEL_PRICES, iterations
multiplier, OK/TIGHT/OVER budget verdicts; **provider migration calculator**
(`migration_calculator.py`): re-price a usage ledger on target models
(cheapest-first matrix, unpriceable rows excluded from both sides, honest
tokenizer caveat always rendered); **cross-run learning cache**
(`learning_cache.py`, opt-in `[memory] learning_cache`): memoize *verified*
sub-results across runs (required `verified_by` provenance, TTL + LRU cap,
refuses to store anything the secret detector flags); **energy/CO2
accounting** (`energy_accounting.py`, **built but unreached: nothing in
production imports it**): clearly-labeled estimates from configurable
Wh/1k-token + grid-CO2 coefficients (output tokens weighted 3x), disclaimer
always rendered; **Redis tool cache** (`redis_tool_cache.py`,
`[tools] output_cache_backend = "redis"`): cross-process/cross-host tier
reusing the same key canonicalization, namespace-scoped purge, fail-open on
any Redis error; **WAL contention audit** (`test_wal_contention.py`): pins the
16-concurrent-writers / zero-lock-errors promise + the WAL/busy_timeout pragmas
in CI; **cold-start guard** (`test_cli_cold_start.py`): `maverick --help` stays
fast (~0.1s, well under the 300ms target) because importing the CLI defers
every heavy/optional dep — a fresh-interpreter test fails CI if a module-level
`import httpx`/provider-SDK/vector-store/numpy sneaks into the import path; **query-plan regression CI** (`test_query_plans.py`): hot world-model
queries must SEARCH via an index, never full-scan; **cost-attribution API**
(`GET /api/v1/cost/by-tag` on the dashboard): spend bucketed by episode/goal
tag — the JSON face of the tag split for chargeback/BI; **real-time SSE event
stream** (`GET /api/v1/goals/{id}/events/stream`): a `text/event-stream` live
tail of a goal's events that emits each as it lands and ends on terminal status
or disconnect — tails the durable `goal_events` log so it works across the
worker/dashboard process split (the polling `/events` endpoint stays for simple
clients); **streaming tool_result**
(`ToolRegistry.set_chunk_listener`): a tool fn may be an async generator or
return a sync generator of chunks — chunks stream to the registered listener
(dashboard/TUI live view) as they're produced while the model still receives
the joined text, so the model protocol is unchanged; **tool-output cache**
for read-only tools (`tool_cache.py`) with opt-in **warm-on-start** (`[tools]
output_cache_snapshot`: persist entries to a JSONL snapshot, reload the
still-fresh ones on the next run's first lookup); **memory-leak quarantine**
(`leak_quarantine.py`): per-component watchdog that flags sustained monotonic
growth and quarantines the component for recycling (sawtooth never trips it); **network egress accounting**
(`egress_accounting.py`); **run health score** (`health_score.py`); **real-time anomaly detection**
(`realtime_anomaly.py`): the online companion to the batch cross-run
analyzer — feed a metric (latency / per-step cost / tokens) as it happens and
a rolling-window z-score flags a spike *mid-run* (a `StreamMonitor` watches
each stream independently), so a runaway is caught live, not in a post-mortem;
**replayable
trace** format (`replay_trace.py`) with **trace pinning to commit**
(`trace_pin.py`: every run stamps a `trace_meta` event carrying the
workspace's commit/branch/dirty state at start — best-effort, never blocks —
and `trace_commit()` reads it back so replays tie to exact code); **cost split by tag** (`cost_by_tag.py`) and
**provider cost-curve fitter** (`cost_curve_fitter.py`); provider health board
(`provider_health.py`); proactive **provider rate-limit predictor**
(`rate_limit_predictor.py`); shared tool-reliability layer (`tool_reliability.py`,
`retry.py`); circuit breaker (`circuit_breaker.py`); adaptive thinking budget
(`thinking_budget.py`). **Self-tuning budgets** (`budget_tuner.py`, `maverick
budget-tune`): learn a `max_dollars` recommendation from the historical
per-goal spend distribution (a high percentile + margin, so the common case
fits while a runaway still trips it), bucketable by an injected task-class
classifier; read-only — the operator sets the value. **Failure-mode telemetry** (`failure_telemetry.py`, `maverick failures`,
default-on `[telemetry] failure_modes`): a failed run records a canonical mode
(budget / auth / timeout / shield / sandbox / network / error) to a local JSONL
sink — the orchestrator tees from its budget and generic failure seams,
best-effort and a no-op when the telemetry is off — so an operator sees the
*distribution* of failures and fixes the dominant cause. Local-first, no
mandatory egress. **Continuous profiling daemon** (`profiling_daemon.py`,
`python -m maverick.profiling_daemon`, opt-in `[perf] profiling`): a sampling
profiler that periodically runs `py-spy record` against the live process and
drops speedscope/flame-graph profiles under `data_dir("profiles/")` — py-spy
samples from outside the interpreter (no GIL cost) so it's safe to leave on in
production; default-OFF, with an injectable runner/clock so the schedule is
tested without spawning py-spy.

## UX surfaces

- **i18n community portal** (`maverick_dashboard/i18n_portal.py`): the
  no-Python on-ramp for new dashboard-chrome languages — `scaffold(lang)`
  emits a fill-in catalog (every key seeded with English), `validate_catalog`
  lints a submission against the English reference (lang-code shape, missing/
  unknown keys, blank values, unbalanced `{placeholder}` tokens — a precise
  diff for a translation PR's CI), and `load_external_catalogs` /
  `merged_messages` overlay validated `<lang>.json` files from `[i18n]
  portal_dir` onto the built-ins so an operator drops in a community
  translation and the dashboard speaks it with no rebuild (malformed catalogs
  skipped, never blanking the UI).
- **Computer-use calibration + multi-monitor + vision clicking**
  (`computer_calibration.py`, `multi_monitor.py`, `vision_click.py`):
  per-axis affine calibration fitted by least squares over clicked targets
  (deterministic target grid, residual/drift report, atomic 0600
  persistence) corrects model-space clicks to screen space; a
  `VirtualDesktop` models multi-monitor geometry (negative origins,
  `monitor_at`, global/local transforms, `[computer_use] monitor` pinning)
  over lazily-imported mss; `resolve_click("the blue button")` consults the
  GUI element memory first, falls back to an injected vision seam with a
  confidence floor (`LowConfidenceError` below it, nothing memorized on
  refusal), upserts what it learns, and applies the saved calibration.
- **Hardware sensors tool** (`tools/hardware_sensors.py`, `[sensors]` extra,
  opt-in `[tools] hardware_sensors = true` or
  `MAVERICK_ENABLE_HARDWARE_SENSORS=1`, wizard step included): read host
  temperatures/fans/battery via psutil with a `/sys/class/thermal` fallback
  and an injected reader for tests; unavailable categories say "unavailable on
  this host" — readings are never fabricated. The psutil import ignores the
  process current directory so workspace files cannot hijack the optional
  dependency.
- **Voice biometric unlock — companion factor only** (`voice_unlock.py`,
  opt-in `[voice] biometric_unlock`): speaker verification over an injected
  embedder with three hard stances — a voice match **never authenticates on
  its own** (`decide()` returns `companion_ok`; callers combine it with an
  existing factor — replay/synthesis is practical), profiles are local
  embedding centroids (never raw audio, 0600) with first-class
  `delete_profile`, and the whole feature is off by default.
- **Onboarding personalization v2** (`onboarding_v2.py`): post-install
  personalization from *actual early usage* — long conversations suggest
  compaction, repeated task verbs point at templates, a high approval-denial
  ratio suggests the supervised director profile, repeated same-class
  failures surface the self-healing remedy, multi-channel use suggests
  channel niceties; every suggestion carries the observation that justifies
  it and the exact action, nothing is applied, and thin usage returns an
  honest "not enough usage yet" instead of generic tips.
- **Self-healing UX** (`self_healing.py`): a failed run is diagnosed into
  its failure class (budget exceeded, provider auth, rate-limited, shield
  block, sandbox missing, timeout, killswitch) and answered with an ordered
  list of concrete remedies — each carrying the exact command or config edit,
  with reversible config suggestions tagged; **nothing is auto-applied** —
  surfacing the fix is the healing, the human stays in charge.
- **Power-user keymap** (`keymap.py`, `[tui.keys]` /
  `MAVERICK_TUI_KEYS`, `python -m maverick.keymap [--validate]`): validated
  TUI keybindings — conflicts, unknown actions, and invalid keys are
  rejected, Ctrl-C is reserved as the unrebindable emergency exit, and a bad
  override set degrades to the stock keymap rather than an unusable one;
  `handle_key` is the pure key→action adapter the monitor/focus model
  consume.
- **Achievements** (`achievements.py`): a local-only milestone ledger
  *derived from recorded history* (never self-reported; nothing leaves the
  machine) — first/10/100 completed goals, a 5+-sub-goal swarm, 3+ channels,
  10 approval decisions — unlocking exactly once into an atomic 0600 store;
  evaluated on view, never per-turn.
- **Share links + device handoff** (`share_link.py`, `[sharing] secret`
  required — no unsigned mode): a share link is a signed, expiring,
  *read-only* token referencing a goal (carries no content; constant-time
  verification, expiry/signature fail closed); a device handoff is a
  *one-time* signed code moving a session between the user's devices —
  `claim()` consumes the nonce so a replayed/stolen-but-used code is dead
  (5-minute default TTL, expired nonces pruned).
- **Director mode** (`director_mode.py`): state an *outcome* and pick the
  autonomy level — `supervised` / `semi` / `autonomous` profiles map to the
  existing controls (consent mode, review-checkpoint intervals, a budget
  multiplier over the configured cap, plan-execute-reflect topology) so one
  choice sets the whole envelope. `direct()` is pure assembly (starts
  nothing; the hard Budget ceiling still applies at run time); profiles are
  config-overridable and unknown profiles are refused — an autonomy level is
  never guessed.
- **Predictive approvals** (`predictive_approvals.py`): learns the operator's
  historical approve/deny rate per (action, risk tier) from approval history and
  *suggests* a default — auto-approve-candidate / auto-deny-candidate /
  always-ask — with a confidence from sample size. A suggestion surfaced to the
  human, never an auto-decision; high/critical actions are never auto-approve
  candidates.
- **Channel auto-routing** (`channel_autorouting.py`, `[channels.routing]`): a
  pure decision function picking the best-fit reply channel/handler from an
  inbound message's signals (length, detected language, urgency, attachment
  types, an injected classifier) against a configurable rule table, with
  `explain()`; passthrough when unconfigured.
- **Provider-side caching analytics** (`provider_cache_analytics.py`): parses
  prompt-cache telemetry into a hit-rate / $-saved report (cache-read vs write
  vs uncached at configured prices), per-role breakdown, and "unstable prefix"
  recommendations for roles with a low hit rate.
- **Consent ergonomics** (`consent_ergonomics.py`): improves the consent UX
  without weakening it — batches related pending prompts into one grouped ask,
  renders a plain-language summary, and remembers "ask once this session" for an
  exact (action, scope) in an injected **session-scoped** store (expiring, NOT a
  persistent grant); composes with `safety.consent`, never bypassing its
  decision.
- **Static accessibility audit** (`a11y_audit.py`, `python -m
  maverick.a11y_audit --ci`, wired as a CI step): an offline structural WCAG
  pass over the shipped dashboard templates — img alt, form-control labels
  (`for`/`id`, wrapping `<label>`, `aria-label`), `<html lang>`, positive
  `tabindex`, empty interactive controls, heading-level skips — with Jinja
  placeholders treated as opaque text. Complements the live `a11y` tool
  (pa11y/axe); the audit pass fixed the two real findings it surfaced (chat
  textarea + fleet-name input now labelled).
- **TUI mouse mode** (`tui_mouse.py`, opt-in `[tui] mouse`): `maverick
  monitor`'s plan tree becomes clickable — SGR (xterm 1006) mouse tracking
  enabled/restored around the Live view, a click hit-tests its row to the
  plan-tree node and focuses/expands it (`NodeHitMap` + `FocusModel`, pure and
  terminal-free so it's unit-tested without a tty). Off by default; degrades
  to keyboard/auto-refresh on terminals that don't report mouse events.
- **CLI** — `maverick init` (wizard — with **branching paths**: a mode picker
  routes consumer users to a tailored short flow (`run_consumer`) while
  advanced users get the full step sequence, and the deployment answer
  (desktop/docker/vps/phone) filters the channel/sandbox questions that
  follow; `--fast` and `--resume` skip/restore branches), `start`, `resume`, `monitor` (Rich plan-tree
  TUI), `status --cost`, `export`, `replay`, `logs`, `ps`, `whoami`,
  `maverick diag` (circuit-breaker states, provider rate-limit counts, per-goal
  health score, cost-by-tag, and replay of a `MAVERICK_TRACE_DIR` run trace),
  `maverick config-lint` (validate `~/.maverick/config.toml` for unknown
  sections/keys + obvious type mistakes with closest-match suggestions;
  `config_lint.py`), and `maverick costs` (cross-run per-day spend from the
  recorded episode ledger; `cost_report.py`).
- **GitHub App** — `/webhook/github` (dashboard): a labeled or `/maverick`-mentioned
  issue drives a swarm that clones the repo, fixes it, and opens a PR
  (`github_app.py`, HMAC-verified). **GitLab Issues** — `/webhook/gitlab`:
  assign an issue to the bot, get a goal (`X-Gitlab-Token` constant-time
  verify, `X-Gitlab-Event-UUID` replay dedup), completing the
  Linear/Jira/GitHub/GitLab issue-trigger family (`issue_webhooks.py`).
- **N-of-M dual control** (`safety/dual_control.py`, `WorldModel.decide_approval`
  / `approval_signoffs`) — the **two-person rule** / segregation of duties: a
  high/critical-risk action needs N **distinct** approvers before it's granted
  (`[security] approvals_required`, flat or per-risk), and the requester can't
  approve their own request (`allow_self_approval = false`). Each dashboard
  approve is a vote by a verified principal; a single deny rejects; a repeat vote
  counts once (enforced by the `(approval_id, approver)` PK); `GET
  /approvals/{id}/state` shows quorum progress; a barred self-approval is 403.
  Off by default (`required = 1` = legacy single approver); SQLite migration v21.
- **Web dashboard** — run list, plan-tree, chat at `/chat`, approval queue with
  **collaborative supervision** (claim/release endpoints so two supervisors
  never double-handle a review — atomic claims, 409 on conflicts, claims
  surfaced in the pending list — plus `decided_by` attribution on every
  decision; SQLite migration v13 + Postgres parity, tenant-scoped), and
  an **oversight console** (`/oversight`): live fleet state, the approval queue,
  a per-guardrail intervention roll-up, and an inline **"why this action"
  drill-down** (the reasoning/tool chain + cost for a running agent, owner-scoped)
  (`maverick dashboard`). **Search across runs** — a live search box on the
  goals page over `GET /api/v1/goals/search` (text match on title/description/
  result, owner-scoped, decrypt-then-filter since those fields are encrypted at
  rest). **Pinned watch list** (`/api/v1/pins`, per-principal,
  most-recent-first), **saved dashboard views** (`/api/v1/views`: named
  filter/query-param sets), **annotated traces**
  (`/api/v1/goals/{id}/annotations`: human notes pinned to replay-trace steps),
  **multi-run dashboard** (`/api/v1/runs/compare?ids=…`: side-by-side
  status/events/errors for up to 8 runs), and **plain-language explanations**
  (`/api/v1/goals/{id}/explain`: a deterministic, template-rendered narrative
  of the run — `plain_language.py`, no LLM call, never hallucinates beyond the
  log). Pins/views/annotations persist tenant-aware in `ux_store.py`.
  **Run-events firehose** — `WS /ws/v1/runs/{id}/events`: a goal's events
  stream over WebSocket as they land (resume via `since_id`, terminal status
  closes the stream; auth mirrors the HTTP policy and OIDC applies to WS).
  **Inline cost preview** — `GET /api/v1/goals/{id}/cost-preview`: plan-time
  token/dollar projection + OK/TIGHT/OVER verdict before a goal runs.
  **"Why this cost" drill-down** — `GET /api/v1/goals/{id}/cost-breakdown`:
  spend decomposed by episode outcome (dollars/tokens/counts).
  **Replay export to MP4** — `replay_video.py` +
  `GET /api/v1/goals/{id}/replay-storyboard`: a run rendered to a watchable
  video — the deterministic core builds a captioned frame storyboard with
  per-step durations from the event gaps (secret/PII-scrubbed), then encodes
  via Pillow + the sandbox-mediated ffmpeg tool when present; when the video
  stack is absent it still emits the frame manifest + the exact ffmpeg command
  for out-of-band encoding (no new hard dependency).
  **Run gallery** — `GET/POST/DELETE /api/v1/gallery[/{id}]`:
  deployment-wide curation of exemplary runs (blurb + curator attribution,
  upsert, capped), each entry enriched with live status and links to the
  tutorial/explain exports; access-checked per viewer.
  **Run-as-tutorial export** — `GET /api/v1/goals/{id}/tutorial.md`
  (`tutorial_export.py`): the run rendered as step-by-step markdown (goal →
  approach → steps with preserved code fences → dead ends → outcome),
  deterministic templates over the event log, secret-scrubbed, no LLM call.
  **Cross-run anomaly detection** — `GET /api/v1/goals/{id}/anomalies`
  (`cross_run_anomaly.py`): a run scored against the deployment's behavioral
  baseline — novel event kinds (high), event-volume spikes (runaway-loop
  signal), error-rate spikes — conservative 3σ thresholds, silent on cold
  deployments (<5 baseline runs); signals for a human, not verdicts.
  **Cost anomaly alerts** — `GET /api/v1/cost/anomalies`: per-goal spend
  outliers above mean + Nσ over the recent window (needs ≥3 priced goals).
  **Accessibility + i18n** — a font axis independent of the theme
  (`?font=dyslexic` / cookie: OpenDyslexic-preferring stack with wider
  letter/word spacing, composes with the high-contrast theme) and **chrome
  i18n in en/fr/de/ja/zh** (`maverick_dashboard/i18n.py`: dict catalog +
  `t()` template helper; `?lang=` → cookie → `Accept-Language`; user data is
  never translated; catalog-completeness pinned by test).
- **Cost** — per-run reports, live cost meter, `maverick start --dry-cost`
  forecasting (`cost_forecast.py`).
- **Templates / marketplace v2** — starter-goals library + community template
  registry (`maverick template browse/add`), **hash-verified installs**
  (`catalog.py` sha256 pinning), **ratings**: indexes carry display-only
  `rating`/`ratings_count` aggregates (clamped, malformed-safe), `browse`
  renders ★-bars, and a local ledger (`marketplace_ratings.py`,
  `maverick template rate <name> <1-5>` / `ratings-export`) keeps your own
  ratings ready for an index-PR submission — no hosted ratings service.
- **Marketplace stats** — `GET /api/v1/marketplace/stats` (`marketplace_stats.py`):
  aggregates the local ratings ledger into total / average / 1–5★ distribution /
  per-kind breakdown / top-rated — the JSON face of the stats view. Self-host-first
  (the operator's own ratings; no hosted community service); pure aggregation.
- **Skill validator service** — `POST /api/v1/skills/validate` on the
  dashboard: lint a SKILL.md body (same linter as `maverick skill validate`)
  from CI or an editor against a self-hosted instance; size-capped, nothing
  persisted.
- **Localized money display** — `format_money` tool (`money_format.py`): format
  an amount per a (locale, currency) pair — symbol placement, grouping/decimal
  separators, the currency's decimal places — with an optional operator-supplied
  FX rate (`$1,234.56` → `1.234,56 €` → `¥1,235`). Offline display layer,
  distinct from the live-FX `currency` conversion tool; a curated locale subset.
- **DuckDB analytics** — `maverick analytics` (`duckdb_analytics.py`, `[duckdb]`
  extra): load the world model's goals/episodes into an in-memory DuckDB and run
  OLAP over the history — per-goal cost percentiles, time-bucketed spend, top
  goals, and **ad-hoc read-only SQL** (`--sql`, refuses anything but
  SELECT/WITH). This is the analytical use DuckDB is actually good at; the live
  transactional world model stays SQLite/Postgres (DuckDB is the wrong engine
  for the concurrent write path).
- **Cost retrospective** — `maverick cost-retro` (`cost_retrospective.py`): a
  spend review over the recorded per-goal/episode costs — the costliest goals,
  how much went to **failed** work (effort with no delivered result), how
  concentrated spend is (a Pareto signal), and rule-based observations to act
  on. Deterministic over the world model; read-only.
- **UX cluster (2028-H1)** — **plan-tree minimap** (`plan_minimap.py` +
  `GET /api/v1/goals/{id}/minimap`): a compact glyph-per-node subtree render
  with a depth budget; **multi-tenant overview** (`/tenants/overview`,
  admin-only like `/tenants`): per-tenant goal rollups + today's spend +
  suspended flags; **replay annotation export** (`annotation_export.py`):
  a run's annotations to markdown or SRT-timed cues; **personalized starter
  templates** (`starter_templates.py` + `GET /api/v1/templates/suggested`):
  the catalog ranked from the user's own goal history; **adaptive UI
  density** (comfortable/compact via `?density=`/cookie/config); **pluggable
  themes** (`themes.py`: `[dashboard] themes` rendered as CSS variables,
  strict `#hex` validation so config can't inject CSS); **templates
  marketplace** (`/templates`: catalog + ratings, "use" prefills the chat
  form — no auto-start); **live voice captions** (`live_captions.py` + SSE
  `GET /api/v1/voice/captions`): a rolling caption window over an injected
  transcript source (finalized vs in-flight, word-boundary trimming);
  **dashboard voice commands** (`POST /api/v1/voice/transcribe` + a mic
  button on the chat composer): record a spoken goal in the browser
  (MediaRecorder), re-encode it to 16 kHz WAV client-side (`_voice_mic.js` —
  no server ffmpeg needed), transcribe it server-side through the kernel STT
  backends (OpenAI/Groq Whisper, local faster-whisper, or the built-in local
  whisper.cpp engine, warmed at dashboard startup with a 503+Retry-After
  "warming" handshake while the first-run model download finishes — via
  `tools/voice.py`), and
  fill the goal field for review — speech never auto-submits; falls back to
  the browser's own speech recognition when no STT backend is configured;
  on by default, off via `[voice] dashboard_commands = false` /
  `MAVERICK_VOICE_COMMANDS=0`; the goal page's clarifying-question reply
  box has the same mic, and finished results get a **read-aloud** button
  (`POST /api/v1/voice/speak`: kernel TTS backends with the voice-safety
  redaction pass, browser speechSynthesis fallback).
- **Browser extension** (`extensions/browser/`, opt-in `[dashboard]
  allow_extension` plus `MAVERICK_DASHBOARD_TOKEN` — fail-closed CORS scoped
  to extension origins only): a
  Manifest-V3 WebExtension (no build step, loopback-only host permissions,
  `script-src 'self'`) with popup chat against the existing goals API and a
  "send this page" action shipping title/URL/selection as goal context;
  static tests pin the manifest security properties (no remote code).
- **ARIA-first navigation** (`tools/aria_navigate.py`, registered with the
  browser tool): drive the page via the accessibility tree — `snapshot`
  (stable node ids), `find` (role+name), `activate` (click/focus by node) —
  the live counterpart to the static `a11y_tree` extractor.
- **WebRTC tool** (`tools/webrtc_tool.py`, `[webrtc]` extra): data-channel
  offer/answer/send/close over lazily-imported aiortc (signalling is the
  caller's; media tracks out of scope, stated honestly). **Built but not
  registered by default** — nothing in production imports it; a caller opts in
  by registering the factory. It holds a bidirectional channel to an arbitrary
  peer, so it is tiered `high` risk and containment-denied alongside
  `websocket`.
- **Audio understanding** (`tools/audio_understanding.py`, `[clap]` extra):
  zero-shot NON-SPEECH classification — a CLAP model embeds the clip and
  free-text labels ("glass breaking", "dog barking", "fire alarm") into one
  space and ranks them; `op=embed` returns the raw audio embedding. The
  embedders are injected seams (ranking math tested offline); the default
  adapters lazy-load transformers' ClapModel (`MAVERICK_CLAP_MODEL`,
  workspace-confined paths, stdlib-only WAV decode).
- **Conversational supervisor** (`conversational_supervisor.py`): natural-
  language supervision of running work — a deterministic intent grammar
  (reusing the voice-command compiler, with an optional llm seam that
  re-parses paraphrases through the *same* grammar, never a guess that
  mutates) answers reads ("what's running?", "how much today?", "what
  failed?") from cheap indexed passes over the world model + usage ledger,
  and routes mutating intents (pause/resume/reprioritize) through a strict
  `as_bool` confirm gate to world-model methods that actually exist
  (pause = status `blocked` + a supervision event; prioritize = a
  `goal:<id>:priority` fact — stated in the docstring).
- **Voice-only mode** (`voice_only.py`, `[voice] only_mode`, default OFF):
  an all-speech session loop — injected utterance source → handler →
  injected `speak` seam (default routes the TTS path, which redacts) — with
  a tested deterministic speech-shaping pass that turns markdown/code into a
  spoken summary ("I wrote 40 lines to app.py"). Mic capture + playback
  hardware are the operator's adapters behind the seams.
- **Voice macros** (`voice_macros.py`): named multi-step command sequences
  ("morning routine" → status, failures, summary) triggered by one phrase;
  persisted 0600, each step **re-validated against the grammar at trigger
  time** (a smuggled unparseable step is skipped, never dispatched) and
  risky steps keep their confirm gates **individually** — a macro never
  pre-authorizes. Bounded step count.
- **Augmented terminal charts** (`terminal_charts.py`, `maverick charts`):
  inline sparklines (▁▂▃▄▅▆▇█) and bars for spend/day (usage ledger), goal
  throughput (world), and tool-latency percentiles (`tool_latency`) — the
  ASCII renderer is the tested core, `rich` panels a thin lazy wrapper;
  honest empty-state lines when there's no data.
- **Streaming voice channel v2** (`maverick_channels/streaming_voice.py`):
  the protocol layer for streaming ASR + **barge-in** — partial/final
  hypothesis events drive endpointing on an injected clock, and speech onset
  while the bot is talking halts playback immediately (`stop_speaking()`),
  preserving the interrupted reply as partially-delivered. Fully offline-
  tested with scripted event sequences; the real streaming ASR + playback
  engine plug into the seams.
- **Speech-to-action live mic** (`live_mic.py`): a hardware-free loop —
  injected chunk source → injected transcriber → the deterministic
  voice-command grammar → injected action callback, with risky intents
  behind a strict confirm gate (only a real `True` authorises; no confirm
  hook = fail-closed denied; a raising action is logged, not fatal).
  `whisper_transcriber()` builds the real adapter on faster-whisper
  (`[voice]` extra, `MAVERICK_WHISPER_MODEL`); any mic adapter that yields
  bytes plugs in.
- **Image edit tool** (`tools/image_edit.py`): the edit verbs to pair with
  replicate's generation — hosted inpaint/variation/upscale over the same
  Replicate API surface (default models are operator knobs:
  `MAVERICK_{INPAINT,VARIATION,UPSCALE}_MODEL` or per-call `model=`; local
  images inlined as data URIs) and local crop/resize/rotate via Pillow
  (`[computer-use]` extra, no key). Every model-supplied path is
  workspace-confined.
- **ASR meeting listener** (`meeting_listener.py`): consume any
  transcript-segment stream into minutes — rolling timestamped transcript,
  merged speaker turns, action items via a deterministic heuristic
  (assignment patterns, imperative openers, `action item:`/`TODO:` markers)
  with an optional llm seam that falls back to the heuristic on failure;
  `finalize()` writes the session artifact to `data_dir("meetings")` 0600.
  Injected clock, fully reproducible offline.
- **Audio diarization + emotion** (`audio_analysis.py`): honest-scope
  heuristic diarization — cosine-distance thresholding over injected frame
  embeddings with centroid label reuse (S1-S2-S1 exchanges come back
  labelled; no clustering/overlap/VAD, stated plainly) — plus zero-shot
  emotion ranking over the same CLAP seams as audio understanding
  (`[clap]` extra shared, real frame embedder included).
- **Embedded-device tool** (`tools/embedded_device.py`): JTAG + I2C access
  to the **operator's own** devices. JTAG mediates OpenOCD strictly through
  `sandbox.exec()` — halt/resume/reset, bounded memory reads (reads never
  auto-halt, so they can't silently change target state), flash write; the
  destructive ops (flash, reset) stay refused until `[embedded] allow_flash
  = true` (default OFF, wizard-exposed). I2C is a pure protocol layer over
  an injected bus seam (`smbus2` via the `[i2c]` extra). Every op names its
  explicit target — no autodetect-and-flash; failures are `ERROR:` strings.
- **Perceptual hashing** (`perceptual_hash.py`): an 8×8 average hash for
  detecting whether two computer-use screenshots show the same screen.
- **Marketplace federation** (`marketplace_federation.py` +
  `federation_envelope.py`): export/import signed listing bundles between
  instances (`maverick-marketplace-fed/1`) — import verifies the Ed25519
  envelope **fail-closed** (bad/missing signature, unknown origin, or a
  missing `cryptography` library all reject the whole envelope), enforces
  the `[federation] marketplace_peers` trust list, namespaces imports as
  `origin/name` so they can never shadow local listings, and re-runs the
  local moderation scan on every import. Ratings do NOT federate — their
  provenance can't be verified, stated plainly.
- **Channel federation** (`channel_federation.py`): forward messages
  between instances' channels over the same envelope discipline — a
  bounded 0600 outbound queue with user ids pseudonymized via per-pair
  HMAC, inbound verify fail-closed against the pinned per-origin key,
  addressed-to-us check, per-peer token-bucket rate limit (injected
  clock), and delivery into the normal handler as `channel="fed:<origin>"`
  so federated traffic hits every existing chokepoint. The HTTP binding is
  deliberately the operator's; the transport is an injected seam.
- **Marketplace donate-direct** (`marketplace_donations.py`): skill authors
  declare a donation link; validation enforces https + an allowlist of
  donation hosts (GitHub Sponsors, Ko-fi, Open Collective, Liberapay, Buy
  Me a Coffee) and the federation import strips invalid ones. Links only —
  no payment processing, no checkout proxying, no referral codes.
- **Benchmark reproducibility audits** (`benchmark_reproducibility.py`):
  every new benchmark run can carry a manifest
  (`maverick-bench-repro/1`: host fingerprint, config + input digests, env
  key presence/absence — never values); `verify_reproduction(a, b)` says
  exactly which digests differ and calls runs "comparable" only when
  config+inputs match; `audit_report()` sweeps the stored history. Two
  runs with differing digests are never claimed comparable.
- **Compaction v6 hybrid** (`compaction/hybrid.py`, *experimental — opt-in
  via `[compaction] hybrid`, wired into the live compaction path*): the
  strategy picker learned from this deployment's own outcomes —
  deterministic features over the message window, a per-(feature-bucket,
  strategy) outcome ledger (atomic 0600), epsilon-greedy with an injected
  PRNG, and an optional pure-Python logistic `fit()` (no torch) whose
  versioned weights the picker consults when present. Cold start = the
  existing default strategy; every failure falls open like all compaction
  paths. An online-learning heuristic, not a pretrained model — stated in
  the docstring. With the knob on, `compaction.plugins.compact_with` (the
  agent's live dispatcher) consults the picker, maps its abstract
  vocabulary onto the registered implementations (truncate/structural →
  heuristic, retrieval → graph, summarize → learned), and records the
  achieved shrink back into the ledger so selection improves on this
  instance's own results; an explicitly configured
  `[context] compaction_strategy` always wins, with a one-time warning
  when both knobs are set.
- **Sandbox pool: Firecracker-warm + cross-run pooling**
  (`sandbox/firecracker.py` + `sandbox/pool.py`, `[sandbox]
  cross_run_pool` default OFF): Firecracker warm mode keeps one e2b
  microVM alive between execs (the local firectl path can't, and says so
  honestly); the cross-run pool parks a still-healthy docker/podman
  backend at run end (bounded, TTL, injected clock) and hands it to the
  next run under a strict **scrub contract** — workdir re-pointed, env
  scrubbed per exec, and only engines whose `run --rm`-per-exec model
  provably carries no state are eligible (local/firecracker/ssh/k8s/
  devcontainer are excluded with reasons; they always build fresh).
- **Speculative drafting across providers** (`speculative_decode.py`): a
  cheap draft model proposes, the target verifies-or-revises in one call;
  per-(draft,target) accept-rate ledger with a floor below which it falls
  back to plain target calls — application-level drafting, explicitly not
  logit-level decoding; models resolve by role, never hardcoded.
- **Out-of-process model proxy** (`model_proxy.py`): the provider key lives
  in a separate proxy process; the agent's `base_url` points at it with only a
  proxy client token, not the provider credential — the proxy authenticates the
  caller, strips the client token, injects the real key, and allows only
  model-inference routes by default.
- **Watch glance endpoint** (`GET /api/v1/glance`): the fixed tiny payload
  the watch scaffold renders.
- **Granular redaction UI** (`GET /redact` page + `POST
  /api/v1/redact/preview`): paste text, see every secret/PII finding as a
  kind + span (never the raw value), and pick per-kind what to scrub —
  empty selection runs full provable redaction; a granular selection
  replaces only the chosen kinds' spans and *honestly* reports
  `proven_clean: false` with the residual kinds left behind, instead of a
  false guarantee. Preview-only: nothing is stored server-side.
- **Goal Map — visual graph editor** (`/graph-editor` + `GET /api/v1/goal-tree` +
  retitle/reparent/add-child endpoints): the goal forest as an interactive
  SVG node graph — server-side layered layout (pure, unit-tested), pan/
  zoom, status colors — with editing that refuses cycles and self-parenting
  (400), access-checks both ends, and creates children **pending, not
  auto-run** (stated in the UI). A keyboard path via labeled selects keeps
  it accessible.
- **Drag-and-drop goal builder** (`/goal-builder`): compose a goal from
  blocks — steps (ordered checklist), budget, channel, priority — native
  HTML5 DnD plus keyboard add/move/remove buttons, live brief preview. The
  budget block is enforced as the runner's real `max_dollars` (clamped to
  the server cap); channel/priority ride in the brief and say so.
- **Embedded analytics web component**
  (`/static/lightwork-analytics.js` + `/embed-demo`; the pre-rebrand `/static/maverick-analytics.js` URL and `<maverick-analytics>` tag still work): a self-contained
  `<lightwork-analytics>` custom element (Shadow DOM, no framework, no CDN)
  fetching the real spend + goals endpoints and hand-drawing SVG
  sparklines/bars; same-origin/token limits documented in the JS header and
  on the demo page; errors render as HTTP status, never fake data.
- **Benchmark live dashboard** (`/benchmarks` + `GET /api/v1/benchmarks`):
  per-suite trend sparklines + regression verdicts over the real
  `continuous_benchmark` history (`bench_track`), via the real
  `detect_regression`; honest empty state naming the record command. No
  fabricated competitor numbers — this page is *this deployment's* recorded
  runs.
- **Embedded video walkthroughs** (`/walkthroughs` + `POST
  /api/v1/goals/{id}/walkthrough`): standardizes `~/.maverick/walkthroughs/`,
  drives the real `replay_video.render` (sandbox-mediated ffmpeg; reports
  encoded vs manifest-only honestly with the exact argv), generates a real
  WebVTT captions track from the storyboard frames, and lists MP4s with
  native `<video controls>` + `<track>`; strict name-pattern media serving.
- **3D plan view** (`/plan-tree-3d`): raw WebGL (no three.js) point-sprite
  nodes + line edges over the same `goal-tree` endpoint, orbit/zoom,
  click-to-focus overlay; the text tree is always present in `<details>`
  as the accessible/no-WebGL representation, and the WebXR "Enter VR"
  button appears only when `navigator.xr` reports support (untested
  without headset hardware — stated on the page).
- **RTL language support** (`i18n.py` `RTL_LANGS` + `dir_for()`):
  `dir="rtl"` driven by the active language (ar/he/fa/ur) through the
  existing lang resolution, logical-property CSS in the base layout, and an
  Arabic community-seed catalog (genuinely translated starter keys, English
  fallback; he/fa/ur activate the moment a catalog lands — they're not
  offered in the picker until one does, to avoid implying support).
- **Mobile push v2** (`push_v2.py`): a device registry layered on the v1
  notify path — each device registers a backend, a minimum priority floor,
  and optional quiet hours; routing fans out only to eligible devices, with
  `urgent` always breaking through quiet hours (page-me semantics); every
  fan-out lands in a bounded delivery ledger so "did my phone get that?" is
  answerable.
- **Smart notification batching** — opt-in `[notifications]
  batch_window_seconds` (`notification_batcher.py`): coalesces the
  low/normal-priority push stream (ntfy/Pushover/Discord/Slack) into one
  windowed digest ("5 updates" with the lines folded in) so a long run doesn't
  turn a phone into a slot machine; **high/urgent** notifications cut the line
  and deliver immediately (flushing any pending batch first, so order holds). A
  daemon flusher drives the window; unconfigured, `notify()` is unchanged.

## Distribution & install

- **Packaging** — 8-package lockstep cohort (public PyPI consumption remains
  disabled until every name is reserved), a GHCR image, PyInstaller binaries,
  reduced licensed source archives, checksums, SBOMs, and Sigstore material.
  Tauri/MSI outputs remain authenticated source-bootstrap/build engineering
  artifacts rather than customer product installers.
- **Backwards-compat tooling** — `maverick migrate` (`migrate.py`): walks an
  existing config forward — real migration advisories (Twilio WhatsApp → the
  first-party Cloud API adapter), unknown-section lint with did-you-mean
  suggestions (a typo'd section silently no-ops), and a mechanical-rename
  engine that only writes behind a timestamped backup (rename table empty
  today; 2.0 renames land on it). Dry-run default.
- **Deployment blueprints** — reference architectures for **Kubernetes / AWS
  ECS (Fargate) / Fly.io / Railway** (`deploy/reference-architectures/`):
  contract-tested manifests sharing the canonical image, `:8765` dashboard
  surface, `/home/maverick/.maverick` state volume (image runs as the unprivileged `maverick` user, not root), and secrets-from-platform-store
  rule. **Devcontainer + Codespaces template** (`.devcontainer/`) mirrors CI's
  editable install so `maverick --help` and the test suite work on open.
- **Scaffold generators** — `template_generator` tool: emit a validator-clean
  `SKILL.md` (op=skill) or a `Channel`-subclass adapter scaffold with the
  start/send/stop seams (op=channel); deterministic codegen.
- **Native desktop engineering scaffold** (`apps/desktop/`): a Tauri v2 shell for the local
  dashboard — splash polls `127.0.0.1:8765/healthz` and redirects, spawning
  `maverick dashboard` as its own child when the port is closed (kills only
  that child on exit); macOS/Windows/Linux bundle targets, ships unsigned
  (installer-desktop posture), building needs Rust + Tauri CLI. It is not
  attached to product releases or claimed as a signed customer installer.
- **Windows MSI engineering build** (`apps/installer-msi/`): WiX v4 authoring set — per-user
  scope, stable UpgradeCode + MajorUpgrade, user-PATH component, a launcher
  that bootstraps the bundled wheel into the console-script target —
  plus `build.ps1` and a dispatch-only `build-msi.yml` workflow; building
  and signing happen on a Windows host (maintainer act); the output is not a
  current product-release artifact.
- **Reduced multi-arch builds** (`deploy/multiarch/`): buildx Dockerfile +
  script for exactly linux/amd64 and linux/arm64, installing the constrained
  core + shield cohort with an optional dashboard. The runtime includes native
  dependencies; RISC-V is rejected because there is no tested wheel/toolchain
  path. Use the standard image for the complete eight-package platform.
- **Hosted demo cluster blueprint**
  (`deploy/reference-architectures/demo-cluster/`): compose + k8s manifests
  for a public read-only demo — the dashboard has no global read-only flag,
  so an nginx deny-proxy (`limit_except GET HEAD`) fronts it with the
  bearer injected upstream; a seeder creates finished demo goals through
  the real world model; DNS/TLS/operating demo.maverick.dev is a
  maintainer act. Contract-tested like the other reference architectures.
- **RFCs** — [RFC 0001: Lightwork 2.0](./rfcs/0001-maverick-2.0.md) (config
  schema v2 + async-only channel SDK + connector re-homing, migration story
  riding `maverick migrate`) and [RFC 0002: Plugin API v2](./rfcs/0002-plugin-api-v2.md)
  (static manifests discovered without importing plugin code, lifecycle hooks,
  the wire shape for the gRPC plugin host) — both Draft, open for comment.
- **Self-hosted relay** — a stdlib edge service (`deploy/relay/relay.py`) that
  HMAC-signs an inbound POST and forwards it to a dashboard's `/webhook/start`
  exactly as `maverick.webhooks` verifies (replay-defended; signature
  round-trip tested) — the self-hostable counterpart to a hosted bridge.
- **Docs** — MkDocs site, [getting started](./getting-started.md), 30-recipe
  [cookbook](./cookbook/), [architecture](./architecture.md),
  [embedding guide](./embedding.md), [security hardening](./security-hardening.md),
  [comparison page](./comparison.md) (Lightwork vs the field, claims grounded in
  this catalogue), [press kit](./press-kit.md), [showcase wall](./showcase.md)
  (built-with-Lightwork submissions by PR), and a self-serve
  [observability integrations guide](./integrations/observability-partners.md)
  (OpenRouter provider, OTLP-generic tracing incl. LangSmith, Helicone via
  base_url override).
- **Long-form handbook** ([`docs/handbook.md`](./handbook.md)): the front
  door — mental model, guided tour, day-2 operations, safety posture,
  extension points, and a map of every other doc; every cited command and
  module verified against the tree.
- **Localized docs** ([`docs/i18n/`](./i18n/)): real, native-quality human
  translations of the getting-started guide into **9 languages** — Spanish,
  Japanese, German, French, Brazilian Portuguese, Korean, Russian, Italian,
  Hindi — each following its language's software-docs register, with code
  blocks/commands/paths kept byte-identical and a source-commit header so
  staleness is trackable. The **docs MT pipeline** (`docs_i18n.py`, `python
  -m maverick.docs_i18n`) machine-translates the tail under hard quality
  gates (fenced code preserved, glossary + structure verified before
  anything is written, human translations never overwritten); `--check` is
  offline, and the model resolves by the `translator` role.
- **Distribution program kits** ([`docs/programs/`](./programs/)): 24
  runnable playbooks — Summit v1 (virtual) + Summit v2 (hybrid delta) +
  Conference v3 (flagship delta), university outreach, integration
  partnerships (business half), GitHub Stars campaign, office hours,
  sponsorship tiers (incl. the tier-2 gate + renewal terms), conference
  booth, swag, ambassadors, Skill of the Year, community survey,
  foundation exploration, badge program, curriculum kit, community grants,
  regional meetups, hackathon series, localized communities, public
  roadmap voting, skill + channel certification (mechanical bars over the
  real gates), tutorial video seasons 2-4 (per-episode scripts, every
  command verified), and press kit v2 + an evidence-gated case-study
  template. Each kit reuses the shipped machinery (skill validator,
  moderation gauntlet, ratings, plugin matrix CI, sigstore/CA signing,
  retrospective generators) instead of inventing parallel process; founder
  decisions (amounts, dates, license grants) are explicitly marked, never
  invented; executing the programs is a maintainer act.
- **2.0 release machinery** ([`docs/migration-2.0.md`](./migration-2.0.md)
  + [`docs/release-checklist-2.0.md`](./release-checklist-2.0.md)): the
  operator migration playbook (rehearsable today — snapshot, `maverick
  migrate`/`schema-plan`/`config-lint` dry runs, apply, verify, rollback)
  and the release gate the maintainer cut runs through (CI matrix,
  contract checks, deprecation sunsets, migration rehearsal, LTS branch
  cut, signing). **Governance**: the Safety Steering Group charter
  ([`docs/governance/safety-steering-group.md`](./governance/safety-steering-group.md))
  and the elected-TSC charter draft with explicit launch gates
  ([`docs/governance/governance-v2-tsc.md`](./governance/governance-v2-tsc.md)).
  **Strategy**: the five-year vision essay
  ([`docs/strategy/vision-2031.md`](./strategy/vision-2031.md)), every
  backward-looking claim grounded in this catalogue.
