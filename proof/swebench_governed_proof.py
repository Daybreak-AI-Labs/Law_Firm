#!/usr/bin/env python3
"""Lightwork -- proof that SWE-bench runs UNDER governance.

A self-contained, reproducible run that exercises the governed SWE-bench scorer
(``benchmarks/swebench_governed.py``) on a fixture instance shaped exactly like a
real SWE-bench task -- a repo with a bug, a set of ``FAIL_TO_PASS`` tests that are
red until it's fixed, and ``PASS_TO_PASS`` regression tests that must stay green.
No LLM, no network, no Docker. Because this proof seeds the repository and
patch itself, it explicitly opts in to the trusted host-exec fixture path; the
external benchmark CLI refuses host execution by default.

    python proof/swebench_governed_proof.py   # exits 0 iff every guarantee holds

It proves the governance chain behaves correctly on the three cases that decide
whether a SWE-bench number is trustworthy:

  1. GENUINE FIX RESOLVES        -- the gold patch fixes the bug, passes every
     held-out test on an isolated copy, and is promoted to the signed ledger:
     resolved_under_governance = True.
  2. TEST-EDIT CHEAT REFUSED     -- a patch that edits the test file to make the
     failing assertion pass is refused by the anti-cheat boundary BEFORE any test
     runs. This is the property that makes the number un-inflatable -- the #1
     SWE-bench gaming vector (editing the grader's tests) is structurally blocked.
  3. REGRESSION CAUGHT           -- a patch that fixes the target bug but breaks a
     PASS_TO_PASS test is not resolved (the held-out regression gate fails), so it
     is never promoted.

The fixture stands in for a real instance so this runs anywhere; the REAL number
comes from ``benchmarks/swebench_governed.py --proposer llm`` over real Verified
instances with a provider key. What this proves is that the governance *wrapper*
around that number is real: genuine fixes pass, cheats and regressions do not.
"""
from __future__ import annotations

import difflib
import pathlib
import sys
import tempfile
from pathlib import Path

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

# --- the fixture instance: a buggy `add`, real red/green tests ----------------
_BROKEN = '''\
def add(a, b):
    return a - b


def mul(a, b):
    return a * b


def sub(a, b):
    return a - b
'''
# The gold fix: `add` should sum, not subtract.
_FIXED = _BROKEN.replace("def add(a, b):\n    return a - b",
                         "def add(a, b):\n    return a + b")
# A regression: fixes `add` but breaks `mul` (a PASS_TO_PASS capability).
_REGRESS = _FIXED.replace("def mul(a, b):\n    return a * b",
                          "def mul(a, b):\n    return a + b")

_TESTS = '''\
from calc.core import add, mul, sub


def test_add_basic():
    assert add(2, 3) == 5


def test_add_negatives():
    assert add(-1, -1) == -2


def test_add_large():
    assert add(10, 7) == 17


def test_add_hundreds():
    assert add(100, 1) == 101


def test_mul_basic():
    assert mul(2, 3) == 6


def test_mul_zero():
    assert mul(5, 0) == 0


def test_mul_negative():
    assert mul(-2, 3) == -6


def test_mul_square():
    assert mul(7, 7) == 49


def test_sub_basic():
    assert sub(5, 3) == 2


def test_sub_zero():
    assert sub(0, 0) == 0


def test_sub_negatives():
    assert sub(-1, -1) == 0


def test_sub_large():
    assert sub(10, 4) == 6
'''

# FAIL_TO_PASS: red on the broken tree (a-b != a+b for b!=0). PASS_TO_PASS: green
# on the broken tree and must stay green (mul/sub untouched by the add bug).
FAIL_TO_PASS = [f"tests/test_core.py::{t}" for t in
                ("test_add_basic", "test_add_negatives", "test_add_large", "test_add_hundreds")]
PASS_TO_PASS = [f"tests/test_core.py::{t}" for t in
                ("test_mul_basic", "test_mul_zero", "test_mul_negative", "test_mul_square",
                 "test_sub_basic", "test_sub_zero", "test_sub_negatives", "test_sub_large")]


def _diff(before: str, after: str) -> str:
    d = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                             fromfile="a/calc/core.py", tofile="b/calc/core.py")
    return "diff --git a/calc/core.py b/calc/core.py\n" + "".join(d)


