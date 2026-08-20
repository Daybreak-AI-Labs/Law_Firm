"""REST goal admission and discovery obey the client-matter ethical wall."""
from __future__ import annotations

import maverick_dashboard.auth as auth
import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal
from maverick_dashboard.app import app

client = TestClient(app)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maverick import world_model
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    api_mod._world_cache.clear()
    app_mod._world_cache.clear()
    return world_model.WorldModel(db)


@pytest.fixture(autouse=True)
def oidc_operator(monkeypatch):
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    def _verify(token, **_kwargs):
        return VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token},
        )

    monkeypatch.setattr(auth, "verify_oidc_token", _verify)
    from maverick_dashboard import rbac

    monkeypatch.setattr(rbac, "default_role", lambda: "operator")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("MAVERICK_DASHBOARD_MAX_GOALS_PER_MIN", "1000")

    async def _do_not_dispatch(*_args, **_kwargs):
        assert _kwargs.get("concurrency_principal", "").startswith("user:")
        return "queued"

    import maverick.runner as runner

    monkeypatch.setattr(runner, "run_goal_in_background_async", _do_not_dispatch)


def _as(user: str, *, post: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


def _legal_goal(project_id: int, **overrides) -> dict:
    body = {
        "title": "Prepare privileged case analysis",
        "project_id": project_id,
        "domain": "legal_investigations",
    }
    body.update(overrides)
    return body


def test_authenticated_create_is_atomically_bound_to_matter_and_domain(world):
    project_id = world.create_project("Vance v. Ortiz", owner="user:alice")

    response = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id),
        headers=_as("alice", post=True),
    )

    assert response.status_code == 201, response.text
    assert response.json()["project_id"] == project_id
    goal = world.get_goal(response.json()["id"])
    assert goal is not None
    assert goal.owner == "user:alice"
    assert goal.project_id == project_id
    assert goal.domain == "legal_investigations"


def test_shipped_base_legal_profile_is_admitted(world):
    project_id = world.create_project("Research matter", owner="user:alice")

    response = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id, domain="legal"),
        headers=_as("alice", post=True),
    )

    assert response.status_code == 201, response.text
    assert world.get_goal(response.json()["id"]).domain == "legal"


def test_revocation_between_request_check_and_insert_writes_no_goal(
    world, monkeypatch,
):
    from maverick import world_model

    project_id = world.create_project("Race matter", owner="user:alice")
    world.add_project_member(project_id, "user:bob", "staff", added_by="user:alice")
    real_create = world_model.WorldModel.create_matter_goal

    def _revoke_at_durable_seam(self, *args, **kwargs):
        assert self.deactivate_project_member(project_id, "user:bob") is True
        return real_create(self, *args, **kwargs)

    monkeypatch.setattr(
        world_model.WorldModel,
        "create_matter_goal",
        _revoke_at_durable_seam,
    )

    response = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id),
        headers=_as("bob", post=True),
    )

    assert response.status_code == 404
    assert world.list_goals() == []


