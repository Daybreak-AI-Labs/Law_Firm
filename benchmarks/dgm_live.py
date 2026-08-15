#!/usr/bin/env python3
"""LIVE runner for the governed DGM (Darwin-Godel) solver self-improvement loop.

``benchmarks/dgm_uplift.py`` is the library; ``proof/dgm_uplift_proof.py`` drives
its whole chain on a SYNTHETIC fixture (no LLM, no network) to prove the six
guarantees. This driver is the real-world sibling: it runs the SAME governed
chain against REAL staged SWE-bench Verified instances with the REAL LLM solver
(``benchmarks/solvers/baseline_v0``) and the REAL proposer
(``dgm_uplift.llm_solver_proposer``).

The loop, end to end:

  1. Load instances from a manifest (``swebench_governed._load_manifest`` -- the
     format ``fetch_swe_bench_verified.py`` emits).
  2. Optionally pre-filter to instances THIS host can actually grade for free
     (``swebench_governed._env_pregate``); report how many were dropped.
  3. Deterministically split held-in / held-out (``dgm_uplift.split_instances``).
  4. Evaluate the baseline solver on the HELD-IN slice only, to build the
     proposer's feedback (the held-out slice stays unseen -- that is the whole
     point of the uplift metric).
  5. Author a candidate solver patch: ``llm_solver_proposer`` (paid), or a
     stub patch (``--stub``, free) so the entire driver runs end-to-end at $0.
  6. ``dgm_uplift.govern_solver_change`` -- the full governed chain (editable
     surface, held-out uplift with overfit refusal, per-emitted-patch anti-cheat,
     capability non-escalation, Ed25519-signed + reversible promotion).
  7. Print an honest scoreboard: held-in / held-out resolved-rates for both arms,
     the verdict (PROMOTED / REFUSED / OVERFIT), the ledger path, and spend.

Every stage fails closed; one bad instance never crashes the run.

    # Free smoke (stub proposer + a synthetic offline solver + tiny corpus):
    python benchmarks/dgm_live.py --manifest tiny.jsonl --keys ./keys \
        --solver ./synth_solver --held-out-frac 0.7 --stub

    # Live paid round (real LLM solver + real proposer), gradable-filtered:
    python benchmarks/dgm_live.py --manifest instances.jsonl --keys ./keys \
        --ledger ./dgm_ledger.json --gradable-only --held-out-frac 0.5
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Same sys.path wiring dgm_uplift / the proof use, so ``import dgm_uplift`` and
# ``import swebench_governed`` resolve the sibling benchmark modules and their
# ``maverick`` imports find maverick-core.
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

log = logging.getLogger(__name__)

# Mirrors the baseline_v0 solver's per-call ``max_dollars`` cap. Used only for
# the pre-flight worst-case spend CEILING (``--abort-at-dollars``); the actual
# spend is read back from the solver's per-call spend log.
_SOLVER_CALL_CAP = 0.10
_PROPOSER_EST = 0.05
_SPEND_LOG_ENV = "MAVERICK_DGM_SPEND_LOG"


class RunLockHeld(RuntimeError):
    """Raised when another live DGM run already holds the corpus lock."""


def _acquire_run_lock(lock_path: Path):
    """Take an EXCLUSIVE, non-blocking lock so two DGM runs can never share a
    corpus (the exact bug that double-spent and cross-corrupted repos: an
    orphaned run survived, a second was launched, both hit the same checkouts).

    The OS lock (``flock`` on POSIX, ``msvcrt`` byte-range lock on Windows) is
    kernel-released the instant the holding process dies, so a crashed/killed
    run leaves NO stale lock and a fresh run reclaims it automatically. Returns
    the open handle; the CALLER MUST keep it alive for the run's duration
    (closing it releases the lock). Raises :class:`RunLockHeld` if a LIVE run
    holds it (message names the holding pid)."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            # Lock a byte well beyond the tiny PID text. Locking byte zero also
            # blocks contenders from reading the holder PID on Windows; a
            # beyond-EOF byte-range lock preserves that diagnostic while still
            # refusing a second process/handle.
            fh.seek(4096)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.seek(0)
        holder = (fh.read() or "").strip() or "unknown pid"
        fh.close()
        raise RunLockHeld(
            f"another DGM run is already active (holds {lock_path}, pid {holder}); "
            "refusing to start a second run on the same corpus. Wait for it, or "
            "kill it first.") from e
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    os.fsync(fh.fileno())
    return fh


