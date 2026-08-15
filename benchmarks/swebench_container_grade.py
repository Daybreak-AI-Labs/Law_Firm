#!/usr/bin/env python3
"""Grade a SWE-bench instance in its OFFICIAL per-instance container, under the
governance chain.

Yesterday's money-waste was the *environment*: a single host cannot reproduce
each instance's exact Python era + pinned deps, so instances graded NOENV/EMPTY
and the agent's spend was thrown at things that could never grade. The fix is
per-instance containers -- the SWE-bench project publishes a pre-built image per
instance (``sweb.eval.x86_64.<id>``) that already contains the repo at
``base_commit`` with the correct environment. This module grades inside that
image, reusing the OFFICIAL spec, eval script, and grader
(:mod:`swebench.harness`), so the era-mismatch bug class cannot recur -- the
environment travels with the instance.

What stays ours -- the governance -- is unchanged:

* the anti-cheat boundary (:func:`maverick.coding_mode.defensive_validate`) runs
  HOST-side on the candidate patch text before anything is applied;
* a baseline (no-candidate) grade confirms the graded FAIL_TO_PASS actually fail
  in the image (honest mis-seed guard);
* a resolved candidate is promoted through the same signed, reversible ledger
  gate (:func:`benchmarks.swebench_governed._promote`).

Backend-agnostic: the only container primitive needed is
``run_in_image(image, script, timeout) -> RunOutput``. Three are provided --
``modal`` (per-instance Modal Sandboxes, pay-per-second, no host), ``docker``
(a local Docker daemon on a VM), and an injectable fake for offline tests. So
the same governed grade runs on Modal, on a Docker VM, or under test with no
network and no spend.

    # validate one instance in a real container (a few cents, NO Anthropic $):
    python benchmarks/swebench_container_grade.py --instance astropy__astropy-12907 \
        --backend modal --gold        # grade the gold patch: must resolve
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "maverick-core"))

log = logging.getLogger(__name__)

# Docker Hub namespace the official pre-built eval images are published under.
# make_test_spec's ``instance_image_key`` is the bare repo:tag; the published
# image is docker.io/<namespace>/<key> with ``__`` -> ``_1776_`` (Docker Hub
# forbids ``__`` in repo names). Overridable for a private mirror.
DEFAULT_IMAGE_NAMESPACE = "swebench"


@dataclass
class RunOutput:
    """Result of running a script inside a container image."""
    stdout: str
    exit_code: int
    stderr: str = ""


@dataclass
class ContainerGradeResult:
    """The governed verdict for one candidate graded in the official image."""
    instance_id: str
    boundary_ok: bool = False
    boundary_reason: str = ""
    candidate_apply_ok: bool = False
    resolved: bool = False          # official get_eval_report resolution
    fail_to_pass_passed: list[str] = field(default_factory=list)
    fail_to_pass_failed: list[str] = field(default_factory=list)
    pass_to_pass_passed: list[str] = field(default_factory=list)
    pass_to_pass_failed: list[str] = field(default_factory=list)
    error: str = ""
    raw_tail: str = ""

    @property
    def fail_to_pass_all(self) -> int:
        return len(self.fail_to_pass_passed) + len(self.fail_to_pass_failed)


# --- official spec / image ----------------------------------------------------

def make_spec(instance: dict):
    """Official :class:`swebench...TestSpec` for an instance dict (the source of
    truth for image, environment, eval procedure, and grading)."""
    from swebench.harness.test_spec.test_spec import make_test_spec
    return make_test_spec(instance)


def instance_image(spec, namespace: str = DEFAULT_IMAGE_NAMESPACE) -> str:
    """Fully-qualified official image for an instance, e.g.
    ``swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest``.

    Docker Hub repository names forbid ``__``; the official harness therefore
    substitutes ``_1776_`` for ``__`` whenever a namespace is prefixed (see
    ``swebench.harness.test_spec.TestSpec.instance_image_key``). ``make_spec``
    builds a namespace-less spec, so ``spec.instance_image_key`` is the bare
    ``repo:tag`` and we apply the identical substitution here -- otherwise the
    pull 404s (``astropy__astropy`` is not a valid Docker Hub repo)."""
    key = spec.instance_image_key
    return f"{namespace}/{key}".replace("__", "_1776_") if namespace else key


def build_grade_script(spec, candidate_patch: str) -> str:
    """The shell script run INSIDE the instance image: apply the candidate patch
    to ``/testbed`` (base64-piped so no diff byte can break the heredoc), then
    run the OFFICIAL ``eval_script`` (which applies the grader's test_patch and
    runs the graded tests between the Start/End markers). An empty candidate is
    the BASELINE grade (eval_script only).

    ``exec 2>&1`` merges stderr into stdout up front: the eval_script runs under
    ``set -x`` and emits the Start/End markers as no-op ``: 'marker'`` commands,
    so the markers surface only on the trace stream (stderr) while pytest's
    PASSED/FAILED lines go to stdout. The official harness captures a single
    combined log; we reproduce that ordered stream so the grader sees the
    markers wrapping the results (separate streams -> zero tests parsed)."""
    patch = candidate_patch or ""
    lines = ["#!/bin/bash", "exec 2>&1", "set -uo pipefail"]
    if patch.strip():
        b64 = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        lines += [
            "cd /testbed",
            f"echo {b64} | base64 -d > /tmp/maverick_candidate.diff",
            # git apply is the same primitive the official harness uses for the
            # model patch; a non-applying candidate prints a marker we detect.
            "git apply -v /tmp/maverick_candidate.diff "
            "|| echo '__MAVERICK_CANDIDATE_APPLY_FAILED__'",
        ]
    lines.append(spec.eval_script)
    return "\n".join(lines) + "\n"


# --- grading ------------------------------------------------------------------

def _parse_report(spec, instance: dict, log_text: str) -> ContainerGradeResult:
    """Turn container stdout into a result via the OFFICIAL grader
    (``get_eval_report``), so resolution matches the leaderboard exactly."""
    from swebench.harness.constants import (
        FAIL_TO_PASS,
        KEY_INSTANCE_ID,
        KEY_PREDICTION,
        PASS_TO_PASS,
        ResolvedStatus,
    )
    from swebench.harness.grading import get_eval_report

    out = ContainerGradeResult(instance_id=instance["instance_id"])
    out.candidate_apply_ok = "__MAVERICK_CANDIDATE_APPLY_FAILED__" not in log_text
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(log_text)
        log_path = fh.name
    prediction = {
        KEY_INSTANCE_ID: instance["instance_id"],
        KEY_PREDICTION: "x",  # non-empty so the grader runs the tests report
        "model_name_or_path": "maverick-container-grade",
    }
    try:
        report = get_eval_report(spec, prediction, log_path, include_tests_status=True)
    except Exception as e:  # pragma: no cover -- defensive; report the raw tail
        out.error = f"grader failed: {e}"
        out.raw_tail = log_text[-1500:]
        return out
    finally:
        try:
            Path(log_path).unlink()
        except OSError:
            pass
    inst_report = report.get(instance["instance_id"], {})
    out.resolved = bool(inst_report.get("resolved", False))
    tests = inst_report.get("tests_status", {}) or {}
    f2p = tests.get(FAIL_TO_PASS, {}) or {}
    p2p = tests.get(PASS_TO_PASS, {}) or {}
    out.fail_to_pass_passed = list(f2p.get("success", []))
    out.fail_to_pass_failed = list(f2p.get("failure", []))
    out.pass_to_pass_passed = list(p2p.get("success", []))
    out.pass_to_pass_failed = list(p2p.get("failure", []))
    out.raw_tail = log_text[-1500:]
    _ = ResolvedStatus  # imported for clarity / future status reporting
    return out


def grade_in_container(instance: dict, candidate_patch: str, run_in_image, *,
                       timeout: float = 1800.0,
                       namespace: str = DEFAULT_IMAGE_NAMESPACE) -> ContainerGradeResult:
    """Grade ``candidate_patch`` for ``instance`` inside its official image.

    ``run_in_image(image, script, timeout) -> RunOutput`` is the only container
    primitive required. No governance here -- this is the pure grader; the
    governed entrypoint is :func:`governed_container_grade`.
    """
    spec = make_spec(instance)
    image = instance_image(spec, namespace=namespace)
    script = build_grade_script(spec, candidate_patch)
    ro = run_in_image(image, script, timeout)
    result = _parse_report(spec, instance, ro.stdout or "")
    if ro.exit_code not in (0, 1) and not result.raw_tail:
        # 0/1 are normal (pytest returns 1 on failures). Anything else with no
        # parseable output is an infra error, surfaced not swallowed.
        result.error = result.error or f"container exit {ro.exit_code}: {ro.stderr[-500:]}"
    return result


def _grading_sensitive_paths(patch: str) -> list[str]:
    """Paths a candidate touches that could rig the grader rather than fix the
    bug (conftest/pytest.ini/setup*/pyproject/requirements/Makefile...). Reuses
    the single definition in :mod:`swebench_governed` so container grading and
    host grading refuse exactly the same set (no drift). Imported lazily to keep
    this module's import graph light and one-directional."""
    from swebench_governed import _grading_sensitive_paths as host_check
    return host_check(patch)


