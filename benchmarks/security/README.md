# Security benchmarks

Capability benchmarks live one level up (`benchmarks/`). This directory
measures the **shield** — the safety detection layer — on two axes that a
security benchmark must cover: **detection accuracy** and **hot-path latency**.

It exists because `docs/safety.md` advertises an F1 number for the *full
agent-shield SDK* with nothing reproducible in-repo, and **no number at all
for the built-in fallback most users actually run**. This makes both auditable.

## What's here (v1 — offline, no SDK, no API key)

| File | What it does |
|---|---|
| `corpus.py` | Labelled dataset: `train` (the existing regression corpus the rules were tuned on), `heldout` (novel attacks authored here), `benign` (agent-realistic, incl. false-positive bait). Obfuscations are built at runtime — no encoded blobs in source. |
| `detector_score.py` | Scores every offline backend → TPR/FPR/F1 with Wilson 95% CIs, an evasion sweep, and a leakage guard. Writes `RESULTS.md`. |
| `latency_bench.py` | Per-scan p50/p95/p99/max for the hot-path scanners on adversarial input. |
| `RESULTS.md` | Generated, checked-in (`source=measured`). |

The pass/fail **CI gate** for latency/ReDoS lives with the shield:
`packages/maverick-shield/tests/test_scan_latency_gate.py`. A scan that
times out **fails the build** — a hung scan that fails open is a *detection
bypass*, not a perf nit.

```bash
python benchmarks/security/detector_score.py     # accuracy dashboard + RESULTS.md
python benchmarks/security/latency_bench.py       # latency distribution
```

## Read the numbers honestly

- **`train` TPR is a regression check, not skill.** The built-in rules were
  hand-tuned on that corpus (e.g. `persona_takeover` was widened *because* a
  corpus case slipped past it). A high train TPR only means "no regression."
- **`heldout` TPR / F1 is the v1 capability signal**, with the important limit
  that this is a small, repository-authored split. `RESULTS.md` is the
  authoritative current artifact; docs may reproduce it only with its corpus,
  confidence intervals, and internal-diagnostic caveat.
- **This dashboard is not a generalized marketing efficacy figure.** A
  credible headline number requires pinned external held-out data and the
  end-to-end methodology in `RUNBOOK_SECURITY.md` (v2).
- Backends needing the SDK or an LLM are listed as **UNAVAILABLE**, never
  silently dropped.

## v2 (needs network + API budget) — see `RUNBOOK_SECURITY.md`

External held-out sets (AgentDojo, JailbreakBench) for scale, and an
end-to-end AgentDojo run (shield ON vs OFF → attack-success-rate reduction).
**Never vendor HarmBench/AdvBench payloads** — reference by ID, pull on
demand, and run candidates through `detector_score.train_overlap()` first so a
trained-on phrase can't inflate the score.