def test_authenticated_chat_create_uses_same_atomic_matter_boundary(world):
    project_id = world.create_project("Chat matter", owner="user:alice")

    response = client.post(
        "/chat/send",
        data={
            "title": "Draft client advice",
            "project_id": str(project_id),
            "domain": "legal_investigations",
        },
        headers=_as("alice", post=True),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    goal = world.list_goals()[0]
    assert goal.owner == "user:alice"
    assert goal.project_id == project_id
    assert goal.domain == "legal_investigations"


@pytest.mark.parametrize(
    "data",
    [
        {"title": "Missing matter", "domain": "legal"},
        {"title": "Missing domain", "project_id": "placeholder"},
    ],
)
def test_authenticated_chat_create_rejects_loose_work(world, data):
    project_id = world.create_project("Chat matter", owner="user:alice")
    if data.get("project_id"):
        data = {**data, "project_id": str(project_id)}

    response = client.post(
        "/chat/send",
        data=data,
        headers=_as("alice", post=True),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert world.list_goals() == []


def test_authenticated_chat_form_lists_only_accessible_matters(world):
    own = world.create_project("Alice Client", owner="user:alice")
    world.create_project("Bob Secret Client", owner="user:bob")

    response = client.get("/chat", headers=_as("alice"))

    assert response.status_code == 200
    assert f'<option value="{own}">' in response.text
    assert "Alice Client</option>" in response.text
    assert "Bob Secret Client" not in response.text
    assert 'name="domain"' in response.text
    assert "legal investigations" in response.text.lower()


def test_authenticated_compose_is_atomically_filed_before_dispatch(world):
    project_id = world.create_project("Builder Matter", owner="user:alice")

    response = client.post(
        "/api/v1/goals/compose",
        json={
            "title": "Build a privileged chronology",
            "project_id": project_id,
            "domain": "legal_investigations",
            "steps": ["Collect the dated communications"],
        },
        headers=_as("alice", post=True),
    )

    assert response.status_code == 201, response.text
    goal = world.get_goal(response.json()["id"])
    assert goal.owner == "user:alice"
    assert goal.project_id == project_id
    assert goal.domain == "legal_investigations"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "No matter", "domain": "legal"},
        {"title": "No specialist", "project_id": 1},
    ],
)
def test_authenticated_compose_rejects_loose_work(world, body):
    if body.get("project_id"):
        body = {
            **body,
            "project_id": world.create_project("Matter", owner="user:alice"),
        }

    response = client.post(
        "/api/v1/goals/compose",
        json=body,
        headers=_as("alice", post=True),
    )

    assert response.status_code == 400
    assert world.list_goals() == []


def test_authenticated_compose_rejects_nonmember(world):
    project_id = world.create_project("Bob Matter", owner="user:bob")

    response = client.post(
        "/api/v1/goals/compose",
        json={
            "title": "Cross the ethical wall",
            "project_id": project_id,
            "domain": "legal",
        },
        headers=_as("alice", post=True),
    )

    assert response.status_code == 404
    assert world.list_goals() == []


def test_authenticated_child_inherits_matter_and_domain_atomically(world):
    project_id = world.create_project("Parent matter", owner="user:alice")
    parent_id = world.create_matter_goal(
        "Parent analysis",
        principal="user:alice",
        domain="legal",
        project_id=project_id,
    )

    response = client.post(
        f"/api/v1/goals/{parent_id}/children",
        json={"title": "Research sub-issue"},
        headers=_as("alice", post=True),
    )

    assert response.status_code == 201, response.text
    child = world.get_goal(response.json()["id"])
    assert child.parent_id == parent_id
    assert child.owner == "user:alice"
    assert child.project_id == project_id
    assert child.domain == "legal"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"title": "loose", "domain": "legal_investigations"}, "project_id"),
        ({"title": "generic", "project_id": 1}, "legal specialist domain"),
    ],
)
def test_authenticated_create_rejects_matterless_or_domainless(
    world, body, detail,
):
    if body.get("project_id"):
        project_id = world.create_project("Matter", owner="user:alice")
        body = {**body, "project_id": project_id}

    response = client.post(
        "/api/v1/goals", json=body, headers=_as("alice", post=True),
    )

    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert world.list_goals() == []


def test_nonmember_and_revoked_member_cannot_create_in_matter(world):
    project_id = world.create_project("Restricted matter", owner="user:alice")

    nonmember = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id),
        headers=_as("bob", post=True),
    )
    assert nonmember.status_code == 404

    world.add_project_member(project_id, "user:bob", "staff", added_by="user:alice")
    assert world.deactivate_project_member(project_id, "user:bob") is True
    revoked = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id),
        headers=_as("bob", post=True),
    )
    assert revoked.status_code == 404
    assert world.list_goals() == []


def test_unknown_and_nonlegal_domains_fail_before_goal_insert(world):
    project_id = world.create_project("Matter", owner="user:alice")

    unknown = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id, domain="legal_not_a_real_specialist"),
        headers=_as("alice", post=True),
    )
    nonlegal = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id, domain="finance_cashflow"),
        headers=_as("alice", post=True),
    )

    assert unknown.status_code == 400
    assert "unknown specialist" in unknown.json()["detail"]
    assert nonlegal.status_code == 400
    assert "unknown specialist" in nonlegal.json()["detail"]
    assert world.list_goals() == []


