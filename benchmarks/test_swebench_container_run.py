"""Offline test for the governed container RUN driver.

No network / Modal / Docker: a fake ``run_in_image`` returns canned official
logs (baseline fails FAIL_TO_PASS, candidate passes everything), ephemeral
operator keys sign the promotion, and we assert the full compose --
grade -> baseline-fails -> resolved -> capability -> signed ledger -- plus that
the INDEPENDENT auditor accepts the signed record. This pins the wiring that
turns container grading into a governed, signed run, for $0.
"""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

pytest.importorskip("swebench", reason="official swebench harness required")
pytest.importorskip("cryptography")

import swebench_container_grade as CG  # noqa: E402
import swebench_container_run as RUN  # noqa: E402
from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT  # noqa: E402
from testdata.astropy_12907 import INSTANCE as _ASTROPY  # noqa: E402


def _instance() -> dict:
    return copy.deepcopy(_ASTROPY)


def _canned_log(inst: dict, *, all_pass: bool) -> str:
    """Minimal official-format log. all_pass=True -> every FAIL_TO_PASS passes;
    all_pass=False -> every FAIL_TO_PASS fails (the bug). PASS_TO_PASS always
    pass (SWE-bench definition)."""
    lines = [str(START_TEST_OUTPUT)]
    for t in inst["FAIL_TO_PASS"]:
        lines.append(f"{'PASSED' if all_pass else 'FAILED'} {t}")
    for t in inst["PASS_TO_PASS"]:
        lines.append(f"PASSED {t}")
    lines.append(str(END_TEST_OUTPUT))
    return "install ...\n" + "\n".join(lines) + "\n"


def _baseline_then_candidate_runner(inst):
    """First call (baseline, empty patch) -> FAIL_TO_PASS fail; second call
    (candidate) -> everything passes. Mirrors a real bug fixed by the patch."""
    logs = [_canned_log(inst, all_pass=False), _canned_log(inst, all_pass=True)]
    seq = {"i": 0}

    def run(image, script, timeout):
        out = logs[min(seq["i"], len(logs) - 1)]
        seq["i"] += 1
        return CG.RunOutput(stdout=out, exit_code=1)
    run.seq = seq
    return run


def _keys(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    keys = tmp_path / "keys"
    keys.mkdir()
    priv = ed25519.Ed25519PrivateKey.generate()
    (keys / "operator.priv.hex").write_text(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex())
    (keys / "operator.pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return keys


def test_gold_resolves_promotes_and_signs(tmp_path):
    from maverick.self_improvement import PromotionLedger
    inst = _instance()
    keys = _keys(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    ledger = PromotionLedger(path=ledger_path)
    runner = _baseline_then_candidate_runner(inst)

    o = RUN.govern_one(inst, inst["patch"], runner, proposer="oracle",
                       keys_dir=keys, ledger=ledger, namespace="swebench",
                       timeout=60, check_baseline=True)

    assert o.boundary_ok is True
    assert o.baseline_fails is True
    assert o.resolved is True
    assert o.promoted is True
    assert o.resolved_under_governance is True
    assert o.approver_id
    assert runner.seq["i"] == 2                      # baseline + candidate
    # a signed record landed in the ledger
    recs = __import__("json").loads(ledger_path.read_text())
    assert recs and recs[0].get("approval_signature")


def test_signed_run_verifies_under_independent_auditor(tmp_path):
    from maverick.self_improvement import PromotionLedger
    inst = _instance()
    keys = _keys(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    ledger = PromotionLedger(path=ledger_path)
    RUN.govern_one(inst, inst["patch"], _baseline_then_candidate_runner(inst),
                   proposer="oracle", keys_dir=keys, ledger=ledger,
                   namespace="swebench", timeout=60, check_baseline=True)

    r = subprocess.run(
        [sys.executable, str(_ROOT / "benchmarks/audit_ledger.py"),
         "--ledger", str(ledger_path), "--keys", str(keys)],
        capture_output=True, text=True)
    assert r.returncode == 0, f"auditor rejected a valid signed run: {r.stdout}\n{r.stderr}"


def test_baseline_miss_seed_blocks_promotion(tmp_path):
    """If FAIL_TO_PASS already pass at baseline (mis-seeded/bad image), the run
    must NOT resolve or promote -- caught at container cost, nothing signed."""
    from maverick.self_improvement import PromotionLedger
    inst = _instance()
    keys = _keys(tmp_path)
    ledger = PromotionLedger(path=tmp_path / "ledger.json")

    # baseline (first call) shows everything passing -> mis-seeded
    def runner(image, script, timeout):
        return CG.RunOutput(stdout=_canned_log(inst, all_pass=True), exit_code=1)

    o = RUN.govern_one(inst, inst["patch"], runner, proposer="oracle",
                       keys_dir=keys, ledger=ledger, namespace="swebench",
                       timeout=60, check_baseline=True)
    assert o.resolved is False
    assert o.promoted is False
    assert "mis-seeded" in o.reason


def test_test_editing_candidate_refused_before_container(tmp_path):
    from maverick.self_improvement import PromotionLedger
    inst = _instance()
    keys = _keys(tmp_path)
    ledger = PromotionLedger(path=tmp_path / "ledger.json")
    cheat = (
        "diff --git a/astropy/modeling/tests/test_separable.py "
        "b/astropy/modeling/tests/test_separable.py\n"
        "--- a/astropy/modeling/tests/test_separable.py\n"
        "+++ b/astropy/modeling/tests/test_separable.py\n"
        "@@ -1 +1 @@\n-assert x\n+assert True\n")
    calls = {"n": 0}

    def runner(image, script, timeout):
        calls["n"] += 1
        return CG.RunOutput(stdout="", exit_code=0)

    o = RUN.govern_one(inst, cheat, runner, proposer="llm-fake",
                       keys_dir=keys, ledger=ledger, namespace="swebench",
                       timeout=60, check_baseline=True)
    assert o.boundary_ok is False
    assert o.promoted is False
    assert calls["n"] == 0                            # container never invoked


def test_sample_ids_is_deterministic_and_sorted():
    pool = [f"repo__proj-{i}" for i in range(100)]
    a = RUN.sample_ids(pool, 5, seed=1729)
    b = RUN.sample_ids(pool, 5, seed=1729)
    assert a == b                                     # reproducible
    assert a == sorted(a)                             # sorted output
    assert len(a) == 5 and len(set(a)) == 5
    assert RUN.sample_ids(pool, 0, seed=1) == sorted(pool)   # 0 = all
