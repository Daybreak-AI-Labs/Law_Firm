"""Atomic, encrypted, opaque client/matter conflict intake."""
from __future__ import annotations

import importlib.util
import sqlite3
import threading

import pytest
from maverick.world_model import SCHEMA_VERSION, PotentialConflict, WorldModel


def _open(
    world: WorldModel,
    *,
    principal: str = "user:alice",
    client_name: str = "Acme, Inc.",
    matter_number: str = "2026-001",
    adverse_parties: tuple[str, ...] = ("Jones LLC",),
) -> int:
    return world.create_client_matter(
        "Acme v. Jones",
        principal=principal,
        domain="legal_litigation_mgmt",
        matter_number=matter_number,
        jurisdiction="Tennessee",
        client_name=client_name,
        adverse_parties=adverse_parties,
    )


def test_atomic_intake_creates_client_matter_acl_and_parties(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    matter_id = _open(world)

    matter = world.get_project(matter_id)
    assert matter is not None
    assert matter["client_name"] == "Acme, Inc."
    assert matter["matter_number"] == "2026-001"
    assert matter["jurisdiction"] == "Tennessee"
    assert matter["domain"] == "legal_litigation_mgmt"
    assert matter["egress_mode"] == "local_only"
    assert world.project_member_role(matter_id, "user:alice") == "responsible_attorney"
    assert [(p["name"], p["role"]) for p in world.list_matter_parties(matter_id)] == [
        ("Jones LLC", "adverse"),
        ("Acme, Inc.", "client"),
    ]


def test_normalized_conflict_is_opaque_and_rolls_back_every_row(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    _open(world)
    before = world.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

    with pytest.raises(PotentialConflict) as caught:
        _open(
            world,
            principal="user:bob",
            client_name="  ACME INC  ",
            matter_number="2026-002",
            adverse_parties=(),
        )

    assert "Acme" not in str(caught.value)
    assert world.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == before
    assert world.conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1


def test_existing_client_reuse_requires_active_membership_and_counts_only_visible(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    first = _open(world)
    client_id = world.get_project(first)["client_id"]
    world.add_project_member(first, "user:bob", "staff", added_by="user:alice")

    with pytest.raises(PotentialConflict):
        world.create_client_matter(
            "Hidden reuse",
            principal="user:mallory",
            domain="legal",
            matter_number="2026-002",
            jurisdiction="Georgia",
            client_id=client_id,
        )

    second = world.create_client_matter(
        "Acme estate plan",
        principal="user:alice",
        domain="legal",
        matter_number="2026-003",
        jurisdiction="Florida",
        client_id=client_id,
    )
    assert world.get_project(second)["client_name"] == "Acme, Inc."
    assert world.list_clients(principal="user:alice")[0]["matter_count"] == 2
    # Bob may know the shared client, but not that Alice has another matter.
    assert world.list_clients(principal="user:bob")[0]["matter_count"] == 1
    assert world.list_clients(principal="user:mallory") == []


def test_concurrent_same_client_intakes_serialize_to_one_success(tmp_path):
    db = tmp_path / "world.db"
    worlds = (WorldModel(db), WorldModel(db))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    result_lock = threading.Lock()

    def run(world: WorldModel, principal: str, number: str) -> None:
        barrier.wait()
        try:
            _open(
                world,
                principal=principal,
                client_name="Acme, Inc.",
                matter_number=number,
                adverse_parties=(),
            )
        except PotentialConflict:
            outcome = "conflict"
        else:
            outcome = "created"
        with result_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=run, args=(worlds[0], "user:alice", "2026-001")),
        threading.Thread(target=run, args=(worlds[1], "user:bob", "2026-002")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["conflict", "created"]
    assert worlds[0].conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
    assert worlds[0].conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_party_add_is_responsible_attorney_only_and_conflict_checked(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    matter_id = _open(world)
    world.add_project_member(matter_id, "user:bob", "attorney", added_by="user:alice")

    assert world.add_matter_party(
        matter_id, "New Witness", "witness", principal="user:bob",
    ) is None
    assert world.add_matter_party(
        matter_id, "New Witness", "witness", principal="user:alice",
    ) is not None
    with pytest.raises(PotentialConflict):
        world.add_matter_party(
            matter_id, "new-witness", "related", principal="user:alice",
        )


def test_static_bearer_never_resolves_as_a_matter_member(tmp_path):
    world = WorldModel(tmp_path / "world.db")
    matter_id = _open(world)
    goal_id = world.create_goal(
        "Privileged matter goal",
        owner="user:alice",
        domain="legal",
        project_id=matter_id,
    )
    static = "user:dashboard-static-bearer"

    with pytest.raises(ValueError, match="shared credential"):
        world.add_project_member(matter_id, static, "viewer", added_by="user:alice")
    with pytest.raises(ValueError):
        world.create_matter_goal(
            "Static attempt",
            principal=static,
            domain="legal",
            project_id=matter_id,
        )

    # Even a legacy/tampered active row cannot revive the shared credential at
    # any durable resolution surface.
    world.conn.execute(
        "INSERT INTO matter_memberships("
        "project_id, principal, role, active, added_by, created_at) "
        "VALUES(?, ?, 'viewer', 1, 'legacy', 1)",
        (matter_id, static),
    )
    world.conn.commit()
    assert world.project_member_role(matter_id, static) is None
    assert world.get_project(matter_id, principal=static) is None
    assert world.list_projects(principal=static) == []
    assert world.list_clients(principal=static) == []
    assert world.list_matter_parties(matter_id, principal=static) == []
    assert world.list_project_members(matter_id, principal=static) == []
    assert world.project_status_counts(matter_id, principal=static) == {}
    assert world.list_goals(accessible_by=static) == []
    assert world.get_goal(goal_id) is not None  # the row still exists for Alice
    assert all(row["principal"] != static for row in world.list_project_members(matter_id))


def test_principal_scoped_matter_reads_filter_before_decryption(tmp_path, monkeypatch):
    world = WorldModel(tmp_path / "world.db")
    matter_id = _open(world)
    world.create_goal("Privileged title", project_id=matter_id)

    called = False
    original = world._project_from_row

    def project_from_row(row):
        nonlocal called
        called = True
        return original(row)

    monkeypatch.setattr(world, "_project_from_row", project_from_row)
    assert world.get_project(matter_id, principal="user:mallory") is None
    assert called is False
    assert world.list_matter_parties(matter_id, principal="user:mallory") == []
    assert world.list_project_members(matter_id, principal="user:mallory") == []
    assert world.project_status_counts(matter_id, principal="user:mallory") == {}


@pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None,
    reason="cryptography extra is not installed",
)
def test_client_party_and_matter_metadata_are_encrypted_but_still_match(
    monkeypatch, tmp_path,
):
    from maverick import crypto_at_rest as car

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", bytes(range(32)).hex())
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr(car, "_KEY_PATH", tmp_path / "keys" / "at_rest.key")
    db = tmp_path / "world.db"
    world = WorldModel(db)
    matter_id = _open(world)

    raw = sqlite3.connect(db)
    client_name = raw.execute("SELECT name FROM clients").fetchone()[0]
    party_names = [row[0] for row in raw.execute("SELECT name FROM matter_parties")]
    matter = raw.execute(
        "SELECT name, matter_number, jurisdiction FROM projects WHERE id = ?",
        (matter_id,),
    ).fetchone()
    assert client_name.startswith("MVKAR") and "Acme" not in client_name
    assert all(value.startswith("MVKAR") for value in party_names)
    assert all(value.startswith("MVKAR") for value in matter)
    assert "2026-001" not in repr(matter)

    with pytest.raises(PotentialConflict):
        world.check_potential_conflicts(["acme inc"])


def test_v35_migration_adds_client_matter_columns_and_tables(tmp_path):
    db = tmp_path / "v34.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version(version) VALUES(34);
        CREATE TABLE projects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            description TEXT,
            owner TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            egress_mode TEXT NOT NULL DEFAULT 'local_only',
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL
        );
        INSERT INTO projects(name, created_at) VALUES('legacy matter', 1);
        CREATE TABLE matter_memberships(
            project_id INTEGER NOT NULL,
            principal TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            added_by TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            PRIMARY KEY (project_id, principal)
        );
        INSERT INTO matter_memberships(
            project_id, principal, role, active, added_by, created_at
        ) VALUES(
            1, 'user:dashboard-static-bearer', 'viewer', 1, 'legacy', 1
        );
        """
    )
    conn.commit()
    conn.close()

    world = WorldModel(db)
    assert world.schema_version == SCHEMA_VERSION
    columns = {
        row[1] for row in world.conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    assert {"client_id", "matter_number", "jurisdiction"} <= columns
    assert world.get_project(1)["client_id"] is None
    assert world.get_project(1)["matter_number"] == ""
    assert world.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
    ).fetchone()
    assert world.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='matter_parties'"
    ).fetchone()
    assert world.conn.execute(
        "SELECT active FROM matter_memberships "
        "WHERE principal = 'user:dashboard-static-bearer'"
    ).fetchone()[0] == 0