def test_legal_domain_without_terminal_gate_fails_closed(world, monkeypatch):
    from maverick import domain as domain_mod

    project_id = world.create_project("Matter", owner="user:alice")
    ungated = domain_mod.DomainProfile(
        name="legal_ungated_test",
        workflow=[domain_mod.WorkflowStep(name="draft", gate=None)],
    )
    monkeypatch.setattr(
        domain_mod,
        "enabled_domains",
        lambda: {"legal_ungated_test": ungated},
    )

    response = client.post(
        "/api/v1/goals",
        json=_legal_goal(project_id, domain="legal_ungated_test"),
        headers=_as("alice", post=True),
    )

    assert response.status_code == 400
    assert "end with a review or approval gate" in response.json()["detail"]
    assert world.list_goals() == []


def test_global_admin_has_no_matter_bypass_for_create_list_or_search(
    world, monkeypatch,
):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "user:root")
    visible_project = world.create_project("Root member matter", owner="user:alice")
    hidden_project = world.create_project("Bob-only matter", owner="user:bob")
    world.add_project_member(
        visible_project, "user:root", "viewer", added_by="user:alice",
    )
    visible = world.create_goal(
        "ethical-wall needle visible",
        owner="user:alice",
        domain="legal_investigations",
        project_id=visible_project,
    )
    hidden = world.create_goal(
        "ethical-wall needle hidden",
        owner="user:bob",
        domain="legal_investigations",
        project_id=hidden_project,
    )

    listed = client.get("/api/v1/goals", headers=_as("root"))
    searched = client.get(
        "/api/v1/goals/search",
        params={"q": "ethical-wall needle"},
        headers=_as("root"),
    )
    denied_create = client.post(
        "/api/v1/goals",
        json=_legal_goal(hidden_project),
        headers=_as("root", post=True),
    )

    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()} == {visible}
    assert searched.status_code == 200
    assert {row["id"] for row in searched.json()} == {visible}
    assert hidden not in {row["id"] for row in searched.json()}
    assert denied_create.status_code == 404
    assert len(world.list_goals()) == 2


def test_revocation_immediately_removes_goal_from_list_and_search(world):
    project_id = world.create_project("Matter", owner="user:alice")
    world.add_project_member(project_id, "user:bob", "staff", added_by="user:alice")
    goal_id = world.create_goal(
        "revocation needle",
        owner="user:alice",
        domain="legal_investigations",
        project_id=project_id,
    )
    assert {row["id"] for row in client.get(
        "/api/v1/goals", headers=_as("bob"),
    ).json()} == {goal_id}

    assert world.deactivate_project_member(project_id, "user:bob") is True

    assert client.get("/api/v1/goals", headers=_as("bob")).json() == []
    assert client.get(
        "/api/v1/goals/search",
        params={"q": "revocation needle"},
        headers=_as("bob"),
    ).json() == []


def test_authenticated_resume_refuses_legacy_unfiled_goal(world):
    goal_id = world.create_goal(
        "Legacy loose work",
        owner="user:alice",
        domain="legal",
    )
    world.set_goal_status(goal_id, "failed", result="retry me")

    response = client.post(
        f"/api/v1/goals/{goal_id}/resume",
        headers=_as("alice", post=True),
    )

    assert response.status_code == 400
    assert world.get_goal(goal_id).status == "failed"


def test_authenticated_resume_rechecks_current_matter_filing(world):
    project_id = world.create_project("Retry Matter", owner="user:alice")
    goal_id = world.create_matter_goal(
        "Filed retry",
        principal="user:alice",
        domain="legal",
        project_id=project_id,
    )
    world.set_goal_status(goal_id, "failed", result="retry me")

    response = client.post(
        f"/api/v1/goals/{goal_id}/resume",
        headers=_as("alice", post=True),
    )

    assert response.status_code == 204, response.text
    assert world.get_goal(goal_id).status == "pending"
