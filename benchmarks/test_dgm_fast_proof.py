"""The in-process governed self-improvement proof must keep producing the three
correct verdicts through the REAL gate: a generalising change promotes, a
memorising change is caught by the overfit guard, and a no-op is refused -- all
signed and independently auditable. Pure in-process; no network, no LLM, $0.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

import dgm_fast_proof as proof  # noqa: E402
import dgm_uplift  # noqa: E402


def _govern(patch, tasks, solver_dir, keys, ledger, workroot):
    return dgm_uplift.govern_solver_change(
        solver_dir, patch, tasks, keys_dir=keys, ledger=ledger,
        workroot=workroot, held_out_frac=0.5, timeout=60, score_fn=proof._score_fn)


def _setup(tmp_path):
    from maverick.self_improvement import PromotionLedger
    tasks = proof.build_corpus()
    solver_dir = tmp_path / "solver"
    solver_dir.mkdir()
    (solver_dir / "solver.py").write_text(proof._BASELINE)
    keys = proof._make_keys(tmp_path)
    ledger = PromotionLedger(path=tmp_path / "ledger.json")
    return tasks, solver_dir, keys, ledger


def test_real_generalising_change_is_promoted(tmp_path):
    tasks, solver_dir, keys, ledger = _setup(tmp_path)
    res = _govern(proof._diff(proof._BASELINE, proof._CAND_REAL),
                  tasks, solver_dir, keys, ledger, tmp_path / "g")
    assert res.boundary_ok
    assert res.candidate_held_out > res.baseline_held_out   # real held-out uplift
    assert res.uplift > 0
    assert res.samples >= 10                                 # cleared code-rung min_samples
    assert not res.overfit
    assert res.promoted, res.reason


def test_memorising_change_is_caught_by_overfit_guard(tmp_path):
    tasks, solver_dir, keys, ledger = _setup(tmp_path)
    held_in, _ = dgm_uplift.split_instances(tasks, held_out_frac=0.5)
    golds = {t.spec: t.gold for t in tasks}
    held_in_mul = [t.spec for t in held_in if t.spec.split()[1] == "*"]
    patch = proof._diff(proof._BASELINE, proof._overfit_source(held_in_mul, golds))
    res = _govern(patch, tasks, solver_dir, keys, ledger, tmp_path / "g")
    assert res.candidate_held_in > res.baseline_held_in      # lifted SEEN tasks
    assert res.candidate_held_out <= res.baseline_held_out   # but not UNSEEN
    assert res.overfit
    assert not res.promoted


def test_noop_change_is_refused(tmp_path):
    tasks, solver_dir, keys, ledger = _setup(tmp_path)
    res = _govern(proof._diff(proof._BASELINE, proof._CAND_NOOP),
                  tasks, solver_dir, keys, ledger, tmp_path / "g")
    assert res.uplift == 0
    assert not res.overfit
    assert not res.promoted


def test_full_driver_runs_and_audits_clean(tmp_path):
    # The end-to-end driver returns 0 only when all three verdicts are correct
    # AND the independent ledger audit re-verifies every signature.
    assert proof.main([]) == 0
