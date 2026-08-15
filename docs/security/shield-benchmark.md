# Shield benchmark & safety architecture

This page is written to be **scrutinized**. It reports reproducible, measured
numbers for Lightwork's safety detection — including where it is weak — and
explains why agent safety here is an *architecture*, not a single classifier.
Every number below is produced by `benchmarks/security/detector_score.py`
(offline, no SDK, no API key) and can be regenerated in seconds.

## Defense in depth, not one detector

A prompt-injection classifier alone is a brittle moat: novel phrasings evade
any fixed ruleset, and a single miss can be catastrophic if the agent acts on
it. Lightwork layers detection *and* containment so a miss at one layer is
caught at the next:

1. **Built-in fallback** (`maverick_shield.builtin_rules`) — always present,
   even when the full SDK isn't installed (per the kernel rule "runs without
   the shield, fail-open"). Fast, ReDoS-gated regex + de-obfuscation. Measured
   below.
2. **Weighted heuristics** (`maverick.safety.jailbreak_heuristics`) — a scorer
   wired into `safety.remote_scan` that catches softer phrasings the regex
   misses.
3. **Optional LLM cascade** (`MAVERICK_CASCADE_SHIELD=1`) — a model judge for
   ambiguous cases. Higher recall, costs a call; off by default.
4. **Full agent-shield SDK** — the separate private/unpublished ~115-pattern
   ruleset. Referenced here, not measured, and not available from public PyPI.
5. **Action-layer containment** — the part most "AI safety" layers lack. Even
   when text detection misses, the *action* is bounded:
   - writes through connectors and the `database` tool are **confirm-gated**
     (a dry run until `confirm=true`) — so "drop every table" / "POST to C2"
     can't execute unattended;
   - shell is **sandbox-mediated** (`sandbox.exec()`), never a raw
     `subprocess` from a tool;
   - agents run under **scoped capabilities** (sub-agents can only narrow, never
     widen) and a hard **budget cap** (`budget.check()`);
   - the audit log is **hash-chained and optionally Ed25519-signed** for
     tamper-evident evidence.

   In the held-out benchmark below, the action-abuse misses
   (`destructive_action`, `privilege_escalation`, `sandbox_escape`) are exactly
   the cases this layer contains: the dangerous step requires confirmation or a
   capability the agent doesn't hold, regardless of the text slipping past the
   regex.

## What the offline fallback actually scores

Methodology (baked into the harness so it can't drift):

- **Three splits.** `train` is the corpus the regex was tuned on — a high score
  there proves *no regression*, not skill. `heldout` is novel phrasings the
  rules were never edited for — **this is the real capability signal**. `benign`
  is agent-realistic non-attacks, including false-positive bait (attack
  vocabulary in innocent contexts), which sets the FPR floor.
- **Wilson 95% confidence intervals** on every rate (the held-out set is
  intentionally small; the CI keeps us honest about that).
- **Evasion sweep**: each held-out attack is re-tested under zero-width,
  full-width, homoglyph, and base64 obfuscation.
- **Leakage guard** (`detector_score.train_overlap`): any external prompt that
  normalizes to a trained-on phrase is dropped before scoring.

The table below is the 2026-07-29 snapshot generated into the authoritative
[`benchmarks/security/RESULTS.md`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/benchmarks/security/RESULTS.md)
artifact from the current corpus (53 train / 31 held-out / 32 benign):

| backend | TPR train¹ | TPR held-out (95% CI) | FPR benign (95% CI) | F1 held-out |
|---|---|---|---|---|
| `builtin@high` | 18.9% | 71.0% [53–84] | 6.2% [2–20] | 0.800 |
| `builtin@medium` | 20.8% | 83.9% [67–93] | 6.2% [2–20] | 0.881 |
| `defense_in_depth`² | 75.5% | 83.9% [67–93] | 9.4% [3–24] | 0.867 |

¹ Regression check only. ² Regex `OR` weighted heuristics — the realistic
posture when the SDK is absent.

Evasion (held-out TPR, `defense_in_depth`): zero-width 71.0%, full-width 71.0%,
homoglyph 71.0%, base64 100% (the base64 path de-obfuscates and re-scans).

### Read this honestly

The offline fallback is a **tripwire, not the primary defense**. This small,
repository-authored held-out set currently measures 71.0% for `builtin@high`
and 83.9% for the combined posture, but five held-out cases still miss and the
confidence intervals are wide. These values are an internal reproducible
diagnostic, not a generalized public recall claim. Real deployment safety also
depends on confirmation gates, capabilities, sandbox isolation, budget limits,
and egress controls. A credible headline efficacy number requires pinned
external held-out sets plus end-to-end attack-success-rate measurement.

Current held-out misses and false positives are listed in
[`benchmarks/security/RESULTS.md`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/benchmarks/security/RESULTS.md)
(regenerated with each run) so the gaps are tracked, not forgotten.

## Latency & ReDoS are a hard CI gate

A scan that hangs is a detection *bypass*, not a perf nit:
`packages/maverick-shield/tests/test_scan_latency_gate.py` fails the build if a
hot-path scan exceeds its budget on adversarial input. `latency_bench.py`
reports the p50/p95/p99/max distribution.

## Reproduce it

```bash
python benchmarks/security/detector_score.py     # accuracy table + RESULTS.md
python benchmarks/security/latency_bench.py        # latency distribution
```

No API key, no SDK, no network. The corpus (`benchmarks/security/corpus.py`)
carries no encoded blobs — obfuscated variants are built at runtime.

## Roadmap to a published recall number

The credible public recall figure comes from scale + end-to-end measurement,
documented in [`RUNBOOK_SECURITY.md`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/benchmarks/security/RUNBOOK_SECURITY.md):
external held-out sets (AgentDojo, JailbreakBench) run through the leakage
guard, and an end-to-end AgentDojo run measuring attack-success-rate reduction
with the shield **on vs. off** (the number that actually matters: not "did we
flag the string" but "did the agent get compromised"). That axis needs an LLM
budget and is tracked, not claimed.