def _reset_repos_to_base(manifest_path: Path) -> tuple[int, list[str]]:
    """Reset every staged repo to its EXACT ``base_commit`` (+ clean untracked)
    and verify HEAD, BEFORE any solver runs. Guarantees a pristine start even if
    a prior killed run left a throwaway test-fixture commit checked out (which a
    naive ``reset --hard HEAD`` would then cement -- the bug that produced an
    invalid run). Returns ``(n_ok, problems)``; rows without ``base_commit``
    (older manifests) are skipped with a note so behavior is unchanged."""
    import json
    import subprocess
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": os.environ.get("PATH", "")}

    def _git(*a, cwd):
        return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True,
                              text=True, timeout=180, env=env)

    ok, problems = 0, []
    for line in Path(manifest_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        iid, rp, base = row["instance_id"], Path(row["repo_path"]), row.get("base_commit")
        if not base:
            problems.append(f"{iid}: manifest has no base_commit (skipped reset)")
            continue
        if not (rp / ".git").is_dir():
            problems.append(f"{iid}: repo missing at {rp}")
            continue
        _git("reset", "--hard", "-q", base, cwd=rp)
        _git("clean", "-fdxq", cwd=rp)
        head = _git("rev-parse", "HEAD", cwd=rp).stdout.strip()
        if head == base:
            ok += 1
        else:
            problems.append(f"{iid}: HEAD {head[:12]} != base {base[:12]} after reset")
    return ok, problems


@dataclass
class DriverOutcome:
    """Everything the scoreboard needs -- returned so tests can assert on it."""

    verdict: str = "NO-RUN"
    reason: str = ""
    n_instances: int = 0
    n_dropped: int = 0
    n_held_in: int = 0
    n_held_out: int = 0
    boundary_ok: bool = False
    overfit: bool = False
    promoted: bool = False
    approver_id: str | None = None
    baseline_held_in: float = 0.0
    candidate_held_in: float = 0.0
    baseline_held_out: float = 0.0
    candidate_held_out: float = 0.0
    samples: int = 0
    ledger_path: str = ""
    spend_dollars: float = 0.0


def _load_libs():
    """Import the sibling benchmark libraries (kept lazy to avoid E402)."""
    import dgm_uplift
    import swebench_governed
    return dgm_uplift, swebench_governed


def _filter_gradable(swg, instances: list, workroot: Path, *, timeout: float) -> tuple[list, int]:
    """Keep only instances this host can grade for free (``_env_pregate``).

    Returns (kept, dropped). Fail-closed: an instance that errors in the pregate
    is dropped, not run.

    The pregate runs under the instance's era-correct venv interpreter
    (``MAVERICK_TEST_PYTHON``), exactly like ``swebench_governed.main`` and
    ``dgm_uplift.score_instance`` do. Without this the filter grades every
    instance with HOST python -- on a venv'd pod (all instances on their own
    3.8/3.9 era) that drops the ENTIRE corpus and the run dies NO-RUN, or worse,
    keeps a wrong-era subset the later venv-aware scoring disagrees with."""
    import os

    kept: list = []
    dropped = 0
    for i, inst in enumerate(instances):
        vpy = swg._venv_python(inst)
        prev_tp = os.environ.get("MAVERICK_TEST_PYTHON")
        if vpy:
            os.environ["MAVERICK_TEST_PYTHON"] = vpy
        try:
            why = swg._env_pregate(inst, workroot / f"pre{i}", timeout=timeout)
        except Exception as e:  # noqa: BLE001 -- pregate must never crash the run
            why = f"pregate error: {e}"
        finally:
            if vpy:
                if prev_tp is None:
                    os.environ.pop("MAVERICK_TEST_PYTHON", None)
                else:
                    os.environ["MAVERICK_TEST_PYTHON"] = prev_tp
        if why:
            dropped += 1
            print(f"  [DROP ]  {inst.instance_id:40}  {why}")
        else:
            kept.append(inst)
    return kept, dropped


def _baseline_feedback(dgm, solver_dir: Path, held_in: list, workroot: Path,
                       *, timeout: float, score_fn=None, checkpoint=None) -> str:
    """Run the baseline solver on the HELD-IN slice and describe what it failed.

    Only held-in instances are ever shown to the proposer, so the held-out slice
    the gate promotes on stays genuinely unseen. Shares the baseline-arm
    checkpoint so these held-in verdicts are REUSED when the baseline arm scores
    the full corpus (no duplicate agent spend on the held-in slice)."""
    resolved = dgm.run_solver(solver_dir, held_in, workroot, timeout=timeout,
                              score_fn=score_fn, checkpoint=checkpoint)
    failed = [i.instance_id for i in held_in if not resolved.get(i.instance_id)]
    if not failed:
        return "The baseline solver already resolves every seen instance."
    lines = "\n".join(f"- {iid}" for iid in failed)
    return ("Seen (held-in) instances the current solver FAILED to resolve "
            "(its FAIL_TO_PASS tests still fail):\n" + lines +
            "\n\nGeneralise the solver so it resolves more of these -- never "
            "special-case an instance id.")


def _stub_patch(solver_dir: Path, stub_patch: str | None) -> str:
    """The known-good improvement patch for --stub mode (no LLM).

    Resolution: ``--stub-patch PATH`` if given, else ``<solver>/stub_improve.patch``.
    Lets a smoke run drive the ENTIRE chain for $0 with a hand-written diff a real
    proposer would have emitted (exactly what the proof does)."""
    candidates: list[Path] = []
    if stub_patch:
        candidates.append(Path(stub_patch))
    candidates.append(Path(solver_dir) / "stub_improve.patch")
    for c in candidates:
        if c.is_file():
            return c.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "--stub needs a known-good patch: pass --stub-patch PATH or place "
        f"stub_improve.patch in {solver_dir}")


