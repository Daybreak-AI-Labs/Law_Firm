#!/usr/bin/env python3
"""Live governed self-improvement, proven in SECONDS on an in-process task.

The SWE-bench uplift cycle is the honest-but-heavy demonstration of the DGM
gate: every graded instance is an LLM solve + a full container test suite, so a
single cycle is hours long and dies if the host is recycled mid-run. That cost
is entirely in the *score function*, not in the governance machinery. This
driver keeps the EXACT SAME gate -- ``dgm_uplift.govern_solver_change`` -- and
swaps in a score function that grades a tiny arithmetic task IN-PROCESS, in
microseconds. A full cycle now runs in seconds, for $0, deterministically, and
cannot be lost to a container restart.

What it proves (the gate has teeth, and they are the real production teeth):

  * PROMOTED -- a candidate solver that genuinely generalises (learns to handle
    ``*``) lifts the HELD-OUT resolved-rate; the gate clears the real ``code``
    rung (min_samples=10, capability evidence, human signature), signs it into
    the ledger, and an INDEPENDENT auditor re-verifies the signature.
  * OVERFIT -- a candidate that memorises the answers to the SEEN (held-in)
    tasks lifts held-in but not held-out; the overfit guard catches it and
    refuses. This is why the held-out split exists.
  * REFUSED -- a candidate that changes nothing measurable earns no promotion.

The task is arithmetic, but the governance is identical to the SWE-bench cycle:
boundary review of the solver patch, isolated baseline/candidate evaluation on a
deterministic held-in/held-out split, the overfit guard, the capability
non-escalation proof, and a human-signed promotion to a verifiable ledger.

    python benchmarks/dgm_fast_proof.py            # run all three, audit
    python benchmarks/dgm_fast_proof.py --keep     # keep the run dir + ledger
"""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "packages" / "maverick-core"))

import dgm_uplift  # noqa: E402


@dataclass
class Task:
    """One arithmetic instance. ``spec`` is 'A op B'; ``gold`` the answer."""
    instance_id: str
    spec: str
    gold: str
    # dgm_uplift.score_instance is bypassed (we inject score_fn), so the heavy
    # SWE-bench fields are not needed; these keep the object self-describing.
    language: str = "python"


# --- the corpus ---------------------------------------------------------------
# 12 multiply tasks (the baseline solver CANNOT do these) + 12 add/sub tasks (it
# can). 24 instances so a 50/50 split yields 12 held-out -- clearing the real
# code-rung min_samples=10 with margin. Fixed operands => fully reproducible.

def build_corpus() -> list[Task]:
    tasks: list[Task] = []
    muls = [(6, 7), (8, 9), (3, 4), (12, 12), (5, 11), (7, 7),
            (9, 6), (4, 13), (11, 3), (2, 19), (10, 10), (15, 4)]
    for i, (a, b) in enumerate(muls):
        tasks.append(Task(f"mul-{i:02d}", f"{a} * {b}", str(a * b)))
    adds = [(6, 7, "+"), (8, 9, "-"), (30, 4, "+"), (12, 5, "-"),
            (5, 11, "+"), (7, 7, "-"), (9, 6, "+"), (14, 13, "-"),
            (11, 3, "+"), (2, 19, "+"), (10, 10, "-"), (15, 4, "+")]
    for i, (a, b, op) in enumerate(adds):
        gold = str(a + b) if op == "+" else str(a - b)
        tasks.append(Task(f"addsub-{i:02d}", f"{a} {op} {b}", gold))
    return tasks


# --- the solver under improvement ---------------------------------------------
# Baseline handles + and - but returns a wrong answer ("0") for * -- real,
# generalisable headroom, not a rigged 0->1 jump (add/sub already resolve).

_BASELINE = '''\
def solve(instance):
    """Evaluate a two-operand integer expression 'A op B' -> answer string."""
    a, op, b = instance.spec.split()
    a, b = int(a), int(b)
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return "0"
'''

# A candidate that LEARNS the general rule for '*': generalises to every unseen
# multiply task -> lifts held-out -> deserves promotion.
_CAND_REAL = '''\
def solve(instance):
    """Evaluate a two-operand integer expression 'A op B' -> answer string."""
    a, op, b = instance.spec.split()
    a, b = int(a), int(b)
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    if op == "*":
        return str(a * b)
    return "0"
'''

# A candidate that changes nothing observable (a comment) -> no uplift -> refuse.
_CAND_NOOP = '''\
def solve(instance):
    """Evaluate a two-operand integer expression 'A op B' -> answer string."""
    # tidy-up only: behaviour is unchanged.
    a, op, b = instance.spec.split()
    a, b = int(a), int(b)
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return "0"
'''


def _overfit_source(held_in_mul_specs: list[str], golds: dict[str, str]) -> str:
    """A candidate that MEMORISES the seen multiply tasks -- lifts held-in but
    not held-out. The overfit guard must catch this."""
    memo = {s: golds[s] for s in held_in_mul_specs}
    return (
        'def solve(instance):\n'
        '    """Evaluate a two-operand integer expression \'A op B\' -> answer string."""\n'
        '    a, op, b = instance.spec.split()\n'
        '    a, b = int(a), int(b)\n'
        '    if op == "+":\n'
        '        return str(a + b)\n'
        '    if op == "-":\n'
        '        return str(a - b)\n'
        f'    _memo = {json.dumps(memo)}\n'
        '    if instance.spec in _memo:\n'
        '        return _memo[instance.spec]\n'
        '    return "0"\n'
    )


