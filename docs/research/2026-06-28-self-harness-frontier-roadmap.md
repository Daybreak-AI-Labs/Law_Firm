# Self-Harness frontier roadmap — what's built, and the live-model seams that remain

**Status:** living design note · **Module:** `maverick.self_harness` (+
`self_harness_eval`, `self_improvement_runner`, `cli`, `agent`) · companion to
`docs/proposals/self-harness.md`

The self-harness improvement backlog is **built**. Every item is implemented as
real, tested, offline-verifiable code; where an item genuinely needs a live model
(generate / judge an answer), that piece is an **injected seam** with a
deterministic offline default — exactly how the existing A/B scorer,
`similarity_fn`, `metamorphic_fn`, and `classifier_fn` seams already work, all
tested offline with stubs. This note records the shape and the remaining live
wiring.

## Shipped (waves 5–19)

| Stage | What shipped | Key knob / seam |
|-------|--------------|-----------------|
| Config | operator config + wizard floors + dashboard parity | `[self_harness]`, `settings()` |
| Validate | Wilson confidence, cost/latency/tool gates, adaptive support | `confidence_z`, `max_*_factor`, `min_support_by_class` |
| Validate | metamorphic robustness (survive paraphrase) | `metamorphic_fn` |
| Propose | structured `{line,hypothesis}`, best-of-N Pareto selection | `candidates_per_signature`, structured `propose_fn` |
| Mine | semantic-similarity seam + offline default; per-domain bucketing | `semantic_mining`/`similarity_fn`, `mine_bucket_by` |
| Evaluate | reference-free A/B evaluator + eval corpus | `self_harness_eval.corpus_ab_scorers` / `corpus_split` |
| Drive | cycle (mine→gate→retire) + `self-harness run` + fleet sweep | `run_self_harness_cycle` / `--all-models` |
| Efficacy | recall→outcome counters + causal A/B demotion | `note_outcome` / `line_efficacy` / `review_efficacy` |
| Rollout | canary / staged rollout (graduate / demote) | `run_self_harness(canary=)` / `review_canaries` |
| Recall | per-domain AND per-tool scoped recall (composite key + agent wiring) | `recall_addendum(domain=, tools=)` |
| Mine | per-tool component profiles (bucket by the failing tool) | `mine_bucket_by=("tool",)` |
| Validate | holdout rotation — cross-validate across K folds, accept only if it generalizes | `holdout_rotations` |
| Evaluate | self-consistency judge (majority vote over diverse framings) | `llm_judge(samples=)` / `judge_samples` |
| Operate | efficacy / canary / domain on the CLI + dashboard; wizard writes the knobs | `harness efficacy` / `harness canary` / `forget --domain` |
| Observe | governance-readiness (frozen / gate-off) on report + CLI | `SelfHarnessReport.frozen` / `.gate_enabled` |
| Conflicts | semantic-judge seam over the lexical heuristic | `find_conflicts(classifier_fn=)` |

Every wave is opt-in, leaves default behavior **byte-identical**, keeps the
determinism proof at **11/11**, and routes any promotion through the one governed
gate. Backlog item map: #1 driver (`run_self_harness_cycle`), #2/#4 reference-free
evaluator (`self_harness_eval`), #3 eval corpus (`load_eval_corpus`/`corpus_split`),
#5 best-of-N, #6 semantic mining, #7 component profiles (per-domain AND per-tool
shipped; see below), #8 per-domain mining, #9 conflict detection, #13 metamorphic,
#14 judge-drift visibility, #16 canary, #17 efficacy, #18 scheduled retirement,
#19 LLM conflict classifier, #22 scoped recall, #23 worker-model learning. Later
robustness adds: holdout rotation (`holdout_rotations`) and self-consistency
judging (`judge_samples`).

## Remaining — operator data + one provider binding, not harness code

The harness side is done, including the **LLM-backed** evaluator (`llm_runner` /
`llm_judge` in `self_harness_eval`, same fail-open shape as `llm_proposer`, tested
with a fake provider). What's left is operator-supplied:

