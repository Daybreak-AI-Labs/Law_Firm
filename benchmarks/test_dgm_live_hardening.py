"""Tests for the two hardening fixes that stop the failure modes which wasted
money in a live session:

  1. an exclusive run-lock so two DGM runs can NEVER share a corpus (the bug
     that let an orphaned run + a relaunch double-spend and cross-corrupt repos);
  2. a pre-run reset of every repo to its EXACT base_commit (so a prior killed
     run's throwaway fixture commit can't leave an invalid checkout).

Pure local git + filesystem -- no network, no agent, no spend.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

import dgm_live  # noqa: E402

# --- run-lock ------------------------------------------------------------------

def test_second_lock_on_same_path_is_refused(tmp_path):
    lock = tmp_path / "run.lock"
    fh1 = dgm_live._acquire_run_lock(lock)
    try:
        with pytest.raises(dgm_live.RunLockHeld) as ei:
            dgm_live._acquire_run_lock(lock)
        assert str(fh1 and "pid") in str(ei.value) or "already active" in str(ei.value)
    finally:
        fh1.close()


def test_lock_is_released_on_close_and_reacquirable(tmp_path):
    lock = tmp_path / "run.lock"
    fh1 = dgm_live._acquire_run_lock(lock)
    fh1.close()                          # simulate the holding run exiting
    fh2 = dgm_live._acquire_run_lock(lock)   # must succeed now
    fh2.close()


def test_lock_records_holder_pid(tmp_path):
    import os
    lock = tmp_path / "run.lock"
    fh = dgm_live._acquire_run_lock(lock)
    try:
        assert lock.read_text().strip() == str(os.getpid())
    finally:
        fh.close()


# --- reset repos to base_commit -----------------------------------------------

def _git(*a, cwd):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": __import__("os").environ.get("PATH", "")}
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True,
                          text=True, env=env, check=True)


def _make_repo(tmp_path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "src.py").write_text("x = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, base


def test_reset_restores_repo_left_on_a_fixture_commit(tmp_path):
    repo, base = _make_repo(tmp_path)
    # simulate a killed run: a throwaway fixture commit ON TOP of base, plus an
    # untracked file and a dirty edit -- exactly the corrupt state we saw.
    (repo / "test_x.py").write_text("assert True\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "swebench: grader test fixture (throwaway)", cwd=repo)
    (repo / "src.py").write_text("x = 999  # dirty edit\n")
    (repo / "junk.txt").write_text("untracked\n")
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() != base

    manifest = tmp_path / "m.jsonl"
    manifest.write_text(json.dumps(
        {"instance_id": "x__y-1", "repo_path": str(repo), "base_commit": base}) + "\n")

    n_ok, problems = dgm_live._reset_repos_to_base(manifest)
    assert n_ok == 1 and problems == []
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() == base   # back to base
    assert (repo / "src.py").read_text() == "x = 1\n"                    # dirty edit gone
    assert not (repo / "test_x.py").exists()                            # fixture gone
    assert not (repo / "junk.txt").exists()                            # untracked cleaned


def test_reset_reports_a_missing_base_commit(tmp_path):
    manifest = tmp_path / "m.jsonl"
    manifest.write_text(json.dumps(
        {"instance_id": "x__y-1", "repo_path": str(tmp_path / "nope")}) + "\n")
    n_ok, problems = dgm_live._reset_repos_to_base(manifest)
    assert n_ok == 0 and any("no base_commit" in p for p in problems)


def test_reset_reports_a_missing_repo(tmp_path):
    manifest = tmp_path / "m.jsonl"
    manifest.write_text(json.dumps(
        {"instance_id": "x__y-1", "repo_path": str(tmp_path / "gone"),
         "base_commit": "0" * 40}) + "\n")
    n_ok, problems = dgm_live._reset_repos_to_base(manifest)
    assert n_ok == 0 and any("repo missing" in p for p in problems)
