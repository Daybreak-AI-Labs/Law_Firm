"""The self-learning aggregate proof must keep passing every in-process
capability (governed gate, dreaming, hindsight, snapshot/rollback, flows
self-evolution, fleet memory, operating record, evaluator co-evolution) and the
signed-audit-chain integrity check. Run as a subprocess so the proof's env/
MAVERICK_HOME mutation never leaks into the rest of the suite. In-process only
(--no-scripts); the heavier proof/*.py scoreboards have their own tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_all_in_process_self_learning_capabilities_pass():
    r = subprocess.run(
        [sys.executable, str(_ROOT / "benchmarks" / "self_learning_proof.py"), "--no-scripts"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"self-learning proof failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    assert "ALL SELF-LEARNING CAPABILITIES PROVEN" in r.stdout
