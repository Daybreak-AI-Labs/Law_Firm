#!/usr/bin/env python3
"""FREE pre-paid smoke test for best-of-N. Exercises the EXACT code path that a
paid best-of-N run depends on -- the orchestrator's workdir diff-capture and
reset -- on a REAL staged repo, with NO LLM and NO spend. If capture comes back
empty (the blobless-clone `partialclonefilter` guard bug that silently produced
universal [EMPTY] and burned real money), this exits non-zero so the launcher
aborts BEFORE spending. Run before any MAVERICK_BEST_OF_N run.

Usage:  python3 bon_smoke.py <staged_repo_dir> [more_repo_dirs...]
Exit 0 only if best-of-N can capture AND reset on every repo given.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "packages" / "maverick-core"))

from maverick.orchestrator import (  # noqa: E402
    _capture_workdir_diff,
    _reset_workdir_to_head,
)

_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
_SAFE_GIT_CONFIG = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]


def _smoke_one(repo: Path) -> tuple[bool, str]:
    """Edit a real tracked file, confirm capture sees it and reset reverts it.
    Restores the repo afterward. Returns (ok, detail)."""
    if not (repo / ".git").exists():
        return False, "not a git repo"
    # pick a tracked python file to perturb
    try:
        out = subprocess.run(["git", *_SAFE_GIT_CONFIG, "-C", str(repo), "ls-files", "*.py"],
                             capture_output=True, text=True, timeout=30, env=_ENV)
        files = [f for f in out.stdout.splitlines() if f.strip()]
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"ls-files failed: {e}"
    if not files:
        return False, "no tracked .py file to perturb"
    target = repo / files[0]
    original = target.read_bytes()
    marker = b"\n# maverick-bon-smoke-probe\n"
    try:
        target.write_bytes(original + marker)
        diff = _capture_workdir_diff(repo)
        captured = bool(diff.strip()) and "maverick-bon-smoke-probe" in diff
        _reset_workdir_to_head(repo)
        reverted = target.read_bytes() == original
    finally:
        # hard-restore no matter what, so the smoke test never mutates staging
        if target.read_bytes() != original:
            target.write_bytes(original)
    if not captured:
        return False, "CAPTURE RETURNED EMPTY (best-of-N would produce [EMPTY])"
    if not reverted:
        return False, "reset did not revert the edit"
    return True, f"captured {len(diff)}B + reset OK on {files[0]}"


def main(argv: list[str]) -> int:
    repos = [Path(a) for a in argv]
    if not repos:
        print("usage: bon_smoke.py <staged_repo_dir> [more...]", file=sys.stderr)
        return 2
    all_ok = True
    for repo in repos:
        ok, detail = _smoke_one(repo)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {repo.name}: {detail}")
        all_ok = all_ok and ok
    if all_ok:
        print("BON SMOKE PASSED: best-of-N capture+reset works on the real staging.")
        return 0
    print("BON SMOKE FAILED: best-of-N is broken on this staging -- DO NOT spend. "
          "Send this to Claude.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
