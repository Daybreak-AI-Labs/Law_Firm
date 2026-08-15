#!/usr/bin/env python3
"""Governed self-improvement on REAL CODE: a weak LLM bug-fixing agent vs an
improved self-repairing one, measured on HELD-OUT real Python tasks, signed.

This is the caveat-mover: it moves the DGM demonstration off arithmetic and onto
actual programming. Same governance gate as the SWE-bench cycle
(``dgm_uplift.govern_solver_change``), but the tasks are real buggy Python
functions graded by real unit tests, run in-process.

  * baseline agent  -- one LLM shot, no feedback (a deliberately WEAK agent, so
                       there is genuine headroom to improve).
  * improved agent  -- the SAME agent plus a self-repair loop: it runs the
                       PUBLIC tests, reads the failure, and retries (up to K).
                       It never sees the HIDDEN grading tests.

The gate promotes the improved agent only if it lifts the HELD-OUT hidden-test
solve rate (generalisation, not memorisation), clears the real code rung, and is
human-signed into the audit ledger. Every LLM call is priced; hard $ cap.

    python benchmarks/realcode_uplift.py --n 6 --max-dollars 3      # pilot
    python benchmarks/realcode_uplift.py --max-dollars 12           # full
"""
from __future__ import annotations

import argparse
import difflib
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "packages" / "maverick-core"))

import dgm_uplift  # noqa: E402

# --- LLM cost meter ------------------------------------------------------------
from maverick import llm as _llm  # noqa: E402
from realcode_corpus import TASKS  # noqa: E402
from realcode_grade import run_tests  # noqa: E402

_SPEND = {"usd": 0.0, "calls": 0, "cap": 1e9}
_ORIG = _llm.LLM.complete


def _metered(self, system, messages, *a, **kw):
    if _SPEND["usd"] >= _SPEND["cap"]:
        raise RuntimeError(f"cost ceiling ${_SPEND['cap']:.2f} reached (${_SPEND['usd']:.2f})")
    resp = _ORIG(self, system, messages, *a, **kw)
    try:
        c = _llm._response_call_cost(kw.get("model"), resp)
    except Exception:
        c = None
    _SPEND["usd"] += c or 0.0
    _SPEND["calls"] += 1
    return resp


_llm.LLM.complete = _metered


# --- the two agents (solver.py source; the candidate is baseline + a self-repair
#     loop). Both import the shared bits so the diff is minimal and legible. ----

_BASELINE = '''\
import os
import re
from maverick.llm import LLM
from realcode_grade import run_tests  # available to the agent; the weak one won't use it

_MODEL = os.environ.get("REALCODE_SOLVER_MODEL") or None

def _extract(text):
    m = re.search(r"```(?:python)?\\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()

def solve(instance):
    """One shot: fix the buggy function. No test feedback (weak baseline)."""
    llm = LLM()
    sys_p = "You fix buggy Python. Return ONLY the corrected function in a ```python block."
    user = f"Problem: {instance.problem}\\n\\nBuggy code:\\n```python\\n{instance.buggy_code}```"
    return _extract(llm.complete(sys_p, [{"role": "user", "content": user}], max_tokens=400, model=_MODEL).text)
'''

_IMPROVED = '''\
import os
import re
from maverick.llm import LLM
from realcode_grade import run_tests  # available to the agent; the weak one won't use it

_MODEL = os.environ.get("REALCODE_SOLVER_MODEL") or None

def _extract(text):
    m = re.search(r"```(?:python)?\\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()

def solve(instance):
    """Self-repair: fix, run the PUBLIC tests, feed failures back, retry (K=3).
    The HIDDEN grading tests are never shown -- the fix must generalise."""
    llm = LLM()
    sys_p = "You fix buggy Python. Return ONLY the corrected function in a ```python block."
    user = f"Problem: {instance.problem}\\n\\nBuggy code:\\n```python\\n{instance.buggy_code}```"
    code = ""
    for _ in range(3):
        code = _extract(llm.complete(sys_p, [{"role": "user", "content": user}], max_tokens=400, model=_MODEL).text)
        ok, err = run_tests(code, instance.public_tests)
        if ok:
            return code
        user = (f"Problem: {instance.problem}\\n\\nYour code:\\n```python\\n{code}```\\n\\n"
                f"It FAILED a visible test with:\\n{err}\\nReturn the corrected function.")
    return code
'''


