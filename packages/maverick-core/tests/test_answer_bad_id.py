"""`maverick answer <id>` must not report success for a non-existent question.

A typo'd question id used to print 'answered #99999' and exit 0 (the UPDATE
silently matched zero rows). world.answer() now returns whether a row matched
so the CLI can flag the bad id.
"""
from __future__ import annotations

from pathlib import Path

from maverick.world_model import open_world


def test_world_answer_returns_match_flag(tmp_path: Path):
    w = open_world(tmp_path / "world.db")
    gid = w.create_goal("g", "")
    qid = w.ask("Q?", goal_id=gid)
    assert w.answer(qid, "a") is True
    assert w.answer(999999, "a") is False


def test_world_answer_owner_scope_is_atomic(tmp_path: Path):
    w = open_world(tmp_path / "world.db")
    gid = w.create_goal("private", owner="agent:alpha")
    qid = w.ask("Private question?", goal_id=gid)

    assert w.answer(qid, "stolen", expected_owner="agent:bravo") is False
    assert [q.id for q in w.open_questions(gid)] == [qid]
    assert w.answer(qid, "mine", expected_owner="agent:alpha") is True
    assert w.open_questions(gid) == []
