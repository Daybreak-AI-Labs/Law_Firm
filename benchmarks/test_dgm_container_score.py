"""Offline tests: the DGM's uplift eval graded in OFFICIAL containers.

The DGM's held-out uplift is only as honest as its grading environment --
measuring "self-improvement" on a host that can't reproduce each instance's
era is exactly the bug class that produced NOENV/EMPTY money waste. These
tests pin ``dgm_uplift.container_score_fn`` (grading hook -> official image)
and the ``score_fn`` plumbing through ``run_solver`` / ``govern_solver_change``,
with a FAKE ``run_in_image`` and the committed astropy fixture: no network,
no Modal, no spend.

Each test encodes a predicted failure mode:
  * baseline re-graded per solver arm  -> cost x2       (cache: graded once)
  * mis-seeded instance inflates rates -> fake uplift   (scores False, cached)
  * solver emits a test-editing patch  -> rigged grade  (refused at $0)
  * corpus not in Verified             -> silent 0-rate (raises, loud)
  * end-to-end: the governed chain promotes REAL uplift measured in containers
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

pytest.importorskip("swebench", reason="official swebench harness required")
pytest.importorskip("cryptography")

import dgm_uplift  # noqa: E402
import swebench_container_grade as CG  # noqa: E402
from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT  # noqa: E402
from swebench_governed import Instance  # noqa: E402
from testdata.astropy_12907 import INSTANCE as _ASTROPY  # noqa: E402


def _raw(iid: str | None = None) -> dict:
    r = copy.deepcopy(_ASTROPY)
    if iid:
        r["instance_id"] = iid
    return r


def _inst(raw: dict, repo_path: Path) -> Instance:
    return Instance(
        instance_id=raw["instance_id"], repo_path=repo_path,
        fail_to_pass=list(raw["FAIL_TO_PASS"]), pass_to_pass=list(raw["PASS_TO_PASS"]),
        gold_patch=raw["patch"], test_patch=raw.get("test_patch", ""),
        brief="astropy separability matrix bug")


def _log(raw: dict, *, all_pass: bool) -> str:
    lines = [str(START_TEST_OUTPUT)]
    for t in raw["FAIL_TO_PASS"]:
        lines.append(f"{'PASSED' if all_pass else 'FAILED'} {t}")
    for t in raw["PASS_TO_PASS"]:
        lines.append(f"PASSED {t}")
    lines.append(str(END_TEST_OUTPUT))
    return "install ...\n" + "\n".join(lines) + "\n"


def _runner(raws: dict[str, dict], *, mis_seeded: bool = False):
    """Fake run_in_image: baseline scripts (no candidate apply) show the bug
    (FAIL_TO_PASS fail) unless mis_seeded; candidate scripts resolve it.
    Records every call as (kind, image)."""
    calls: list[tuple[str, str]] = []

    def run(image, script, timeout):
        # every raw shares the astropy test ids, so any raw works for the log
        raw = next(iter(raws.values()))
        is_candidate = "maverick_candidate.diff" in script
        calls.append(("candidate" if is_candidate else "baseline", image))
        return CG.RunOutput(
            stdout=_log(raw, all_pass=(is_candidate or mis_seeded)), exit_code=1)
    run.calls = calls
    return run


def _keys(tmp_path: Path) -> Path:
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


# --- container_score_fn unit behavior ------------------------------------------

def test_resolves_when_baseline_fails_and_candidate_passes(tmp_path):
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw})
    score = dgm_uplift.container_score_fn(runner, verified={raw["instance_id"]: raw})
    assert score(_inst(raw, tmp_path), raw["patch"], tmp_path / "w") is True
    kinds = [k for k, _ in runner.calls]
    assert kinds == ["baseline", "candidate"]


def test_baseline_graded_once_across_solver_arms(tmp_path):
    # Predicted failure: both solver arms re-grade the same instance's baseline
    # -> double container cost. The verdict is a property of the INSTANCE, so
    # the second scoring call must reuse the cached baseline.
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw})
    score = dgm_uplift.container_score_fn(runner, verified={raw["instance_id"]: raw})
    inst = _inst(raw, tmp_path)
    assert score(inst, raw["patch"], tmp_path / "a") is True
    assert score(inst, raw["patch"], tmp_path / "b") is True
    kinds = [k for k, _ in runner.calls]
    assert kinds.count("baseline") == 1          # cached across calls
    assert kinds.count("candidate") == 2


def test_mis_seeded_instance_scores_false_and_never_grades_candidates(tmp_path):
    # Predicted failure: FAIL_TO_PASS already pass at baseline (bad image /
    # mis-seed) -> ANY applying patch would grade "resolved", inflating both
    # arms. Must score False, cache the verdict, and skip candidate grading.
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw}, mis_seeded=True)
    score = dgm_uplift.container_score_fn(runner, verified={raw["instance_id"]: raw})
    inst = _inst(raw, tmp_path)
    assert score(inst, raw["patch"], tmp_path / "a") is False
    assert score(inst, raw["patch"], tmp_path / "b") is False
    kinds = [k for k, _ in runner.calls]
    assert kinds == ["baseline"]                 # once, cached; no candidate run


def test_cheating_solver_patch_refused_at_zero_container_cost(tmp_path):
    # Predicted failure: a solver "improves" by editing the graded tests. The
    # host boundary must refuse BEFORE any container spend.
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw})
    score = dgm_uplift.container_score_fn(runner, verified={raw["instance_id"]: raw})
    cheat = (
        "diff --git a/astropy/modeling/tests/test_separable.py "
        "b/astropy/modeling/tests/test_separable.py\n"
        "--- a/astropy/modeling/tests/test_separable.py\n"
        "+++ b/astropy/modeling/tests/test_separable.py\n"
        "@@ -1 +1 @@\n-assert x\n+assert True\n")
    assert score(_inst(raw, tmp_path), cheat, tmp_path / "w") is False
    assert runner.calls == []


def test_empty_patch_scores_false_without_container(tmp_path):
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw})
    score = dgm_uplift.container_score_fn(runner, verified={raw["instance_id"]: raw})
    assert score(_inst(raw, tmp_path), "", tmp_path / "w") is False
    assert runner.calls == []


def test_non_verified_instance_raises_loud(tmp_path):
    # Predicted failure: a synthetic/staged corpus under container grading
    # silently scoring 0 would fake a flat baseline. Must raise instead.
    raw = _raw()
    runner = _runner({raw["instance_id"]: raw})
    score = dgm_uplift.container_score_fn(runner, verified={})
    with pytest.raises(RuntimeError, match="not in SWE-bench Verified"):
        score(_inst(raw, tmp_path), raw["patch"], tmp_path / "w")


# --- the full governed chain, graded in containers ------------------------------

_SOLVER_V0 = 'def solve(instance):\n    return ""\n'
_SOLVER_PATCH = (
    "diff --git a/solver.py b/solver.py\n"
    "--- a/solver.py\n"
    "+++ b/solver.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def solve(instance):\n"
    '-    return ""\n'
    "+    return instance.gold_patch\n")


def test_govern_solver_change_promotes_real_uplift_measured_in_containers(tmp_path):
    """End to end: baseline solver resolves nothing; the candidate patch makes
    it emit real fixes; grading runs in (fake) official containers; the gate
    sees held-out uplift, signs, and the ledger holds the signature."""
    from maverick.self_improvement import (
        _RUNG_POLICY,
        PromotionLedger,
        SelfImprovementController,
    )

    raws = {f"astropy__astropy-1290{i}": _raw(f"astropy__astropy-1290{i}")
            for i in (7, 8)}
    instances = [_inst(r, tmp_path) for r in raws.values()]
    runner = _runner(raws)
    score = dgm_uplift.container_score_fn(runner, verified=raws)

    solver = tmp_path / "solver"
    solver.mkdir()
    (solver / "solver.py").write_text(_SOLVER_V0)

    keys = _keys(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    ledger = PromotionLedger(path=ledger_path)
    policy = {k: dict(v) for k, v in _RUNG_POLICY.items()}
    policy["code"]["min_samples"] = 1
    ctrl = SelfImprovementController(ledger=ledger, rung_policy=policy)

    res = dgm_uplift.govern_solver_change(
        solver, _SOLVER_PATCH, instances, keys_dir=keys, ledger=ledger,
        workroot=tmp_path / "gov", held_out_frac=0.5, timeout=60,
        controller=ctrl, score_fn=score)

    assert res.boundary_ok is True
    assert res.baseline_held_out == 0.0          # v0 emits no patch
    assert res.candidate_held_out == 1.0         # v1's fixes resolve in-container
    assert res.overfit is False
    assert res.promoted is True, res.reason
    assert res.uplift == 1.0
    recs = json.loads(ledger_path.read_text())
    assert recs and recs[-1].get("approval_signature")
    # and the containers actually did the grading for the candidate arm
    kinds = [k for k, _ in runner.calls]
    assert kinds.count("candidate") == len(instances)
    assert kinds.count("baseline") == len(instances)   # cached: once per instance


class _I:
    def __init__(self, iid):
        self.instance_id = iid


def _mk_solver(tmp_path):
    d = tmp_path / "solver"
    d.mkdir()
    (d / "solver.py").write_text("def solve(instance):\n    return 'x'\n")
    return d


def test_checkpoint_resume_skips_graded_instances_and_matches(tmp_path):
    # A killed cycle must RESUME: instances already in the checkpoint are not
    # re-graded (no wasted agent spend), and the resolved-map is identical.
    calls = {"n": 0}

    def scorer(inst, patch, wr, *, timeout=600.0):
        calls["n"] += 1
        return inst.instance_id.endswith("odd")

    insts = [_I("a-even"), _I("b-odd"), _I("c-even")]
    solver = _mk_solver(tmp_path)
    ckpt = tmp_path / "ck.jsonl"
    r1 = dgm_uplift.run_solver(solver, insts, tmp_path / "w1",
                               score_fn=scorer, checkpoint=ckpt)
    assert calls["n"] == 3
    calls["n"] = 0
    r2 = dgm_uplift.run_solver(solver, insts, tmp_path / "w2",
                               score_fn=scorer, checkpoint=ckpt)
    assert calls["n"] == 0            # all reused from checkpoint
    assert r1 == r2 == {"a-even": False, "b-odd": True, "c-even": False}


def test_partial_checkpoint_resumes_only_the_remainder(tmp_path):
    solver = _mk_solver(tmp_path)
    ckpt = tmp_path / "ck.jsonl"
    ckpt.write_text('{"instance_id": "a-even", "resolved": false}\n')  # a already done
    graded = []

    def scorer(inst, patch, wr, *, timeout=600.0):
        graded.append(inst.instance_id)
        return True

    out = dgm_uplift.run_solver(solver, [_I("a-even"), _I("b-odd")], tmp_path / "w",
                                score_fn=scorer, checkpoint=ckpt)
    assert graded == ["b-odd"]        # a-even skipped, only b-odd graded
    assert out == {"a-even": False, "b-odd": True}


def test_run_solver_defaults_to_host_scoring_unchanged(tmp_path, monkeypatch):
    """score_fn=None must keep the host score_instance path byte-for-byte (the
    offline proof and synthetic corpora depend on it)."""
    seen = {}

    def fake_score(inst, patch, workroot, *, timeout=600.0):
        seen["called"] = True
        return False

    monkeypatch.setattr(dgm_uplift, "score_instance", fake_score)
    solver = tmp_path / "solver"
    solver.mkdir()
    (solver / "solver.py").write_text(_SOLVER_V0)
    raw = _raw()
    out = dgm_uplift.run_solver(solver, [_inst(raw, tmp_path)], tmp_path / "w")
    assert out == {raw["instance_id"]: False}
    assert seen.get("called") is True