- **An LLM client + eval corpus**: pass your `llm` to `llm_runner`/`llm_judge`
  (or inject custom `run_fn`/`judge_fn`) and author the `{model|domain: [(goal,
  expected)]}` corpus the JSON loader + deterministic split already consume. The
  calibration-freeze interlock (surfaced as `SelfHarnessReport.frozen`) guards
  judge drift.
- **An outcome stream** wired into `note_outcome` from a trustworthy run signal
  (coding-mode test result, hindsight regression — the same ground truth
  `collect_calibration` consumes). `review_efficacy`/`review_canaries` then act on
  real correlations rather than test fixtures.

## Component-level profiles (#7) — built (per-domain AND per-tool)

Per-**domain** and per-**tool** mine + scoped recall are both shipped, on one
mechanism: a weakness is mined under a composite key `"<model>\x00domain=<d>"` or
`"<model>\x00tool=<t>"` and recalled only for matching runs. `tool` is an
allowlisted bucket dim (`_BUCKET_DIMS`); the "component" a failure belongs to is
derived from the reflexion's `tools_used` list as the tool **in play at failure**
(its last entry, via `_bucket_value`). The agent passes its available tools to
`recall_addendum(tools=…)` (sorted, so the recalled block stays byte-identical
regardless of registry order), so a `web_fetch` lesson rides only runs that can
call `web_fetch`. `note_recall`/`note_outcome` credit the same scopes, keeping
usage-retirement and efficacy in step.

## Robustness — holdout rotation (#C7) and self-consistency (#C8)

Two later additions harden the evaluation without touching the gate:

- **Holdout rotation** (`holdout_rotations`, default 1): instead of one fixed 30%
  unseen slice, `_validate_rotated` partitions the held-in+held-out pool into K
  deterministic folds (`corpus_kfold_splits`) and accepts only if the lift holds
  on **every** fold — a lower-variance generalization test that a single lucky
  split can't pass by accident. Default 1 = the historical single split.
- **Self-consistency judge** (`judge_samples` → `llm_judge(samples=)`, default 1):
  the LLM-as-judge is asked N times with different meaning-preserving framings
  (diverse reasoning paths, since the seam exposes no temperature) and the
  **majority** yes/no vote wins; a tie or all-indeterminate samples fall open to
  the deterministic heuristic. Default 1 = the single call, unchanged.

## Reachability wave (2026-07) — the built lifecycle, operable end-to-end

Three shipped mechanisms were built but not reachable from the surfaces that
drive them; this wave wires them, changing no default behavior:

- **`judge_samples` now reaches the auto-built judge**: `run_self_harness_cycle`
  forwards the config knob to `_auto_evaluator`, so self-consistency judging
  actually turns on for a scheduled `self-harness run` (it was previously only
  reachable by calling `llm_judge(samples=)` by hand).
- **Canary staging is driver-reachable**: `[self_harness] promote_as_canary`
  (default false) and `self-harness run --canary` route through a new
  `run_self_harness_pass(canary=)` parameter to `run_self_harness(canary=)`.
  Before this, passing `canary=` through the cycle raised a swallowed TypeError
  and silently returned an empty report — the staged-rollout valve existed but
  no scheduler or operator command could open it. The wizard's advanced
  follow-up writes the knob on opt-in.
- **Tool-scoped outcome credit**: the orchestrator's `_record_harness_outcome`
  now passes the tools the run actually **invoked** (from the blackboard's
  observation posts) to `note_outcome`, so a `tool=`-scoped line accumulates
  outcomes, not just recalls — without this, a tool-scoped canary could never
  graduate or be demoted by the counter-driven review. Credit is deliberately
  the *used* subset of the *available* tool scopes recall injected: an
  available-but-unused tool's guidance didn't shape the outcome.
