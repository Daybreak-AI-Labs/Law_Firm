# Risk-limited DGM and self-learning, 2026-07-14

## Decision

Lightwork now has a substantially stronger experimental self-improvement
protocol. It does **not** yet have evidence for a "beyond SOTA" claim.

The implementation objective in this pass was therefore twofold:

1. incorporate promising frontier mechanisms without weakening governance; and
2. make future superiority claims falsifiable under a sealed, equal-budget
   benchmark rather than inferred from architecture or unit tests.

The production-oriented paths fail closed. Compatibility paths remain opt-in
development tools and must not be presented as independent evaluation.

## 2026-07-15 implementation addendum

The stock code-rung runner is now explicitly a research-only search path. It
cannot promote or apply code, and persistent archive-parent branching remains
disabled until development and confirmation provenance are durably separated.
Before a coding-model proposal, it materializes one bounded Git-tracked source
capture and binds both the proposal context and evaluator to that same capture;
any later capture change aborts the run. Concurrent archive writers merge under
a strict cross-process transaction, and corrupt existing lineage evidence is
never silently replaced.

Candidate execution now requires a non-host, no-egress, non-root sandbox that
also attests bounded output capture and authenticated structured test results.
No bundled backend currently satisfies that complete contract, so the stock
runner deliberately refuses before provider or candidate execution. This is a
closed security boundary, not a claim that autonomous code evolution is live.

The broader learning/runtime pass also propagates global HALT through proposal,
evaluation, persistence, rollout, factory-learning, and skill-distillation
boundaries; requires proof-bearing rollout/rollback receipts; hardens tenant
budget reservation settlement; and treats agent/fleet exchange as an untrusted
signed boundary with durable replay and idempotency state. Workflow generation
and the JD agent factory now screen provider inputs/outputs, bind tool names,
enforce caller scope before ranking, and pin triggered templates to an immutable
tenant-bound revision.

These changes improve the protocol and its failure behavior. They still do not
establish comparative performance, seal the development corpus from the
candidate, provide an externally anchored evaluation broker, or justify a
frontier/superiority claim.

## Threat model

An adaptive agent can overfit task wording, evaluator quirks, model-judge
preferences, repeated holdout feedback, operationally cheap shortcuts, or the
promotion mechanism itself. A crash or concurrent writer can also publish an
artifact without its receipt, or a receipt without the artifact. These are
security failures because the system can learn to maximize the measurement
while moving away from the operator's intent.

The controls below assume the trusted controller, filesystem account, evaluator
broker, and operator remain trusted. They do not make same-host secrets safe
from arbitrary code running with the same privileges. Untrusted candidates must
run out of process with capability, network, secret, and resource isolation.

## Primary-source synthesis

