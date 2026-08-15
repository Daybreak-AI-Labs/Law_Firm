"""Offline tests for governed grading in the official SWE-bench container.

No network, no Modal, no Docker: a committed real-instance fixture
(astropy__astropy-12907) drives the OFFICIAL spec/image/eval-script/grader, and
a FAKE ``run_in_image`` returns a canned official test log. This pins the two
things that must be exactly right -- the grade script we run in the container,
and the resolution parsed back out -- without spending a cent.
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
import swebench_container_grade as CG  # noqa: E402
from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT  # noqa: E402
from testdata.astropy_12907 import INSTANCE as _ASTROPY  # noqa: E402


def _instance() -> dict:
    return copy.deepcopy(_ASTROPY)


def _canned_log(inst: dict, *, all_pass: bool) -> str:
    """A minimal official-format test log: PASSED/FAILED lines between the
    Start/End markers the eval_script emits. all_pass=False fails one
    FAIL_TO_PASS target."""
    lines = [str(START_TEST_OUTPUT)]
    for i, t in enumerate(inst["FAIL_TO_PASS"]):
        lines.append(f"{'FAILED' if (not all_pass and i == 0) else 'PASSED'} {t}")
    for t in inst["PASS_TO_PASS"]:
        lines.append(f"PASSED {t}")
    lines.append(str(END_TEST_OUTPUT))
    return "install log ...\n" + "\n".join(lines) + "\ntrailing ...\n"


def _fake_runner(log_text: str, *, exit_code: int = 1):
    calls = {"scripts": []}

    def run(image, script, timeout):
        calls["scripts"].append((image, script))
        return CG.RunOutput(stdout=log_text, exit_code=exit_code)
    run.calls = calls
    return run


# --- image + script -----------------------------------------------------------

def test_official_image_name():
    spec = CG.make_spec(_instance())
    # Docker Hub forbids '__' in repo names -> official '_1776_' substitution when
    # a namespace is prefixed. The bare (namespace-less) key keeps the '__'.
    assert CG.instance_image(spec) == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    assert CG.instance_image(spec, namespace="") == "sweb.eval.x86_64.astropy__astropy-12907:latest"


def test_grade_script_applies_candidate_and_runs_official_eval():
    spec = CG.make_spec(_instance())
    cand = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    script = CG.build_grade_script(spec, cand)
    # candidate is applied (base64-piped, not a fragile heredoc) before the eval
    assert "base64 -d > /tmp/maverick_candidate.diff" in script
    assert "git apply -v /tmp/maverick_candidate.diff" in script
    # the OFFICIAL eval script (with its Start/End markers) is what runs the tests
    assert str(START_TEST_OUTPUT) in script and str(END_TEST_OUTPUT) in script
    # a candidate ordering: apply must precede the eval's test output
    assert script.index("maverick_candidate.diff") < script.index(str(START_TEST_OUTPUT))


def test_baseline_script_has_no_candidate_apply():
    spec = CG.make_spec(_instance())
    script = CG.build_grade_script(spec, "")
    assert "maverick_candidate.diff" not in script
    assert str(START_TEST_OUTPUT) in script


# --- grading via the official grader ------------------------------------------

def test_resolved_when_all_targets_pass():
    inst = _instance()
    runner = _fake_runner(_canned_log(inst, all_pass=True))
    r = CG.grade_in_container(inst, "diff --git a/x b/x\n", runner, timeout=60)
    assert r.resolved is True
    assert len(r.fail_to_pass_failed) == 0
    assert len(r.fail_to_pass_passed) == len(inst["FAIL_TO_PASS"])
    assert r.candidate_apply_ok is True


def test_not_resolved_when_a_target_fails():
    inst = _instance()
    runner = _fake_runner(_canned_log(inst, all_pass=False))
    r = CG.grade_in_container(inst, "diff --git a/x b/x\n", runner, timeout=60)
    assert r.resolved is False
    assert len(r.fail_to_pass_failed) >= 1


def test_candidate_apply_failure_is_flagged():
    inst = _instance()
    log = _canned_log(inst, all_pass=False).replace(
        "install log ...", "install log ...\n__MAVERICK_CANDIDATE_APPLY_FAILED__")
    r = CG.grade_in_container(inst, "diff --git a/x b/x\n", _fake_runner(log), timeout=60)
    assert r.candidate_apply_ok is False


# --- backend isolation -------------------------------------------------------

def test_docker_runner_disables_network_and_hardens_container(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(CG.subprocess, "run", fake_run)
    out = CG.docker_runner(cpus=1.0, memory="256m")("img:latest", "echo hi", 30)

    assert out.exit_code == 0
    argv = calls["argv"]
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "--pids-limit" in argv
    assert calls["kwargs"]["timeout"] == 30


def test_modal_runner_fails_closed_without_network_acknowledgement(monkeypatch):
    class BombModal:
        class App:
            @staticmethod
            def lookup(*args, **kwargs):  # pragma: no cover - must not be reached
                raise AssertionError("Modal should not be called")

    monkeypatch.setitem(sys.modules, "modal", BombModal)
    out = CG.modal_runner()("img:latest", "echo hi", 30)

    assert out.exit_code == 2
    assert "refused" in out.stderr


# --- governance ---------------------------------------------------------------

def test_governed_refuses_a_test_editing_candidate_before_container():
    inst = _instance()
    # a candidate that edits the graded test file must be refused HOST-side; the
    # container runner must never even be called.
    cheat = (
        "diff --git a/astropy/modeling/tests/test_separable.py "
        "b/astropy/modeling/tests/test_separable.py\n"
        "--- a/astropy/modeling/tests/test_separable.py\n"
        "+++ b/astropy/modeling/tests/test_separable.py\n"
        "@@ -1 +1 @@\n-assert x\n+assert True\n"
    )
    runner = _fake_runner(_canned_log(inst, all_pass=True))
    r = CG.governed_container_grade(inst, cheat, runner, check_baseline=False)
    assert r.boundary_ok is False
    assert runner.calls["scripts"] == []          # container never invoked
    assert r.resolved is False


def test_governed_baseline_guard_rejects_when_targets_pass_at_baseline():
    inst = _instance()
    # Baseline (empty candidate) shows FAIL_TO_PASS PASSING -> mis-seeded/bad
    # image -> refuse, at container cost, before crediting anything.
    runner = _fake_runner(_canned_log(inst, all_pass=True))
    r = CG.governed_container_grade(inst, "diff --git a/x b/x\n", runner, check_baseline=True)
    assert "mis-seeded" in r.error


def test_governed_promotes_path_grades_candidate_after_good_baseline(monkeypatch):
    inst = _instance()
    # Baseline fails the targets (real bug), candidate resolves them.
    logs = [_canned_log(inst, all_pass=False), _canned_log(inst, all_pass=True)]
    seq = {"i": 0}

    def run(image, script, timeout):
        out = logs[min(seq["i"], len(logs) - 1)]
        seq["i"] += 1
        return CG.RunOutput(stdout=out, exit_code=1)

    r = CG.governed_container_grade(inst, "diff --git a/x b/x\n", run, check_baseline=True)
    assert r.boundary_ok is True
    assert r.resolved is True
    assert seq["i"] == 2                            # baseline + candidate