def _spend_from_log(path: Path) -> float:
    """Sum the solver's per-call actual $ costs (best-effort; 0.0 if absent)."""
    total = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                total += float(line)
    except (OSError, ValueError):
        pass
    return total


def _author_patch(args, dgm, solver_dir: Path, held_in: list, workroot: Path,
                  score_fn=None, checkpoint=None) -> str:
    """Produce the candidate solver patch (stub file, or the live LLM proposer)."""
    if args.stub:
        return _stub_patch(solver_dir, args.stub_patch)
    feedback = _baseline_feedback(dgm, solver_dir, held_in, workroot / "feedback",
                                  timeout=args.timeout, score_fn=score_fn,
                                  checkpoint=checkpoint)
    return dgm.llm_solver_proposer(solver_dir, feedback)


def _abort_ceiling(args, n_instances: int, n_held_in: int) -> float:
    """Worst-case $ ceiling for the run (every solver call hits its cap)."""
    n_calls = n_instances * 2 + (0 if args.stub else n_held_in)
    return n_calls * _SOLVER_CALL_CAP + (0.0 if args.stub else _PROPOSER_EST)


def _run_governed(args, dgm, solver_dir: Path, patch: str, instances: list,
                  work: Path, score_fn=None, checkpoint_dir=None):
    """Build a min_samples-tuned controller and drive the governed chain."""
    from maverick.self_improvement import (
        _RUNG_POLICY,
        PromotionLedger,
        SelfImprovementController,
    )
    policy = {k: dict(v) for k, v in _RUNG_POLICY.items()}
    policy["code"]["min_samples"] = args.min_samples  # --min-samples -> the gate
    ledger = PromotionLedger(path=args.ledger)
    ctrl = SelfImprovementController(ledger=ledger, rung_policy=policy)
    return dgm.govern_solver_change(
        solver_dir, patch, instances, keys_dir=args.keys, ledger=ledger,
        workroot=work / "gov", held_out_frac=args.held_out_frac,
        timeout=args.timeout, controller=ctrl, score_fn=score_fn,
        checkpoint_dir=checkpoint_dir)


