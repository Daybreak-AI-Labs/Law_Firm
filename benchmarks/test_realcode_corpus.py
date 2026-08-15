"""The real-code uplift corpus must stay well-formed: every task's no-op stub
FAILS its hidden tests (so there is a real bug to fix), and public/hidden are
both non-empty (the agent has something to self-repair against, and something
disjoint to be graded on). Pure execution, no LLM, $0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from realcode_corpus import TASKS  # noqa: E402
from realcode_grade import run_tests  # noqa: E402


def test_corpus_is_well_formed():
    assert len(TASKS) >= 10
    ids = [t.instance_id for t in TASKS]
    assert len(ids) == len(set(ids)), "duplicate instance_id"
    for t in TASKS:
        assert t.public_tests.strip(), f"{t.instance_id}: empty public tests"
        assert t.hidden_tests.strip(), f"{t.instance_id}: empty hidden tests"
        # the stub must NOT already pass the hidden tests -- otherwise there is
        # no bug and the task is free (would inflate the baseline solve-rate).
        ok, _ = run_tests(t.buggy_code, t.hidden_tests)
        assert not ok, f"{t.instance_id}: stub already passes hidden tests (no real bug)"
