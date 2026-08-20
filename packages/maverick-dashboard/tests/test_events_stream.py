"""Real-time SSE stream of goal events (/api/v1/goals/{id}/events/stream)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated_world(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    yield


def _goal_with_events(tmp_path, n, *, status=None):
    from maverick import world_model
    w = world_model.WorldModel(world_model.DEFAULT_DB)
    gid = w.create_goal("stream me", "")
    for i in range(n):
        w.append_event(gid, "coder", "status", f"step {i}")
    if status:
        w.set_goal_status(gid, status)
    return gid


def test_sse_event_format():
    from maverick_dashboard.api import _sse_event
    e = type("E", (), {"id": 7, "agent": "coder", "kind": "tool",
                       "content": "ran x", "ts": 1.0})()
    frame = _sse_event(e)
    assert frame.startswith("id: 7\n")
    assert "event: tool\n" in frame
    assert frame.endswith("\n\n")
    body = frame.split("data: ", 1)[1].split("\n", 1)[0]
    assert json.loads(body)["content"] == "ran x"


def test_stream_unknown_goal_404():
    assert client.get("/api/v1/goals/999999/events/stream").status_code == 404


def test_stream_emits_events_with_limit(tmp_path):
    gid = _goal_with_events(tmp_path, 3)
    resp = client.get(f"/api/v1/goals/{gid}/events/stream?limit=3")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert ": connected" in body
    assert body.count("event: status") == 3
    assert "step 0" in body and "step 2" in body


def test_stream_ends_on_terminal_status(tmp_path):
    gid = _goal_with_events(tmp_path, 2, status="done")
    # No limit: the stream drains the 2 events, then ends because the goal is
    # terminal. poll=0 keeps the tail loop from sleeping.
    resp = client.get(f"/api/v1/goals/{gid}/events/stream?poll=0")
    assert resp.status_code == 200
    body = resp.text
    assert body.count("event: status") == 2
    assert "event: end" in body
    assert '"status": "done"' in body


def test_stream_since_skips_old(tmp_path):
    gid = _goal_with_events(tmp_path, 3, status="done")
    # since=large -> no historical events; terminal status ends it immediately.
    resp = client.get(f"/api/v1/goals/{gid}/events/stream?since=100000&poll=0")
    assert resp.status_code == 200
    assert resp.text.count("event: status") == 0
    assert "event: end" in resp.text


def test_stream_drains_full_backlog_before_end(monkeypatch, tmp_path):
    """A terminal goal with more events than one poll batch must stream the
    WHOLE backlog before the end frame — not just the first batch."""
    from maverick_dashboard import api

    monkeypatch.setattr(api, "_SSE_MAX_BATCH", 5)
    gid = _goal_with_events(tmp_path, 12, status="done")
    resp = client.get(f"/api/v1/goals/{gid}/events/stream?poll=0")
    assert resp.status_code == 200
    body = resp.text
    assert body.count("event: status") == 12
    assert body.count("event: end") == 1
    # the end frame comes after the last backlog event, never before
    assert body.rfind("step 11") < body.rfind("event: end")


def test_stream_rejects_when_concurrency_cap_is_full(monkeypatch, tmp_path):
    from maverick_dashboard import api

    gid = _goal_with_events(tmp_path, 1)

    class FullSemaphore:
        def locked(self):
            return True

        async def acquire(self):  # pragma: no cover - route must not wait here
            raise AssertionError("should reject instead of acquiring")

    monkeypatch.setattr(api, "_get_sse_semaphore", lambda: FullSemaphore())
    resp = client.get(f"/api/v1/goals/{gid}/events/stream")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"


def test_stream_poll_parameter_does_not_control_sleep(monkeypatch, tmp_path):
    from maverick_dashboard import api

    gid = _goal_with_events(tmp_path, 0, status="done")
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)
    resp = client.get(f"/api/v1/goals/{gid}/events/stream?poll=0")
    assert resp.status_code == 200
    assert "event: end" in resp.text
    assert sleeps == []


def test_stream_stops_before_yielding_after_matter_membership_revocation(
    monkeypatch, tmp_path,
):
    """A stream admitted while authorized must not survive an ethical-wall revoke."""
    from maverick import world_model
    from maverick_dashboard import auth

    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda token, **_kwargs: VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token},
        ),
    )

    world = world_model.WorldModel(world_model.DEFAULT_DB)
    matter_id = world.create_project("Privileged matter", owner="user:alice")
    world.add_project_member(
        matter_id, "user:bob", "staff", added_by="user:alice",
    )
    goal_id = world.create_goal(
        "Privileged work", "", owner="user:alice", project_id=matter_id,
    )
    world.append_event(goal_id, "attorney", "finding", "sealed client strategy")
    world.set_goal_status(goal_id, "done")

    original_goal_events = world_model.WorldModel.goal_events
    revoked = False

    def revoke_after_read(self, *args, **kwargs):
        nonlocal revoked
        events = original_goal_events(self, *args, **kwargs)
        if not revoked:
            revoked = True
            assert self.deactivate_project_member(matter_id, "user:bob") is True
        return events

    monkeypatch.setattr(world_model.WorldModel, "goal_events", revoke_after_read)
    response = client.get(
        f"/api/v1/goals/{goal_id}/events/stream",
        headers={"Authorization": "Bearer bob"},
    )

    assert response.status_code == 200
    assert ": connected" in response.text
    assert "sealed client strategy" not in response.text
    assert "event: finding" not in response.text


def test_stream_rechecks_fresh_goal_matter_before_yield(monkeypatch, tmp_path):
    """Re-filing a goal cannot leave a stream authorized by its old matter."""
    from maverick import world_model
    from maverick_dashboard import auth

    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda token, **_kwargs: VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token},
        ),
    )

    world = world_model.WorldModel(world_model.DEFAULT_DB)
    original_matter = world.create_project("Original matter", owner="user:alice")
    new_matter = world.create_project("New matter", owner="user:carol")
    world.add_project_member(
        original_matter, "user:bob", "staff", added_by="user:alice",
    )
    goal_id = world.create_goal(
        "Privileged work", "", owner="user:alice", project_id=original_matter,
    )
    world.append_event(goal_id, "attorney", "finding", "new matter strategy")
    world.set_goal_status(goal_id, "done")

    original_goal_events = world_model.WorldModel.goal_events
    reassigned = False

    def reassign_after_read(self, *args, **kwargs):
        nonlocal reassigned
        events = original_goal_events(self, *args, **kwargs)
        if not reassigned:
            reassigned = True
            assert self.set_goal_project(goal_id, new_matter) is True
        return events

    monkeypatch.setattr(world_model.WorldModel, "goal_events", reassign_after_read)
    response = client.get(
        f"/api/v1/goals/{goal_id}/events/stream",
        headers={"Authorization": "Bearer bob"},
    )

    assert response.status_code == 200
    assert ": connected" in response.text
    assert "new matter strategy" not in response.text
    assert "event: finding" not in response.text
