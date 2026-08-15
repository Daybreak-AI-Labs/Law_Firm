"""Regression: the governed baseline gate must require the GRADED FAIL_TO_PASS
tests to fail at baseline, not merely that *some* test fails.

The prior gate rejected only when the baseline passed EVERY test. A mis-seeded
instance whose FAIL_TO_PASS already pass at baseline -- but that has a
(env-)failing PASS_TO_PASS -- slipped through: a candidate that merely made the
PASS_TO_PASS pass was then scored RESOLVED with no bug ever fixed. This pins the
fix: such an instance is marked ungradable, never resolved.
"""
from __future__ import annotations

import difflib
import pathlib
import sys
from pathlib import Path

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

# add() is ALREADY correct (FAIL_TO_PASS are green at baseline -- the mis-seed);
# mul() is broken so a PASS_TO_PASS is red at baseline.
_SEED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a - b\n"
_MUL_FIXED = _SEED.replace("def mul(a, b):\n    return a - b",
                           "def mul(a, b):\n    return a * b")

_TESTS = (
    "from calc.core import add, mul\n\n\n"
    "def test_add_basic():\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_negatives():\n    assert add(-1, -1) == -2\n\n\n"
    "def test_mul_basic():\n    assert mul(2, 3) == 6\n\n\n"
    "def test_mul_zero():\n    assert mul(5, 0) == 0\n"
)

FAIL_TO_PASS = [f"tests/test_core.py::{t}" for t in ("test_add_basic", "test_add_negatives")]
PASS_TO_PASS = [f"tests/test_core.py::{t}" for t in ("test_mul_basic", "test_mul_zero")]


def _mul_fix_diff() -> str:
    d = difflib.unified_diff(_SEED.splitlines(keepends=True),
                             _MUL_FIXED.splitlines(keepends=True),
                             fromfile="a/calc/core.py", tofile="b/calc/core.py")
    return "diff --git a/calc/core.py b/calc/core.py\n" + "".join(d)


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
    (repo / "calc" / "core.py").write_text(_SEED, encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(_TESTS, encoding="utf-8")
    return repo


def test_mis_seeded_fail_to_pass_is_ungradable_not_resolved(tmp_path):
    pytest.importorskip("cryptography")
    from maverick.self_improvement import PromotionLedger
    from swebench_governed import Instance, govern_candidate

    repo = _seed_repo(tmp_path)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    keys = tmp_path / "keys"
    keys.mkdir()
    priv = ed25519.Ed25519PrivateKey.generate()
    (keys / "operator.priv.hex").write_text(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex(), encoding="utf-8")
    (keys / "operator.pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    inst = Instance(
        instance_id="calc__misseed-1", repo_path=repo,
        fail_to_pass=FAIL_TO_PASS, pass_to_pass=PASS_TO_PASS,
        gold_patch=_mul_fix_diff(), brief="mul() is wrong (but add already works)")
    ledger = PromotionLedger(path=tmp_path / "ledger.json")

    # The candidate 'fixes' mul (a real PASS_TO_PASS repair) -- but the graded
    # FAIL_TO_PASS (add) were never failing, so there was no bug to fix. Under the
    # old gate this scored RESOLVED; it must now be ungradable.
    out = govern_candidate(inst, _mul_fix_diff(), keys_dir=keys, ledger=ledger,
                           workroot=tmp_path / "work", timeout=120.0,
                           allow_host_exec=True)
    assert out.ungradable is True, out.reason
    assert out.resolved_under_governance is False
    assert not out.promoted
    assert ledger.all() == []          # nothing signed for a mis-seeded instance


def test_score_returns_five_tuple_including_failing_count(tmp_path):
    # Contract guard: _score must return (score, all_pass, samples,
    # f2p_passing, f2p_failing). The 5th element (failing count) is load-bearing
    # for the baseline mis-seed gate -- a `-k` sibling can inflate the passing
    # count to the requested total while a real target still fails, so the gate
    # needs failing==0 to tell "mis-seeded" from "real bug + colliding sibling".
    # audit_ledger.py also imports and unpacks _score, so a silent arity change
    # crashes the independent auditor's forensics re-grade.
    from maverick.self_modify_eval import resolve_eval_sandbox
    from swebench_governed import Instance, _score

    repo = _seed_repo(tmp_path)
    inst = Instance(instance_id="calc__misseed-2", repo_path=repo,
                    fail_to_pass=FAIL_TO_PASS, pass_to_pass=PASS_TO_PASS,
                    gold_patch="", brief="arity contract")
    sb = resolve_eval_sandbox(None, repo)
    result = _score(repo, inst, sb, timeout=120.0)
    assert len(result) == 5, f"_score arity changed: {result!r}"
    score, all_pass, samples, f2p_pass, f2p_fail = result
    assert isinstance(f2p_pass, int) and isinstance(f2p_fail, int)
