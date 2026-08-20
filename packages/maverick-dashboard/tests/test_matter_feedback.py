"""Attorney feedback is encrypted, matter-scoped, and fail-closed."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal


@pytest.fixture
def feedback_world(tmp_path, monkeypatch):
    from maverick import world_model
    from maverick_dashboard import api, app, auth

    db = tmp_path / "world.db"
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", bytes(range(32)).hex())
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    world = world_model.WorldModel(db)
    monkeypatch.setattr(api, "_world", lambda: world)
    monkeypatch.setattr(app, "_world", lambda: world)
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
    monkeypatch.setattr(
        auth,
        "global_role_for_principal",
        lambda _principal: "operator",
    )
    return world, db, TestClient(app.app)


def _headers(user: str, *, post: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


def _goal(world, user: str, title: str) -> int:
    matter_id = world.create_project(title + " matter", owner=f"user:{user}")
    return world.create_goal(
        title,
        "",
        owner=f"user:{user}",
        project_id=matter_id,
        domain="legal_obligations",
    )


def test_feedback_note_is_encrypted_and_read_through_current_matter_acl(
    feedback_world,
):
    world, db, client = feedback_world
    goal_id = _goal(world, "alice", "Privileged renewal")
    note = "UNIQUE privileged client feedback 7f55d9"

    written = client.post(
        f"/api/v1/goals/{goal_id}/feedback",
        json={"rating": "down", "note": note},
        headers=_headers("alice", post=True),
    )
    read = client.get(
        f"/api/v1/goals/{goal_id}/feedback",
        headers=_headers("alice"),
    )

    assert written.status_code == 200
    assert read.status_code == 200
    assert read.json()["feedback"]["note"] == note
    assert note.encode() not in db.read_bytes()
    with sqlite3.connect(db) as connection:
        sealed = connection.execute(
            "SELECT note FROM goal_feedback WHERE goal_id = ?", (goal_id,)
        ).fetchone()[0]
    assert str(sealed).startswith("MVKAR1:")


def test_feedback_has_no_cross_matter_read_or_write(feedback_world):
    world, _db, client = feedback_world
    alice_goal = _goal(world, "alice", "Alice only")
    _goal(world, "bob", "Bob only")

    for method in ("get", "post"):
        kwargs = {"headers": _headers("bob", post=method == "post")}
        if method == "post":
            kwargs["json"] = {"rating": "up", "note": "probe"}
        response = getattr(client, method)(
            f"/api/v1/goals/{alice_goal}/feedback", **kwargs
        )
        assert response.status_code == 404

    assert world.matter_feedback_for_goal(
        alice_goal, principal="user:alice"
    ) is None


def test_feedback_persistence_failure_returns_503_and_never_grounds(
    feedback_world, monkeypatch,
):
    world, _db, client = feedback_world
    goal_id = _goal(world, "alice", "Write failure")
    grounded: list[tuple] = []

    def refuse(*_args, **_kwargs):
        raise OSError("simulated durable write refusal")

    monkeypatch.setattr(world, "record_matter_feedback", refuse)
    monkeypatch.setattr(
        "maverick_dashboard.api._ground_outcome",
        lambda *args, **kwargs: grounded.append((args, kwargs)),
    )
    response = client.post(
        f"/api/v1/goals/{goal_id}/feedback",
        json={"rating": "up", "note": "must not disappear"},
        headers=_headers("alice", post=True),
    )

    assert response.status_code == 503
    assert grounded == []
    assert world.matter_feedback_for_goal(
        goal_id, principal="user:alice"
    ) is None