def _build_score_fn(args, dgm):
    """The grading hook for this run: official-container grading when
    --container-grade, else None (the host score_instance default)."""
    if not getattr(args, "container_grade", False):
        return None
    import swebench_container_grade as CG
    runner = (CG.modal_runner() if args.container_backend == "modal"
              else CG.docker_runner())
    return dgm.container_score_fn(runner, namespace=args.namespace)


def _verdict(res) -> tuple[str, str]:
    if res.promoted:
        return "PROMOTED", res.reason
    if res.overfit:
        return "OVERFIT", res.reason
    if not res.boundary_ok:
        return "REFUSED (boundary)", res.reason
    return "REFUSED", res.reason


def run(args, *, workdir: Path | None = None) -> DriverOutcome:
    """Execute the whole driver flow. Returns a DriverOutcome (never raises for a
    per-instance problem; a hard config error like a bad manifest still raises).

    Holds an exclusive corpus lock for its whole duration so a second run can
    never start on the same corpus, and resets every repo to its base_commit
    before scoring so a prior killed run can't leave an invalid checkout."""
    dgm, swg = _load_libs()
    solver_dir = Path(args.solver)
    out = DriverOutcome(ledger_path=str(args.ledger))

    # Exclusive single-run lock (next to the ledger). Refuses to start if a live
    # run holds it -- prevents the concurrent double-run that double-spent.
    ckpt_dir_early = getattr(args, "checkpoint_dir", None) or args.ledger.resolve().parent / "checkpoints"
    Path(ckpt_dir_early).mkdir(parents=True, exist_ok=True)
    try:
        _lock_fh = _acquire_run_lock(Path(ckpt_dir_early) / "run.lock")
    except RunLockHeld as e:
        out.verdict, out.reason = "LOCKED", str(e)
        return out

    score_fn = _build_score_fn(args, dgm)
    try:
        # Pristine repos before any spend: reset to base_commit + verify.
        n_ok, problems = _reset_repos_to_base(args.manifest)
        if problems:
            for p in problems:
                print(f"  [reset] {p}")
        print(f"  [reset] {n_ok} repo(s) verified at base_commit")
        return _run_locked(args, dgm, swg, solver_dir, out, score_fn, workdir=workdir)
    finally:
        _lock_fh.close()  # releases the corpus lock


