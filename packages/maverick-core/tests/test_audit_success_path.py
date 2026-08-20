"""Success-path audit: a completed run records GOAL_START/GOAL_END (and tool
calls record TOOL_CALL/TOOL_RESULT) on the audit chain -- not just the denial
events. Regression guard for the "tamper-evident log only captured denials" gap.
"""
from __future__ import annotations

import hashlib

import pytest
from maverick.budget import Budget
from maverick.orchestrator import run_goal
from maverick.sandbox import LocalBackend
from maverick.world_model import WorldModel


@pytest.fixture
def _audit_home(monkeypatch, tmp_path):
    # Point the default audit log at a temp home and drop the cached singleton so
    # record() rebuilds it against tmp instead of the real ~/.maverick.
    monkeypatch.setattr("maverick.paths.maverick_home", lambda: tmp_path / "home")
    monkeypatch.setattr("maverick.audit.writer._default", None, raising=False)
    return tmp_path


@pytest.mark.asyncio
async def test_run_goal_emits_goal_start_and_end(
    _audit_home, tmp_path, fake_llm, make_llm_response,
):
    fake_llm.scripted = [
        make_llm_response(text="FINAL: the answer is 42"),
        make_llm_response(
            text='{"confidence": 0.95, "accepts": true, "critique": "ok", "issues": []}',
        ),
        make_llm_response(text="FINAL: (no skill)"),
    ]
    world = WorldModel(path=tmp_path / "world.db")
    gid = world.create_goal("compute the answer", "trivial")

    await run_goal(
        llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
        goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
    )

    from maverick.audit.reader import iter_events

    events = [e for e in iter_events(all_days=True) if e.get("goal_id") == gid]
    kinds = {e["kind"] for e in events}
    assert "goal_start" in kinds
    assert "goal_end" in kinds
    end = next(e for e in events if e["kind"] == "goal_end")
    assert end["status"] == "succeeded"
    assert end["result_bytes"] == len(b"the answer is 42")
    assert end["result_sha256"] == hashlib.sha256(
        b"the answer is 42"
    ).hexdigest()
    assert "result" not in end
    assert "the answer is 42" not in repr(end)


@pytest.mark.asyncio
async def test_goal_start_audit_uncertainty_leaves_goal_pending(
    tmp_path, fake_llm, monkeypatch,
):
    from maverick import audit
    from maverick.audit import EventKind

    world = WorldModel(path=tmp_path / "world.db")
    matter_id = world.create_project("Privileged matter", owner="user:attorney")
    gid = world.create_goal(
        "Privileged strategy", owner="user:attorney", project_id=matter_id,
    )
    monkeypatch.setattr(
        audit,
        "audit_event",
        lambda kind, **_payload: False if kind == EventKind.GOAL_START else True,
    )

    with pytest.raises(RuntimeError, match="required GOAL_START audit"):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
        )

    goal = world.get_goal(gid)
    assert goal is not None and goal.status == "pending" and goal.result is None
    assert world.list_episodes(goal_id=gid) == []


@pytest.mark.asyncio
async def test_goal_end_audit_uncertainty_cannot_persist_privileged_result(
    tmp_path, fake_llm, make_llm_response, monkeypatch,
):
    from maverick import audit
    from maverick.audit import EventKind

    privileged = "privileged settlement floor is 875000"
    fake_llm.scripted = [
        make_llm_response(text=f"FINAL: {privileged}"),
        make_llm_response(
            text='{"confidence": 0.95, "accepts": true, "critique": "ok", '
            '"issues": []}',
        ),
        make_llm_response(text="FINAL: (no skill)"),
    ]
    world = WorldModel(path=tmp_path / "world.db")
    matter_id = world.create_project("Privileged matter", owner="user:attorney")
    gid = world.create_goal(
        "Draft settlement advice", owner="user:attorney", project_id=matter_id,
    )
    seen: list[tuple[str, dict]] = []

    def audit_event(kind, **payload):
        seen.append((kind, payload))
        return kind != EventKind.GOAL_END

    monkeypatch.setattr(audit, "audit_event", audit_event)

    with pytest.raises(RuntimeError, match="required GOAL_END audit"):
        await run_goal(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            goal_id=gid, sandbox=LocalBackend(workdir=tmp_path), max_depth=1,
        )

    goal = world.get_goal(gid)
    assert goal is not None and goal.status == "active" and goal.result is None
    end_payload = next(payload for kind, payload in seen if kind == EventKind.GOAL_END)
    assert end_payload["matter_id"] == matter_id
    assert end_payload["result_bytes"] == len(privileged.encode("utf-8"))
    assert end_payload["result_sha256"] == hashlib.sha256(
        privileged.encode("utf-8")
    ).hexdigest()
    assert "result" not in end_payload
    assert privileged not in repr(seen)