def _diff(old: str, new: str) -> str:
    """A git-apply-able unified diff of solver.py (baseline -> candidate)."""
    ud = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="a/solver.py", tofile="b/solver.py")
    return "diff --git a/solver.py b/solver.py\n" + "".join(ud)


def _score_fn(inst, answer, workroot, *, timeout: float = 600.0) -> bool:
    """Instant, deterministic grade: did the solver emit the exact gold answer?"""
    return (answer or "") == inst.gold


# --- the run ------------------------------------------------------------------

def _run_case(name: str, patch: str, tasks: list[Task], solver_dir: Path,
              keys: Path, ledger, workroot: Path):
    res = dgm_uplift.govern_solver_change(
        solver_dir, patch, tasks, keys_dir=keys, ledger=ledger,
        workroot=workroot, held_out_frac=0.5, timeout=60, score_fn=_score_fn)
    print(f"\n=== {name} ===")
    print(f"  boundary_ok      : {res.boundary_ok}")
    print(f"  held-in  resolved: {res.baseline_held_in:.3f} -> {res.candidate_held_in:.3f}")
    print(f"  held-out resolved: {res.baseline_held_out:.3f} -> {res.candidate_held_out:.3f}"
          f"   (uplift {res.uplift:+.3f} over {res.samples} held-out tasks)")
    print(f"  overfit          : {res.overfit}")
    print(f"  PROMOTED         : {res.promoted}")
    print(f"  reason           : {res.reason}")
    return res


def _make_keys(run_dir: Path) -> Path:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    keys = run_dir / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    (keys / "operator.priv.hex").write_text(priv.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    (keys / "operator.pub").write_bytes(priv.public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw))
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="keep the run dir + ledger")
    args = ap.parse_args(argv)

    from maverick.self_improvement import PromotionLedger

    tmp = Path(tempfile.mkdtemp(prefix="dgm_fast_proof_"))
    tasks = build_corpus()

    # Learn the deterministic split so the OVERFIT candidate can memorise EXACTLY
    # the seen (held-in) multiply tasks -- govern_solver_change uses the same split.
    held_in, held_out = dgm_uplift.split_instances(tasks, held_out_frac=0.5)
    golds = {t.spec: t.gold for t in tasks}
    held_in_mul = [t.spec for t in held_in if t.spec.split()[1] == "*"]
    held_out_mul = [t.spec for t in held_out if t.spec.split()[1] == "*"]

    print("MAVERICK -- GOVERNED SELF-IMPROVEMENT, PROVEN IN-PROCESS ($0, seconds)")
    print("=" * 74)
    print(f"  corpus: {len(tasks)} tasks  (held-in {len(held_in)} / held-out {len(held_out)})")
    print(f"  multiply tasks the baseline fails: held-in {len(held_in_mul)}, "
          f"held-out {len(held_out_mul)}  (the real headroom)")
    print("  gate: real 'code' rung (min_samples=10, capability evidence, human signature)")

    solver_dir = tmp / "solver"
    solver_dir.mkdir()
    (solver_dir / "solver.py").write_text(_BASELINE)

    keys = _make_keys(tmp)
    ledger_path = tmp / "ledger.json"
    ledger = PromotionLedger(path=ledger_path)

    results = {}
    results["REAL (generalises to *)"] = _run_case(
        "CASE 1  REAL improvement (should PROMOTE)",
        _diff(_BASELINE, _CAND_REAL), tasks, solver_dir, keys, ledger, tmp / "gov_real")
    results["OVERFIT (memorises seen)"] = _run_case(
        "CASE 2  OVERFIT candidate (should be CAUGHT)",
        _diff(_BASELINE, _overfit_source(held_in_mul, golds)),
        tasks, solver_dir, keys, ledger, tmp / "gov_overfit")
    results["NO-OP (no change)"] = _run_case(
        "CASE 3  NO-OP candidate (should be REFUSED)",
        _diff(_BASELINE, _CAND_NOOP), tasks, solver_dir, keys, ledger, tmp / "gov_noop")

    # --- independent audit of the signed ledger --------------------------------
    print("\n" + "=" * 74)
    print("  INDEPENDENT LEDGER AUDIT (re-verify signatures against the public key)")
    audit = subprocess.run(
        [sys.executable, str(_HERE / "audit_ledger.py"),
         "--ledger", str(ledger_path), "--keys", str(keys)],
        capture_output=True, text=True)
    print(audit.stdout.rstrip() or "(no audit output)")
    if audit.returncode != 0:
        print(audit.stderr.rstrip())

    # --- verdict summary + honesty checks --------------------------------------
    real = results["REAL (generalises to *)"]
    overfit = results["OVERFIT (memorises seen)"]
    noop = results["NO-OP (no change)"]
    ok = (real.promoted and real.uplift > 0
          and overfit.overfit and not overfit.promoted
          and not noop.promoted)
    print("\n" + "=" * 74)
    print(f"  RESULT: {'ALL THREE VERDICTS CORRECT' if ok else 'UNEXPECTED -- see above'}")
    print(f"    real improvement   -> PROMOTED={real.promoted} (uplift {real.uplift:+.3f})")
    print(f"    overfit candidate  -> OVERFIT={overfit.overfit}, promoted={overfit.promoted}")
    print(f"    no-op candidate    -> promoted={noop.promoted}")
    print(f"    ledger audit exit  -> {audit.returncode} (0 = all signatures valid)")

    if args.keep:
        print(f"\n  kept: {tmp}")
    else:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if (ok and audit.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