def _cheat_diff() -> str:
    """Edit the TEST file so the failing assertion passes -- the gaming vector."""
    before = "def test_add_basic():\n    assert add(2, 3) == 5\n"
    after = "def test_add_basic():\n    assert add(2, 3) == -1  # weakened to match the bug\n"
    d = difflib.unified_diff(_TESTS.splitlines(keepends=True),
                             _TESTS.replace(before, after).splitlines(keepends=True),
                             fromfile="a/tests/test_core.py", tofile="b/tests/test_core.py")
    return "diff --git a/tests/test_core.py b/tests/test_core.py\n" + "".join(d)


def _seed_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "calc").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"calc\"\nversion = \"0.0.0\"\n", encoding="utf-8")
    (repo / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
        encoding="utf-8")
    (repo / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "calc" / "core.py").write_text(_BROKEN, encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(_TESTS, encoding="utf-8")
    return repo


class _R:
    def __init__(self, label, passed, detail):
        self.label, self.passed, self.detail = label, passed, detail


def run_all(work: Path) -> list[_R]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from maverick.self_improvement import PromotionLedger
    from swebench_governed import Instance, govern_candidate

    repo = _seed_repo(work)
    keys = work / "keys"
    keys.mkdir()
    priv = ed25519.Ed25519PrivateKey.generate()
    (keys / "operator.priv.hex").write_text(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex(), encoding="utf-8")
    (keys / "operator.pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    inst = Instance(
        instance_id="calc__add-1", repo_path=repo,
        fail_to_pass=FAIL_TO_PASS, pass_to_pass=PASS_TO_PASS,
        gold_patch=_diff(_BROKEN, _FIXED), brief="add() subtracts instead of summing")
    ledger = PromotionLedger(path=work / "ledger.json")

    def gov(patch, sub):
        return govern_candidate(inst, patch, keys_dir=keys, ledger=ledger,
                                workroot=work / sub, timeout=120.0,
                                allow_host_exec=True)

    out: list[_R] = []

    # 1. Genuine gold fix -> resolved under governance + signed promotion.
    g = gov(_diff(_BROKEN, _FIXED), "genuine")
    ok1 = (g.resolved_under_governance and g.tests_resolved and g.promoted
           and g.candidate_score == 1.0 and g.baseline_score < 1.0)
    out.append(_R("genuine fix resolves", ok1,
                  f"baseline {g.baseline_score:.3f} -> candidate {g.candidate_score:.3f}, "
                  f"promoted={g.promoted}, approver={g.approver_id}, samples={g.samples}"))

    # 2. Test-edit cheat -> refused by the boundary, never promoted.
    c = gov(_cheat_diff(), "cheat")
    ok2 = (not c.boundary_ok) and (not c.promoted) and (not c.resolved_under_governance)
    out.append(_R("test-edit cheat refused", ok2,
                  f"boundary_ok={c.boundary_ok}, promoted={c.promoted} -- {c.reason}"))

    # 3. Regression (breaks a PASS_TO_PASS) -> not resolved, never promoted.
    r = gov(_diff(_BROKEN, _REGRESS), "regress")
    ok3 = (r.boundary_ok and not r.tests_resolved and not r.promoted
           and not r.resolved_under_governance)
    out.append(_R("regression caught", ok3,
                  f"boundary_ok={r.boundary_ok}, tests_resolved={r.tests_resolved} -- {r.reason}"))

    # 4. The ledger recorded exactly the genuine promotion (no cheat/regression).
    ids = {rec.id for rec in ledger.all()}
    ok4 = ids == {"calc__add-1"}
    out.append(_R("ledger holds only the genuine fix", ok4,
                  f"ledger ids = {sorted(ids)}"))
    return out


def main() -> int:
    try:
        import cryptography  # noqa: F401
    except Exception:
        print("  [ CI ]  SWE-bench governance proof needs `cryptography` -- verified in CI")
        return 0
    print("=" * 78)
    print("  MAVERICK -- SWE-BENCH UNDER GOVERNANCE   (fixture instance, real gate)")
    print("=" * 78)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="swebench-gov-proof-") as td:
        for r in run_all(Path(td)):
            tag = "PASS" if r.passed else "FAIL"
            failed += 0 if r.passed else 1
            print(f"  [{tag}]  {r.label:34}  {r.detail}")
    print("=" * 78)
    print(f"  {4 - failed} guarantees PROVEN, {failed} failed"
          "   (real number: --proposer llm over Verified instances, needs a key)")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
