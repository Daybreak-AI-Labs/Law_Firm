# Recursive-learning SOTA (mid-2026) and where Lightwork sits on the frontier

**Status:** dated research note (2026-07-04) · **Scope:** self-improving /
recursive-learning agents · **Companion to:** the learning-lifecycle modules
(`reasoning_reward`, `jit_rl`, `calibration`, `approval_signing`,
`evaluator_evolution`, `self_improvement`, `self_harness`, `dreaming`).

A fan-out literature sweep (adversarially fact-checked; every load-bearing claim
below is backed by a primary arXiv source with unanimous 3-0 verification) mapped
the state of self-improving LLM agents as of mid-2026, then graded Lightwork
against it. Headline: the codebase already **cites the freshest papers as
implementation references** — we track arXiv on a weeks-not-months lag — so the
useful output is a precise map of the frontier, where we already sit, and the
narrow places the field has moved that we hadn't. This cycle closed four of
those, and turned the two learning ones on by default.

Caveat carried from the sweep: headline benchmark numbers (DGM's SWE-bench
20→50%, Agent0's +18/24%, JitRL's SOTA/cost claims) are self-reported on the
authors' own harnesses and not independently replicated — treat them as
demonstrations, not settled facts.

## The frontier, by area

The field got formally organized: a 77-page survey, *A Survey of Self-Evolving
Agents* (arXiv:2507.21046, TMLR), taxonomizes by **what / when / how / where** to
evolve and names the open challenges as **safety, scalability, co-evolutionary
dynamics** — i.e. the frontier for enterprise platforms is *governance of
co-evolution, not raw capability*. ICLR 2026 now hosts a dedicated Recursive
Self-Improvement workshop (110 papers).

| Area | SOTA (paper) | Lightwork |
|------|--------------|-----------|
| Evolutionary code self-rewrite | Darwin Gödel Machine (arXiv:2505.22954); ShinkaEvolve (2509.19349); CodeEvolve (2510.14150) — empirical validation replaces the Gödel machine's provable-improvement requirement; archive keeps "worse" ancestors | `maverick-evolve` (config-only mutation + diverse archive); true code self-rewrite **deferred** — the governed `code` rung is the seam |
| Co-evolving evaluator | Red Queen Gödel Machine (arXiv:2606.26294, Cambridge/NVIDIA) — agent + judge co-evolve, "second-order alignment" | `evaluator_evolution.py` (anchor-gated judge promotion; **cites this paper**) |
| Structured / process reward | Agent-RRM (arXiv:2601.22154) — `<think>/<critique>/<score>` instead of a scalar; AgentPRM (2511.08325) — step-wise promise+progress | `reasoning_reward.py` (**new**, this cycle); `prm.py` (cites AgentPRM) |
| Test-time adaptation | JitRL (arXiv:2601.18510) — frozen model, k-NN advantage, no gradients; SEAL (2506.10943) — model emits its own weight-updating self-edits; LifeSkill (2606.04815) | `jit_rl.py` (**new**, this cycle); `adaptive_compute.py`, `best_of_n.py` |
| Supervision-free self-play | Search Self-Play (arXiv:2510.18821, Alibaba) — RAG-verified queries; Agent0 (2511.16043, UNC/Salesforce) — from zero data | RAG-verified ground truth ≈ our **immutable evaluator anchors** |
| Memory | Hindsight (arXiv:2512.12818) — four networks separating evidence from inference, confidence-scored opinions | `hindsight.py`, `procedural_memory.py`, `memory.py` (cites 2602.19320), `experience.py` (HERA 2604.00901) |
| Anti-gaming / provable learning | survey names it; no published system offers signed, auditable proof of *what was learned* | calibration freeze + reward-laundering resistance (**new**); signed audit chain + snapshot/rollback + `operating_record.py` |

**Where we're ahead of the literature:** no verified academic system ships a
signed, cryptographically-lineaged, auditable proof of *what was learned* with
rollback. Papers imply lineage (evolutionary archives, memory networks); we
productized it. On the survey's own axis — governance of co-evolution — that is
the defensible position.