def _run_locked(args, dgm, swg, solver_dir, out, score_fn, *, workdir=None) -> DriverOutcome:
    """The run body, executed while holding the corpus lock (see :func:`run`)."""
    with _tmp(workdir) as td:
        work = Path(td)
        instances = swg._load_manifest(args.manifest)
        if args.gradable_only and score_fn is not None:
            # The host env pre-gate answers "can THIS HOST grade it" -- the wrong
            # question under container grading (every Verified instance grades in
            # its own image). Running it would drop the whole corpus for nothing.
            print("  [note ]  --gradable-only ignored: --container-grade grades "
                  "in official images, host env is irrelevant")
        elif args.gradable_only:
            instances, out.n_dropped = _filter_gradable(
                swg, instances, work / "pregate", timeout=args.timeout)
        if args.limit:
            instances = instances[: args.limit]
        out.n_instances = len(instances)
        if len(instances) < 2:
            out.verdict, out.reason = "NO-RUN", (
                f"need >= 2 gradable instances, have {len(instances)}")
            return out

        held_in, held_out = dgm.split_instances(instances, held_out_frac=args.held_out_frac)
        out.n_held_in, out.n_held_out = len(held_in), len(held_out)

        # The gate refuses on min_samples at the END -- after the proposer and
        # both solver arms have already spent. If the held-out slice can't meet
        # the evidence floor, that refusal is knowable NOW for $0: don't start.
        if len(held_out) < args.min_samples:
            out.verdict, out.reason = "NO-RUN", (
                f"held-out slice ({len(held_out)}) < --min-samples "
                f"({args.min_samples}); the gate would refuse AFTER spending -- "
                "grow the gradable corpus or lower --min-samples")
            return out

        if args.abort_at_dollars:
            ceiling = _abort_ceiling(args, len(instances), len(held_in))
            if ceiling > args.abort_at_dollars:
                out.verdict, out.reason = "ABORTED", (
                    f"worst-case spend ceiling ${ceiling:.2f} exceeds "
                    f"--abort-at-dollars ${args.abort_at_dollars:.2f}; not started")
                return out

        # Checkpoints persist NEXT TO THE LEDGER (not in the temp workdir, which
        # is wiped on restart) so a killed cycle resumes instead of re-spending.
        ckpt_dir = getattr(args, "checkpoint_dir", None)
        if ckpt_dir is None:
            ckpt_dir = args.ledger.resolve().parent / "checkpoints"
        Path(ckpt_dir).mkdir(parents=True, exist_ok=True)
        base_ckpt = Path(ckpt_dir) / "baseline_arm.jsonl"

        spend_log = work / "spend.log"
        prev = os.environ.get(_SPEND_LOG_ENV)
        os.environ[_SPEND_LOG_ENV] = str(spend_log)
        try:
            # Persist the authored candidate patch: the proposer is
            # non-deterministic, so on RESUME we must reuse the EXACT patch the
            # candidate-arm checkpoint belongs to (re-authoring could yield a
            # different solver, making the resumed verdicts inconsistent).
            patch_file = Path(ckpt_dir) / "candidate_patch.diff"
            if patch_file.exists() and patch_file.read_text().strip() and not args.stub:
                patch = patch_file.read_text()
                print(f"  [resume] reusing persisted candidate patch ({len(patch)} bytes)")
            else:
                patch = _author_patch(args, dgm, solver_dir, held_in, work,
                                      score_fn=score_fn, checkpoint=base_ckpt)
                if (patch or "").strip():
                    patch_file.write_text(patch)
            res = _run_governed(args, dgm, solver_dir, patch, instances, work,
                                score_fn=score_fn, checkpoint_dir=ckpt_dir)
        finally:
            if prev is None:
                os.environ.pop(_SPEND_LOG_ENV, None)
            else:
                os.environ[_SPEND_LOG_ENV] = prev
        out.spend_dollars = _spend_from_log(spend_log)

    out.boundary_ok = res.boundary_ok
    out.overfit = res.overfit
    out.promoted = res.promoted
    out.approver_id = res.approver_id
    out.baseline_held_in, out.candidate_held_in = res.baseline_held_in, res.candidate_held_in
    out.baseline_held_out, out.candidate_held_out = res.baseline_held_out, res.candidate_held_out
    out.samples = res.samples
    out.verdict, out.reason = _verdict(res)
    return out