def host_boundary(instance: dict, candidate_patch: str, *,
                  check_gold_overlap: bool = True) -> tuple[bool, str]:
    """The $0 host-side anti-cheat boundary, shared by every container-grading
    caller (the governed grade below, and the DGM's ``container_score_fn``):
    ``defensive_validate`` (test/setup-edit refusal; optional gold-overlap
    warning) plus the grading-sensitive-config hard refusal. One definition so
    a candidate refused on one path is refused on all of them."""
    from maverick.coding_mode import defensive_validate

    f2p = list(instance.get("FAIL_TO_PASS") or instance.get("fail_to_pass") or [])
    p2p = list(instance.get("PASS_TO_PASS") or instance.get("pass_to_pass") or [])
    gold = "" if not check_gold_overlap else (
        instance.get("patch", "") or instance.get("gold_patch", ""))
    dv = defensive_validate(candidate_patch, fail_to_pass=f2p, pass_to_pass=p2p,
                            gold_patch=gold)
    if not dv.ok:
        return False, ("; ".join(dv.blocked_paths) or "anti-cheat boundary refused")
    sensitive = _grading_sensitive_paths(candidate_patch)
    if sensitive:
        return False, f"candidate edits grading-sensitive config: {sensitive}"
    return True, "no test/config edits"