## Shipped this cycle

Four gaps closed, then the two learning features wired live and defaulted on.

1. **Structured, auditable reward** — `reasoning_reward.py` (Agent-RRM). A
   per-dimension rubric (correctness/completeness/grounding/safety) with a
   **safety veto** that rejects an otherwise-high holistic score, and a
   `to_audit_dict()` that makes the reward legible in the learning audit. Now the
   **default verifier judge** (`verify_final` → `verify_proposal_structured`),
   with a scalar `_parse` fallback for any non-rubric reply so the change is
   backward-compatible. `MAVERICK_REASONING_REWARD=0` opts out.

2. **Reward-laundering resistance** — `calibration.py`. The freeze caught a
   *drifted* verifier; it now also catches a *gamed* one. `record_probe`
   tags adversarial ground-truth cases (the fixed corpus our immutable anchors
   already are), and `resistance = adversarial_discrimination /
   natural_discrimination` freezes learning when the judge stays sharp on normal
   traffic but its edge on adversarial probes collapses. Opt-in
   (`[calibration] min_resistance`), routed through the existing `learning_frozen`
   interlock so it protects donation, the promotion ladder, and evaluator
   co-evolution for free.

3. **Cryptographic approval gate** — `approval_signing.py` +
   `self_improvement.py`. The `code`/`weights` rungs' human consent is now an
   Ed25519 signature over `(candidate id, rung, payload digest)` verified against
   operator-held keys — **non-forgeable** (a self-modifying agent can't sign for
   itself) and **payload-bound** (defeats approve-benign-then-swap). This is the
   reference-monitor answer to "can't the self-rewriting code bypass our
   controls?": the control lives outside the surface the agent can rewrite.
   Opt-in; fail-closed once keys are configured.

4. **JitRL gradient-free adaptation** — `jit_rl.py`. A (state, action, return)
   store with k-NN advantage over the shared `prm.step_features` encoding, wired
   into `best_of_n`'s `select` hook (`make_best_of_n_selector`). Reversible (drop
   rows), auditable (retrieved experiences are inspectable), tenant-isolated —
   the policy rung, not weights. Enabled by default but a **no-op with a cold
   store**. `MAVERICK_JIT_RL=0` opts out.

**Posture:** the two *learning* features default on (kernel rule 1 respected via
graceful fallbacks); the two *enforcement* gates stay opt-in (fail-open, never
require). All four have installer wizard steps under `pick_advanced()`.

## Frontier follow-ups (open)

Ranked by leverage:

1. **JitRL per-step reach.** Today it steers multi-candidate selection
   (best-of-N); a single-pass run has nothing to steer. Wiring it into per-step
   tool selection in the agent loop (record tool outcomes, bias tool choice)
   makes it act on every run — a deeper, higher-risk `agent.py` change.
2. **`reward_audit` → signed audit chain.** The structured reward's rubric is
   attached to the verdict and logged; writing it into the tamper-evident chain
   is what makes "provable learning" literally provable per verdict.
3. **Quantify reward-laundering resistance as a published governance metric.** We
   now measure it; the whitespace the survey leaves open is *defining the
   category* — a reference governance architecture with a bounded, audited
   gaming-gap number.
4. **The governed DGM `code`-rewrite rung.** The approval gate is the trust
   anchor and it's done; the rung itself is still an opaque payload. Making it
   real (sandbox code-eval + capability-diff over the candidate) is the flagship
   "nobody else can safely ship this" capability — DGM-style self-rewrite under a
   reference monitor with signed, reversible, capability-bounded promotion.

The true frontier for a platform like this is exactly the survey's named
challenge — **governed co-evolution** — and the load-bearing primitives are the
ones we already hold: verifiable ground truth (RAG-verified queries ≈ immutable
anchors) and archive/snapshot lineage. Staying ahead means deepening those, not
chasing raw capability.