def _diff(old: str, new: str) -> str:
    ud = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                              fromfile="a/solver.py", tofile="b/solver.py")
    return "diff --git a/solver.py b/solver.py\n" + "".join(ud)


def _score_fn(inst, fixed_code, workroot, *, timeout: float = 60.0) -> bool:
    """Grade on the HIDDEN tests -- the honest held-out signal."""
    ok, _ = run_tests(fixed_code or "", inst.hidden_tests)
    return ok


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
    ap.add_argument("--n", type=int, default=0, help="use first N tasks (0 = all)")
    ap.add_argument("--max-dollars", type=float, default=12.0)
    ap.add_argument("--min-samples", type=int, default=0, help="override code-rung min_samples")
    ap.add_argument("--model", type=str, default="claude-haiku-4-5-20251001",
                    help="the (deliberately weak) model the agent runs on")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    _SPEND["cap"] = args.max_dollars

    import os
    if args.model:
        os.environ["REALCODE_SOLVER_MODEL"] = args.model
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        print("NO PROVIDER KEY: set ANTHROPIC_API_KEY (this makes real LLM calls).")
        return 2

    from maverick.self_improvement import (
        _RUNG_POLICY,
        PromotionLedger,
        SelfImprovementController,
    )

    tasks = TASKS[:args.n] if args.n else TASKS
    tmp = Path(tempfile.mkdtemp(prefix="realcode_uplift_"))
    solver = tmp / "solver"
    solver.mkdir()
    (solver / "solver.py").write_text(_BASELINE)
    keys = _make_keys(tmp)
    ledger = PromotionLedger(path=tmp / "ledger.json")

    policy = {k: dict(v) for k, v in _RUNG_POLICY.items()}
    if args.min_samples:
        policy["code"]["min_samples"] = args.min_samples
    ctrl = SelfImprovementController(ledger=ledger, rung_policy=policy)

    print(f"MAVERICK -- GOVERNED SELF-IMPROVEMENT ON REAL CODE  ({len(tasks)} bug tasks)")
    print("=" * 74)
    print("  baseline = 1-shot LLM fix | candidate = + self-repair vs PUBLIC tests")
    print("  grading  = HIDDEN unit tests (held-out); gate = real code rung\n")

    t0 = time.time()
    res = dgm_uplift.govern_solver_change(
        solver, _diff(_BASELINE, _IMPROVED), tasks, keys_dir=keys, ledger=ledger,
        workroot=tmp / "gov", held_out_frac=0.5, timeout=60,
        controller=ctrl, score_fn=_score_fn)

    print(f"  held-in  solve-rate : {res.baseline_held_in:.3f} -> {res.candidate_held_in:.3f}")
    print(f"  held-out solve-rate : {res.baseline_held_out:.3f} -> {res.candidate_held_out:.3f}"
          f"   (uplift {res.uplift:+.3f} over {res.samples} held-out tasks)")
    print(f"  overfit guard       : {res.overfit}")
    print(f"  PROMOTED            : {res.promoted}")
    print(f"  reason              : {res.reason}")

    # independent audit
    import subprocess
    audit = subprocess.run(
        [sys.executable, str(_HERE / "audit_ledger.py"),
         "--ledger", str(tmp / "ledger.json"), "--keys", str(keys)],
        capture_output=True, text=True)
    for line in audit.stdout.splitlines():
        if "records |" in line or "AUDIT OK" in line or "INVALID" in line:
            print("  audit               : " + line.strip())

    print("\n" + "=" * 74)
    print(f"  LLM spend ${_SPEND['usd']:.2f} ({_SPEND['calls']} calls)   wall {time.time()-t0:.0f}s")
    verdict = ("PROMOTED — the AI improved its own coding agent on real held-out code"
               if res.promoted and res.uplift > 0 else
               f"NOT PROMOTED ({res.reason})")
    print(f"  RESULT: {verdict}")

    if args.keep:
        print(f"\n  kept: {tmp}")
    else:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if (res.promoted and res.uplift > 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
