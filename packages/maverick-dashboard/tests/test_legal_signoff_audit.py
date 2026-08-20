"""Attorney signoff is non-releasable until its exact audit outbox row lands."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_TABLE = "| Duty | Status |\n| --- | --- |\n| Preserve | reviewed |\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maverick import world_model
    from maverick_dashboard import _shared
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    _shared._world_cache.clear()
    instance = world_model.WorldModel(db)
    monkeypatch.setattr(api_mod, "_world", lambda: instance)
    monkeypatch.setattr(app_mod, "_world", lambda: instance)
    return instance


@pytest.fixture
def client(world):
    from maverick_dashboard.app import app

    return TestClient(app, headers={"Origin": "http://testserver"})


def _goal(world, *, owner: str = "operator") -> int:
    matter_id = world.create_client_matter(
        "Client matter",
        principal=owner,
        domain="legal_obligations",
        matter_number="AUDIT-001",
        jurisdiction="Tennessee",
        client_name="Audit Signoff Client",
    )
    goal_id = world.create_goal(
        "Review obligations",
        owner=owner,
        domain="legal_obligations",
        project_id=matter_id,
    )
    world.set_goal_status(goal_id, "done", result=_TABLE)
    return goal_id


def _decision(world, goal_id: int) -> dict:
    return {
        "decision": "approved",
        "expected_updated_at": world.get_goal(goal_id).updated_at,
        "note": "Attorney reviewed the source record.",
    }


def test_audit_failure_leaves_signoff_pending_and_release_closed(
    client, world, monkeypatch,
):
    goal_id = _goal(world)
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "maverick.audit.audit_event",
        lambda *args, **kwargs: captured.append((args, kwargs)) and False,
    )

    first = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_decision(world, goal_id),
    )
    assert first.status_code == 503
    assert world.signoff_for(goal_id)["decision"] == "approved"
    event = world.current_signoff_audit_event(goal_id)
    assert event is not None and event.delivered_at is None
    assert client.post(f"/api/v1/goals/{goal_id}/share").status_code == 503
    assert world.share_links_for_goal(goal_id) == []

    monkeypatch.setattr(
        "maverick.audit.audit_event",
        lambda *args, **kwargs: (captured.append((args, kwargs)), True)[1],
    )
    retry = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_decision(world, goal_id),
    )
    assert retry.status_code == 200, retry.text
    delivered = world.current_signoff_audit_event(goal_id)
    assert delivered is not None and delivered.delivered_at is not None
    assert client.post(f"/api/v1/goals/{goal_id}/share").status_code == 201

    payloads = [kwargs for _args, kwargs in captured]
    assert any(p.get("event_id") == delivered.event_id for p in payloads)
    assert all("result" not in p and "note" not in p for p in payloads)
    assert all(_TABLE not in str(p) for p in payloads)


def test_shared_static_bearer_can_never_certify_legal_work(
    client, world, monkeypatch,
):
    from maverick_dashboard import rbac

    principal = "user:dashboard-static-bearer"
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    rbac.set_role(principal, "attorney")
    goal_id = _goal(world, owner="user:counsel")

    response = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_decision(world, goal_id),
        headers={
            "Authorization": "Bearer shared-secret",
            "Origin": "http://testserver",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "no such goal"
    assert world.signoff_for(goal_id) is None
