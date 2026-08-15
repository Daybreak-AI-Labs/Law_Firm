"""LocalBackend timeout must bound the WHOLE process tree.

subprocess.run's timeout SIGKILLs only the direct /bin/sh child; grandchildren
(a backgrounded ``cmd &``, or even foreground children of a compound list --
dash forks for those too) reparented to init and kept running after exec()
returned 124, so the stated wall-clock bound never bounded host CPU/RAM. The
backend now starts the child in its own session (process group) and kills the
group on timeout, mirroring the reaping every container backend already does.
"""
from __future__ import annotations

import os
import time

import pytest
from maverick.sandbox.local import LocalBackend

pytestmark = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="POSIX process groups required",
)


def _gone(pid: int) -> bool:
    """True when ``pid`` no longer runs (vanished, or a killed zombie awaiting
    init's reap -- state ``Z`` on Linux)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:  # non-Linux POSIX: no /proc -- rely on ProcessLookupError
        return False


def test_timeout_kills_grandchildren_and_keeps_partial_output(tmp_path):
    b = LocalBackend(workdir=tmp_path)
    pidfile = tmp_path / "grandchild.pid"
    res = b.exec(
        f"sleep 300 & echo $! > {pidfile}; echo started; sleep 300",
        timeout=1.0,
    )
    assert res.exit_code == 124
    assert "TIMEOUT" in res.stderr
    # Output produced before the timeout still comes back to the agent.
    assert "started" in res.stdout

    # The backgrounded grandchild must die with the group, not outlive the
    # timeout re-parented to init (give init a moment to reap).
    pid = int(pidfile.read_text().strip())
    deadline = time.time() + 5.0
    while not _gone(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert _gone(pid), "backgrounded grandchild survived the timeout"


def test_exec_success_and_failure_paths_unchanged(tmp_path):
    b = LocalBackend(workdir=tmp_path)
    ok = b.exec("echo hi")
    assert ok.ok and ok.stdout.strip() == "hi"
    bad = b.exec("echo oops >&2; exit 3")
    assert bad.exit_code == 3
    assert bad.stderr.strip() == "oops"
