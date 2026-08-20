"""Offboarding revokes live sessions, ethical-wall ACLs, and release authority."""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal


def test_offboard_immediately_revokes_cookie_membership_and_old_signoff(
    tmp_path, monkeypatch,
):
    from maverick import world_model
    from maverick_dashboard import api, app, auth, invites, rbac

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_DASHBOARD_INVITES", "1")
    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    world = world_model.WorldModel(db)
    monkeypatch.setattr(api, "_world", lambda: world)
    monkeypatch.setattr(app, "_world", lambda: world)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr("maverick.oidc.login_enabled", lambda: False)
    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda token, **_kwargs: VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token, "iat": 1_700_000_000},
        ),
    )
    monkeypatch.setattr(
        auth,
        "qualified_attorney_policy",
        lambda: (True, frozenset({"user:alice@example.com", "user:bob"})),
    )
    monkeypatch.setattr("maverick.audit.audit_event", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "maverick_dashboard.release_policy.deliver_release_audit",
        lambda **_kwargs: "event-test",
    )
    rbac.set_role("user:admin", "admin", actor="bootstrap")
    rbac.set_role("user:alice@example.com", "attorney", actor="bootstrap")
    rbac.set_role("user:bob", "attorney", actor="bootstrap")

    matter_id = world.create_client_matter(
        "Offboarding matter",
        principal="user:alice@example.com",
        domain="legal_obligations",
        matter_number="OFF-001",
        jurisdiction="Tennessee",
        client_name="Offboarding Client",
    )
    world.add_project_member(
        matter_id,
        "user:bob",
        "responsible_attorney",
        added_by="user:alice@example.com",
    )
    goal_id = world.create_matter_goal(
        "Review renewal",
        "",
        principal="user:alice@example.com",
        domain="legal_obligations",
        project_id=matter_id,
    )
    assert goal_id is not None
    world.set_goal_status(goal_id, "done", result="approved draft")
    current = world.get_goal(goal_id)
    assert current is not None
    assert world.record_signoff(
        goal_id,
        "approved",
        decided_by="user:alice@example.com",
        expected_updated_at=current.updated_at,
    )

    alice = TestClient(app.app)
    alice.cookies.set("mvk_session", invites.mint_local_session("alice@example.com"))
    assert alice.get(f"/projects/{matter_id}").status_code == 200

    admin = TestClient(app.app, headers={"Authorization": "Bearer admin"})
    offboard = admin.post(
        "/users/remove",
        data={"principal": "user:alice@example.com"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert offboard.status_code == 303
    assert world.project_member_role(matter_id, "user:alice@example.com") is None
    assert world.signoff_for(goal_id) is None
    assert alice.get(f"/projects/{matter_id}").status_code == 401

    bob = TestClient(app.app, headers={"Authorization": "Bearer bob"})
    release = bob.post(f"/api/v1/goals/{goal_id}/share")
    assert release.status_code == 403, release.text
    assert "sign-off" in release.json()["detail"]
