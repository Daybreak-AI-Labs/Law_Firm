"""Legal releases require both global qualification and exact matter authority."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal

_TABLE = "| Item | Value |\n| --- | --- |\n| Advice | privileged |\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maverick import world_model
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    instance = world_model.WorldModel(db)
    monkeypatch.setattr(api_mod, "_world", lambda: instance)
    monkeypatch.setattr(app_mod, "_world", lambda: instance)
    return instance


@pytest.fixture(autouse=True)
def authenticated_attorneys(monkeypatch):
    from maverick_dashboard import auth, rbac

    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    def _verify(token, **_kwargs):
        return VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token},
        )

    monkeypatch.setattr(auth, "verify_oidc_token", _verify)
    monkeypatch.setattr(rbac, "default_role", lambda: "attorney")
    monkeypatch.setattr(
        auth,
        "qualified_attorney_policy",
        lambda: (True, frozenset({"user:alice", "user:bob"})),
    )


@pytest.fixture
def client():
    from maverick_dashboard.app import app

    return TestClient(app)


def _headers(user: str, *, post: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


def _goal(world, *, bob_role: str | None = None) -> tuple[int, int]:
    project_id = world.create_client_matter(
        "Client v. Counterparty",
        principal="user:alice",
        domain="legal_investigations",
        matter_number="2026-ATTY-001",
        jurisdiction="Tennessee",
        client_name="Attorney Release Client",
        adverse_parties=("Attorney Release Counterparty",),
    )
    if bob_role is not None:
        world.add_project_member(
            project_id,
            "user:bob",
            bob_role,
            added_by="user:alice",
        )
    goal_id = world.create_goal(
        "Privileged advice",
        "",
        owner="user:alice",
        domain="legal_investigations",
        project_id=project_id,
    )
    world.set_goal_status(goal_id, "done", result=_TABLE)
    return project_id, goal_id


def _signoff_body(world, goal_id: int) -> dict:
    return {
        "decision": "approved",
        "expected_updated_at": world.get_goal(goal_id).updated_at,
    }


def test_staff_with_global_attorney_permission_cannot_sign_matter_work(
    client, world,
):
    _project_id, goal_id = _goal(world, bob_role="staff")

    response = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("bob", post=True),
    )

    assert response.status_code == 403
    assert world.signoff_for(goal_id) is None


def test_nonmember_global_admin_has_no_matter_signoff_bypass(
    client, world, monkeypatch,
):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "user:root")
    _project_id, goal_id = _goal(world)

    response = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("root", post=True),
    )

    assert response.status_code == 404
    assert world.signoff_for(goal_id) is None


def test_only_current_matter_attorney_can_mint_share_or_export_csv(client, world):
    _project_id, goal_id = _goal(world, bob_role="staff")
    signed = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("alice", post=True),
    )
    assert signed.status_code == 200, signed.text

    staff_share = client.post(
        f"/api/v1/goals/{goal_id}/share",
        headers=_headers("bob", post=True),
    )
    staff_csv = client.get(
        f"/api/v1/goals/{goal_id}/deliverable.csv",
        headers=_headers("bob"),
    )
    attorney_share = client.post(
        f"/api/v1/goals/{goal_id}/share",
        headers=_headers("alice", post=True),
    )
    attorney_csv = client.get(
        f"/api/v1/goals/{goal_id}/deliverable.csv",
        headers=_headers("alice"),
    )

    assert staff_share.status_code == 403
    assert staff_csv.status_code == 403
    assert attorney_share.status_code == 201, attorney_share.text
    assert attorney_csv.status_code == 200, attorney_csv.text


def test_release_policy_rechecks_original_signers_current_matter_role(
    client, world, monkeypatch,
):
    project_id, goal_id = _goal(world, bob_role="attorney")
    signed = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("alice", post=True),
    )
    assert signed.status_code == 200
    real_role = world.project_member_role

    def _role(pid: int, principal: str):
        if pid == project_id and principal == "user:alice":
            return "staff"
        return real_role(pid, principal)

    monkeypatch.setattr(world, "project_member_role", _role)

    response = client.get(
        f"/api/v1/goals/{goal_id}/deliverable.csv",
        headers=_headers("bob"),
    )

    assert response.status_code == 403
    assert "approving attorney" in response.json()["detail"]


def test_demotion_invalidates_existing_signoff(client, world):
    project_id, goal_id = _goal(world, bob_role="attorney")
    signed = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("bob", post=True),
    )
    assert signed.status_code == 200

    world.add_project_member(
        project_id,
        "user:bob",
        "staff",
        added_by="user:alice",
    )

    assert world.signoff_for(goal_id) is None
    response = client.post(
        f"/api/v1/goals/{goal_id}/share",
        headers=_headers("alice", post=True),
    )
    assert response.status_code == 403


def test_revocation_closes_an_already_minted_public_link(client, world):
    project_id, goal_id = _goal(world)
    signed = client.post(
        f"/api/v1/goals/{goal_id}/signoff",
        json=_signoff_body(world, goal_id),
        headers=_headers("alice", post=True),
    )
    assert signed.status_code == 200
    shared = client.post(
        f"/api/v1/goals/{goal_id}/share",
        headers=_headers("alice", post=True),
    )
    assert shared.status_code == 201
    token = shared.json()["url"].split("/share/", 1)[1]

    world.add_project_member(
        project_id,
        "user:bob",
        "responsible_attorney",
        added_by="user:alice",
    )
    assert world.deactivate_project_member(project_id, "user:alice") is True

    public = TestClient(client.app).get(f"/share/{token}")
    assert public.status_code == 404
    assert "privileged" not in public.text.lower()
