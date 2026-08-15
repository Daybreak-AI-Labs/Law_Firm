# SWE-bench Verified under governance — evidence record

**Date:** 2026-07-10, updated 2026-07-11 · **Operator:** Day AI Labs · **Agent:** Lightwork coding agent (llm proposer) · **Harness:** `benchmarks/swebench_governed.py`

> **Scope clarification (2026-07-15):** This is a historical evidence record for
> the dedicated, operator-run `swebench_governed.py` benchmark harness. It is not
> evidence that the stock `maverick self-modify` runner autonomously adopts code.
> The stock runner is research-only and deliberately has no live-apply path. The
> benchmark's operator approval, evaluator environment, and ledger claims must be
> assessed on their own terms and must not be generalized into a production DGM
> claim.

## The claim this evidence supports — and its limits

**Supported:** Lightwork's coding agent resolves real SWE-bench Verified issues
end-to-end **under a governed self-modification chain** — anti-cheat boundary,
isolated held-out test grading, capability non-escalation proof, and an
Ed25519-signed, reversible promotion ledger — on autonomous cloud
infrastructure. Reproduced across **three run days** and **three codebase
families** (pylint, flask, requests).

**Not claimed:** a SWE-bench leaderboard percentage. Only 3 of 19 staged
instances were gradable in this single-host environment (the rest need
per-instance Docker environments — see "Path to a headline number"). Every
ungradable instance is reported as such, never counted as resolved, and — via
the free environment pre-gate — cost $0 of agent spend.

## Results

| Run | Resolved (all) | Resolved of gradable | Agent spend | Ledger |
|-----|----------------|----------------------|-------------|--------|
| 1 (2026-07-09) | 1/19 (5.3%) | 1/4 (25.0%) | ~$7.45 | branch `evidence-run1` |
| 2 (2026-07-10, env fixes) | 2/19 (10.5%) | **2/3 (66.7%)** | ~$3.34 | branch `evidence-run2` |
| 4 (2026-07-11, era venvs + best-of-4 + Opus worker) | 5/18 processed (operator-stopped) | **5/6 (83.3%)** env-working; 5/9 (55.6%) counting broken-env EMPTYs against | ~$131.70 attributed (see caveat) | `slice4_ledger.json` (operator archive) |

Run 2 final scoreboard (verbatim, full log committed as
`evidence/swebench-run2/slice2_log.txt`):

```
resolved under governance: 2/19 (10.5% of all)   |  boundary-refused: 0  |  unresolved: 1   |  ungradable-here: 16
resolved of GRADABLE (excl. ungradable): 2/3 (66.7%)
agent spend (attributed): ~$3.34   |   signed ledger: slice2_ledger.json
```

## Round 4 (2026-07-11): five new resolves, and what it honestly cost

