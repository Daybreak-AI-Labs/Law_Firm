# Lightwork benchmarks

The wedge claim is **long-horizon work + true multi-agent coordination**.
These benchmarks make that measurable.

## Why benchmarks exist

Without numbers, "better at long-horizon" is marketing. Each benchmark
in this directory:

- Has a verifiable success criterion (file produced, tests pass, etc.)
- Records wall-clock, cost (`$`), tokens, tool calls, and depth.
- Is reproducible from a single `maverick start` command.
- Has a baseline number from a single-shot LLM call for comparison.

Results belong in `RESULTS.md` next to each benchmark, with the run
metadata (date, model assignments, total cost) checked in.

## How to run a benchmark

```bash
# Pre-req: maverick init has been run with at least an Anthropic key.
maverick start "$(cat benchmarks/longhorizon/research-report.md)" \
  --max-dollars 5 --max-wall-seconds 1800 --workdir bench-workspace
```

When done, copy the output (and the budget summary line) into the
corresponding `RESULTS.md`.

## Suite

| Benchmark | Class | Expected wall | Expected cost |
|---|---|---|---|
| `longhorizon/research-report.md` | Research synthesis | 10–20 min | $0.50–$2 |
| `longhorizon/code-refactor.md` | Multi-file refactor | 15–30 min | $1–$3 |
| `longhorizon/multi-step-planning.md` | Planning + revision loop | 20–40 min | $1–$4 |

All three are designed to **fail** for single-shot prompting (too
broad, too many steps) and **succeed** for a recursive swarm with
verify + skill distill enabled.

## Does the learning actually help? (the moat)

The differentiator vs. stateless assistants is that Lightwork *retains and
recalls* what it learns. These benchmarks make that claim falsifiable
instead of marketing:

| Benchmark | Question | Cost |
|---|---|---|
| `recall_precision.py` | Does relevance-gating cut injected noise without losing the right skill? | **free** (deterministic, lexical path) |
| `moat_rigorous.py` | Holding the task fixed, is a *warm* agent (relevant prior in store) never worse — and ideally cheaper — than a *cold* one? | ~$0.5–0.7/run × 3 runs/observation |
| `moat.py` | Original cold-vs-warm A/B (kept for history; superseded by `moat_rigorous.py`'s same-target protocol) | as above |

`recall_precision.py` runs in CI (no key): it shows the relevance gate drops
the lexical false-positive rate from **88% → 12%** with Recall@1 held at 100%
— precision is what matters, because injecting weakly-relevant memory *regresses*
the agent (hard negatives flip answers; large/noisy memory degrades).

`moat_rigorous.py` is the paid, end-to-end proof. Its headline is the
**defensible** one: *warm is never worse than cold* (the property the gate
buys), reported as a not-worse rate + a **median** (outlier-robust) cost delta,
with success parity. Pure aggregation is unit-tested offline
(`test_moat_rigorous.py`); results land in `MOAT_RIGOROUS_RESULTS.md`.

## Does the governance contain unsafe autonomy? (the control plane)

The other differentiator vs. an ungoverned runtime is that Lightwork **contains**
what an autonomous agent is allowed to *do*. `eval_governance.py` makes that
falsifiable instead of marketing -- and, like `eval_smoke.py`, it runs in CI with
**no key**: deterministic, scripted calls into the *real* governance machinery.

| Scenario | Asks | Real control exercised |
|---|---|---|
| `approval-gate` | Is a high-risk actuation ("Pay") gated to a human, while a read isn't? | `safety/action_gate` |
| `egress-lock` | Does enterprise mode refuse a cloud LLM provider and admit a self-hosted one? | `enterprise` |
| `capability-ceiling` | Does an attenuating capability deny an out-of-grant tool but permit a granted one? | `capability` |
| `agent-trust` | Is an inbound-only external agent refused an outbound dial? | `agent_trust` |
| `signed-evidence` | Is a governed action recorded on a chain that verifies clean -- and a one-byte tamper detected? | `audit` (Ed25519 chain) |

It reports a **prevention rate** (unsafe vectors contained) *and* a **utility
rate** (legitimate paths preserved), so it can't be gamed by a control that
blocks everything. This is the CI-runnable half of
[`../docs/strategy/benchmark-plan.md`](../docs/strategy/benchmark-plan.md); the
broader governed-vs-baseline frontier is implemented by
`eval_governance_frontier.py`. Run the fast wiring smoke standalone:
`python benchmarks/eval_governance.py`.

### Deterministic governance frontier

`eval_governance_frontier.py` expands the smoke to **40 fixed definitions**
(24 unsafe, 16 benign lookalikes) across financial/destructive actuation,
enterprise egress, capability attenuation, agent trust, secret-output policy,
budget controls, and signed-evidence integrity. Every definition runs through:

- the real governed control path; and
- an explicit controls-disabled baseline.

Pinned seeds `17,29,43` change execution order to expose state/order leakage;
they are not model-sampling seeds. The suite separately reports unsafe
prevention, harness decision-ledger coverage, native product-event observation
for supported controls, benign scripted-task completion, false positives,
evidence integrity, and median/p95 measured overhead. Action controls exercise
the production risk-aware secure default: high-risk mutations are denied in a
non-interactive run while benign click/type/fill mutations remain admissible.
It writes an Ed25519-signed reproducibility manifest, emits the run public key
as a separate file, and generates
[`GOVERNANCE_FRONTIER_RESULTS.md`](./GOVERNANCE_FRONTIER_RESULTS.md) from that
verified manifest.

```bash
python benchmarks/eval_governance_frontier.py
python benchmarks/eval_governance_frontier.py \
  --verify-manifest benchmarks/results/governance-frontier-v1/measured-manifest.json \
  --trusted-pubkey-file \
  benchmarks/results/governance-frontier-v1/trusted-publisher.pub
python benchmarks/eval_governance_frontier.py --verify-tracked-artifacts
python -m pytest -q benchmarks/test_eval_governance_frontier.py \
  benchmarks/test_governance_metrics.py
```

No model, provider key, or network is used. “Task completion” here means the
benign scripted terminal action remained admissible; natural-language agent
completion still belongs in a later paid live-model arm. Likewise, “prevented”
means the called control returned a denial before the harness marked its
simulated terminal effect admitted; the suite executes no live payment,
deletion, or external side effect. The tracked run uses an ephemeral co-located
signing key and says so explicitly: its signature
proves self-signed run integrity when checked against the separately distributed
public-key file, not off-host production key custody or publisher identity.
The manifest-embedded key is never accepted as its own trust anchor. Publication
validation also checks current harness, control-implementation, and config
digests; a clean measured commit that is an ancestor of `HEAD` when history
preserves it, or the exact signed control-source snapshot after a shallow
checkout, squash, or rebase; and exact deterministic report regeneration.
The control snapshot covers every tracked Python file under `maverick` and
`maverick_shield` plus their package/root metadata, so a change anywhere in that
measured source scope requires regenerating the published artifact. Source
content is decoded as strict UTF-8 and line endings are normalized to LF before
hashing, so Windows CRLF and Linux LF checkouts of identical Git text share the
same signed snapshot; any other content change still invalidates it.

## What does the governance cost? (the overhead)

`eval_harness_overhead.py` answers the objection the frontier suite invites:
if every consequential action carries a receipt, an approval and an audit row,
what does the record *cost*? It runs **12 fixed task definitions** across two
shapes (`analysis`, read-only; `actuation`, ending on a high-risk posting
action) twice under pinned order seeds `11,23,37` — 36 paired instances — with
each task executed both ways:

- **ungoverned** — the step list runs directly: no budget meter, no
  authorization, no receipts, no audit rows, no screening;
- **governed** — the *identical* step list runs through the real control plane
  (`budget.Budget`, `tool_authz.authorize`, `governed_actions` PREPARE/COMMIT
  receipts, world-model approvals at the `dual_control` quorum, `audit_event`
  rows, `shield_policy` + `memory_guard` screens, `secret_detector` redaction).

The model boundary is `stub://deterministic-sha256-v1`, a pure function of the
prompt called by **both** arms, so any token or call-count difference is caused
by governance and nothing else. No key, no network, no LLM: this is an overhead
measurement, not a capability benchmark.

The headline is a **zero** delta: same model calls, same input/output tokens,
same task steps and tool calls, and byte-identical answers and external effects
in every pair. That is a property of *this* control plane, not a truism — a
governance layer built on an LLM critic or an LLM-as-judge policy would show a
positive token delta here. What governance does cost is wall time, reported
against the durable evidence it produces: receipts read back from the lineage
store, audit rows off the signed chain, approvals out of the world model,
counted separately from control invocations that gate a run but persist
nothing. Overhead tracks the number of *consequential* actions rather than
steps, so the `actuation` shape pays visibly more than read-only `analysis`.
The ungoverned arm produced zero governance artifacts of any kind, which is the
control on the measurement. Timings are host-local observations, recorded but
never asserted: `--ci` gates only the deterministic invariants (arm equality,
call/token/step parity, exact artifact counts). Numbers, by-shape split, and
the "what this does not show" caveats live in
[`HARNESS_OVERHEAD_RESULTS.md`](./HARNESS_OVERHEAD_RESULTS.md), generated from
an Ed25519-signed manifest with the run public key emitted alongside it.

```bash
python benchmarks/eval_harness_overhead.py
python benchmarks/eval_harness_overhead.py --ci
python benchmarks/eval_harness_overhead.py \
  --verify-manifest benchmarks/results/harness-overhead-v1/measured-manifest.json \
  --trusted-pubkey-file \
  benchmarks/results/harness-overhead-v1/trusted-publisher.pub
python benchmarks/eval_harness_overhead.py --verify-tracked-artifacts
python -m pytest -q packages/maverick-core/tests/test_eval_harness_overhead.py
```

The three runtime planes that ride these same evidence surfaces — the governed
session kernel, harness self-refinement, and run forking — are documented in
[`../docs/governed-execution.md`](../docs/governed-execution.md).

## Comparing across providers

Re-run the same benchmark with different `[models]` config blocks:

```toml
# all-anthropic
benchmarks/configs/all-anthropic.toml

# orchestrator on Anthropic, workers on Ollama (local)
benchmarks/configs/mixed-local-cloud.toml
```

The `RESULTS.md` for each benchmark records all configurations tried
and their numbers side by side.

## What we are NOT measuring (yet)

- Raw LLM accuracy (SWE-bench / MMLU / etc.) -- those measure the
  model, not the agent system
- Raw red-team / prompt-injection content scanning -- that's Agent
  Shield's territory and has its own suite. (The *action-governance*
  layer -- block/gate/record unsafe actions -- **is** now measured;
  see "Does the governance contain unsafe autonomy?" above.)

Distillation quality (whether auto-generated SKILL.md files actually help
future runs) **is** now measured — see "Does the learning actually help?"
above (`recall_precision.py` + `moat_rigorous.py`).
