# Self-Harness: a governed loop that learns a model-specific harness addendum

**Status:** shipped (governed default-on) · **Module:** `maverick.self_harness`
· **Reference:** *Self-Harness: Harnesses That Improve Themselves* (arXiv 2606.09498)

## Motivation

Maverick already learns **behaviors** — skills and dream insights distilled from
experience and recalled as prompt context. But the **harness itself** (the
operating instructions a model runs under) was static and operator-owned: a
model that keeps making the same class of mistake never adjusts how it is
instructed.

The Self-Harness paper shows that treating the harness as a *model-specific,
learnable* artifact — mining a model's own failure traces, proposing minimal
edits, and regression-validating them — is worth double-digit gains
(+14–21pt pass-rate on Terminal-Bench-2.0 across MiniMax/Qwen/GLM, same models,
harness-only change). The companion paper *Understanding the Challenges in
Iterative Generative Optimization with LLMs* (arXiv 2603.23994) explains why so
few systems do this safely: the make-or-break design choices are hidden, and an
edit that overfits its own examples silently regresses unseen cases.

## What ships

A four-stage loop that **reuses Maverick's existing governance spine** rather
than adding a new ungoverned optimizer:

1. **MINE** — `mine_failures(reflexions, model_id=…)` clusters one model's
   failure reflexions into recurring *weakness signatures*. Model-specific by
   construction: a weakness mined for one model can never leak into another's
   harness (the paper's key lever). A `model_id` was added to the reflexion
   record to enable this; older lines load as `None` (backward compatible).
   Clustering is token-overlap by default; `semantic_mining` (or an injected
   `similarity_fn`) groups failures by *meaning* instead — an embedding-cosine
   seam for the live path, or the deterministic built-in `semantic_similarity`
   (token + character-trigram overlap) offline, so morphological/reworded
   variants cluster. The seam must be deterministic (it is, by default), so the
   determinism guarantee holds with the flag on; a raising seam degrades to "no
   match", never crashing the pass.
2. **PROPOSE** — `propose_addendum(sig, propose_fn=…)` produces a single
   *minimal* operating-guidance line targeting the signature. The LLM proposer
   is an injected seam: `llm_proposer(llm)` ships a reflective proposer in the
   GEPA/RPT shape (arXiv 2507.19457 / 2605.21781) — read the signature + example
   goals, write one minimal line, **fail open** to the deterministic fallback on
   any provider error. That fallback is **failure-class-grounded** (specific
   per-class guidance, not a generic "slow down" line — arXiv 2603.23994 warns
   the starting artifact bounds what the loop can learn) and runs without a
   provider, so the loop stays testable offline. Either way the line flows
   through `_sanitize_line` + the length gate. A proposer may also return a
   **structured** `{line, hypothesis}` (the LLM proposer asks for JSON and parses
   it best-effort, falling back to a plain line then the deterministic fallback);
   the *hypothesis* — a one-line "why this should help" — is carried as METADATA
   into the signed audit + provenance sidecar, never into the prompt (it is
   scrubbed like the line but not policy-screened, since it can't ride a prompt).
   With `candidates_per_signature > 1` the stage does **sealed best-of-N**: it
   draws several candidate lines for the signature and ranks them only on
   held-in development evidence. The winner is frozen before the held-out scorer
   is called; only that winner receives the held-out confirmation evaluation (or
   configured rotation battery). This avoids selecting the maximum on a test set
   and promoting from the same score. It only matters for a *stochastic*
   proposer (a deterministic one collapses to the single candidate, so the
   determinism guarantee is unchanged at the default). With no held-in
   development cases, best-of-N refuses to search on held-out data.
3. **VALIDATE** — `validate_proposal(...)` runs the paper's acceptance test: the
   edit must not regress **either** a held-in split (the mined cases) or a
   **held-out** split (unseen cases — the overfitting guard), and must help at
   least one. Scorers are injected; a live A/B needs a real model, exactly like
   `learning_rollout`'s constraints.
4. **GATE** — each validated proposal becomes a `self_improvement.Candidate` on
   the `prompt` rung and goes through `consider()`. It therefore inherits the
   evidence floor, the calibration-freeze interlock (no learning while the
   verifier is drifting), capability non-escalation, the reversibility
   requirement, and the signed learning audit. **Promotion requires
   `[self_improvement] enable`** — self-harness proposes, the shared gate
   decides.

The accepted addendum is a small per-model block **recalled into the system
prompt** at build time (`recall_addendum(model_id)`, wired into
`Agent._build_system`), keyed on the agent's resolved model so a worker's lesson
never bleeds into the orchestrator's. It is **never a mutation of the kernel
templates** — it is a file entry, and removing it is the rollback handle. This
keeps it inside the same "behavior recalled as context, snapshot + rollback"
safety model as skills and insights.

## Safety properties

- **Risk-limited and ON by default**; `[self_harness] enable = false` or
  `MAVERICK_SELF_HARNESS=0` pauses it. While paused, `recall_addendum` returns
  `""` and the prompt is byte-for-byte unchanged.
- **Trace-poisoning is closed (two layers).** The addendum is recalled into
  every future run of a model across all channels/tenants, so an attacker who
  could plant text in a failure trace could otherwise poison it. (1) `mine_failures`
  only considers **unscoped** failures — no `channel`, no `user_id` — i.e.
  operator-local runs, never remote-user-driven ones (mirrors dreaming's
  unscoped-only guard). (2) Every proposed line — deterministic *or* from an LLM
  proposer — passes through `_sanitize_line`: control chars stripped, all
  whitespace collapsed to single spaces (no multi-line break-out), secrets
  scrubbed, length bounded. A corrupt/tampered store with non-string values is
  rejected by `load_addenda` (no literal `"None"` reaching a prompt). (3) A
  **semantic policy-erosion screen** (`_erodes_policy`) refuses a proposed line
  that tells the model to disable/bypass/ignore its own safety machinery
  (validation, auth, budget, sandbox, audit, ...) — `_sanitize_line` guards
  syntax and the gate guards capability, but neither reads the MEANING of prose
  that rides in every future prompt.
- **Two gates, not one:** self-harness only proposes; promotion needs the
  self-improvement controller. A frozen verifier or a disabled controller leaves
  the store untouched.
- **No overfitting promotion:** when held-out evidence is required, a pure trade
  is rejected and held-in examples cannot inflate the shared gate's sample
  count. Production scorers return structured evidence (`success`, actual
  `samples`, `attempted`, aligned `outcomes`, `complete`, and `clean`); a dirty,
  partial, asymmetric, or budget-exhausted A/B is indeterminate and cannot
  promote. A live caller can tighten the evidence bar with opt-in floors
  (default off, back-compatible): `require_held_out` (never promote on only the
  mined examples), `min_held_out` (an unseen-case floor — a 1-of-1 "win" is not
  evidence), `min_delta` (an effect-size floor), `confidence_z` (a conservative
  lower bound on the **candidate-minus-baseline effect**, accounting for
  uncertainty in both arms, must clear the configured practical-effect floor),
  and `max_cost_factor`/
  `max_latency_factor`/`max_tool_calls_factor` (reject a line that lifts pass-rate
  while regressing **cost/latency/tool-churn**; if a factor is configured, missing
  or invalid metric evidence rejects), and `metamorphic_fn` (a
  meaning-preserving paraphraser — the held-out improvement must **survive
  paraphrasing** the unseen cases, catching a line overfit to surface wording,
  not the weakness). Duplicate or normalized-alias cases, cross-split leakage,
  incomplete transforms, and unchanged/permuted "paraphrases" reject before
  they can count as independent evidence. Mining support is per-class-adaptable
  (`min_support_by_class`). Programmatically injected legacy scalar scorers
  remain a trusted compatibility seam and cannot prove per-case completeness.
- **Reversible + audited:** every applied line has a rollback handle and a signed
  `LEARNING_UPDATE` audit row.
- **Bounded + non-eroding:** an addendum is capped (`_MAX_LINES_PER_MODEL`,
  `_MAX_ADDENDUM_CHARS`) so it can't bloat every prompt, and `_compose_addendum`
  delta-merges a re-promoted line (normalized-exact: case/whitespace/punctuation)
  rather than spending a second slot on a trivial reword — an ACE-style guard
  against "context collapse"/"brevity bias" (arXiv 2510.04618).
- **Auditable provenance:** every applied line's signed `LEARNING_UPDATE` row
  carries *why* it was learned — the weakness signature + rationale, the
  proposer's *hypothesis* (when a structured proposer supplied one), and the
  unseen-split evidence (`held_out_delta`, `samples`) — not just the text, so a
  rollback or compliance review can see the diagnostic behind each edit. The
  same provenance is also kept in a **structured per-line sidecar** (`*.meta.json`,
  keyed by a content-addressed line id) alongside the prompt-bound store — which
  stays byte-stable — reconciled to the block under the same lock, restored on
  rollback, and best-effort (a missing sidecar never affects recall).
- **Retirable (anti-staleness):** prompt guidance goes stale as models, tools,
  and APIs change. `retire_stale(older_than_days=…)` removes lines not active
  within a TTL — where "active" means last re-promoted **or** last *recalled*,
  so a line that keeps getting **used** survives even if it isn't re-promoted.
  Usage is tracked by `note_recall` (wired into `Agent._build_system`'s consumer,
  in-process throttled so the per-prompt hot path stays a pure read with at most
  one cheap write per model per interval). A line with no provenance record
  (legacy) is never auto-retired since its age is unknown. Audited (`retire`).
- **Conflict-aware:** addenda are cumulative, so a later lesson can quietly
  oppose an earlier one. `find_conflicts` / `detect_store_conflicts` flag
  suspected contradictions (shared topic, opposite polarity) into the
  `SelfHarnessReport` and `maverick self-harness conflicts` for operator review.
  An optional injected `classifier_fn` (a semantic judge, e.g. an LLM) refines
  the lexical heuristic — catching reworded conflicts it misses and suppressing
  its false positives, falling back to the heuristic per-pair on any judge error.
  Advisory only — a false positive must never silently drop real guidance.
- **Scoped recall (per-domain AND per-tool):** a line mined scoped to a
  department or a tool (`mine_bucket_by=("domain","tool")`) is stored under a
  composite key (`"<model>\x00domain=<d>"`, `"<model>\x00role=<r>"`, or
  `"<model>\x00tool=<t>"`,
  NUL-separated so it can't collide with a model id) and recalled **only for
  matching runs** via `recall_addendum(model_id, domain=…, tools=…, role=…)` —
  wired into
  `Agent._with_harness_addendum` from the run's `self.domain`, its available
  tools, and the agent's `self.role`. The "tool" a failure belongs to is the tool
  in play at failure (the last
  of the reflexion's `tools_used`); the "role" is the agent role stamped on the
  reflexion at capture — so an orchestrator lesson stops taxing worker prompts
  of the same model. Tool scopes are sorted so the recalled block
  is byte-identical regardless of registry order. Model-wide lines stay under the
  bare model id, so the default store is **byte-identical** and the determinism
  proof is unaffected.
- **Outcome-aware lifecycle:** `note_outcome` records per-line recall→success/
  failure counters plus a bounded **recent-outcomes window** (crediting the same
  model-wide + domain + tool scopes recall injected); `review_efficacy`
  re-measures a line's causal lift with the live A/B
  and **demotes dead weight** (audited `forget`, the reversible direction — never
  the promotion gate); and a `canary` line graduates or is pulled by
  `review_canaries` judging the **recent window** (legacy records with no window
  keep the lifetime-counter behavior). Outcomes
  credit **every model whose guidance the run recalled** — each agent registers
  its resolved model on `ctx.harness_models` at recall time, so worker models'
  lines accumulate counters too, not just the orchestrator's. The
  live A/B itself is the injected `score_with`/`score_without` seam, built out by
  `self_harness_eval.corpus_ab_scorers` (corpus + `run_fn`/`judge_fn`).
- **Relapse recency guard** (`[self_harness] relapse_failure_share`, 0 = off):
  lifetime counters shield a veteran line forever — a few ancient successes and
  nothing counter-driven ever re-examines it. `review_relapses` puts a graduated
  line whose recent window carries the configured failing share **back on
  canary probation** (reversible and audited, phase `relapse`; nothing removed),
  and the next cycle's canary review adjudicates it on fresh evidence.
  `relapse_min_outcomes` is the evidence floor before a window is judged.
- **Robust validation:** `holdout_rotations` cross-validates a candidate across K
  deterministic holdout folds (`corpus_kfold_splits`) and promotes only if the
  lift holds on **every** fold — a lucky single split can't carry it. In sealed
  best-of-N mode, the candidate is fixed first and the rotation battery uses
  only the sealed confirmation pool; losing proposals never touch it. The LLM
  judge supports **self-consistency** (`judge_samples` → `llm_judge(samples=)`):
  ask N times with diverse meaning-preserving framings and take the majority
  vote. Production memoization replicates candidate and baseline arms
  symmetrically and requires every configured draw to remain clean. Sealed
  development and confirmation use opposite deterministic aggregate arm order
  so one arm is not always favored by warmup/cache/provider position. Both
  rotations and replicated judging default to historical single settings.
- **Scoped validation:** a domain-scoped candidate is judged on its own
  department's ground truth. The corpus's domain keys (the loader's documented
  `{model|domain: [...]}` shape) become per-domain evaluation quads via the
  `eval_for_context` seam on `run_self_harness` — the driver auto-builds it
  (`_context_evaluator`) alongside the model-wide A/B, sharing the same
  eval-budget pot. A general corpus under-credits a narrow line; its own
  domain's cases don't.
- **Metamorphic validation, live:** `[self_harness] metamorphic` wires an
  LLM-backed paraphraser (`llm_paraphraser`, summarizer role, same budget pot)
  into the pass's `metamorphic_fn` seam and switches the corpus scorers to
  `judge_unknown` mode so the LLM judge actually scores the paraphrases (by
  default a non-corpus goal is indeterminate, which silently no-ops the check).
  A candidate whose lift doesn't survive rewording is rejected as overfit to
  surface form; `metamorphic_tolerance` allows a small slip.
- **Semantic conflict detection, live:** `maverick self-harness conflicts
  --semantic` wires `llm_conflict_classifier` (verifier role) over the lexical
  heuristic — catching contradictions that share no wording, with per-pair
  fallback to the heuristic if the judge fails.
- **Judge calibration:** `[self_harness] calibrate_judge` feeds each
  corpus-labeled verdict of the evaluation judge into the calibration
  interlock (vote-share confidence vs the operator's `expected` label, source
  `self_harness_judge`), arming the verifier-drift freeze against this loop's
  own judge — a drifting judge pauses learning instead of silently steering it.
  Risk-limited promotion additionally binds the receipt to the exact evaluator,
  requires at least 20 natural correct/incorrect examples from the current
  cycle, and rechecks that receipt immediately before artifact activation.

## Operating it

- `maverick self-harness preview [--model M] [--min-support N]` — read-only dry
  run: shows the weaknesses and the lines it *would* propose, writes nothing.
- `maverick self-harness run [--model M] [--all-models] [--retire/--no-retire]
  [--canary]` —
  run one governed **cycle**: mine → propose → validate → gate, then retire stale
  guidance. The on-demand / scheduled entry point (`run_self_harness_cycle`).
  `--canary` stages this run's promotions on probation (recalled, but graduated
  or pulled from real run outcomes by the canary review); leaving it off defers
  to `[self_harness] promote_as_canary`.
  `--all-models` runs the cycle for **every distinct configured role model** (the
  fleet, `run_self_harness_all_models`), so worker models learn their own
  harness, not just the orchestrator — each mines only its own traces.
  Without a live A/B scorer (a real model + eval harness, injected
  programmatically) it is a **dry** pass that writes no new guidance; stale-line
  retirement still runs — so a cron'd `run` keeps guidance fresh before live
  scoring is wired. Promotion still requires `[self_improvement] enable`. The
  command also surfaces **governance readiness** — if a pass promoted nothing it
  says *why*: learning is frozen (verifier drift), the promotion gate is off, or
  it was a dry pass — so a paused loop is explained, not mistaken for broken
  (`SelfHarnessReport.frozen` / `.gate_enabled`).
- `maverick self-harness show [--verbose]` — what was learned per model;
  `--verbose` adds each line's provenance (signature, held-out delta, samples,
  learned/updated dates) so an operator can judge the evidence behind it.
- `maverick self-harness efficacy --model M` — each line's recall→outcome record
  (successes / failures / rate, with the department tag) so dead weight is visible
  before it's pruned.
- `maverick self-harness canary --model M [--review]` — inspect lines on canary
  probation, or `--review` to advance the lifecycle (graduate the proven, pull the
  failing) from the accumulated counters.
- `maverick self-harness transfer --from M [--to T] [--force]` — try one
  model's proven guidance on other fleet models: graduated lines validated
  against the target's corpus through the full floors + gate (including
  `holdout_rotations` when configured), landing as canaries and never evicting
  a full target block. One-shot per (target, line) via the tried-memory — but
  only pairs judged ON THE MERITS are recorded: a budget-dead evaluation or a
  transient gate refusal stays retryable, while a `forget`/demotion writes the
  memory so a sweep can't resurrect a governed rollback. `[self_harness]
  transfer_auto` runs the fleet sweep on the dream beat.
- `maverick self-harness corpus harvest [--auto]` / `corpus review
  [--accept N] [--reject N] [--accept-all]` — bootstrap the eval corpus from
  hindsight pairs (failed goals whose wording later ran to done); staged
  candidates are human-gated by default, `[self_harness] corpus_harvest =
  "auto"` merges directly on the dream beat. Candidates are secret-redacted
  before they leave the world DB; rejections are remembered (never re-staged);
  `--accept-all --reject N` means "all but N"; merges append to the raw file
  under a cross-process lock, so hand-authored corpus fields survive.
- `maverick self-harness corpus export --out f.json` / `corpus import
  f.json [--replace]` — the hand-editing round trip (mandatory when the
  corpus lives in the world store; identical in file mode). Every
  operator-authored field survives both directions.
- `maverick self-harness corpus quality [--samples N] [--retire]` — measure
  each live case's baseline discriminativeness (a case the baseline always
  passes cannot show a candidate's lift); `--retire` prunes the dead cases,
  raw-preservingly. Harvested rows carry `added_at` so age is visible.
- `maverick self-harness retire --older-than-days N` — prune stale guidance.
- `maverick self-harness conflicts` — flag contradictory lines for review.
- `maverick self-harness log` / `forget [--domain D]` — the audit trail and the
  undo handle (`--domain` scopes the rollback to one department's block).

Config (`[self_harness]`, resolved by `config.get_self_harness` / `settings()`):
`enable`, `risk_limited`, `min_support`, the validation floors
(`require_held_out`, `min_held_out`, `min_delta`, `confidence_z`,
`max_cost_factor`/`max_latency_factor`/
`max_tool_calls_factor`, `min_support_by_class`), `candidates_per_signature`
(sealed best-of-N proposing), `semantic_mining`, `mine_bucket_by`
(per-domain/per-tool mining), `max_promotions_per_cycle`, `holdout_rotations`
(K-fold confirmation), `holdout_ledger`, `holdout_family_alpha`,
`holdout_query_alpha`, `holdout_max_queries`, `judge_samples`
(self-consistency judging), `calibration_max_age_hours`, `promote_as_canary`
(stage every promotion on canary
probation), `efficacy_review`, `eval_corpus`, `eval_budget_dollars` (spend
ceiling for one auto-evaluated cycle's LLM calls — the runner and judge share
the pot, and an exhausted budget fails the evaluation *closed*: candidates are
rejected, never promoted on partial scores), and
`retire_after_days`. Defaults preserve the
loop's historical behavior; the installer wizard ships production-safe floors
when self-harness is enabled and writes the advanced knobs (eval corpus,
per-department scoping, semantic mining, best-of-N, efficacy review, auto-retire)
when an operator opts into the advanced follow-up. `run_self_harness_pass` reads
these automatically, and the dashboard `/learned` page surfaces per-line
provenance, recall→outcome counters, canary state, the department tag, and
conflict warnings (parity with `show --verbose` / `efficacy` / `canary` /
`conflicts`).

For unattended evaluation, `risk_limited = true` selects conservative defaults
as one coherent profile: `require_held_out = true`, `min_held_out = 8`,
`min_delta = 0.02`, `confidence_z = 1.96`,
`candidates_per_signature = 3`, `max_promotions_per_cycle = 1`,
`holdout_rotations = 1`, `judge_samples = 3`,
`calibrate_judge = true`, `calibration_max_age_hours = 24`,
`metamorphic = true`, `promote_as_canary = true`,
`max_cost_factor = 1.25`, `max_latency_factor = 1.25`,
`max_tool_calls_factor = 1.10`, and `eval_budget_dollars = 5`. It also requires
an explicitly provisioned `holdout_ledger` and the exact deployed system-prompt
snapshot used by both A/B arms. The default family alpha is `0.05`, charged in
two `0.025` queries at most (confirmation and metamorphic). Invalid numeric
overrides, absent authoritative metrics, stale calibration, missing prompt
identity, or a missing/exhausted/tampered ledger fail closed. Explicit
constituent settings may tighten but cannot weaken these floors, caps, and
required controls. The profile does not turn `enable` on or enable the shared
self-improvement gate.

One provisioned ledger is one statistical study: exact case/label views are
recorded separately, but reordered corpora, metadata edits, overlapping subsets,
and evaluator/signature changes cannot mint a fresh query allowance. A new
study requires explicit provisioning of a new ledger.

Provision and independently verify the ledger before running the profile:

```console
maverick self-harness holdout provision --path /protected/self-harness-holdout.db
maverick self-harness holdout verify --path /protected/self-harness-holdout.db --json
maverick self-harness run --model MODEL \
  --system-prompt-file /protected/deployed-system-prompt.txt
```

Runtime never recreates a missing ledger, because deletion must not reset a
spent statistical budget. The SQLite ledger uses strict schema checks,
append-only triggers, full synchronization, a hash chain, atomic query charging,
and commit readback. This is a same-host durable control, not an external trust
anchor: production operators should replicate its tip to append-only/WORM audit
storage, restrict filesystem/database administration, and use an authoritative
remote broker when multiple hosts share a holdout.

Promotion is likewise a recoverable transaction. The controller durably writes
`PREPARE`, applies the exact compare-and-swap artifact revision, then writes
`COMMIT`; a proven unchanged artifact can be `ABORT`ed. Startup recovery inspects
unresolved preparations and blocks conflicting promotion until the applied
artifact and receipt agree. Prompt-addendum and verifier-artifact deployments
use this protocol, so a crash cannot silently turn a receipt-only or
artifact-only update into an accepted promotion.

- Automatic operation: set `[self_harness] auto_run = true` and the fleet-wide
  cycle (`run_self_harness_all_models`) runs as part of `maverick dream` — the
  same nightly beat that consolidates insights and turns the data-engine
  flywheel — so the harness operates itself with no second cron entry.
  Alternatively a scheduler runs `maverick self-harness run` (or calls
  `run_self_harness_cycle(...)`) directly, which executes one governed pass then
  prunes stale guidance. For live promotion the pass auto-builds its A/B from
  `[self_harness] eval_corpus` (or the caller injects a held-in/held-out scorer)
  and engages the self-improvement controller; without either the cycle is a
  safe dry pass that still retires and reviews canaries.

## Addresses paper #2's "hidden design choices"

The loop makes the previously-implicit choices explicit and tunable:
*starting artifact* = the current per-model addendum; *editability scope* = a
single appended guidance line on the `prompt` rung (never code/tools);
*credit horizon* = the failure signature mined from traces; *batching* =
`min_support` (the evidence floor) and the held-in/held-out split sizes.
