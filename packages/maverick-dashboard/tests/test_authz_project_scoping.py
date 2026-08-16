"""Project ("matter") authorization: no ownerless read, no cross-project filing.

Three holes, all in the same surface:

1. ``projects_create`` stored the owner from ``goal_owner_filter``, which
   returns None for an admin -- so every admin-created project was stored
   OWNERLESS.
2. ``project_detail`` admitted ``owner in ("", caller)``, so those ownerless
   projects were readable by any authenticated user. ``list_projects`` filters
   on ``owner = ?`` and never showed them, which is the shape of an IDOR:
   invisible in the index, fetchable by id.
3. The goal list on that page was unfiltered, so it disclosed other users' goal
   titles and statuses; and ``goal_set_project`` authorized the goal but not the
   TARGET project, letting anyone file a goal into a stranger's project.

For a firm this is an ethical-wall question, not a cosmetic one: the project
page is where matter membership will live.

Hermetic in the style of ``test_authz_owner_scoping.py`` -- the bearer token
string is the OIDC subject, no real JWT.
"""
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

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    api_mod._world_cache.clear()
    app_mod._world_cache.clear()
    return world_model.WorldModel(db)


@pytest.fixture
def oidc(monkeypatch):
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    def _verify(token, **_kw):
        return VerifiedPrincipal(
            sub=token, issuer="https://issuer.example", audience="maverick",
            claims={"sub": token},
        )

    monkeypatch.setattr(auth, "verify_oidc_token", _verify)


def _as(user: str, *, post: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


# --- 1. an admin's project is owned, not ownerless -------------------------


def test_admin_created_project_records_the_admin_as_owner(world, oidc, monkeypatch):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    r = client.post("/projects", data={"name": "Estate of Vance"},
                    headers=_as("root", post=True), follow_redirects=False)
    assert r.status_code == 303, r.text
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    assert world.get_project(pid)["owner"] == "user:root"


# --- 2. an ownerless project is not world-readable -------------------------


def test_ownerless_project_is_not_readable_by_a_stranger(world, oidc):
    pid = world.create_project("Legacy matter", owner="")
    assert client.get(f"/projects/{pid}", headers=_as("mallory")).status_code == 404


def test_a_project_you_own_is_still_readable(world, oidc):
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("alice")).status_code == 200


def test_another_users_project_404s(world, oidc):
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("mallory")).status_code == 404


def test_admin_still_sees_every_project(world, oidc, monkeypatch):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("root")).status_code == 200


# --- 3. the goal list on the page is scoped too ----------------------------


def test_project_page_does_not_leak_another_users_goal_titles(world, oidc):
    pid = world.create_project("Shared matter", owner="user:alice")
    gid = world.create_goal("Privileged strategy memo", "", owner="user:bob")
    world.set_goal_project(gid, pid)
    mine = world.create_goal("Alice own filing", "", owner="user:alice")
    world.set_goal_project(mine, pid)

    body = client.get(f"/projects/{pid}", headers=_as("alice")).text
    assert "Alice own filing" in body
    assert "Privileged strategy memo" not in body


# --- 4. filing a goal authorizes the TARGET project ------------------------


def test_cannot_file_a_goal_into_someone_elses_project(world, oidc):
    victim = world.create_project("Ortiz matter", owner="user:alice")
    gid = world.create_goal("mallory goal", "", owner="user:mallory")

    r = client.post(f"/chat/goal/{gid}/project", data={"project_id": str(victim)},
                    headers=_as("mallory", post=True), follow_redirects=False)
    assert r.status_code == 404
    assert world.get_goal(gid).project_id is None


def test_can_file_a_goal_into_your_own_project(world, oidc):
    pid = world.create_project("Alice matter", owner="user:alice")
    gid = world.create_goal("alice goal", "", owner="user:alice")

    r = client.post(f"/chat/goal/{gid}/project", data={"project_id": str(pid)},
                    headers=_as("alice", post=True), follow_redirects=False)
    assert r.status_code == 303
    assert world.get_goal(gid).project_id == pid


def test_clearing_a_goals_project_still_works(world, oidc):
    pid = world.create_project("Alice matter", owner="user:alice")
    gid = world.create_goal("alice goal", "", owner="user:alice")
    world.set_goal_project(gid, pid)

    r = client.post(f"/chat/goal/{gid}/project", data={"project_id": ""},
                    headers=_as("alice", post=True), follow_redirects=False)
    assert r.status_code == 303
    assert world.get_goal(gid).project_id is None
