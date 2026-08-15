# Running the full SWE-bench DGM uplift on a Mac mini

Your Mac mini is a perfect host for this: it stays up (unlike this session's
container, which recycles mid-solve and is the *only* reason the run can't
finish here). The Mac is only the **conductor** — the LLM solve is an API call
and every patch is graded on **Modal** (x86 Linux, in the cloud). So Apple
Silicon vs Intel does not matter, and you do **not** install or run Docker
locally. No GPU, nothing heavy runs on the Mac itself.

**Cost:** the run ≈ $50–100 (Anthropic + Modal). The Mac itself: electricity.
**Runtime:** ≈ 1–3 hours for the 3-instance mechanism proof; longer for the
10-instance statistical run.

---

## 0. One-time setup (already have the repo? skip to step 1)

```bash
git clone https://github.com/Daybreak-AI-Labs/Lightwork.git
cd Lightwork
chmod +x benchmarks/setup_mac.sh
./benchmarks/setup_mac.sh          # brew python@3.11, .venv, all packages, keys
```

`setup_mac.sh` is idempotent and spends nothing. It creates `.venv`, installs
the workspace, generates your Ed25519 signing keys in `~/dgm-keys` with a `0700`
directory and `0600` private key, and checks Modal auth.

Treat `~/dgm-keys/operator.priv.hex` as an offline governance secret: do not copy
it into the repo, mount it into containers, paste it into prompts, or expose it
to any local shell that an LLM-controlled agent can operate. The benchmark
runner only needs the key directory for the final human-approved signing/audit
step; solving and Modal grading must not receive this directory as workspace
input.

## 1. Open a shell and bring your secrets (nothing secret is committed)

```bash
cd ~/Lightwork
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...            # your key (the model)
modal token new                                # only if preflight says Modal isn't authed
```

## 2. Preflight — prove the stack works for **$0** before spending

```bash
./benchmarks/preflight_mac.sh
```

This runs the **real** governed DGM gate, the signed audit chain, and the
self-learning scoreboards entirely in-process (instant, free), then pings Modal.
**Do not proceed until it prints `ALL CHECKS PASSED`.** This is the "check your
work before spending" gate — if anything here is red, the paid run would just
burn money hitting the same wall. The preflight also fails if `~/dgm-keys` is
not `0700` or `operator.priv.hex` is not `0600`; rerun `setup_mac.sh` to harden
existing keys.

## 3. Keep the Mac awake for the whole run

The run is unattended and can take hours; a sleeping Mac pauses it. Run the
whole thing under `caffeinate` inside `tmux` so it survives a closed lid or a
dropped SSH session:

```bash
tmux new -s dgm
# (inside tmux) re-activate + re-export, because tmux is a fresh shell:
cd ~/Lightwork && source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...
```

## 4a. The 5-instance mechanism proof (start here — cheapest, ~$20–45)

```bash
# Recommended: auto-pick the 5 easiest Verified instances (smallest gold
# patches -- the band where a budget uplift can actually show).
caffeinate -s python benchmarks/realswe_dgm.py \
    --keys ~/dgm-keys --out-dir ~/realswe_dgm \
    --easy 5 --max-dollars 45
# detach and let it run:  Ctrl-b then d      (the run keeps going)
#
# Or name instances yourself (disclosed selection, any repo):
#   --ids django__django-11179,django__django-11880,sympy__sympy-23950
```

This runs each real GitHub issue under a **baseline** agent budget
(25 turns / $3 / 25 min wall) and a **candidate** budget
(**120 turns / $12 / 90 min wall**), grades every patch in the official
SWE-bench container on Modal, and if the candidate lifts the held-out
resolved-rate it **promotes the config change through the real governance rung
and signs it into the ledger**.

> **The goldilocks band.** Uplift only shows on instances the weak config
> can't finish but the strong one can. Too-hard corpora (the original sympy/
> pytest internals ids failed at every budget) and too-easy ones both give a
> flat 0 delta. `--easy N` ranks all 500 Verified instances by gold-patch size
> and takes the N smallest — data-driven, reproducible, not hand-picked. The
> per-instance caps ($12 candidate / $3 baseline) are what bind;
> `--max-dollars` (default 45) is the global runaway guard.

> **Runtime.** A candidate instance can take up to ~90 min, so the full run
> may take a few hours. That's exactly why it runs under `tmux` + `caffeinate`
> and is checkpointed — walk away and let it finish. It is **resumable** (each
> solve is checkpointed to `~/realswe_dgm/verdicts.json`) and **hard
> cost-capped** (`--max-dollars`), so if the Mac ever reboots you just re-run
> the identical command — it skips finished solves and never re-pays.

## 4b. The 10-instance statistical run (optional, more convincing, ~$80–120)

Only after 4a looks good. This stages a 10-instance corpus and runs the full
governed uplift with a real held-out split:

```bash
caffeinate -s python benchmarks/build_dgm_manifest.py --n 10 --seed 1729 \
    --stage-dir ~/dgm_stage --manifest ~/dgm_stage/manifest.jsonl
caffeinate -s python benchmarks/dgm_live.py \
    --manifest ~/dgm_stage/manifest.jsonl \
    --keys ~/dgm-keys --ledger ~/dgm_stage/ledger.json \
    --solver benchmarks/solvers/agent_v0 \
    --container-grade --container-backend modal \
    --min-samples 5 --held-out-frac 0.5
```

> **Backend must be `modal`.** Do not pass a local-docker backend on the Mac —
> grading images are x86 Linux and run in the cloud on purpose.

## 5. Watch it

```bash
tmux attach -t dgm                             # see live progress / the verdict
# or tail the checkpoint as it fills in:
cat ~/realswe_dgm/verdicts.json
```

## 6. When it finishes — **don't trust, verify** the signed ledger

The driver prints the baseline→candidate held-out resolved-rate, the
PROMOTED/REFUSED verdict, and signs it. Re-verify it independently:

```bash
python benchmarks/audit_ledger.py \
    --ledger ~/realswe_dgm/ledger.json --keys ~/dgm-keys
#   (10-instance run: --ledger ~/dgm_stage/ledger.json)
```

A clean `AUDIT OK` over the record count means every promotion in the ledger is
Ed25519-signed by your operator key and the hash chain is intact — the
governance claim is cryptographically checkable, not just asserted.

## 7. Keep the result

The signed ledger is the artifact worth keeping — copy it somewhere safe:

```bash
cp ~/realswe_dgm/ledger.json ~/Desktop/dgm-ledger-$(date +%Y%m%d).json
```

That JSON is the Monday proof: a real agent measurably improved its own coding
budget on real GitHub issues, and the improvement was gated, promoted, and
signed under governance — auditable by anyone with the public key.

---

### If something goes wrong

- **Preflight fails on Modal** → `modal token new`, re-run preflight.
- **A solve errors / the Mac reboots** → just re-run the same step-4 command;
  it resumes from the checkpoint and never re-pays for finished solves.
- **You hit the `--max-dollars` cap** → it stops cleanly; raise the cap and
  re-run to continue, or read the partial verdict as-is.
- **`no-diff` on an instance** → that instance was genuinely not solved by that
  arm; it counts as unresolved, which is the honest outcome, not an error.
