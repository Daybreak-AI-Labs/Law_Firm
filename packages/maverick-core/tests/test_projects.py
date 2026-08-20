"""Projects ("matters"): a workspace grouping related goals (schema v19)."""
from __future__ import annotations

import sqlite3

import pytest
from maverick.world_model import SCHEMA_VERSION, WorldModel


def test_create_list_and_count(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("Q3 Close", description="quarterly close", owner="user:a", domain="finance_gl_close")
    p = w.get_project(pid)
    assert p["name"] == "Q3 Close" and p["description"] == "quarterly close"
    assert p["domain"] == "finance_gl_close" and p["status"] == "active"
    assert p["egress_mode"] == "local_only"
    w.create_goal("Reconcile", domain="finance_gl_close", project_id=pid)
    w.create_goal("Flux", domain="finance_gl_close", project_id=pid)
    listed = w.list_projects()
    assert listed[0]["id"] == pid and listed[0]["goal_count"] == 2


def test_list_goals_project_filter_and_filing(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("P")
    a = w.create_goal("a", project_id=pid)
    b = w.create_goal("b")  # unfiled
    assert {g.id for g in w.list_goals(project_id=pid)} == {a}
    assert w.get_goal(b).project_id is None
    w.set_goal_project(b, pid)                         # file it
    assert {g.id for g in w.list_goals(project_id=pid)} == {a, b}
    w.set_goal_project(a, None)                        # unfile it
    assert {g.id for g in w.list_goals(project_id=pid)} == {b}


def test_status_counts(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("P")
    g1 = w.create_goal("a", project_id=pid)
    w.create_goal("b", project_id=pid)
    w.set_goal_status(g1, "done", result="ok")
    assert w.project_status_counts(pid) == {"done": 1, "pending": 1}


def test_owner_scoped_listing(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    w.create_project("mine", owner="user:a")
    w.create_project("theirs", owner="user:b")
    assert [p["name"] for p in w.list_projects(owner="user:a")] == ["mine"]
    assert len(w.list_projects()) == 2  # None = all


def test_creator_becomes_responsible_attorney_and_membership_scopes_lists(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    mine = w.create_project("Mine", owner="user:alice")
    other = w.create_project("Other", owner="user:bob")
    assert w.project_member_role(mine, "user:alice") == "responsible_attorney"
    assert w.project_member_role(other, "user:alice") is None
    assert [p["id"] for p in w.list_projects(principal="user:alice")] == [mine]

    shared = w.create_goal("Shared strategy", owner="user:bob", project_id=mine)
    unfiled = w.create_goal("Alice scratch", owner="user:alice")
    visible = w.list_goals(accessible_by="user:alice")
    assert {g.id for g in visible} == {shared, unfiled}


def test_matter_egress_mode_is_default_deny_and_responsible_attorney_only(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("Client matter", owner="user:lead")
    w.add_project_member(pid, "user:reviewer", "attorney", added_by="user:lead")

    assert w.get_project(pid)["egress_mode"] == "local_only"
    assert w.set_project_egress_mode(
        pid, "approved_services", principal="user:reviewer"
    ) is False
    assert w.set_project_egress_mode(
        pid, "approved_services", principal="user:admin"
    ) is False
    assert w.set_project_egress_mode(
        pid, "approved_services", principal="user:lead"
    ) is True
    assert w.get_project(pid)["egress_mode"] == "approved_services"
    with pytest.raises(ValueError, match="egress_mode"):
        w.set_project_egress_mode(pid, "anything", principal="user:lead")
    with pytest.raises(ValueError, match="egress_mode"):
        w.create_project("Unsafe", egress_mode="cloud")


def test_revoked_member_loses_matter_reads_and_final_responsible_is_guarded(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("Matter", owner="user:lead")
    gid = w.create_goal("Privileged memo", owner="user:lead", project_id=pid)
    w.add_project_member(pid, "user:staff", "staff", added_by="user:lead")
    assert [g.id for g in w.list_goals(accessible_by="user:staff")] == [gid]
    assert w.deactivate_project_member(pid, "user:staff") is True
    assert w.list_goals(accessible_by="user:staff") == []
    with pytest.raises(ValueError, match="final responsible attorney"):
        w.deactivate_project_member(pid, "user:lead")


def test_attorney_demotion_or_revocation_invalidates_their_signoff(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    pid = w.create_project("Matter", owner="user:lead")
    w.add_project_member(pid, "user:reviewer", "attorney", added_by="user:lead")
    gid = w.create_goal(
        "Privileged memo",
        owner="user:lead",
        domain="legal",
        project_id=pid,
    )
    w.set_goal_status(gid, "done", result="reviewed")
    w.record_signoff(gid, "approved", decided_by="user:reviewer")
    w.add_project_member(pid, "user:reviewer", "staff", added_by="user:lead")
    assert w.signoff_for(gid) is None

    w.add_project_member(pid, "user:reviewer", "attorney", added_by="user:lead")
    w.record_signoff(gid, "approved", decided_by="user:reviewer")
    assert w.deactivate_project_member(pid, "user:reviewer") is True
    assert w.signoff_for(gid) is None


def test_atomic_filing_requires_current_and_destination_membership(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    source = w.create_project("Source", owner="user:alice")
    target = w.create_project("Target", owner="user:bob")
    gid = w.create_goal("Alice memo", owner="user:alice", project_id=source)

    assert w.set_goal_project(gid, target, principal="user:alice") is False
    assert w.get_goal(gid).project_id == source
    w.add_project_member(target, "user:alice", "staff", added_by="user:bob")
    assert w.set_goal_project(gid, target, principal="user:alice") is True
    assert w.get_goal(gid).project_id == target
    assert w.deactivate_project_member(target, "user:alice") is True
    assert w.set_goal_project(gid, None, principal="user:alice") is False
    assert w.get_goal(gid).project_id == target


def test_atomic_matter_goal_create_requires_live_exact_membership(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    project_id = w.create_project("Client matter", owner="user:alice")

    goal_id = w.create_matter_goal(
        "Draft motion",
        "Privileged strategy",
        principal="user:alice",
        domain="legal_briefs",
        project_id=project_id,
    )
    assert goal_id is not None
    goal = w.get_goal(goal_id)
    assert goal is not None
    assert goal.owner == "user:alice"
    assert goal.domain == "legal_briefs"
    assert goal.project_id == project_id

    assert w.create_matter_goal(
        "Intruder work",
        principal="user:mallory",
        domain="legal_briefs",
        project_id=project_id,
    ) is None
    w.add_project_member(project_id, "user:bob", "staff", added_by="user:alice")
    assert w.deactivate_project_member(project_id, "user:bob") is True
    assert w.create_matter_goal(
        "Revoked work",
        principal="user:bob",
        domain="legal_briefs",
        project_id=project_id,
    ) is None
    assert [g.id for g in w.list_goals()] == [goal_id]


def test_episode_and_spend_queries_follow_matter_membership(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    alice_matter = w.create_project("Alice", owner="user:alice")
    bob_matter = w.create_project("Bob", owner="user:bob")
    alice_goal = w.create_goal("Alice run", owner="user:alice", project_id=alice_matter)
    bob_goal = w.create_goal("Bob run", owner="user:bob", project_id=bob_matter)
    unfiled = w.create_goal("Legacy admin queue", owner="user:bob")
    for goal_id, dollars in ((alice_goal, 1.0), (bob_goal, 2.0), (unfiled, 3.0)):
        episode_id = w.start_episode(goal_id)
        w.end_episode(episode_id, "done", "ok", cost_dollars=dollars)

    alice = w.list_episodes(accessible_by="user:alice")
    assert {e.goal_id for e in alice} == {alice_goal}
    assert w.total_spend(accessible_by="user:alice")["dollars"] == 1.0

    admin_legacy = w.list_episodes(
        accessible_by="user:root", include_all_unfiled=True,
    )
    assert {e.goal_id for e in admin_legacy} == {unfiled}
    assert bob_goal not in {e.goal_id for e in admin_legacy}

def test_migration_from_v18_adds_project_id(tmp_path):
    # A pre-projects (v18) DB: goals without project_id, version pinned 18.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE goals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER,
            title TEXT NOT NULL, description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            deadline REAL, result TEXT,
            owner TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '');
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version(version) VALUES(18);
        INSERT INTO goals(title, status, created_at, updated_at)
            VALUES('legacy', 'done', 0, 0);
        """
    )
    conn.commit()
    conn.close()
    w = WorldModel(db)  # opening runs the v19 (projects) migration + any later
    assert w.schema_version == SCHEMA_VERSION
    assert w.get_goal(1).project_id is None          # legacy goal: unfiled
    pid = w.create_project("New")
    w.set_goal_project(1, pid)                        # and it can now be filed
    assert w.get_goal(1).project_id == pid


def test_migration_v32_backfills_exact_owner_membership_but_not_ownerless(tmp_path):
    db = tmp_path / "v31.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version(version) VALUES(31);
        CREATE TABLE projects(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT,
            owner TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
        INSERT INTO projects(name, owner, created_at)
            VALUES('owned', 'user:alice', 10), ('ownerless', '', 20);
        """
    )
    conn.commit()
    conn.close()

    w = WorldModel(db)
    assert w.schema_version == SCHEMA_VERSION
    assert w.project_member_role(1, "user:alice") == "responsible_attorney"
    assert w.list_project_members(2) == []
    assert w.get_project(1)["egress_mode"] == "local_only"
    assert w.get_project(2)["egress_mode"] == "local_only"
