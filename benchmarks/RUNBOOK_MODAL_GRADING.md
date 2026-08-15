# Runbook — governed SWE-bench grading in official per-instance containers

## What this fixes

Yesterday's wasted money was the **environment**: one host can't reproduce 500
different Python eras + pinned deps, so instances graded `NOENV`/`EMPTY` and we
paid the agent to work on things that could never grade.

The SWE-bench project publishes a **pre-built image per instance**
(`swebench/sweb.eval.x86_64.<id>`) that already contains the repo at
`base_commit` with the correct environment. `benchmarks/swebench_container_grade.py`
grades **inside that image**, reusing the official spec, eval script, and grader,
so the era-mismatch bug class **cannot recur** — the environment travels with the
instance. Our governance (anti-cheat boundary, mis-seed guard, signed promotion)
runs around it, unchanged.

Backends: **Docker** (a VM with a Docker daemon) for untrusted patches, or
**Modal** only when you explicitly acknowledge provider-default networking in an
isolated trusted environment. Same grading code path.

## Step 0 — one-time setup (Modal)

```bash
pip install modal
modal token new          # opens a browser once; links your Modal account
```

(Modal gives new accounts free monthly credit; a single-instance validation is
a few cents. No Anthropic key is used — this is grading only, no agent.)

## Step 1 — validate the environment on ONE instance ($0 Anthropic, ~cents Modal)

This is the whole point: prove the official image + grading work end-to-end
**before** spending on an agent. Grade the instance's **gold patch** — it must
resolve. Then grade the **baseline** (no patch) — it must NOT resolve.

```bash
cd ~/Lightwork
# gold patch must RESOLVE (proves image + grading are correct):
python benchmarks/swebench_container_grade.py --instance astropy__astropy-12907 --backend docker --gold
# baseline must NOT resolve (proves the tests really fail without a fix):
python benchmarks/swebench_container_grade.py --instance astropy__astropy-12907 --backend docker
```

Expected (gold run):

```
instance: astropy__astropy-12907
image:    swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest
grading:  GOLD patch (must resolve) via docker
====================================================================
  candidate applied: True
  FAIL_TO_PASS: 2 pass / 0 fail
  PASS_TO_PASS: 13 pass / 0 fail
  RESOLVED (official): True
====================================================================
VALIDATION: PASS -- official image + grading work end to end
```

If the gold run prints `VALIDATION: PASS`, the environment problem is solved for
real — every instance now grades in its own correct container. If it fails,
**stop and send the output** before spending on any run.

### Modal for trusted isolated validation only

The Docker backend runs untrusted candidate code with `--network none` and
additional hardening. The Modal API used here does not expose an equivalent
per-sandbox no-egress switch, so Modal fails closed by default. If you are
grading only trusted patches in an isolated Modal environment with no reachable
secrets or internal services, opt in explicitly:

```bash
python benchmarks/swebench_container_grade.py --instance astropy__astropy-12907 --backend modal --gold --allow-network
```

The official images are large (~1–2 GB each); Docker pulls on first use, and
Modal caches them remotely.

## Step 2 — plug into the governed run (the DGM / SWE-bench score)

`swebench_container_grade.governed_container_grade(instance, patch, run_in_image)`
returns the official `resolved` verdict after the host-side anti-cheat boundary
and a baseline mis-seed guard. Layer promotion on top by reusing
`swebench_governed._promote(...)` (same signed, reversible ledger) — the module
holds no keys and writes no ledger itself, so the governance stays in one place.

This replaces the era-venv host grading in the governed scorer with
container grading; the governance chain (boundary → baseline-fails → resolved →
signed promotion) is identical. The corpus no longer needs a hand-built winnable
set — **every** Verified instance is gradable because every instance carries its
own environment.

## Cost shape (honest)

- **Per-instance grade:** seconds-to-minutes of container CPU. Modal bills per
  second; a full-500 grade sweep is compute-cheap (no GPUs — this was never a
  GPU workload, which is why RunPod overcharged).
- **The expensive part remains the agent** (Anthropic), and that is now spent
  ONLY on instances that provably grade — no more NOENV/EMPTY waste.
- Validate on one instance first; only then scale.

## Why this is the credibility unlock

With correct per-instance environments you can grade a **random** sample of the
500 (or all of it), which is what kills the "you cherry-picked the easy ones"
criticism the old 62-instance winnable set invited. Correct environments + the
existing governance chain = an honest, non-cherry-picked, signed governed number.