- **Eval spend is budgeted** (kernel rule 3): `[self_harness]
  eval_budget_dollars` caps one auto-evaluated cycle's LLM calls (runner +
  judge share one `Budget`; corpus cases × A/B arms × rotations × judge
  samples multiplies fast, and the pass runs unattended). Exhaustion fails the
  evaluation **closed**: `llm_runner`/`llm_judge` re-raise `BudgetExceeded`
  (instead of degrading to `""`/heuristic, which would hit the arms
  asymmetrically and skew the measured delta), the case lands indeterminate in
  `_rate`, a fully-exhausted arm goes NaN, and the validator's finite-check
  rejects the candidate. Unset (default) = uncapped, the historical behavior;
  the wizard defaults the cap to $5 when an eval corpus is configured.
- **Worker-model outcome credit**: `Agent._with_harness_addendum` registers its
  resolved model on `ctx.harness_models` whenever guidance is recalled (the
  `ctx.skills_used` pattern), and `_record_harness_outcome` credits **every**
  registered model — not only the orchestrator's — each scoped to the run's
  domain + invoked tools. Before this, worker models' lines earned recalls but
  never outcomes, so a worker canary promoted by `run --all-models` could
  never graduate or be demoted by the counter-driven review.
- **Corpus bootstrapping** (`[self_harness] corpus_harvest = off|propose|auto`):
  hindsight pairs — a goal that FAILED (a reflexion exists) and whose wording
  later ran to DONE — become `{goal, expected}` eval-corpus candidates, the
  succeeded goal's recorded result supplying the expected hint (the strongest
  natural label run history offers; ACE's label-free caveat is why `auto` is
  an explicit opt-in). `propose` stages candidates for `maverick self-harness
  corpus review` (accept/reject before anything enters the loop's ground
  truth); harvesting rides the dream beat and `self-harness corpus harvest`.
- **Cross-model transfer** (testing the refuted "inherently model-specific"
  claim on the operator's own fleet): `self_harness.run_transfer` tries a
  source model's graduated model-wide lines (`transferable_lines` excludes
  probation + relapsing lines; scoped lines stay home) on target models
  through the SAME validation floors and governed gate, landing survivors as
  canaries. A persisted tried-memory keyed (target, normalized line) makes
  every attempt one-shot, so the nightly sweep (`[self_harness]
  transfer_auto`, default off, riding `auto_run` on the dream beat) spends
  ~nothing at steady state. Operator surface: `maverick self-harness transfer
  --from M [--to T] [--force]`; the driver builds per-target evaluators from
  the corpus on one shared eval-budget pot.
