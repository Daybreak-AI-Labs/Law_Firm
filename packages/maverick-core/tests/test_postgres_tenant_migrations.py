"""Postgres backend: versioned-migration planner + tenant read-scoping.

These exercise the *pure* logic (no live Postgres, no psycopg) so the upgrade
ladder and the tenant predicate are covered even when no DB service is present.
The full round-trip lives in test_postgres_world.py (skipped without a DSN).
"""
from __future__ import annotations

import maverick.world_model_backends.postgres as pg
import pytest


def test_pending_from_fresh_db_applies_everything():
    pending = pg.pending_migrations(0)
    versions = [v for v, _ in pending]
    assert versions == sorted(versions)  # ascending
    assert versions[0] == 1  # base schema first
    assert pg._PG_SCHEMA_VERSION in versions  # tenant migration included


def test_pending_skips_already_applied():
    # A DB already at the latest version has nothing to do.
    assert pg.pending_migrations(pg._PG_SCHEMA_VERSION) == []
    # A DB at v1 still needs the tenant migrations (v10 columns, v11 uniques),
    # approval-claims (v13), goal-domain (v14), projects (v15), artifacts (v16),
    # share/signoff/origin (v17), temporal fact_history (v18), rate_events (v19),
    # N-of-M dual control (v21), cluster-wide halt (v22), provider spend (v23),
    # facts provenance columns (v24), fleet learning store (v25),
    # corpus family (v26), episode cache-token spend columns (v27), and
    # per-principal goal ownership (v28), erasure receipts (v29), and the
    # approval-audit transactional outbox (v30), and the 64-bit fact write
    # sequence (v31).
    pending = pg.pending_migrations(1)
    assert [v for v, _ in pending] == [
        10, 11, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v10 still needs the remaining upgrades.
    assert [v for v, _ in pg.pending_migrations(10)] == [
        11, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v11 still needs the rest of the ladder.
    assert [v for v, _ in pg.pending_migrations(11)] == [
        13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v13 still needs goal-domain + projects + artifacts + share/etc.
    assert [v for v, _ in pg.pending_migrations(13)] == [
        14, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v14 still needs projects + artifacts + share/signoff/origin.
    assert [v for v, _ in pg.pending_migrations(14)] == [
        15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v15 still needs artifacts + share/signoff/origin + fact_history.
    assert [v for v, _ in pg.pending_migrations(15)] == [
        16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v16 still needs the share/signoff/origin + fact_history migrations.
    assert [v for v, _ in pg.pending_migrations(16)] == [
        17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v17 still needs fact_history + rate_events.
    assert [v for v, _ in pg.pending_migrations(17)] == [
        18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v18 still needs the rate_events migration.
    assert [v for v, _ in pg.pending_migrations(18)] == [
        19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v19 still needs dual-control + halt + provider-spend + facts trust.
    assert [v for v, _ in pg.pending_migrations(19)] == [
        21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v21 still needs halt + provider-spend + facts trust.
    assert [v for v, _ in pg.pending_migrations(21)] == [
        22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v22 still needs provider-spend + facts trust.
    assert [v for v, _ in pg.pending_migrations(22)] == [
        23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v23 still needs the facts provenance migration + the store.
    assert [v for v, _ in pg.pending_migrations(23)] == [
        24, 25, 26, 27, 28, 29, 30, 31,
    ]
    # A DB at v24 still needs the fleet learning store + corpus family.
    assert [v for v, _ in pg.pending_migrations(24)] == [25, 26, 27, 28, 29, 30, 31]
    # A DB at v25 still needs the corpus family.
    assert [v for v, _ in pg.pending_migrations(25)] == [26, 27, 28, 29, 30, 31]


def test_pending_is_ordered_with_custom_ladder():
    ladder = [(3, ["c"]), (1, ["a"]), (2, ["b"])]
    assert pg.pending_migrations(0, ladder) == [(1, ["a"]), (2, ["b"]), (3, ["c"])]
    assert pg.pending_migrations(1, ladder) == [(2, ["b"]), (3, ["c"])]


def test_tenant_migration_adds_column_and_index_for_root_tables():
    stmts = dict(pg.MIGRATIONS)[10]
    joined = "\n".join(stmts)
    for table in pg._TENANT_TABLES:
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT" in joined
        assert f"idx_pg_{table}_tenant" in joined


def test_tenant_unique_migration_makes_constraints_tenant_aware():
    stmts = dict(pg.MIGRATIONS)[11]
    joined = "\n".join(stmts)
    # The global UNIQUEs are dropped and replaced with COALESCE(tenant_id,'')
    # expression indexes so single-tenant (NULL) dedup is preserved.
    assert "DROP CONSTRAINT IF EXISTS facts_key_key" in joined
    assert "uq_pg_facts_tenant_key" in joined
    assert "uq_pg_conversations_tenant_chan_user" in joined
    assert "uq_pg_processed_tenant_chan_ext" in joined
    assert joined.count("COALESCE(tenant_id, '')") == 3  # one per unique table


def test_approval_claims_migration_adds_collaboration_columns():
    stmts = dict(pg.MIGRATIONS)[13]
    joined = "\n".join(stmts)
    assert "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS claimed_by TEXT" in joined
    assert "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS claimed_at DOUBLE PRECISION" in joined
    assert "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decided_by TEXT" in joined


def test_schema_version_is_latest_migration():
    assert pg._PG_SCHEMA_VERSION == 31
    assert max(v for v, _ in pg.MIGRATIONS) == pg._PG_SCHEMA_VERSION


def test_fact_write_clock_migration_is_64_bit_and_indexed():
    joined = "\n".join(dict(pg.MIGRATIONS)[31])
    assert "write_seq BIGSERIAL" in joined
    assert "idx_pg_facts_write_seq" in joined


def test_goal_owner_migration_is_additive_and_backfills_legacy_rows():
    assert dict(pg.MIGRATIONS)[28] == [
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS owner "
        "TEXT NOT NULL DEFAULT '';",
    ]
    assert [v for v, _ in pg.pending_migrations(27)] == [28, 29, 30, 31]


def test_erasure_receipt_migration_is_tenant_scoped_and_immutable():
    joined = "\n".join(dict(pg.MIGRATIONS)[29])
    assert "CREATE TABLE IF NOT EXISTS erasure_receipts" in joined
    assert "PRIMARY KEY (tenant_id, receipt_id)" in joined
    assert "erasure_receipts_no_update" in joined
    assert "erasure_receipts_no_early_delete" in joined
    assert [v for v, _ in pg.pending_migrations(28)] == [29, 30, 31]


def test_rate_events_migration_adds_table():
    joined = "\n".join(dict(pg.MIGRATIONS)[19])
    assert "CREATE TABLE IF NOT EXISTS rate_events" in joined
    assert "idx_pg_rate_events_key_ts" in joined


def test_artifacts_migration_adds_table():
    assert "CREATE TABLE IF NOT EXISTS artifacts" in "\n".join(dict(pg.MIGRATIONS)[16])


def test_share_signoff_origin_migration_adds_tables():
    joined = "\n".join(dict(pg.MIGRATIONS)[17])
    assert "CREATE TABLE IF NOT EXISTS share_links" in joined
    assert "CREATE TABLE IF NOT EXISTS signoffs" in joined
    assert "CREATE TABLE IF NOT EXISTS goal_origins" in joined


def test_goal_domain_migration_adds_domain_column():
    stmts = dict(pg.MIGRATIONS)[14]
    assert "ALTER TABLE goals ADD COLUMN IF NOT EXISTS domain TEXT" in "\n".join(stmts)


def test_projects_migration_adds_table_and_goal_fk():
    joined = "\n".join(dict(pg.MIGRATIONS)[15])
    assert "CREATE TABLE IF NOT EXISTS projects" in joined
    assert "ALTER TABLE goals ADD COLUMN IF NOT EXISTS project_id INTEGER" in joined


def test_tenant_scope_noop_without_active_tenant(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: None)
    frag, params = pg._tenant_scope()
    assert frag == ""
    assert params == []


def test_erasure_scope_never_turns_unbound_admin_view_into_global_delete(
    monkeypatch,
):
    monkeypatch.setattr(pg, "_active_tenant", lambda: None)
    frag, params = pg._erasure_tenant_scope("g.tenant_id")
    assert frag == "g.tenant_id IS NULL"
    assert params == []


def test_fact_mutation_scope_is_exact_when_unbound_or_bound(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: None)
    assert pg._mutation_tenant_scope() == ("tenant_id IS NULL", [])

    monkeypatch.setattr(pg, "_active_tenant", lambda: "Acme")
    assert pg._mutation_tenant_scope("f.tenant_id") == (
        "f.tenant_id = %s",
        ["Acme"],
    )


def test_compatibility_precedence_prefers_active_tenant(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "Acme")
    fragment, params = pg._tenant_precedence("f.tenant_id")
    assert fragment == "CASE WHEN f.tenant_id = %s THEN 0 ELSE 1 END"
    assert params == ["Acme"]


def test_invalid_explicit_tenant_cannot_become_unscoped():
    from maverick.paths import InvalidTenantError, reset_tenant, set_tenant

    token = set_tenant("x" * 201)
    try:
        with pytest.raises(InvalidTenantError, match="too long"):
            pg._tenant_scope()
    finally:
        reset_tenant(token)


def test_corrupt_client_resolution_cannot_become_unscoped(monkeypatch):
    from maverick import client

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    monkeypatch.setattr(
        client,
        "_raw_client_id",
        lambda: (_ for _ in ()).throw(client.ClientBindingError("corrupt binding")),
    )
    with pytest.raises(client.ClientBindingError, match="corrupt binding"):
        pg._tenant_scope()


class _RetentionCursor:
    rowcount = 3

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return (4,)


class _RetentionTx:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.mark.parametrize(
    ("method_name", "table"),
    [
        ("purge_episodes_before", "episodes"),
        ("purge_goal_events_before", "goal_events"),
    ],
)
def test_retention_requires_exact_active_tenant(monkeypatch, method_name, table):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "Acme")
    monkeypatch.setattr(pg, "_strict_tenant_isolation", lambda: False)
    cursor = _RetentionCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: _RetentionTx(cursor))

    assert getattr(model, method_name)(123.0) == 3
    sql, params = cursor.calls[0]
    assert f"DELETE FROM {table}" in sql
    assert "tenant_id = %s" in sql
    assert "OR tenant_id IS NULL" not in sql
    assert params == (123.0, "Acme")


@pytest.mark.parametrize(
    "method_name",
    [
        "purge_episodes_before",
        "purge_goal_events_before",
        "prune_goal_events",
        "prune_processed_messages",
        "prune_conversations",
    ],
)
def test_retention_refuses_unbound_execution(monkeypatch, method_name):
    from maverick.paths import TenantPolicyError

    monkeypatch.setattr(pg, "_active_tenant", lambda: None)
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(
        model,
        "_tx",
        lambda: (_ for _ in ()).throw(AssertionError("must not open transaction")),
    )

    with pytest.raises(TenantPolicyError, match="refusing unbound"):
        getattr(model, method_name)(123.0)


def test_legacy_prune_methods_use_exact_tenant(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "Acme")
    monkeypatch.setattr(pg.time, "time", lambda: 200.0)
    cursor = _RetentionCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: _RetentionTx(cursor))

    assert model.prune_goal_events(older_than_seconds=50.0) == 3
    event_sql, event_params = cursor.calls[0]
    assert "tenant_id = %s" in event_sql
    assert "OR tenant_id IS NULL" not in event_sql
    assert event_params == (150.0, "Acme")

    cursor.calls.clear()
    assert model.prune_processed_messages(older_than_seconds=50.0) == 3
    processed_sql, processed_params = cursor.calls[0]
    assert "tenant_id = %s" in processed_sql
    assert "IS NULL" not in processed_sql
    assert processed_params == (150.0, "Acme")


def test_conversation_prune_uses_exact_tenant_for_parent_and_children(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "Acme")
    monkeypatch.setattr(pg.time, "time", lambda: 200.0)
    cursor = _RetentionCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: _RetentionTx(cursor))

    assert model.prune_conversations(idle_for_seconds=50.0) == 3
    assert len(cursor.calls) == 2
    for sql, params in cursor.calls:
        assert "tenant_id = %s" in sql
        assert "IS NULL" not in sql
        assert params == (150.0, "Acme")


def test_tenant_scope_filters_to_active_tenant_null_tolerant(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(pg, "_strict_tenant_isolation", lambda: False)
    frag, params = pg._tenant_scope()
    assert frag == "(tenant_id = %s OR tenant_id IS NULL)"
    assert params == ["acme"]


def test_strict_tenant_isolation_excludes_legacy_null(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(pg, "_strict_tenant_isolation", lambda: True)
    frag, params = pg._tenant_scope()
    # Strict mode: only the tenant's own rows, no NULL-legacy tolerance.
    assert frag == "tenant_id = %s"
    assert params == ["acme"]


def test_strict_isolation_reads_env(monkeypatch):
    monkeypatch.delenv("MAVERICK_STRICT_TENANT_ISOLATION", raising=False)
    assert pg._strict_tenant_isolation() is False
    monkeypatch.setenv("MAVERICK_STRICT_TENANT_ISOLATION", "1")
    assert pg._strict_tenant_isolation() is True
    monkeypatch.setenv("MAVERICK_STRICT_TENANT_ISOLATION", "off")
    assert pg._strict_tenant_isolation() is False


def test_tenant_scope_custom_column(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "acme")
    frag, _ = pg._tenant_scope("goals.tenant_id")
    assert frag == "(goals.tenant_id = %s OR goals.tenant_id IS NULL)"


def test_create_goal_persists_owner_with_tenant(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "tenant-a")
    monkeypatch.setattr(pg.time, "time", lambda: 123.0)
    monkeypatch.setattr(pg, "_seal", lambda value: value)

    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchone(self):
            return (99,)

    class FakeTx:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self.cursor

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: FakeTx(cursor))

    assert model.create_goal(
        "owned goal", "description", parent_id=4, owner="user:alice",
    ) == 99
    sql, params = cursor.calls[0]
    assert "created_at, updated_at, owner, tenant_id" in sql
    assert params == (
        4, "owned goal", "description", 123.0, 123.0, "user:alice", "tenant-a",
    )


def test_list_episodes_combines_owner_goal_and_tenant_scopes(monkeypatch):
    """Owner filtering must narrow, never replace, the tenant boundary."""
    monkeypatch.setattr(pg, "_active_tenant", lambda: "tenant-a")
    monkeypatch.setattr(pg, "_strict_tenant_isolation", lambda: False)

    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchall(self):
            return []

    class FakeTx:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self.cursor

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: FakeTx(cursor))

    assert model.list_episodes(limit=7, goal_id=42, owner="user:alice") == []

    sql, params = cursor.calls[0]
    assert sql.count("JOIN goals g ON g.id = e.goal_id") == 1
    assert "e.goal_id = %s" in sql
    assert "g.owner = %s" in sql
    assert "(g.tenant_id = %s OR g.tenant_id IS NULL)" in sql
    assert params == (42, "user:alice", "tenant-a", 7)


def test_list_projects_applies_owner_and_tenant_scope(monkeypatch):
    monkeypatch.setattr(pg, "_active_tenant", lambda: "tenant-a")
    monkeypatch.setattr(pg, "_strict_tenant_isolation", lambda: False)

    class FakeCursor:
        def __init__(self):
            self.calls = []
            self._rows = [(7, "name", "desc", "alice", "eng", "active", 123.0)]

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return (2,)

    class FakeTx:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self.cursor

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    model = pg.PostgresWorldModel.__new__(pg.PostgresWorldModel)
    monkeypatch.setattr(model, "_tx", lambda: FakeTx(cursor))

    projects = model.list_projects(owner="alice")

    assert projects[0]["owner"] == "alice"
    assert projects[0]["goal_count"] == 2
    project_sql, project_params = cursor.calls[0]
    assert "WHERE owner=%s AND (tenant_id = %s OR tenant_id IS NULL)" in project_sql
    assert project_params == ("alice", "tenant-a")
    goal_count_sql, goal_count_params = cursor.calls[1]
    assert goal_count_sql == (
        "SELECT COUNT(*) FROM goals WHERE project_id=%s "
        "AND (tenant_id = %s OR tenant_id IS NULL)"
    )
    assert goal_count_params == (7, "tenant-a")
