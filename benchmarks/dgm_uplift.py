#!/usr/bin/env python3
"""Governed DGM uplift: the agent improves ITS OWN solver, provably, under the gate.

``swebench_governed.py`` measures how many real bugs the solver fixes under
governance. This module closes the Darwin-Gödel loop: the artifact being
modified is the *solver itself* -- the code that decides how bugs get fixed --
and the fitness signal is the solver's resolved-rate on a HELD-OUT slice of
instances it has never seen. A solver change is promoted only when the whole
chain passes:

  1. **Editable-surface boundary** -- the solver patch may touch only the
     declared solver files; a patch that reaches for the control plane
     (the gate, the verifier, the signer...) is refused structurally.
  2. **Held-out uplift** -- baseline solver vs candidate solver, each run on
     BOTH splits of the instance corpus, on isolated copies. Promotion is
     judged on the held-OUT resolved-rate; a candidate that gains only on the
     held-in (seen) instances is flagged OVERFIT and refused -- a solver that
     memorised its training tasks did not get smarter.
  3. **Cheat-propagation guard** -- every instance patch the solver emits is
     itself anti-cheat-validated (``defensive_validate``): a "better" solver
     that learned to edit the graders' tests scores ZERO, so gaming can't
     launder through the uplift metric.
  4. **Capability non-escalation** -- the solver patch is proven to grant no
     new authority (process/network/dynamic-code classes).
  5. **Human-signed, reversible promotion** -- Ed25519 operator signature bound
     to the exact patch; the previous solver version is archived for one-step
     rollback; the promotion lands in the append-only ledger.

The result is the auditable uplift curve: "solver v0 resolved X% held-out;
the governed loop produced v1 resolving X+Δ% -- every step signed, reversible,
and refused when it was memorisation or test-tampering."

A solver is a directory containing ``solver.py`` exposing
``solve(instance) -> str`` (a unified diff for the instance's repo, or ``""``
to pass). Instances are :class:`swebench_governed.Instance` rows.

No LLM and no Docker are needed to *evaluate* a solver change (real pytest on
isolated copies); the LLM is only needed to *author* solver patches
(:func:`llm_solver_proposer`, provider key required) -- or a human/oracle can
supply one (``--patch``).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shlex
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "packages" / "maverick-core"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

log = logging.getLogger(__name__)


# --- running a solver version over instances -----------------------------------

def load_solver(solver_dir: Path) -> Path:
    """Validate that ``solver.py`` exists and return its path.

    Solver code may be LLM-authored and is therefore untrusted. Do not import it
    in the controller process: importing executes top-level Python before the
    governance/capability gates can refuse the change. ``run_solver`` executes
    the solver in the eval sandbox instead.
    """
    import ast

    path = Path(solver_dir) / "solver.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    has_solve = any(isinstance(n, ast.FunctionDef) and n.name == "solve" for n in tree.body)
    if not has_solve:
        raise TypeError(f"{path} does not define solve(instance) -> str")
    return path


def _instance_payload(inst, repo_path: Path) -> dict:
    """JSON-safe instance view passed to untrusted solver subprocesses."""
    payload: dict = {}
    # Carry the instance's own JSON-safe scalar fields first: light corpora
    # (e.g. the fast-proof arithmetic tasks) define fields like spec/gold that
    # the canonical SWE-bench keys below don't cover, and the sandboxed solver
    # only sees this payload.
    for key, val in (getattr(inst, "__dict__", {}) or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(val, (str, int, float, bool)) or val is None:
            payload[key] = val
        elif isinstance(val, (list, tuple)) and all(
                isinstance(x, (str, int, float, bool)) for x in val):
            payload[key] = list(val)
    payload.update({
        "instance_id": inst.instance_id,
        "repo_path": str(repo_path),
        "fail_to_pass": list(getattr(inst, "fail_to_pass", []) or []),
        "pass_to_pass": list(getattr(inst, "pass_to_pass", []) or []),
        "gold_patch": getattr(inst, "gold_patch", ""),
        "brief": getattr(inst, "brief", ""),
        "language": getattr(inst, "language", "python"),
        "total_tests": getattr(inst, "total_tests", 0),
    })
    return payload


_SOLVER_RUNNER = r'''
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

solver_path = Path(sys.argv[1])
payload_path = Path(sys.argv[2])
payload = json.loads(payload_path.read_text(encoding="utf-8"))
inst = SimpleNamespace(**payload)
name = f"dgm_solver_{uuid.uuid4().hex}"
spec = importlib.util.spec_from_file_location(name, solver_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
solve = getattr(mod, "solve", None)
if not callable(solve):
    raise TypeError(f"{solver_path} does not define solve(instance) -> str")
patch = solve(inst) or ""
if not isinstance(patch, str):
    raise TypeError("solve(instance) must return str")
sys.stdout.write(patch)
'''


def _windows_shell_quote_arg(value: str) -> str:
    """Quote one argv element for LocalBackend's Windows ``cmd.exe``.

    ``SandboxV2.exec`` deliberately accepts a command string, so a local
    Windows sandbox ultimately parses it with ``cmd.exe``.  POSIX single
    quotes are ordinary filename characters there, while an unquoted path can
    turn ``&`` or ``|`` into a second command.  Keep every element inside
    double quotes, fail closed on cmd expansion/control characters, and double
    trailing backslashes so Windows argv parsing cannot consume the closing
    quote.

    Percent and delayed-expansion bangs remain active even inside double
    quotes under cmd, so rejecting them is safer than attempting context-
    dependent escaping.  Windows filenames cannot contain a double quote or
    NUL/newline, making those refusals lossless for this path-only command.
    """
    value = str(value)
    if any(char in value for char in ('\0', '\r', '\n', '"', '%', '!')):
        raise ValueError("unsafe character in Windows sandbox argument")
    trailing = len(value) - len(value.rstrip("\\"))
    if trailing:
        value = value[:-trailing] + ("\\" * (trailing * 2))
    return f'"{value}"'


def _sandbox_shell_quote_arg(value: str, sb) -> str:
    """Quote for the shell used by ``sb``, not merely the controller OS."""
    if os.name == "nt" and getattr(sb, "host_visible_fs", False):
        return _windows_shell_quote_arg(value)
    # Remote/container backends remain POSIX even when their controller runs
    # on Windows.  shlex.quote is the correct renderer for those shells.
    return shlex.quote(str(value))


def _solve_in_sandbox(solver_path: Path, inst, workroot: Path, sb, *,
                      timeout: float = 600.0) -> str:
    """Run one untrusted solver invocation through the eval sandbox.

    The solver sees a throwaway copy of the instance repo, not ``inst.repo_path``,
    so side effects performed while deciding what patch to emit cannot tamper
    with the source tree later materialized by ``score_instance``.
    """
    from maverick.self_modify_eval import _default_materialize

    work = Path(workroot)
    work.mkdir(parents=True, exist_ok=True)
    repo_copy = work / "solver-visible-repo"
    # Light corpora (spec/gold tasks with no source tree) have no repo_path;
    # the solver still runs sandboxed, against an empty throwaway dir.
    src_repo = getattr(inst, "repo_path", None)
    if src_repo:
        _default_materialize(src_repo, repo_copy)
    else:
        repo_copy.mkdir(parents=True, exist_ok=True)
    runner = work / "run_solver_once.py"
    payload = work / "instance.json"
    runner.write_text(_SOLVER_RUNNER, encoding="utf-8")
    payload.write_text(json.dumps(_instance_payload(inst, repo_copy)), encoding="utf-8")
    argv = (sys.executable, "-I", str(runner), str(solver_path), str(payload))
    cmd = " ".join(_sandbox_shell_quote_arg(arg, sb) for arg in argv)
    res = sb.exec(cmd, timeout=timeout)
    if not getattr(res, "ok", False):
        raise RuntimeError((getattr(res, "stderr", "") or "solver failed").strip()[:500])
    return getattr(res, "stdout", "") or ""


def _instance_test_python(inst) -> str:
    """Absolute path to this instance's era-correct venv interpreter, or ""
    when MAVERICK_SWEBENCH_VENVS is unset / no venv exists. Mirrors
    ``swebench_governed._venv_python`` so DGM grades with the SAME interpreter
    the governed harness uses (each instance's own-era python, not host python3);
    inlined rather than imported to avoid a heavier dep cycle."""
    import os

    base = os.environ.get("MAVERICK_SWEBENCH_VENVS", "").strip()
    if not base:
        return ""
    iid = getattr(inst, "instance_id", "")
    p = Path(base) / iid / "bin" / "python"
    return str(p) if p.exists() else ""


def score_instance(inst, patch: str, workroot: Path, *, timeout: float = 600.0) -> bool:
    """Did this patch resolve this instance, honestly?

    Anti-cheat first (a solver that edits tests/setup gets no credit), then
    apply to an isolated copy and run the instance's own FAIL_TO_PASS +
    PASS_TO_PASS suites. Fail-closed on any error.

    Grading runs under this instance's era-correct venv interpreter when one is
    configured (MAVERICK_SWEBENCH_VENVS): ``MAVERICK_TEST_PYTHON`` is pointed at
    it for the duration of grading and restored afterward, so the DGM eval loop
    honours the SAME per-instance interpreter the governed SWE-bench harness
    does. No venv configured -> vpy == "" -> the env is left untouched."""
    import os

    from maverick.coding_mode import defensive_validate, run_failing_tests
    from maverick.self_modify_eval import _default_materialize, git_apply, resolve_eval_sandbox

    if not (patch or "").strip():
        return False
    dv = defensive_validate(patch, fail_to_pass=inst.fail_to_pass,
                            pass_to_pass=inst.pass_to_pass, gold_patch=inst.gold_patch)
    if not dv.ok:
        return False
    work = Path(workroot)
    prev_tp = os.environ.get("MAVERICK_TEST_PYTHON")
    vpy = _instance_test_python(inst)
    if vpy:
        os.environ["MAVERICK_TEST_PYTHON"] = vpy
    try:
        # SWE-bench grading semantics: the grader's test_patch goes on first
        # (the graded tests usually don't exist at base_commit).
        def _prep(dst: Path):
            _default_materialize(inst.repo_path, dst)
            if (getattr(inst, "test_patch", "") or "").strip():
                tp = git_apply(inst.test_patch, dst)
                if not getattr(tp, "ok", False):
                    return None
            return resolve_eval_sandbox(None, dst)

        # Baseline honesty guard on its OWN isolated copy: the graded FAIL_TO_PASS
        # must FAIL before any candidate is applied. If they already pass at
        # baseline the instance is mis-seeded (no bug to fix) and ANY applying
        # patch would score all_pass -- inflating the DGM resolved-rate. The
        # baseline runs on a SEPARATE tree from the candidate so its compiled
        # bytecode (__pycache__ of the unpatched source) can't shadow the patched
        # code when the candidate is graded.
        base_tree = work / "base"
        sb_base = _prep(base_tree)
        if sb_base is None:
            return False
        base = run_failing_tests(base_tree, inst.fail_to_pass, [],
                                 sb_base, timeout=timeout, language=inst.language)
        if base.all_pass:
            return False
        # Candidate on a fresh copy.
        cand_tree = work / "cand"
        sb_cand = _prep(cand_tree)
        if sb_cand is None:
            return False
        applied = git_apply(patch, cand_tree)
        if not applied.ok:
            return False
        res = run_failing_tests(cand_tree, inst.fail_to_pass, inst.pass_to_pass,
                                sb_cand, timeout=timeout, language=inst.language)
        return bool(res.all_pass)
    except Exception:
        log.warning("score_instance failed for %s", inst.instance_id, exc_info=True)
        return False
    finally:
        if vpy:
            if prev_tp is None:
                os.environ.pop("MAVERICK_TEST_PYTHON", None)
            else:
                os.environ["MAVERICK_TEST_PYTHON"] = prev_tp


def _load_checkpoint(path) -> dict[str, bool]:
    """Read a resolved-map from a JSONL checkpoint ({instance_id, resolved} per
    line); ``{}`` if absent/unreadable. Later lines win (a re-grade overrides)."""
    done: dict[str, bool] = {}
    try:
        if path and Path(path).exists():
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if line:
                    r = json.loads(line)
                    done[r["instance_id"]] = bool(r["resolved"])
    except (OSError, ValueError, KeyError):
        log.warning("could not read checkpoint %s (starting fresh)", path, exc_info=True)
    return done


def run_solver(solver_dir: Path, instances: list, workroot: Path,
               *, timeout: float = 600.0, score_fn=None, checkpoint=None
               ) -> dict[str, bool]:
    """Resolved-map for one solver version over a set of instances.

    ``score_fn(inst, patch, workroot, *, timeout) -> bool`` is the grading hook;
    default is the host-path :func:`score_instance`. Inject
    :func:`container_score_fn` to grade every emitted patch inside the
    instance's OFFICIAL per-instance container -- the honest mode for real
    SWE-bench corpora, where one host cannot reproduce every instance's era.

    ``checkpoint`` is a JSONL path (OUTSIDE the auto-cleaned workroot) that
    persists each instance's verdict as it lands. On restart the run RESUMES:
    already-graded instances are skipped, so a 3-hour agent-solver cycle
    survives a container restart instead of losing everything. Instances
    already present are not re-solved (no wasted agent spend).

    The solver itself is LLM-authored and untrusted, so it is never imported in
    this controller process: :func:`_solve_in_sandbox` executes it in the eval
    sandbox against a throwaway repo copy before any patch is graded."""
    from maverick.self_modify_eval import resolve_eval_sandbox

    solver_path = load_solver(solver_dir)
    scorer = score_fn or score_instance
    done = _load_checkpoint(checkpoint)
    workroot = Path(workroot)
    sb = resolve_eval_sandbox(None, workroot)
    out: dict[str, bool] = {}
    for i, inst in enumerate(instances):
        iid = inst.instance_id
        if iid in done:
            out[iid] = done[iid]
            log.info("resume: %s already graded (%s)", iid, done[iid])
            continue
        inst_work = workroot / f"i{i}"
        try:
            patch = _solve_in_sandbox(solver_path, inst, inst_work / "solve", sb, timeout=timeout)
        except Exception:
            log.warning("solver crashed on %s", iid, exc_info=True)
            patch = ""
        res = scorer(inst, patch, inst_work / "score", timeout=timeout)
        out[iid] = res
        if checkpoint:
            try:
                Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)
                with open(checkpoint, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"instance_id": iid, "resolved": bool(res)}) + "\n")
            except OSError:
                log.warning("could not append to checkpoint %s", checkpoint, exc_info=True)
    return out


def container_score_fn(run_in_image, *, namespace: str | None = None,
                       timeout: float = 1800.0, verified: dict | None = None):
    """A ``score_fn`` that grades each emitted patch inside the instance's
    OFFICIAL SWE-bench container -- the structural fix for the era-mismatch bug
    class in the DGM's uplift measurement. Without this, held-out "uplift" is
    measured in whatever environment the host happens to have, and the number
    is noise (the exact failure that burned money on the pod runs).

    Per emitted patch: $0 host anti-cheat boundary (gold-overlap ARMED -- a
    solver output is never gold by definition) -> baseline mis-seed guard in
    the image (cached per instance: the baseline verdict is a property of the
    instance, shared across both solver arms and both splits -- saves ~half the
    container runs) -> candidate graded by the official grader.

    ``verified`` maps instance_id -> raw SWE-bench Verified row; None lazy-loads
    the dataset once on first use. An instance that is NOT in Verified raises --
    a synthetic/staged corpus must keep the host ``score_instance`` path, and
    silently scoring 0 would fake a flat baseline instead of failing the run.
    The larger of the caller's per-arm ``timeout`` and this factory's floor is
    used: official eval scripts pip-install inside the image, which can dwarf a
    host-tuned test timeout."""
    import swebench_container_grade as CG

    ns = namespace or CG.DEFAULT_IMAGE_NAMESPACE
    floor = float(timeout)
    state = {"verified": verified}
    baseline_fails: dict[str, bool] = {}   # instance_id -> targets fail w/o patch

    def _raw(instance_id: str) -> dict:
        if state["verified"] is None:
            from swebench_container_run import load_verified
            state["verified"] = load_verified()
        raw = state["verified"].get(instance_id)
        if raw is None:
            raise RuntimeError(
                f"{instance_id} is not in SWE-bench Verified -- no official "
                "image exists for it. Use the host score_instance path for a "
                "synthetic/staged corpus, or inject `verified`.")
        return raw

    def score(inst, patch: str, workroot, *, timeout: float = floor) -> bool:
        raw = _raw(inst.instance_id)
        if not (patch or "").strip():
            return False
        ok, reason = CG.host_boundary(raw, patch)          # $0, before any container
        if not ok:
            log.info("boundary refused %s: %s", inst.instance_id, reason)
            return False
        eff = max(float(timeout or 0.0), floor)
        iid = inst.instance_id
        if iid not in baseline_fails:
            base = CG.grade_in_container(raw, "", run_in_image,
                                         timeout=eff, namespace=ns)
            baseline_fails[iid] = (not base.error) and bool(base.fail_to_pass_failed)
            if not baseline_fails[iid]:
                log.warning("mis-seeded/bad-image %s: FAIL_TO_PASS do not fail "
                            "at baseline (error=%r)", iid, base.error)
        if not baseline_fails[iid]:
            return False
        r = CG.grade_in_container(raw, patch, run_in_image, timeout=eff, namespace=ns)
        return bool(r.resolved and r.candidate_apply_ok and not r.error)

    return score


def split_instances(instances: list, *, held_out_frac: float = 0.34) -> tuple[list, list]:
    """Deterministic held-in/held-out split of instances by id (stable
    content-hash ordering via ``self_harness_eval.corpus_split``)."""
    from maverick.self_harness_eval import corpus_split
    by_id = {i.instance_id: i for i in instances}
    in_ids, out_ids = corpus_split([{"goal": i.instance_id} for i in instances],
                                   held_out_frac=held_out_frac)
    return [by_id[i] for i in in_ids], [by_id[i] for i in out_ids]


# --- the governed solver-change decision ----------------------------------------

@dataclass
class UpliftResult:
    """Verdict on one proposed solver self-modification."""

    boundary_ok: bool = False
    baseline_held_out: float = 0.0     # what the gate promotes on
    candidate_held_out: float = 0.0
    baseline_held_in: float = 0.0
    candidate_held_in: float = 0.0
    samples: int = 0                   # held-out evidence count (test executions)
    overfit: bool = False
    capability_widens: bool | None = None
    promoted: bool = False
    approver_id: str | None = None
    reason: str = ""
    resolved_map: dict = field(default_factory=dict)

    @property
    def uplift(self) -> float:
        return self.candidate_held_out - self.baseline_held_out


def _rate(resolved: dict[str, bool], instances: list) -> float:
    if not instances:
        return 0.0
    return sum(1 for i in instances if resolved.get(i.instance_id)) / len(instances)


def govern_solver_change(
    solver_dir: Path,
    solver_patch: str,
    instances: list,
    *,
    keys_dir: Path,
    ledger=None,
    workroot: Path,
    surface_globs: tuple[str, ...] = ("solver.py",),
    held_out_frac: float = 0.34,
    timeout: float = 600.0,
    controller=None,
    change_id: str | None = None,
    score_fn=None,
    checkpoint_dir=None,
) -> UpliftResult:
    """Decide whether a solver self-modification may be promoted. Never applies
    the patch to the live solver -- the caller does that (with
    :func:`apply_and_archive`) only when ``result.promoted``."""
    import hashlib

    from maverick import approval_signing as asig
    from maverick import self_modify as sm
    from maverick.self_improvement import Candidate, SelfImprovementController
    from maverick.self_modify_capability import capability_delta
    from maverick.self_modify_eval import _default_materialize, git_apply

    out = UpliftResult()
    workroot = Path(workroot)

    # 1. Boundary: only the declared solver surface is editable; the protected
    #    control-plane list applies regardless of the globs.
    review = sm.review_patch(solver_patch, sm.EditableSurface(editable_globs=surface_globs))
    out.boundary_ok = review.ok
    if not review.ok:
        out.reason = f"boundary refused: {review.reason}"
        return out

    # 2. Materialize baseline + candidate SOLVERS; apply the patch to candidate.
    base_dir, cand_dir = workroot / "solver_base", workroot / "solver_cand"
    try:
        _default_materialize(solver_dir, base_dir)
        _default_materialize(solver_dir, cand_dir)
    except Exception as e:
        out.reason = f"could not isolate solver: {e}"
        return out
    applied = git_apply(solver_patch, cand_dir)
    if not applied.ok:
        out.reason = f"solver patch did not apply: {applied.reason}"
        return out

    # 3. Capability non-escalation proof over the solver patch BEFORE the
    #    candidate solver is ever executed. A widening patch must not get a
    #    chance to run arbitrary top-level/solve code and cause side effects
    #    before the gate refuses it.
    cb = ca = None
    probe: tuple[str, ...] = ()
    try:
        baseline_files = {"solver.py": (base_dir / "solver.py").read_text(encoding="utf-8")}
        cb, ca, probe = capability_delta(solver_patch, baseline_files=baseline_files)
        if cb is not None and ca is not None and probe:
            out.capability_widens = any(ca.permits(t) and not cb.permits(t) for t in probe)
        if out.capability_widens:
            out.reason = "capability widening refused before solver execution"
            return out
    except Exception:
        log.debug("capability_delta failed", exc_info=True)

    # 4. Run BOTH solver versions on BOTH instance splits (isolated copies,
    #    real tests, per-emitted-patch anti-cheat inside score_instance).
    held_in, held_out = split_instances(instances, held_out_frac=held_out_frac)
    if not held_out:
        out.reason = "no held-out instances (need >= 2 instances)"
        return out
    # Checkpoints live OUTSIDE workroot (which is a temp dir) so a restart can
    # resume. Distinct files per arm: the baseline and candidate solvers are
    # different, so their verdicts must never share a key space.
    ckpt_base = ckpt_cand = None
    if checkpoint_dir is not None:
        Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)
        ckpt_base = Path(checkpoint_dir) / "baseline_arm.jsonl"
        ckpt_cand = Path(checkpoint_dir) / "candidate_arm.jsonl"
    r_base = run_solver(base_dir, instances, workroot / "eval_base",
                        timeout=timeout, score_fn=score_fn, checkpoint=ckpt_base)
    r_cand = run_solver(cand_dir, instances, workroot / "eval_cand",
                        timeout=timeout, score_fn=score_fn, checkpoint=ckpt_cand)
    out.resolved_map = {"baseline": r_base, "candidate": r_cand}
    out.baseline_held_in, out.candidate_held_in = _rate(r_base, held_in), _rate(r_cand, held_in)
    out.baseline_held_out, out.candidate_held_out = _rate(r_base, held_out), _rate(r_cand, held_out)
    # Evidence count must be in the SAME unit as the fitness metric: the metric
    # is a per-INSTANCE resolved-rate (each held-out instance is one independent
    # trial), so samples = number of held-out instances, not the sum of their
    # test ids. Counting test ids inflates the evidence n and lets a tiny
    # instance corpus clear the code-rung min_samples on a handful of trials.
    out.samples = len(held_out)

    # 5. Overfit guard: gained on seen instances but not unseen -> memorisation.
    out.overfit = (out.candidate_held_in > out.baseline_held_in
                   and out.candidate_held_out <= out.baseline_held_out)
    if out.overfit:
        out.reason = (f"OVERFIT: held-in {out.baseline_held_in:.3f}->{out.candidate_held_in:.3f} "
                      f"but held-out {out.baseline_held_out:.3f}->{out.candidate_held_out:.3f}; "
                      "solver memorised seen instances")
        return out

    # 5. Capability non-escalation proof over the solver patch. Pass the current
    #    solver source as the differential baseline (baseline_files) -- without
    #    it capability_delta has no "before" to diff against and the proof is
    #    unset, so the code rung falls back to demanding one (fail-closed) and a
    #    genuine uplift can never promote.
    cb = ca = None
    probe: tuple[str, ...] = ()
    try:
        baseline_files = None
        solver_py = Path(solver_dir) / "solver.py"
        if solver_py.exists():
            baseline_files = {"solver.py": solver_py.read_text(encoding="utf-8")}
        cb, ca, probe = capability_delta(solver_patch, baseline_files=baseline_files)
        if cb is not None and ca is not None and probe:
            out.capability_widens = any(ca.permits(t) and not cb.permits(t) for t in probe)
    except Exception:
        log.debug("capability_delta failed", exc_info=True)

    # 6. Human-signed promotion through the governed gate -> ledger.
    import os
    priv_path = Path(keys_dir) / "operator.priv.hex"
    if not priv_path.exists():
        out.reason = f"no operator signing key at {priv_path}"
        return out
    cand = Candidate(
        rung="code",
        summary=(f"solver self-modification: held-out resolved-rate "
                 f"{out.baseline_held_out:.3f} -> {out.candidate_held_out:.3f}"),
        baseline_score=out.baseline_held_out, candidate_score=out.candidate_held_out,
        samples=out.samples, payload=solver_patch,
        payload_sha256=hashlib.sha256(solver_patch.encode()).hexdigest(),
        capability_before=cb, capability_after=ca, probe_tools=probe,
        rollback={"archive": "previous solver version archived by apply_and_archive"},
        id=change_id or f"dgm-uplift-{uuid.uuid4().hex[:8]}")
    sig = asig.sign_request(asig.ApprovalRequest.for_candidate(cand),
                            priv_path.read_text().strip())
    cand = Candidate(**{**cand.__dict__, "approval_signature": sig})

    prev = {k: os.environ.get(k) for k in ("MAVERICK_APPROVER_KEYS_DIR", "MAVERICK_SELF_IMPROVEMENT")}
    os.environ["MAVERICK_APPROVER_KEYS_DIR"] = str(keys_dir)
    os.environ["MAVERICK_SELF_IMPROVEMENT"] = "1"
    try:
        ctrl = controller or SelfImprovementController(ledger=ledger)
        verdict = ctrl.promote(cand)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out.promoted = bool(verdict.ok)
    out.approver_id = verdict.approver_id
    out.reason = (f"PROMOTED: held-out {out.baseline_held_out:.3f} -> "
                  f"{out.candidate_held_out:.3f} over {out.samples} tests"
                  if verdict.ok else f"gate refused: {verdict.blocking_reason}")
    return out


def apply_and_archive(solver_dir: Path, solver_patch: str, archive_dir: Path,
                      *, version: str) -> Path:
    """Apply a PROMOTED patch to the live solver, archiving the prior version
    first. Returns the archive path -- the rollback handle (restore = copy the
    archived ``solver.py`` back)."""
    import shutil

    from maverick.self_modify_eval import git_apply

    solver_dir, archive_dir = Path(solver_dir), Path(archive_dir)
    snap = archive_dir / version
    snap.mkdir(parents=True, exist_ok=True)
    shutil.copy2(solver_dir / "solver.py", snap / "solver.py")
    applied = git_apply(solver_patch, solver_dir)
    if not applied.ok:
        raise RuntimeError(f"promoted patch failed to apply live: {applied.reason}")
    return snap


def rollback_solver(solver_dir: Path, archive_snapshot: Path) -> None:
    """One-step revert: restore the archived solver version."""
    import shutil
    shutil.copy2(Path(archive_snapshot) / "solver.py", Path(solver_dir) / "solver.py")


# --- the LLM proposer (authors solver patches; needs a provider key) -----------

def _extract_fenced_diff(text: str) -> str:
    """Pull a unified diff out of a possibly fenced model reply."""
    text = (text or "").strip()
    if "```" in text:
        for p in text.split("```"):
            body = p.removeprefix("diff").removeprefix("patch").strip("\n")
            if body.lstrip().startswith(("diff --git", "--- ")):
                return body
    return text


def _extract_fenced_code(text: str) -> str:
    """Pull the FULL file body out of a fenced model reply, for the full-file
    proposer (the model returns the complete new solver.py).

    Cheap models paraphrase the file and SPLIT it across several ```python
    blocks with prose in between (observed live: the top of the file in one
    block, ``solve()`` in a later block, so 'largest block' grabs a half with
    no ``solve``). So: if one block already defines ``solve``, use the largest
    such block; otherwise CONCATENATE all code blocks in order to reassemble a
    split file. No fences at all -> the whole text."""
    text = (text or "").strip()
    if "```" not in text:
        return text
    blocks = []
    parts = text.split("```")
    for i in range(1, len(parts), 2):          # fenced blocks are odd-indexed
        b = parts[i]
        nl = b.find("\n")
        if nl != -1 and b[:nl].strip().lower() in ("python", "py", ""):
            b = b[nl + 1:]
        b = b.strip("\n")
        if b.strip():
            blocks.append(b)
    if not blocks:
        return text

    def _is_full_module(b: str) -> bool:
        # a self-contained solver.py: defines solve AND carries module setup
        # (an import or a leading docstring) -- not just the solve() fragment.
        return "def solve" in b and (
            "import " in b or b.lstrip().startswith(('"""', "'''", "from ")))

    full = [b for b in blocks if _is_full_module(b)]
    if full:
        return max(full, key=len)               # one clean complete file
    return "\n".join(blocks)                     # reassemble a split file


def _parse_search_replace(reply: str) -> tuple[list[tuple[str, str]], str]:
    """Parse Aider-style SEARCH/REPLACE edit blocks out of a model reply:

        <<<<<<< SEARCH
        <exact existing lines>
        =======
        <replacement lines>
        >>>>>>> REPLACE

    Returns ``(edits, error)`` where edits is a list of ``(search, replace)``.
    This is the robust proposer interface: the model quotes only the small
    region it changes -- no line-number arithmetic (diffs), no reproducing the
    whole file (which cheap and capable models alike split mid-statement across
    code fences). ``("", ...)`` -> no well-formed block found."""
    import re
    blocks = re.findall(
        r"<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE",
        reply or "", flags=re.DOTALL)
    edits = [(s, r) for s, r in blocks]
    if not edits:
        return [], "no SEARCH/REPLACE blocks found in reply"
    return edits, ""


def _apply_search_replace(src_text: str, edits: list[tuple[str, str]]) -> tuple[str, str]:
    """Apply SEARCH/REPLACE edits to ``src_text`` by EXACT string match. Each
    SEARCH must occur exactly once (0 -> not found; >1 -> ambiguous), so the
    edit is deterministic and unambiguous. Returns ``(new_text, error)``."""
    text = src_text
    for i, (search, replace) in enumerate(edits):
        if not search.strip():
            return "", f"edit {i}: empty SEARCH"
        n = text.count(search)
        if n == 0:
            return "", (f"edit {i}: SEARCH not found (must be quoted EXACTLY):\n"
                        f"{search[:200]}")
        if n > 1:
            return "", (f"edit {i}: SEARCH matches {n} places (add context to "
                        f"make it unique):\n{search[:200]}")
        text = text.replace(search, replace, 1)
    if text == src_text:
        return "", "edits changed nothing"
    return text, ""


def _full_file_to_patch(solver_dir: Path, new_source: str) -> tuple[str, str]:
    """Diff a COMPLETE new ``solver.py`` against the current one via git -- the
    robust proposer path. LLMs write correct Python far more reliably than
    correct unified-diff arithmetic (two live cycles died on hunk headers /
    non-matching context); having the model return the whole file and letting
    ``git diff`` author the patch removes that entire failure class.

    Returns ``(patch, error)``: ``("", why)`` if the reply isn't a plausible
    solver (must define ``solve``) or changes nothing."""
    import shutil
    import subprocess
    import tempfile

    body = (new_source or "").strip()
    if not body:
        return "", "empty reply (no file body)"
    if "def solve" not in body:
        return "", "reply does not define solve(); not a solver file"
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": os.environ.get("PATH", "")}

    def _git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=60, env=env)

    td = tempfile.mkdtemp(prefix="dgm-full-")
    try:
        scratch = Path(td) / "s"
        shutil.copytree(solver_dir, scratch,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        _git("init", "-q", ".", cwd=scratch)
        _git("add", "-A", cwd=scratch)
        _git("-c", "user.email=dgm@local", "-c", "user.name=dgm",
             "commit", "-qm", "base", cwd=scratch)
        (scratch / "solver.py").write_text(
            body if body.endswith("\n") else body + "\n", encoding="utf-8")
        canon = _git("diff", cwd=scratch).stdout
        if not canon.strip():
            return "", "new file is identical to the current solver"
        return canon, ""
    except Exception as e:
        return "", f"full-file diff error: {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _canonicalize_solver_patch(solver_dir: Path, diff_text: str) -> tuple[str, str]:
    """Turn a model-authored diff into a CANONICAL, guaranteed-applying one.

    LLMs miscount hunk headers ("corrupt patch at line N" -- the exact failure
    that killed the first live cycle, and the same class the agent path solved
    with worktree diffs over prose diffs). Rather than trust the model's line
    arithmetic: apply the diff to a scratch git copy of the solver, letting
    ``--recount`` re-derive the counts from the hunk bodies, then re-emit
    ``git diff`` -- git's own arithmetic, canonical by construction.

    Returns ``(canonical_diff, error)``: ``("", why)`` when the diff is
    unusable even recounted (the retry prompt gets ``why``); a diff that
    changes nothing also returns ``("", ...)`` so a no-op can't reach the gate.
    """
    import shutil
    import subprocess
    import tempfile

    if not (diff_text or "").strip():
        return "", "empty reply (no diff found)"
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": os.environ.get("PATH", "")}

    def _git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=60, env=env)

    td = tempfile.mkdtemp(prefix="dgm-canon-")
    try:
        scratch = Path(td) / "s"
        shutil.copytree(solver_dir, scratch,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        _git("init", "-q", ".", cwd=scratch)
        _git("add", "-A", cwd=scratch)
        _git("-c", "user.email=dgm@local", "-c", "user.name=dgm",
             "commit", "-qm", "base", cwd=scratch)
        patch_file = Path(td) / "p.diff"
        patch_file.write_text(diff_text if diff_text.endswith("\n")
                              else diff_text + "\n", encoding="utf-8")
        last_err = ""
        for extra in ((), ("--recount",), ("--recount", "--whitespace=fix")):
            r = _git("apply", *extra, str(patch_file), cwd=scratch)
            if r.returncode == 0:
                canon = _git("diff", cwd=scratch).stdout
                if not canon.strip():
                    return "", "diff applied but changed nothing"
                return canon, ""
            last_err = (r.stderr or r.stdout or "").strip()[-500:]
        return "", f"git apply failed even with --recount: {last_err}"
    except Exception as e:  # scratch trouble is a repair failure, not a crash
        return "", f"canonicalize error: {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def llm_solver_proposer(solver_dir: Path, feedback: str, *, model: str | None = None,
                        max_tokens: int = 8000) -> str:
    """Author a solver improvement via the coding-role model and return a
    git-canonical, guaranteed-applying patch.

    Strategy: ask for the COMPLETE rewritten ``solver.py`` (not a diff) and let
    ``git diff`` author the patch. LLMs write correct Python far more reliably
    than correct unified-diff arithmetic -- two live cycles died on hunk headers
    / non-matching context before this. One pointed retry if the reply isn't a
    usable solver. As a last resort, if the reply happened to be a diff we
    canonicalize that instead, so a diff-shaped answer still has a chance.
    Returns "" when nothing usable was produced, so the gate refuses honestly
    (never promotes garbage). Fail-loud keyless."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")):
        raise RuntimeError(
            "llm_solver_proposer needs a provider key (ANTHROPIC_API_KEY, or "
            "MAVERICK_PROVIDER_READY=1 with a configured provider).")
    from maverick.llm import LLM, ROLE_MODELS

    # The proposer is ONE call per cycle -- author the solver improvement with a
    # capable CODER-role model (config-overridable; never a hard-coded id, kernel
    # rule 2). A weak default returned the file split across blocks / with broken
    # string literals, defeating every reassembly heuristic; a capable model
    # returns one clean, complete, valid file.
    if model is None:
        from maverick.config import get_role_model
        model = get_role_model("coder") or ROLE_MODELS.get("coder")

    src = (Path(solver_dir) / "solver.py").read_text(encoding="utf-8")
    system = (
        "You improve a bug-fixing solver (solver.py, whose solve(instance) -> a "
        "unified diff or ''). Given its source and feedback on instances it "
        "failed, propose edits as one or more SEARCH/REPLACE blocks in EXACTLY "
        "this format:\n"
        "<<<<<<< SEARCH\n<lines copied verbatim from the file>\n=======\n"
        "<the replacement lines>\n>>>>>>> REPLACE\n"
        "Copy the SEARCH lines EXACTLY (whitespace included) and include enough "
        "context that each SEARCH occurs exactly once. Keep solve(instance) -> "
        "str and never raise. Never edit tests; never special-case instance "
        "ids -- generalise. Output only SEARCH/REPLACE blocks, no prose.")
    base_user = (f"Current solver.py:\n```python\n{src}\n```\n\n"
                 f"Evaluation feedback:\n{feedback}\n\n"
                 "Reply with SEARCH/REPLACE blocks that improve the solver.")
    llm = LLM()

    def _ask(user: str) -> tuple[str, str]:
        text = (llm.complete(system, [{"role": "user", "content": user}],
                             max_tokens=max_tokens, model=model).text or "")
        # Primary: SEARCH/REPLACE edits -> apply by exact match -> git diff.
        edits, why = _parse_search_replace(text)
        if edits:
            new_src, aerr = _apply_search_replace(src, edits)
            if new_src:
                patch, gerr = _full_file_to_patch(solver_dir, new_src)
                if patch:
                    return patch, ""
                return "", gerr
            why = aerr
        # Fallbacks: a full file, or a hand-authored diff (older reply shapes).
        p2, w2 = _full_file_to_patch(solver_dir, _extract_fenced_code(text))
        if p2:
            return p2, ""
        dtext = _extract_fenced_diff(text)
        if dtext.lstrip().startswith(("diff --git", "--- ")):
            dp, dw = _canonicalize_solver_patch(solver_dir, dtext)
            if dp:
                return dp, ""
        return "", why

    patch, why = _ask(base_user)
    if patch:
        return patch
    log.warning("proposer reply unusable (%s); retrying once", why)
    patch2, why2 = _ask(base_user + f"\n\nYour previous reply was unusable "
                        f"({why}). Reply with ONLY SEARCH/REPLACE blocks; copy "
                        "each SEARCH region byte-for-byte from the file above.")
    if patch2:
        return patch2
    log.warning("proposer retry also unusable (%s)", why2)
    return ""   # gate refuses on empty patch -> honest 'no usable improvement'


__all__ = [
    "UpliftResult",
    "load_solver",
    "score_instance",
    "container_score_fn",
    "_instance_test_python",
    "run_solver",
    "split_instances",
    "govern_solver_change",
    "apply_and_archive",
    "rollback_solver",
    "llm_solver_proposer",
]