- **Per-role component profiles** (the Continual Harness direction, survey
  taxonomy's per-role axis): reflexions now carry the agent `role` in play at
  failure (goal-level captures stamp `orchestrator`), `role` joins the
  allowlisted mining dims (`mine_bucket_by=("role",)`, config-gated for all
  roles), and recall/usage/outcome scoping gains `role=` — wired from
  `Agent._with_harness_addendum` via `self.role`. A `role=orchestrator` lesson
  rides only orchestrator prompts instead of taxing every agent of the same
  model; scope ordering is `domain`, `role`, then sorted `tool`s
  (deterministic).
- **Judge calibration** (`[self_harness] calibrate_judge`, default off): every
  corpus-labeled verdict the auto-built LLM judge issues is recorded into the
  calibration interlock as a (self-consistency vote-share confidence,
  agrees-with-the-operator's-`expected`-label) sample, source
  `self_harness_judge` — so `calibration.learning_frozen` can detect **this
  loop's own judge** drifting and pause promotions, not only the coding
  verifier. Ties (the judge abstained) and unlabeled cases (`judge_unknown`
  paraphrases) are skipped. Closes the research note's open question #4.
- **Outcome recency + relapse re-probation**: `note_outcome` now also keeps a
  bounded (20-entry) per-line `recent_outcomes` window; `review_canaries`
  judges the window when present (a re-probated veteran can't hide behind
  lifetime successes; legacy no-window records keep the old behavior); and
  `review_relapses` (behind `[self_harness] relapse_failure_share`, default
  off) puts a graduated line whose recent failing share crosses the threshold
  back on canary probation — advisory-first (nothing removed, audited as
  phase `relapse`), with the *next* cycle's canary review adjudicating on
  fresh evidence. `line_efficacy`/`harness efficacy` expose the recent rate.
- **LLM-backed metamorphic + conflict seams, shipped**: `metamorphic_fn` and
  `classifier_fn` existed as injected seams with no built-in builder and no
  reachability. `llm_paraphraser` (summarizer role) now backs the paraphrase
  check behind `[self_harness] metamorphic` (+`metamorphic_tolerance`), and
  `corpus_ab_scorers` gains `judge_unknown` so the LLM judge can actually
  score paraphrased (non-corpus) goals — without it the metamorphic branch
  silently no-oped on the whole auto path (non-corpus goals were always
  indeterminate). `llm_conflict_classifier` (verifier role) backs
  `self-harness conflicts --semantic`. Both share the cycle's eval-budget pot
  / fall back safely.
- **Self-scheduled operation** (`[self_harness] auto_run`, default off):
  `maverick dream` — the platform's existing nightly learning beat — also runs
  the fleet-wide harness cycle (`run_self_harness_all_models`) when the knob is
  on, following the data-engine flywheel's opt-in/never-breaks-dreaming
  pattern. `run_self_harness_cycle` was documented as "to be called by a
  scheduler" but nothing in the platform scheduled it; now the loop operates
  itself off the cron entry operators already have.
- **Domain-keyed corpus validation**: the corpus loader always documented
  `{model|domain: [...]}` keys, but only the model key ever reached
  validation. `run_self_harness` gains an `eval_for_context` seam (default
  `None` = unchanged): given a signature's mining context it returns the
  `(held_in, held_out, score_with, score_without)` quad to judge that
  signature by. The driver builds it from the corpus's domain keys
  (`_context_evaluator`, sharing the cycle's one eval-budget pot), so a
  `domain=finance` weakness is validated against the finance cases instead of
  the general pool — and a domain-keyed-only corpus now promotes its
  departments' lines even when the model-wide A/B can't build.

## Wave: adversarial review hardening (2026-07-05)

A 10-angle / 11-verifier adversarial review of the transfer + corpus-harvest
wave confirmed and fixed a cluster of lifecycle-integrity defects:

- **Merits-only tried-memory**: `run_transfer` no longer records pairs whose
  evaluation was indeterminate (budget-dead arm — `validate_proposal`'s
  `_INDETERMINATE_REASON` sentinel) or whose gate refusal was environmental
  (self-improvement off, calibration freeze, evidence floor); judged keys
  flush per target, targets are deduped, and `forget`/demotion now WRITES the
  memory so a nightly sweep can't resurrect a governed rollback.
- **Fail-closed evaluation under budget death**: `_rate` treats a
  `BudgetExceeded` as invalidating the whole arm (NaN, never a prefix mean
  compared against a full-corpus memoized baseline with a fabricated Wilson
  n); `_memo_scorer` never caches an indeterminate arm; the transfer
  paraphraser is memoized per goals-tuple (one batch per target per sweep).
- **Gate parity**: transfer honors `holdout_rotations` (same ALL-folds rule as
  the home cycle) and respects `_MAX_LINES_PER_MODEL` capacity — a transfer
  canary can no longer evict a target's graduated lines.
- **Corpus integrity**: stage/merge/resolve serialize under a cross-process
  lock on the live corpus path; merges append to the RAW file (operator
  fields survive); harvest candidates are secret-redacted before leaving the
  world DB; missing timestamps fail the hindsight guard CLOSED; rejections
  are remembered (`.rejected.json`) and excluded before the candidate cap;
  `--accept-all --reject N` means "all but N"; out-of-range review indexes
  are hard errors and the CLI echoes exactly what it resolved.
- **Honest CLIs**: interactive `transfer`/`corpus harvest` pass
  `raise_errors=True` (a write failure is no longer indistinguishable from
  "nothing to do"); the wizard uses a select for the harvest mode and warns
  when nightly knobs are configured without `auto_run`/a corpus.
- **Isolation parity** (from the follow-up security review, which otherwise
  found no new exploitable surface): the harvest's goal side now skips
  owner-scoped goals, matching the reflexion side's unscoped-only filter --
  one tenant's goal text is never mined into the shared eval corpus.

## Wave: gap closure (2026-07-05, "do it all")

Every remaining tractable gap from the post-merge assessment, in one wave:

- **Statistics**: the memoized baseline moved into `_auto_evaluator` /
  `_context_evaluator` (cycle, rotation folds, efficacy review, and transfer
  all stop re-buying the same measurement), AVERAGES two clean draws before
  freezing (halving the correlated-draw variance), and never caches a dirty
  arm — `llm_runner`/`llm_judge` count fail-open degradations and
  `corpus_ab_scorers` exposes `last_clean`, so an outage 0.0 can no longer
  masquerade as a real baseline. `validate_proposal` treats a
  `BudgetExceeded` mid-paraphrase as an INDETERMINATE rejection (never a free
  pass on the robustness check). `_validation_floors(st)` is now the one
  source of floors for the pass, the cycle, and transfer.
- **Proof**: 9th guarantee, *rollback durability* — a forgotten line is never
  resurrected by a transfer sweep, a rejected candidate never re-stages, and
  an indeterminate verdict stays retryable (merits-only one-shot).
- **Corpus quality lifecycle**: harvested rows carry `added_at` (raw file
  only); `maverick self-harness corpus quality [--retire]` measures each live
  case's baseline discriminativeness on the eval budget and prunes dead
  ground truth raw-preservingly.
- **Sealing parity**: the machine-owned pending/rejected sidecars are sealed
  under at-rest encryption (loaders unseal transparently, plaintext-tolerant);
  the live corpus stays operator-editable plaintext by design.
- **Surfaces**: /learned resolves staged candidates in-page (admin POST, same
  verdicts as the CLI, stale indexes are a 409 + re-list) and shows the
  transfer-memory stats; CLI echoes of stored goal text go through the
  terminal-control stripper repo-wide (status/history/ps/review).
- **Ops**: `reflexion.list_recent` tail-reads the log; the learning stat
  counts the full ledger; the harvest CLI defers key/path defaulting to the
  runner.
- **Deferred at the right altitude**: multi-node shared learning state is a
  DESIGN (docs/proposals/fleet-learning-state.md — world-DB tables behind a
  store seam, battery-verified on both backends) and live-LLM validation is a
  deploy-night checklist in that proposal, not something a keyless CI can
  fake.

## Wave: fleet learning store, phase 1 (2026-07-05)

`docs/proposals/fleet-learning-state.md` moved from design to code for the
three LEARNING stores: `[self_harness] store = "world"` (default `"files"`,
byte-identical) routes the addenda map, the provenance sidecar, and the
transfer tried-memory to world-database tables (`maverick.learning_store`;
SQLite + Postgres ladders at v25, migration governance regenerated), so a
multi-host fleet learns as ONE — transfer sweeps see every host's guidance
and outcome evidence aggregates. The seam: an explicit store path always
means the file store at that path (tests/tenant redirection unchanged); the
default location resolves to the configured store. Postgres read-modify-writes
serialize under a session advisory lock; `maverick self-harness migrate-store`
imports a host's files (idempotent, merge-on-conflict, verified, files
renamed to backups). The proof battery gained its 10th guarantee, *fleet
store parity*: the same seeded workload learns identical content in both
stores, 8/8 concurrent promotions survive the DB path, and the audited
forget round-trips.

**Phase 2 (same day)**: the corpus family joins — `harness_corpus` rows at
v26 (kinds live/pending/rejected + `extra` preserving non-list top-level
entries), routed at the eval module's single read/write funnels keyed off
the configured `eval_corpus` path, DB-side RMW lock in every corpus writer,
`migrate-store` carries the corpus files, and `corpus export`/`corpus
import [--replace]` keep hand-editing first-class (every operator field
survives the round trip). Battery guarantee 11, *fleet corpus store*. The
proposal is COMPLETE.

## Principle (unchanged across every wave)

**Off-by-default / byte-identical default**, **determinism proof 11/11**, and **the
governed gate decides promotion** (demotion uses the reversible audited `forget`
path, never the gate). Scoped recall keeps the default store byte-identical
because model-wide lines stay under the bare `model_id` key — the composite
namespace only appears when domain mining is on.