def governed_container_grade(instance: dict, candidate_patch: str, run_in_image, *,
                             timeout: float = 1800.0,
                             namespace: str = DEFAULT_IMAGE_NAMESPACE,
                             check_baseline: bool = True,
                             check_gold_overlap: bool = True) -> ContainerGradeResult:
    """Grade under governance: anti-cheat boundary (host) -> grading-sensitive
    config refusal -> optional baseline mis-seed guard -> candidate grade in the
    official image.

    Mirrors :func:`swebench_governed.govern_candidate`'s boundary strictness
    exactly, so a candidate refused on the host path is refused here too.
    ``check_gold_overlap=False`` disables ONLY the gold-copy similarity warning
    (for the oracle/gold proposer, whose candidate IS gold by definition);
    test/setup-edit refusal always stays armed.

    Promotion/signing is layered by the caller (reusing
    ``swebench_governed._promote``) so this module has no key material and no
    ledger side effects -- it only decides ``resolved``.
    """
    out = ContainerGradeResult(instance_id=instance["instance_id"])

    # 1. Anti-cheat boundary, host-side, before anything runs in the container
    #    (shared definition -- see host_boundary).
    ok, reason = host_boundary(instance, candidate_patch,
                               check_gold_overlap=check_gold_overlap)
    out.boundary_ok = ok
    if not ok:
        out.boundary_reason = reason
        return out

    # 2. Baseline mis-seed guard: the graded FAIL_TO_PASS must FAIL with no
    #    candidate. In the official image this is true by construction, but a
    #    mis-seeded instance (or a bad image) is caught here at container cost.
    if check_baseline:
        base = grade_in_container(instance, "", run_in_image, timeout=timeout, namespace=namespace)
        if base.error:
            out.error = f"baseline grade error: {base.error}"
            return out
        if not base.fail_to_pass_failed:
            out.error = ("mis-seeded/bad-image: FAIL_TO_PASS do not fail at baseline "
                         f"(passed {len(base.fail_to_pass_passed)}/{base.fail_to_pass_all})")
            return out

    # 3. Candidate grade -> official resolution.
    cand = grade_in_container(instance, candidate_patch, run_in_image,
                              timeout=timeout, namespace=namespace)
    cand.boundary_ok = True
    return cand


# --- container backends -------------------------------------------------------

