"""Legal signoff and its signed-audit intent share one durable transaction."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest
from maverick.world_model import WorldModel


def _done_goal(world: WorldModel) -> tuple[int, str]:
    matter_id = world.create_project("Client matter", owner="user:counsel")
    result = "Privileged reviewed advice"
    goal_id = world.create_goal(
        "Advice",
        owner="user:counsel",
        domain="legal_obligations",
        project_id=matter_id,
    )
    world.set_goal_status(goal_id, "done", result=result)
    return goal_id, result


def test_signoff_queues_exact_digest_and_delivery_is_idempotent(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    goal_id, result = _done_goal(world)
    version = world.get_goal(goal_id).updated_at

    assert world.record_signoff(
        goal_id,
        "approved",
        decided_by="user:counsel",
        expected_updated_at=version,
    ) is True
    event = world.current_signoff_audit_event(goal_id)
    assert event is not None
    assert event.event_id.startswith("legal-signoff-v1-")
    assert event.deliverable_updated_at == version
    assert event.deliverable_sha256 == hashlib.sha256(result.encode()).hexdigest()
    assert event.delivered_at is None

    # Exact retry neither changes the signoff nor creates another event.
    assert world.record_signoff(
        goal_id,
        "approved",
        decided_by="user:counsel",
        expected_updated_at=version,
    ) is False
    assert world.current_signoff_audit_event(goal_id).event_id == event.event_id
    count = world.conn.execute(
        "SELECT COUNT(*) FROM signoff_audit_outbox WHERE goal_id = ?",
        (goal_id,),
    ).fetchone()[0]
    assert count == 1

    assert world.mark_signoff_audit_delivered(event.event_id) is True
    delivered = world.current_signoff_audit_event(goal_id)
    assert delivered is not None and delivered.delivered_at is not None
    assert world.mark_signoff_audit_delivered(event.event_id) is True
    assert world.current_signoff_audit_event(goal_id).delivered_at == delivered.delivered_at


def test_outbox_insert_failure_rolls_back_signoff(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    goal_id, _result = _done_goal(world)
    world.conn.execute(
        "CREATE TRIGGER refuse_signoff_audit BEFORE INSERT ON signoff_audit_outbox "
        "BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END"
    )
    world.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        world.record_signoff(
            goal_id,
            "approved",
            decided_by="user:counsel",
            expected_updated_at=world.get_goal(goal_id).updated_at,
        )

    assert world.signoff_for(goal_id) is None
    assert world.conn.execute(
        "SELECT COUNT(*) FROM signoff_audit_outbox"
    ).fetchone()[0] == 0
