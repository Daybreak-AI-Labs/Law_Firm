"""The depth proof drives REAL LLM calls (the model is the learner), so it self-
skips when no provider key is present -- CI parity with the other live-LLM
benchmarks. When a key IS present it must pass all three flagships (dreaming
held-out lift, self-harness LLM proposer promotion, evaluator LLM challenger)
plus the signed-audit-chain check, under a small hard cost cap. Subprocess so its
env/MAVERICK_HOME mutation never leaks into the rest of the suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MAVERICK_PROVIDER_READY")),
    reason="self_learning_depth makes real LLM calls; no provider key configured")
def test_depth_flagships_pass_with_real_llm():
    r = subprocess.run(
        [sys.executable, str(_ROOT / "benchmarks" / "self_learning_depth.py"),
         "--max-dollars", "6"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, f"depth proof failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    assert "DEPTH PROVEN" in r.stdout