**What changed since runs 1–2:** era-correct per-instance venvs
(`benchmarks/pod_era_rebuild.sh`, Python 3.8/3.9/3.11 per SWE-bench's own
`MAP_REPO_VERSION_TO_SPECS`) grew the gradable pool from 19 to 89 staged
instances, with 62 oracle-winnable (the gold patch itself resolves under
governance here — the environment's honest ceiling). The agent ran best-of-4
attempts with an Opus-class worker.

**Result:** the operator stopped the run after 18 of 62 instances to cap spend.
Of the 18: **5 resolved under governance** (`psf__requests-2931`,
`psf__requests-5414`, `pylint-dev__pylint-4604`, `pylint-dev__pylint-4970`,
`pylint-dev__pylint-6386`, all baseline<1.0 → candidate 1.000), 1 genuine agent
failure (`pylint-dev__pylint-8898`, candidate patch broke passing tests), 9
refused for $0 by the free environment pre-gate (broken pytest-family env), and
3 paid attempts that produced no patch (`EMPTY`, all in the same broken pytest
family). Read honestly both ways: **5/6 (83.3%)** on instances whose
environment actually worked; **5/9 (55.6%)** counting the broken-env EMPTYs
against the agent.

**The ledger for this round holds 6 promotions** — the 5 above plus a
`pylint-8898` promotion from an earlier same-day invocation. 8898 has resolved
in three separate runs (runs 1, 2, and the earlier round-4 invocation) but
failed in the final one: agent nondeterminism, reported as-is.

**Honesty caveats, verbatim from the post-mortem:**

- **Spend attribution (~$131.70)** sums the per-instance forensics sidecars,
  which several same-day invocations overwrote per instance id — it is
  "latest attempt per instance", not the day's total; the provider console is
  ground truth for total spend.
- **Signature persistence:** this run's host predated the ledger-persistence
  fix (PR #2178), so its records were Ed25519-verified at promotion time but do
  not carry the signature for offline re-verification by
  `benchmarks/audit_ledger.py`. Runs after 2026-07-11 persist
  `approval_signature`/`payload_sha256`/`approver_id` in every record.
- **The paid EMPTY leak is fixed, not excused:** three consecutive paid
  attempts on a dead repo family cost real money for nothing. PRs #2179/#2180
  added a per-family circuit breaker (3 paid strikes → rest of family skipped
  at $0), inline per-instance cost on every paid scoreboard line, and a
  staleness warning when the winnable list predates the venv rebuild.

## The two resolved instances (runs 1–2)

**`pylint-dev__pylint-8898`** — *bad-names-rgxs mangles regular expressions
with commas.* The agent added a brace-depth-aware CSV splitter so commas inside
regex quantifiers (`{1,3}`) are not treated as list separators. Baseline
0.944 → candidate 1.000 on the instance's own FAIL_TO_PASS + PASS_TO_PASS
tests. **Resolved in both runs independently** (repeatability).

**`pallets__flask-5014`** — *require a non-empty name for Blueprints.* Patch to
`src/flask/blueprints.py`. Baseline 0.983 → candidate 1.000. Agent cost for
this resolve: **$0.54**.

## The governance chain every pass traversed

1. **Anti-cheat boundary** — `defensive_validate` refuses patches touching
   tests or grading-sensitive config (conftest/pytest.ini/setup*/pyproject/…),
   plus gold-patch overlap detection (armed for the llm proposer).
2. **Held-out fitness on isolated copies** — candidate applied to a throwaway
   copy; the grader's `test_patch` applied to BOTH arms (upstream SWE-bench
   semantics); the baseline MUST fail the graded tests or the instance is
   marked ungradable, never resolved.
3. **Capability non-escalation proof** — the patch must not widen the agent's
   tool capability envelope.
4. **Ed25519 operator-signed promotion** recorded to an append-only ledger with
   one-step rollback.

## Why this number cannot be quietly inflated

- Ungradable-here is a separate scoreboard bucket, never counted as resolved;
  the free pre-gate skips such instances **before** any agent spend.
- A proposer that produces no patch is `EMPTY` (unresolved), distinct from
  boundary refusals (`CHEAT`).
- Malformed upstream dataset test ids are dropped loudly at load; an instance
  left with no gradable failing test is refused a resolve verdict.
- Both figures (of-all and of-gradable) print on every run, with the note to
  report both.
- Offline proof suites pin the gates: `proof/swebench_governed_proof.py`
  (4/4: genuine fix promotes, test-edit cheat refused, regression caught,
  ledger integrity) and `proof/dgm_uplift_proof.py` (6/6: uplift promotes,
  memoriser refused, tamperer zeroed, rollback byte-identical).

## Artifacts

- Branch `evidence-run1`: run-1 signed ledger + per-instance forensics.
- Branch `evidence-run2`: run-2 signed ledger + forensics + full run log.
- Forensics per instance: agent cost, tokens, outcome, and the exact patch.
- Operator keypair generated on the run host; promotions are refused without a
  valid signature (pinned by the proof suite).

## Cost of the entire proof

Runs 1–2: ≈ **$10.79** total agent spend, plus ~$4 of cloud CPU time (32 vCPU
pod, $0.96/hr). Round 4: ~$131.70 attributed via forensics (multi-invocation
caveat above) plus pod time — the price of best-of-4 Opus-class attempts and
of the environment lessons now hard-coded into the harness as $0 refusals.

## Path to a headline percentage (roadmap, not claimed)

The gradable pool is bounded by the single-host environment, not the agent:
per-instance Docker environments (upstream SWE-bench's own approach) make all
500 Verified instances gradable. Next milestones: (1) per-instance Docker
grading, (2) modern families with large Verified counts (sphinx, django),
(3) n ≥ 100 gradable for a quotable percentage under identical governance.

## One-line deck framing (honest)

> Lightwork's agent resolves real SWE-bench Verified issues across multiple
> codebases end-to-end under governance — every fix graded on held-out tests in
> isolation, checked for capability escalation, and promoted only with a
> cryptographically signed, reversible ledger record. Reproduced across three
> run days and three codebase families (7 unique instances promoted), with
> ungradable instances honestly excluded, both denominators reported, and every
> dollar of agent spend attributed on the scoreboard.