| Work | Mechanism relevant to Lightwork | Implementation decision |
|---|---|---|
| [Darwin Godel Machine](https://arxiv.org/abs/2505.22954) | Branching archives and empirical self-improvement through modification and evaluation | Preserve diverse lineages, but separate adaptive development fitness from adoption evidence |
| [Huxley-Godel Machine](https://arxiv.org/abs/2510.21614) and its [reference repository](https://github.com/metauto-ai/HGM) | Clade metaproductivity, Thompson-style allocation, decoupled expansion/evaluation, and best-belief selection | Add an experimental clade-aware config search with exact budgets and conservative final development selection |
| [Group-Evolving Agents](https://arxiv.org/abs/2602.04837) | Group evolution and experience exchange across agents | Keep as frontier work; sharing must carry provenance and cannot collapse independent evidence |
| [AlphaEvolve](https://arxiv.org/abs/2506.13131) | Evaluator-driven evolutionary optimization with executable feedback | Retain injected executable evaluators and require independent promotion evidence |
| [GEPA](https://arxiv.org/abs/2507.19457) | Reflective trajectory feedback and Genetic-Pareto search | Retain reflection-rich proposing; a full multi-objective Pareto archive remains future work |
| [Self-Harness](https://arxiv.org/abs/2606.09498) | Weakness mining, minimal harness proposals, and held-in/held-out regression validation | Preserve the proposal loop, but seal best-of-N selection and harden evidence completeness |
| [Continual Harness](https://arxiv.org/abs/2605.09998) | Reset-free adaptation across multiple agent components | Keep component-aware learning as a direction; current proof is narrower than full continual co-adaptation |
| [Reusable holdout](https://proceedings.neurips.cc/paper/2015/hash/bad5f33780c42f2588878a9d07405083-Abstract.html) | Adaptive reuse can invalidate ordinary holdout guarantees | Treat every sealed evaluation as a durable, pre-authorized statistical spend |
| [STOP](https://arxiv.org/abs/2310.02304) | Scaffold self-improvement can optimize the improver itself | Keep mutation capability-bounded and promotion outside candidate authority |
| [School of Reward Hacks](https://arxiv.org/abs/2508.17511) | Reward-hacking behavior can transfer beyond the original training setting | Treat evaluator gaming, provenance, and tamper resistance as safety endpoints |

Reported results belong to those papers' environments. This pass borrows
mechanisms; it does not reproduce their reported performance.

## Implemented protocol

### 1. HGM-inspired metaproductive development search

`maverick_evolve.metaproductive` adds a lineage tree with:

- separate, validated expansion and exact agent/case evaluation budgets;
- at most one observation for each node/case pair;
- complete-clade Beta evidence for Thompson-style parent allocation;
- node-level allocation that prevents new branches from being starved;
- a conservative Wilson lower bound for the fixed development champion;
- no eviction, because removing a descendant would rewrite every ancestor's
  metaproductivity statistic;
- strict bidirectional tree/evidence validation, detached inputs/outputs, and
  budget charges for duplicate or failed mutations; and
- explicit case-family binding for archive reuse. Canonical `EvalCase` data is
  included, the external identity must cover evaluator/check semantics, and
  opaque cases make the evidence deliberately non-resumable. Unequal case
  weights are rejected until the posterior models them honestly.

Outcomes are Bernoulli by default. Fractional outcomes require the explicit
`allow_fractional_outcomes` heuristic mode. In either mode, overlapping clades
make the posteriors correlated: they are exploration heuristics, not calibrated
promotion evidence. The archive is currently in-memory and not thread-safe.
This is HGM-inspired work, not a complete HGM implementation.

### 2. Risk-limited DGM confirmation

`evolve_with_eval`, `evolve_metaproductive_with_eval`, and
`evolve_continuous` can now enforce a prevalidated risk contract before the
first adaptive development evaluation:

- an immutable sealed confirmation snapshot is required;
- at least 20 positive-weight cases are required by default;
- Unicode/whitespace-normalized duplicate prompts are rejected instead of
  inflating effective sample size;
- the caller must bind both the sealed family and evaluator with SHA-256 ids;
- a durable authorization callback must return a fresh, nonce-bound structured
  permit for the exact seed/champion comparison before either arm or scorer
  receives sealed data;
- the seed and frozen champion each receive exactly one evaluation per case;
- arm order is counterbalanced across the immutable case order;
- the weighted paired difference must have a distribution-free Hoeffding lower
  bound strictly above the practical margin; and
- a tie, error, malformed permit, missing authorization, or insufficient lower
  bound keeps the seed.

No holdout budget is spent when search returns the unchanged seed. Continuous
evolution does not publish an unconfirmed archive. Resumed archive configs are
revalidated against the declared config envelope and rescored; persisted scores
are observations, not authority. Archive envelopes use canonical JSON, full
SHA-256 identities/checksums, strict versions, finite values, and size bounds.

The permit binds the request digest, critical value, authorization id, durable
ledger tip, and a maximum one-hour validity window. The durable authorizer is
dependency-injected because custody differs by deployment. Its family/evaluator
digests must bind the complete task and label manifest, grader implementation,
model/system snapshots, study epoch, and query policy. Prompts alone are not a
complete evaluator identity. Risk mode also requires a fresh, internally
coherent calibration receipt bound to the exact evaluator. Its floor applies to
at least 20 natural correct/incorrect examples rather than probe-inflated
totals, readiness is checked again after sealed evaluation, and the development
freeze override is ignored.

### 3. Sealed Self-Harness selection and complete evidence

For best-of-N proposing, candidates compete only on held-in development cases.
One winner is frozen before any proposal receives held-out confirmation. Losing
proposals never touch the sealed set, and the system refuses best-of-N search
when no development split exists.

The automatic evaluator produces structured, per-arm evidence: successes,
samples, attempts, aligned outcomes, completeness, cleanliness, budget status,
and authoritative operational metrics. Candidate and baseline receive symmetric
replication. Partial, dirty, asymmetric, mixed opaque/structured,
budget-exhausted, non-finite, or rate/outcome-inconsistent evidence rejects.

Promotion uses held-out-only denominators when held-out cases exist. Both the
observed lift and its candidate-minus-baseline lower confidence endpoint must
clear the configured practical-effect floor; a large held-in lift cannot
launder weak confirmation evidence. Paired outcomes are used when available.
Configured cost, latency, and tool-call caps require authoritative measurements;
missing metrics cannot pass.
Operational collection is response-local, so concurrent arms cannot spoof or
borrow a shared budget delta.

Metamorphic evaluation is pre-authorized before the paraphraser sees the source
holdout, bound to its model/protocol identity, and fails closed on transformation
or scorer failure. Normalized duplicate, unchanged, permuted, cross-split, or
malformed cases are rejected before scoring. Sealed paths counterbalance which
aggregate A/B arm runs first across development and confirmation; a paired
per-case evaluator remains preferable and is tracked below. Risk-limited
evaluation also binds the exact deployed system prompt used by both arms.

Legacy programmatic scalar scorers remain a trusted compatibility seam. They
cannot prove per-case completeness, pairing, or operational measurements.

### 4. Durable holdout query accounting

Risk-limited Self-Harness requires an explicitly provisioned SQLite ledger:

```console
maverick self-harness holdout provision --path /protected/self-harness-holdout.db
maverick self-harness holdout verify --path /protected/self-harness-holdout.db --json
maverick self-harness run --model MODEL \
  --system-prompt-file /protected/deployed-system-prompt.txt
```

One provisioned ledger is one explicit statistical study and therefore one
global query/alpha budget. Exact normalized case/label views remain separately
auditable, but corpus edits, reordered rows, ignored metadata, unrelated scopes,
or overlapping subsets cannot mint a new allowance. The fixed policy charges
before exposure inside `BEGIN IMMEDIATE`, uses full synchronization, verifies an
append-only SHA-256 chain, rejects unexpected schema objects, disallows hard
links/path replacement, and reads back committed authorization. Query counts and
alpha are never refunded. Runtime refuses to create a missing ledger, so file
deletion cannot silently reset the budget.

This is durable same-host accounting, not cryptographic proof against a
privileged administrator. The hash chain is not keyed and its tip needs an
external append-only/WORM anchor. Multi-host fleets should use one authoritative
remote evaluation broker rather than copied local ledgers.

### 5. Recoverable promotion transactions

The promotion journal is append-only, fsynced, SHA-256-linked, and protected by
a strict cross-process lock. Its JSON projection is derived and repairable.
Prompt-addendum and verifier-artifact changes use:

1. durable `PREPARE` with exact before/after artifact revisions;
2. atomic compare-and-swap artifact activation;
3. durable `COMMIT`, or `ABORT` only with proof that the before revision remains;
4. startup recovery that inspects unresolved transactions and blocks conflicts.

Malformed chains, invalid transitions, duplicate identities, torn events, lock
failure, ambiguous artifact state, and commit failure fail closed. Verifier
promotion uses the dedicated `evaluator` authority rung rather than the policy
rung and requires recovery before serving an artifact. Its acceptance test
compares the exact deployed incumbent and immutable staged challenger on one
frozen newest temporal cohort, macro-averages by independent task, requires a
paired quality lower bound above the controller margin, and requires paired raw
MSE and clipped-Brier upper bounds to show non-regression. An `O_EXCL`, fsynced
receipt consumes that audit cohort before scoring, so rejection, crash, and
retry cannot adaptively reuse it.

The evaluated cohort, incumbent revision, and challenger digest are carried
through the promotion evidence and checked again immediately before activation.
Staged bytes are read once for hash plus parse. Serving reload accepts only the
exact artifact authorized by the durable ledger chain (or an explicit bootstrap
digest), and content-addressed predecessor snapshots plus a strict rollback map
allow a committed rollback to restore ledger and runtime state after restart.

The journal still needs an external trust anchor and a governed retention/GC
policy for old artifact versions.

The DGM archive uses a checksummed local confirmation marker and durably revokes
it before resumed revalidation. That prevents stale on-disk eligibility after a
rejection or crash, but the marker is not an authenticated, externally anchored,
expiring promotion receipt. Independent adoption should remain an operator-
controlled action until that receipt boundary is implemented.

### 6. Outcome-supervised PRM training

The active PRM's own `promise` and `progress` predictions are no longer recycled
as labels. Training requires delayed outcomes from an allowlisted independent
source or explicit trusted labels tied to a task/goal and episode. Unknown
progress is masked. Rows carry task/episode identity, provenance, event time,
and verification time; evaluation keeps tasks together and reserves the newest
task groups. Task-equal weighting prevents a long trace from dominating.

The allowlist is a trust declaration, not authentication. Signed/traceable
outcome producers and stricter issuer authorization remain required.

## Default `risk_limited` Self-Harness profile

The profile remains opt-in and does not enable Self-Harness or the shared
self-improvement controller.

| Control | Default |
|---|---:|
| held-out required / minimum unique cases | `true` / `8` |
| minimum observed lift | `0.02` |
| configured critical value | `1.96` (raised by ledger permit when stricter) |
| development candidates per signature | `3` |
| maximum promotions per cycle | `1` |
| holdout rotations | `1` |
| judge replications | `3` |
| max cost / latency / tool-call factors | `1.25` / `1.25` / `1.10` |
| recent calibration required | `24h` |
| evaluation budget | `$5` |
| holdout family alpha / query alpha / max queries | `0.05` / `0.025` / `2` |
| metamorphic check / canary staging | `true` / `true` |

It additionally requires an explicit holdout-ledger path and the exact deployed
system prompt. Risk controls are non-weakenable: explicit values may tighten
floors and caps, but cannot disable required evidence, reduce statistical/sample
floors, raise resource limits, increase promotion count, or extend calibration
and spending bounds. Invalid numeric overrides fall back to conservative
defaults; required evidence that cannot be established rejects.

## Benchmark contract for a superiority claim

A green test suite proves implementation behavior, not frontier performance.
"Beyond SOTA" is permitted only if all of the following are satisfied:

1. Pre-register Lightwork variants, strongest applicable baselines, primary and
   safety endpoints, exclusion/stopping rules, and a practical margin.
2. Compare reproducible DGM/HGM/GEA-style evolution and Self-Harness/GEPA-style
   harness optimization. Disclose implementation mismatches.
3. Equalize base model snapshot, tools, sandbox, task inputs, context, tokens,
   dollars, wall time, evaluator calls, and expansion/evaluation budgets.
4. Use power-informed independent seeds (at least five; target ten when compute
   permits), retaining failed/frozen runs.
5. Freeze systems before a temporal outer holdout is created or opened. Commit
   its manifest hash and keep tasks, labels, and grader internals out of search,
   reflection, calibration, and human selection.
6. Query the outer holdout once for fixed, pre-registered systems. A discovered
   issue starts a new study with a new outer holdout.
7. Use paired task analysis, per-seed distributions, effect sizes, confidence
   intervals, and multiplicity control. The lower bound against the strongest
   baseline must clear the pre-registered margin.
8. Require no regression in evaluator tampering, secret access, capability
   widening, calibration outage, budget exhaustion, crash-at-every-write,
   concurrency, rollback, or recovery trials.
9. Publish frozen commits, configs, seeds, budgets, environments, raw outcomes,
   and analysis; require an independent rerun before an external claim.

Until that study passes, the accurate statement is:

> Lightwork implements a frontier-informed, risk-limited and governed
> self-improvement protocol. Comparative superiority is under evaluation and is
> not yet proven.

## Remaining frontier work

| Priority | Gap |
|---|---|
| P0 | Isolated remote evaluation broker, keyed receipts, and WORM/third-party ledger anchoring |
| P0 | Temporal outer benchmark with equal-budget, multi-seed baselines and independent reproduction |
| P0 | Signed, expiring archive promotion receipts bound to candidate, family, evaluator, policy, and ledger tip |
| P1 | Authenticated outcome issuers and grader/evaluator artifact signing |
| P1 | Independent evaluator ensembles and adversarial graders |
| P1 | One paired per-case Self-Harness evaluator with deterministic arm-order counterbalancing |
| P1 | Independent calibration labels beyond the current expected-substring proxy |
| P1 | Persistent, concurrency-safe metaproductive archive with calibrated allocation research |
| P1 | Governed GEA-style experience exchange and GEPA-style multi-objective/Pareto search |
| P2 | Artifact retention/GC, legacy-archive quarantine, and migration of opaque scalar integrations |
