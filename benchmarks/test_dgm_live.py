"""Tests for the LIVE governed DGM driver (``benchmarks/dgm_live.py``).

Uses the importlib-load pattern (like ``test_fetch_swe_bench_verified.py``) so
the sibling benchmark scripts import cleanly outside a package. The stub-mode
end-to-end test reuses the proof's synthetic calc fixture -- an OFFLINE
string-replace solver whose v0 fixes bug-kind A and whose v1 also fixes kind B
-- so the whole governed chain (baseline eval, split, propose, govern, sign,
ledger) runs for $0. Corpus size 16 / held_out_frac 0.7 mirrors the proof: it
yields 11 held-out instances, clearing the code rung's min_samples=10 evidence
floor while keeping one unseen bug-kind-B instance for the genuine uplift to fix.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_driver():
    return _load("dgm_live_mod", _HERE / "dgm_live.py")


def _load_proof():
    # The proof module carries the synthetic fixture (seed_instance + solver
    # versions + patch builder); its module-level sys.path wiring also puts
    # maverick-core + benchmarks on the path.
    return _load("dgm_uplift_proof_fixture", _ROOT / "proof" / "dgm_uplift_proof.py")


# --- fixtures -----------------------------------------------------------------

def _write_keys(tmp_path: Path) -> Path:
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
    return keys


def _seed_corpus(tmp_path: Path):
    """Write a 16-instance manifest + an offline solver dir with a stub patch."""
    proof = _load_proof()
    from maverick.self_harness_eval import corpus_split

    ids = [f"calc-inst-{i:02d}" for i in range(1, 17)]
    in_ids, out_ids = corpus_split([{"goal": i} for i in ids], held_out_frac=0.7)
    kind_of: dict[str, str] = {}
    for pos, iid in enumerate(out_ids):
        kind_of[iid] = "B" if pos == 0 else "A"   # held-out: one unseen kind B
    for pos, iid in enumerate(in_ids):
        kind_of[iid] = "B" if pos == 0 else "A"   # held-in: one kind B to learn from

    inst_root = tmp_path / "instances"
    rows = []
    for iid in ids:
        inst = proof._seed_instance(inst_root, iid, kind_of[iid])
        rows.append({
            "instance_id": inst.instance_id,
            "repo_path": str(inst.repo_path),
            "fail_to_pass": inst.fail_to_pass,
            "pass_to_pass": inst.pass_to_pass,
            "gold_patch": inst.gold_patch,
            "brief": inst.brief,
        })
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    solver_dir = tmp_path / "solver"
    solver_dir.mkdir()
    (solver_dir / "solver.py").write_text(proof._SOLVER_V0, encoding="utf-8")
    # The known-good improvement a real proposer would emit: teach the solver
    # bug-kind B. --stub authors the candidate from exactly this diff.
    (solver_dir / "stub_improve.patch").write_text(
        proof._solver_patch(proof._SOLVER_V0, proof._SOLVER_V1), encoding="utf-8")
    return manifest, solver_dir


# --- CLI parsing / defaults ---------------------------------------------------

class TestCLI:
    def test_defaults(self):
        drv = _load_driver()
        args = drv._parse_args(["--manifest", "m.jsonl", "--keys", "k"])
        assert args.held_out_frac == 0.5
        assert args.min_samples == 10          # the code rung's own default
        assert args.stub is False
        assert args.gradable_only is False
        assert args.limit == 0
        assert args.abort_at_dollars == 0.0
        assert args.solver.name == "baseline_v0"
        assert args.ledger.name == "dgm_uplift_ledger.json"

    def test_flags_parse(self):
        drv = _load_driver()
        args = drv._parse_args([
            "--manifest", "m.jsonl", "--keys", "k", "--held-out-frac", "0.7",
            "--min-samples", "4", "--gradable-only", "--limit", "5",
            "--abort-at-dollars", "2.5", "--stub"])
        assert args.held_out_frac == 0.7
        assert args.min_samples == 4
        assert args.gradable_only is True
        assert args.limit == 5
        assert args.abort_at_dollars == 2.5
        assert args.stub is True


# --- pregate filter -----------------------------------------------------------

class _Inst:
    def __init__(self, iid: str, ok: bool):
        self.instance_id, self.ok = iid, ok


class _FakeSwg:
    """Canned _env_pregate: gradable iff the instance is flagged ok."""

    @staticmethod
    def _venv_python(inst):
        return ""

    @staticmethod
    def _env_pregate(inst, workroot, *, timeout):
        return "" if inst.ok else "baseline cannot run its own passing tests"


class TestPregateFilter:
    def test_drops_ungradable(self, tmp_path):
        drv = _load_driver()
        instances = [_Inst("keep-a", True), _Inst("drop-b", False), _Inst("keep-c", True)]
        kept, dropped = drv._filter_gradable(
            _FakeSwg, instances, tmp_path, timeout=1.0)
        assert [i.instance_id for i in kept] == ["keep-a", "keep-c"]
        assert dropped == 1

    def test_pregate_error_drops_closed(self, tmp_path):
        drv = _load_driver()

        class _Boom:
            @staticmethod
            def _venv_python(inst):
                return ""

            @staticmethod
            def _env_pregate(inst, workroot, *, timeout):
                raise RuntimeError("materialize blew up")

        kept, dropped = drv._filter_gradable(
            _Boom, [_Inst("x", True)], tmp_path, timeout=1.0)
        assert kept == [] and dropped == 1

    def test_pregate_runs_under_instance_venv(self, tmp_path, monkeypatch):
        """The filter must grade with each instance's era-correct interpreter
        (MAVERICK_TEST_PYTHON), like main() and score_instance -- otherwise a
        venv'd pod pregates with HOST python and drops the whole corpus."""
        drv = _load_driver()
        monkeypatch.delenv("MAVERICK_TEST_PYTHON", raising=False)
        seen: dict[str, str | None] = {}

        class _VenvSwg:
            @staticmethod
            def _venv_python(inst):
                return f"/venvs/{inst.instance_id}/bin/python"

            @staticmethod
            def _env_pregate(inst, workroot, *, timeout):
                import os
                seen[inst.instance_id] = os.environ.get("MAVERICK_TEST_PYTHON")
                return ""

        import os
        kept, dropped = drv._filter_gradable(
            _VenvSwg, [_Inst("repo__a-1", True), _Inst("repo__b-2", True)],
            tmp_path, timeout=1.0)
        assert dropped == 0 and len(kept) == 2
        # Each pregate saw ITS instance's interpreter...
        assert seen == {
            "repo__a-1": "/venvs/repo__a-1/bin/python",
            "repo__b-2": "/venvs/repo__b-2/bin/python",
        }
        # ...and the env is restored afterward.
        assert os.environ.get("MAVERICK_TEST_PYTHON") is None