def modal_runner(*, cpu: float = 2.0, memory_mb: int = 4096,
                 app_name: str = "maverick-swebench",
                 allow_network: bool = False):
    """A ``run_in_image`` backed by Modal Sandboxes (per-instance, pay-per-use).
    Needs ``pip install modal`` and ``modal token new`` once.

    Candidate patches are untrusted code. Modal does not expose a per-sandbox
    no-egress switch here, so the safe default fails closed instead of silently
    running with provider-default networking. Pass ``allow_network=True`` only
    for trusted patches in an isolated Modal environment with no reachable
    secrets or internal services.
    """
    def run(image: str, script: str, timeout: float) -> RunOutput:
        if not allow_network:
            return RunOutput(
                stdout="",
                stderr=(
                    "modal backend refused: no no-egress sandbox policy is "
                    "available here. Use the docker backend for untrusted "
                    "patches, or pass allow_network=True only in an isolated "
                    "trusted environment."
                ),
                exit_code=2,
            )
        import modal
        app = modal.App.lookup(app_name, create_if_missing=True)
        sb = modal.Sandbox.create(
            "bash", "-c", script,
            image=modal.Image.from_registry(image),
            app=app, timeout=max(60, int(timeout)), cpu=cpu, memory=memory_mb)
        try:
            sb.wait()
            stdout = sb.stdout.read() if hasattr(sb.stdout, "read") else str(sb.stdout)
            stderr = sb.stderr.read() if hasattr(sb.stderr, "read") else str(sb.stderr)
            code = sb.returncode if sb.returncode is not None else 1
        finally:
            try:
                sb.terminate()
            except Exception:  # pragma: no cover
                pass
        return RunOutput(stdout=stdout, exit_code=int(code), stderr=stderr)
    return run


def docker_runner(*, cpus: float = 2.0, memory: str = "4g"):
    """A ``run_in_image`` backed by a local Docker daemon (a VM with Docker).

    Candidate patches execute during the official eval, so containers run with
    no network plus a small set of Docker hardening flags.
    """
    def run(image: str, script: str, timeout: float) -> RunOutput:
        try:
            p = subprocess.run(
                ["docker", "run", "--rm", "--network", "none",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--pids-limit", "512", f"--cpus={cpus}", f"--memory={memory}",
                 "--entrypoint", "bash", image, "-c", script],
                capture_output=True, text=True, timeout=timeout)
            return RunOutput(stdout=p.stdout, exit_code=p.returncode, stderr=p.stderr)
        except subprocess.TimeoutExpired as e:
            return RunOutput(stdout=(e.stdout or b"").decode("utf-8", "replace")
                             if isinstance(e.stdout, bytes) else (e.stdout or ""),
                             exit_code=124, stderr="container timed out")
    return run


# --- CLI: validate one instance ----------------------------------------------

def _load_instance(instance_id: str) -> dict:
    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    for row in ds:
        if row["instance_id"] == instance_id:
            inst = dict(row)
            for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                if isinstance(inst.get(k), str):
                    inst[k] = json.loads(inst[k])
            return inst
    raise SystemExit(f"instance {instance_id!r} not in SWE-bench_Verified")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Grade one SWE-bench instance in its official container.")
    ap.add_argument("--instance", required=True)
    ap.add_argument("--backend", choices=("modal", "docker"), default="modal")
    ap.add_argument("--gold", action="store_true",
                    help="grade the instance's GOLD patch (must resolve) -- the "
                         "environment validation: proves the image + grading work")
    ap.add_argument("--namespace", default=DEFAULT_IMAGE_NAMESPACE)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--allow-network", action="store_true",
                    help="Modal only: acknowledge provider-default networking. "
                         "Do not use for untrusted patches.")
    args = ap.parse_args(argv)

    inst = _load_instance(args.instance)
    patch = inst["patch"] if args.gold else ""
    runner = (modal_runner(allow_network=args.allow_network)
              if args.backend == "modal" else docker_runner())
    spec = make_spec(inst)
    print(f"instance: {args.instance}")
    print(f"image:    {instance_image(spec, namespace=args.namespace)}")
    print(f"grading:  {'GOLD patch (must resolve)' if args.gold else 'BASELINE (must NOT resolve)'} "
          f"via {args.backend}")
    r = grade_in_container(inst, patch, runner, timeout=args.timeout, namespace=args.namespace)
    print("=" * 68)
    print(f"  candidate applied: {r.candidate_apply_ok}")
    print(f"  FAIL_TO_PASS: {len(r.fail_to_pass_passed)} pass / {len(r.fail_to_pass_failed)} fail")
    print(f"  PASS_TO_PASS: {len(r.pass_to_pass_passed)} pass / {len(r.pass_to_pass_failed)} fail")
    print(f"  RESOLVED (official): {r.resolved}")
    if r.error:
        print(f"  error: {r.error}")
    print("=" * 68)
    if args.gold:
        ok = r.resolved
        print("VALIDATION:", "PASS -- official image + grading work end to end" if ok
              else "FAIL -- gold patch did not resolve; investigate before spending")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
