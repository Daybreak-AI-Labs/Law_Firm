"""Run candidate code against a set of asserts in an isolated subprocess with a
timeout. Shared by the improved solver (to self-repair against PUBLIC tests) and
by the grader (to score against HIDDEN tests). Subprocess isolation keeps
model-generated code from touching the harness."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def run_tests(code: str, tests: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """True iff ``code`` + ``tests`` runs to completion with no assertion/error.
    Returns (passed, short_error) -- the error tail feeds the self-repair loop."""
    if not (code or "").strip():
        return False, "empty code"
    src = f"{code}\n\n{tests}\nprint('OK')\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "cand.py"
        f.write_text(src)
        try:
            r = subprocess.run([sys.executable, str(f)], capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        if r.returncode == 0 and "OK" in r.stdout:
            return True, ""
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (err[-1] if err else "failed")[:200]