def _scoreboard(out: DriverOutcome) -> None:
    print("=" * 78)
    print("  MAVERICK -- GOVERNED DGM LIVE   (solver improves itself, under the gate)")
    print("=" * 78)
    if out.n_dropped:
        print(f"  gradable-only pre-filter: dropped {out.n_dropped} ungradable instance(s)")
    print(f"  corpus: {out.n_instances} instances  "
          f"(held-in {out.n_held_in} / held-out {out.n_held_out})")
    print(f"  held-in  resolved-rate:  baseline {out.baseline_held_in:.3f}"
          f"  ->  candidate {out.candidate_held_in:.3f}")
    print(f"  held-out resolved-rate:  baseline {out.baseline_held_out:.3f}"
          f"  ->  candidate {out.candidate_held_out:.3f}"
          f"   (the gate promotes on THIS, over {out.samples} unseen instances)")
    print("-" * 78)
    print(f"  VERDICT: {out.verdict}")
    print(f"  {out.reason}")
    if out.approver_id:
        print(f"  signed by: {out.approver_id}")
    print(f"  ledger: {out.ledger_path}")
    print(f"  solver LLM spend (actual, best-effort): ~${out.spend_dollars:.4f}")
    print("=" * 78)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path,
                    help="JSONL manifest (fetch_swe_bench_verified format)")
    ap.add_argument("--keys", required=True, type=Path,
                    help="operator key dir (operator.priv.hex + operator.pub)")
    ap.add_argument("--ledger", type=Path, default=Path("dgm_uplift_ledger.json"))
    ap.add_argument("--solver", type=Path,
                    default=_ROOT / "benchmarks" / "solvers" / "baseline_v0",
                    help="solver dir under improvement (contains solver.py)")
    ap.add_argument("--held-out-frac", type=float, default=0.5,
                    help="fraction of instances held out (the uplift is judged here)")
    ap.add_argument("--min-samples", type=int, default=10,
                    help="held-out evidence floor passed to the code rung "
                         "(gate default is 10)")
    ap.add_argument("--gradable-only", action="store_true",
                    help="pre-filter to instances this host can grade for free "
                         "(_env_pregate); prints how many were dropped")
    ap.add_argument("--container-grade", action="store_true",
                    help="grade every emitted patch in the instance's OFFICIAL "
                         "SWE-bench container (dgm_uplift.container_score_fn) "
                         "instead of on this host -- the honest mode for real "
                         "Verified corpora. Implies the corpus must be Verified "
                         "instance ids; makes --gradable-only a no-op (the host "
                         "env is irrelevant when grading in containers).")
    ap.add_argument("--container-backend", choices=("modal", "docker"),
                    default="modal", help="container backend for --container-grade")
    ap.add_argument("--namespace", default=None,
                    help="image namespace for --container-grade "
                         "(default: the official 'swebench' Docker Hub namespace)")
    ap.add_argument("--limit", type=int, default=0, help="cap the corpus size (0 = all)")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-arm test-run timeout in seconds")
    ap.add_argument("--abort-at-dollars", type=float, default=0.0,
                    help="refuse to start if the worst-case spend ceiling exceeds "
                         "this (best-effort; 0 = no cap)")
    ap.add_argument("--stub", action="store_true",
                    help="NO-LLM mode: author the candidate from a known-good patch "
                         "file instead of the paid proposer (free end-to-end run)")
    ap.add_argument("--stub-patch", type=Path, default=None,
                    help="explicit patch file for --stub (else <solver>/stub_improve.patch)")
    ap.add_argument("--checkpoint-dir", type=Path, default=None,
                    help="persist per-instance verdicts + the authored patch here "
                         "so a killed cycle RESUMES instead of re-spending "
                         "(default: <ledger-dir>/checkpoints). Delete it to force "
                         "a clean run.")
    return ap.parse_args(argv)


def _tmp(workdir: Path | None):
    """A tempdir context: caller-supplied (kept) or auto (cleaned up)."""
    import contextlib
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)
        return contextlib.nullcontext(str(workdir))
    return tempfile.TemporaryDirectory(prefix="dgm-live-")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = _parse_args(argv)
    out = run(args)
    _scoreboard(out)
    return 0 if out.verdict in ("PROMOTED", "REFUSED", "OVERFIT", "REFUSED (boundary)") else 1


if __name__ == "__main__":
    sys.exit(main())
