#!/usr/bin/env python3
"""Replay the env pre-gate for one sympy instance with full visibility.

The round-3c run pre-gated the whole sympy family NOENV, yet the same
instance's tests pass when run by hand in its venv. This replays each
pre-gate step (venv selection, isolated copy, grader fixture, graded test
run) and prints everything, so the diverging link is visible. Read-only,
no LLM, no spend.  Usage: python3 pod_sympy_diag.py [instance_id]
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MAVERICK_SWEBENCH_VENVS",
                      str(Path.home() / "swebench_stage" / "venvs"))
os.environ.setdefault("MAVERICK_SUPPRESS_SANDBOX_WARNING", "1")

import swebench_governed as G  # noqa: E402

STAGE = Path.home() / "swebench_stage"
iid = sys.argv[1] if len(sys.argv) > 1 else "sympy__sympy-24066"

insts = {i.instance_id: i for i in G._load_manifest(STAGE / "sympy_manifest.jsonl")}
inst = insts[iid]
vp = G._venv_python(inst)
print("venv python  :", vp or "(none found -- host python3 would be used)")
if vp:
    os.environ["MAVERICK_TEST_PYTHON"] = vp
print("gradable     :", inst.gradable, inst.ungradable_reason or "")
print("fail_to_pass :", len(inst.fail_to_pass), inst.fail_to_pass[:2])
print("pass_to_pass :", len(inst.pass_to_pass), inst.pass_to_pass[:2])

from maverick.coding_mode import run_failing_tests  # noqa: E402
from maverick.self_modify_eval import (  # noqa: E402
    _default_materialize,
    git_apply,
    resolve_eval_sandbox,
)

with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "b"
    _default_materialize(inst.repo_path, base)
    print("materialized :", base, "files:", sum(1 for _ in base.rglob("*.py")))
    if (inst.test_patch or "").strip():
        tp = git_apply(inst.test_patch, base)
        print("test_patch   :", "OK" if tp.ok else f"FAILED: {tp.reason[:120]}")
    sb = resolve_eval_sandbox(None, base)
    res = run_failing_tests(base, inst.fail_to_pass, inst.pass_to_pass, sb,
                            timeout=900, language=inst.language)
    print("score        :", res.score, "| all_pass:", res.all_pass,
          "| skipped:", getattr(res, "skipped", None))
    print("runner       :", getattr(res, "runner", ""))
    print("error        :", getattr(res, "error", "") or "(none)")
    for attr in ("output", "raw_output", "stdout", "detail"):
        val = getattr(res, attr, "")
        if val:
            print(f"--- {attr} tail ---")
            print(str(val)[-1200:])
            break
    print("--- result fields ---")
    print({k: v for k, v in vars(res).items()
           if not isinstance(v, str) or len(v) < 200})