# --- baseline solver: keyless returns "" --------------------------------------

class TestBaselineSolver:
    def test_keyless_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MAVERICK_PROVIDER_READY", raising=False)
        solver = _load("baseline_v0_solver",
                       _HERE / "solvers" / "baseline_v0" / "solver.py")

        class _I:
            repo_path = "/nonexistent"
            fail_to_pass = ["tests/test_x.py::test_a"]
            brief = "bug"

        assert solver.solve(_I()) == ""


# --- stub-mode end-to-end -----------------------------------------------------

class TestStubEndToEnd:
    def test_full_chain_promotes(self, tmp_path):
        pytest.importorskip("cryptography")  # Ed25519 signing
        drv = _load_driver()
        manifest, solver_dir = _seed_corpus(tmp_path)
        keys = _write_keys(tmp_path)
        ledger = tmp_path / "ledger.json"

        args = drv._parse_args([
            "--manifest", str(manifest), "--keys", str(keys),
            "--ledger", str(ledger), "--solver", str(solver_dir),
            "--held-out-frac", "0.7", "--timeout", "120", "--stub"])
        out = drv.run(args)

        # The whole governed chain ran and reached a verdict.
        assert out.verdict == "PROMOTED", f"{out.verdict}: {out.reason}"
        assert out.candidate_held_out > out.baseline_held_out
        assert not out.overfit and out.boundary_ok and out.promoted
        assert out.samples >= 10               # cleared the code-rung evidence floor
        assert out.n_held_out >= 10

        # The signed promotion landed in the ledger file.
        assert ledger.exists()
        from maverick.self_improvement import PromotionLedger
        recs = PromotionLedger(path=ledger).all()
        assert len(recs) == 1
        assert recs[0].rung == "code"

    def test_no_run_when_held_out_below_min_samples(self, tmp_path):
        """The min_samples refusal is knowable BEFORE any LLM/solver spend:
        16 instances at held-out-frac 0.5 -> 8 held out < min_samples 10, so
        the driver must refuse at $0 instead of spending and then being
        refused by the gate at the end."""
        pytest.importorskip("cryptography")
        drv = _load_driver()
        manifest, solver_dir = _seed_corpus(tmp_path)
        keys = _write_keys(tmp_path)
        ledger = tmp_path / "ledger.json"
        args = drv._parse_args([
            "--manifest", str(manifest), "--keys", str(keys),
            "--ledger", str(ledger), "--solver", str(solver_dir),
            "--held-out-frac", "0.5", "--min-samples", "10", "--stub"])
        out = drv.run(args)
        assert out.verdict == "NO-RUN"
        assert "min-samples" in out.reason
        assert not ledger.exists()             # nothing ran, nothing signed

    def test_abort_ceiling_refuses_before_running(self, tmp_path):
        pytest.importorskip("cryptography")
        drv = _load_driver()
        manifest, solver_dir = _seed_corpus(tmp_path)
        keys = _write_keys(tmp_path)
        # 16 instances * 2 arms * $0.10 cap = $3.20 worst-case ceiling; a $0.01
        # cap must refuse to start (and never write a ledger).
        ledger = tmp_path / "ledger.json"
        args = drv._parse_args([
            "--manifest", str(manifest), "--keys", str(keys),
            "--ledger", str(ledger), "--solver", str(solver_dir),
            "--held-out-frac", "0.7", "--abort-at-dollars", "0.01", "--stub"])
        out = drv.run(args)
        assert out.verdict == "ABORTED"
        assert not ledger.exists()
