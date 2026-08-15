from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest
from maverick.world_model_backends import postgres
from maverick.world_model_backends.postgres import PostgresWorldModel


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self):
        return (None,)


class RetentionCursor(RecordingCursor):
    rowcount = 2

    def fetchone(self):
        return (2,)


class TemporalDeleteCursor(RecordingCursor):
    rowcount = 1

    def fetchone(self):
        return None


class FactTrustSnapshotCursor(RecordingCursor):
    def fetchall(self):
        return [("risk:vendor", "external value", 0)]


class EmptyFactCursor(RecordingCursor):
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class ErasureReceiptCursor(RecordingCursor):
    def fetchone(self):
        return (2,)

    def fetchall(self):
        return [(201,), (202,)]


class SubjectFactPurgeCursor(RecordingCursor):
    def fetchall(self):
        # An own + compatibility-legacy row may share the same natural key.
        return [
            ("user:sms:alice:email",),
            ("user:sms:alice:email",),
            ("user:sms:alice:phone",),
        ]


class FactHistoryCursor(RecordingCursor):
    def fetchall(self):
        return [
            (1, "k", "legacy", 300.0, None, "old", 2, "internal", None),
            (2, "k", "own", 200.0, 300.0, "new", 3, "internal", "acme"),
        ]


def test_effective_history_splits_legacy_only_across_own_overlap():
    rows = [
        (1, "user:sms:alice:email", "legacy", 100.0, None, "old", 2, "internal", None),
        (2, "user:sms:alice:email", "own", 200.0, 300.0, "new", 3, "internal", "acme"),
    ]

    projected = postgres._effective_fact_history_rows(rows, "acme")

    assert [
        (row[2], row[3], row[4], row[8])
        for row in projected
    ] == [
        ("legacy", 100.0, 200.0, None),
        ("own", 200.0, 300.0, "acme"),
        ("legacy", 300.0, None, None),
    ]
    # The unbound administrative history keeps its prior raw multi-tenant view.
    assert postgres._effective_fact_history_rows(rows, None) == rows


def test_subject_fact_purge_locks_tables_and_accounts_returned_keys(monkeypatch):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(postgres, "_strict_tenant_isolation", lambda: False)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = SubjectFactPurgeCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    assert world.delete_facts_matching("sms:alice") == [
        "user:sms:alice:email",
        "user:sms:alice:phone",
    ]
    assert len(cur.calls) == 3
    lock_sql, lock_params = cur.calls[0]
    live_sql, live_params = cur.calls[1]
    history_sql, history_params = cur.calls[2]
    assert lock_sql == (
        "LOCK TABLE fact_history, facts IN SHARE ROW EXCLUSIVE MODE"
    )
    assert lock_params is None
    assert live_sql.startswith("DELETE FROM facts")
    assert live_sql.endswith("RETURNING key")
    assert history_sql.startswith("DELETE FROM fact_history")
    for sql in (live_sql, history_sql):
        assert "(tenant_id = %s OR tenant_id IS NULL)" in sql
    assert live_params == ("user:sms:alice:%", "acme")
    assert history_params == ("user:sms:alice:%", "acme")


def test_compatibility_history_projects_before_database_side_limit(monkeypatch):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(postgres, "_strict_tenant_isolation", lambda: False)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = FactHistoryCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    history = world.fact_history("k", limit=2)

    assert [version.value for version in history] == ["legacy", "own"]
    [(sql, params)] = cur.calls
    assert "WITH scoped AS" in sql
    assert "JOIN LATERAL" in sql
    assert "LAG(id) OVER (ORDER BY segment_start)" in sql
    assert sql.endswith("ORDER BY MIN(segment_start) DESC, id DESC LIMIT %s")
    assert params == ("k", "acme", "acme", 2)


def test_fact_upsert_advances_write_order_sequence(monkeypatch):
    """A same-timestamp update must sort after a newer key on Postgres too."""
    monkeypatch.setattr(postgres, "_active_tenant", lambda: None)
    monkeypatch.setattr(
        "maverick.world_model._temporal_memory_enabled", lambda: False
    )
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = RecordingCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)
    world.upsert_fact("older-key", "updated last")

    sql, _ = cur.calls[-1]
    assert "write_seq = EXCLUDED.write_seq" in sql
    assert "SET id =" not in sql
    source = inspect.getsource(PostgresWorldModel.upsert_fact)
    assert '"SELECT MAX(updated_at)' not in source
    assert "fact-write-order:" not in source
    recall_source = inspect.getsource(PostgresWorldModel.get_facts)
    assert "ORDER BY write_seq DESC" in recall_source


def test_fact_value_and_trust_share_one_snapshot(monkeypatch):
    """A recall cannot splice a value and trust tier from separate writes."""
    monkeypatch.setattr(postgres, "_active_tenant", lambda: None)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = FactTrustSnapshotCursor()
    tx_count = 0

    @contextmanager
    def tx():
        nonlocal tx_count
        tx_count += 1
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    assert world.get_facts_with_trust() == {
        "risk:vendor": ("external value", 0)
    }
    assert tx_count == 1
    assert len(cur.calls) == 1
    sql, params = cur.calls[0]
    assert "SELECT key, value, trust_tier FROM (" in sql
    assert "FROM facts" in sql
    assert "ORDER BY write_seq DESC" in sql
    assert params == ()


@pytest.mark.parametrize(
    ("method", "args", "kwargs", "expected_order"),
    [
        ("get_facts", (), {}, "ORDER BY write_seq DESC"),
        ("get_facts_with_trust", (), {}, "ORDER BY write_seq DESC"),
        ("get_fact", ("missing",), {}, "ORDER BY write_seq DESC LIMIT 1"),
        (
            "get_fact",
            ("missing",),
            {"as_of": 100.0},
            "ORDER BY valid_from DESC, id DESC LIMIT 1",
        ),
        ("list_facts", ("prefix:",), {}, "ORDER BY write_seq DESC"),
        (
            "search_facts",
            ("prefix:", "needle"),
            {},
            "ORDER BY write_seq DESC",
        ),
    ],
)
def test_unbound_fact_reads_omit_tenant_precedence(
    monkeypatch,
    method,
    args,
    kwargs,
    expected_order,
):
    """Unbound Postgres reads must never emit invalid ``ORDER BY 0`` SQL."""
    monkeypatch.setattr(postgres, "_active_tenant", lambda: None)
    monkeypatch.setattr(
        "maverick.crypto_at_rest.at_rest_enabled",
        lambda: False,
    )
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = EmptyFactCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    getattr(world, method)(*args, **kwargs)

    [(sql, _params)] = cur.calls
    assert expected_order in sql
    assert "ORDER BY 0" not in sql
    assert "CASE WHEN" not in sql
    assert postgres._tenant_precedence() == ("", [])


def test_erasure_receipt_episode_count_scopes_through_parent_goal(monkeypatch):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(postgres, "_strict_tenant_isolation", lambda: False)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = ErasureReceiptCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    assert world.count_erasure_receipt_store(
        "episodes",
        conversation_ids=[1],
        goal_ids=[11],
    ) == 2

    assert len(cur.calls) == 2
    select_sql, select_params = cur.calls[0]
    count_sql, count_params = cur.calls[1]
    for sql in (select_sql, count_sql):
        assert "FROM episodes e JOIN goals g ON g.id = e.goal_id" in sql
        assert "(g.tenant_id = %s OR g.tenant_id IS NULL)" in sql
        assert "e.tenant_id" not in sql
    assert select_params == ([11], "acme")
    assert count_params == ([201, 202], "acme")


def test_temporal_delete_uses_same_per_key_advisory_lock(monkeypatch):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: "acme")
    monkeypatch.setattr(
        "maverick.world_model._temporal_memory_enabled", lambda: True
    )
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = TemporalDeleteCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)
    assert world.delete_fact("shared-key") == 1

    sql = [statement for statement, _params in cur.calls]
    lock_index = next(
        index
        for index, statement in enumerate(sql)
        if "pg_advisory_xact_lock" in statement
    )
    delete_index = next(
        index
        for index, statement in enumerate(sql)
        if statement.startswith("DELETE FROM facts")
    )
    assert lock_index < delete_index
    assert "fact:acme:shared-key" in str(cur.calls[lock_index][1])


def test_rls_sets_fail_closed_sentinel_without_active_tenant(monkeypatch):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: None)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    world._rls = True
    cur = RecordingCursor()

    world._set_tenant_guc(cur)

    assert cur.calls == [
        ("SELECT set_config('maverick.tenant', %s, true)", ("__maverick_no_tenant__",))
    ]


@pytest.mark.parametrize(
    "method,table",
    [
        ("purge_episodes_before", "episodes"),
        ("purge_goal_events_before", "goal_events"),
    ],
)
def test_retention_purge_uses_exact_active_tenant(monkeypatch, method, table):
    monkeypatch.setattr(postgres, "_active_tenant", lambda: "acme")
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    cur = RetentionCursor()

    @contextmanager
    def tx():
        yield cur

    monkeypatch.setattr(world, "_tx", tx)

    assert getattr(world, method)(123.0, dry_run=True) == 2
    sql, params = cur.calls[0]
    assert f"FROM {table}" in sql
    assert "tenant_id = %s" in sql
    assert "IS NULL" not in sql
    assert params == (123.0, "acme")


def test_retention_purge_rejects_missing_active_tenant(monkeypatch):
    from maverick.paths import TenantPolicyError

    monkeypatch.setattr(postgres, "_active_tenant", lambda: None)
    world = PostgresWorldModel.__new__(PostgresWorldModel)

    with pytest.raises(TenantPolicyError, match="refusing unbound Postgres"):
        world.purge_goal_events_before(123.0)


def test_rls_policy_does_not_allow_unset_tenant_bypass():
    sql = PostgresWorldModel._rls_policy_sql("goals").lower()

    assert "current_setting('maverick.tenant'" in sql
    assert "tenant_id = nullif" in sql
    assert " with check " in f" {sql} "
    assert " or " not in sql
    assert "is null" not in sql


def test_rls_apply_raises_when_policy_cannot_be_installed_or_verified(monkeypatch):
    world = PostgresWorldModel.__new__(PostgresWorldModel)

    @contextmanager
    def failing_tx():
        raise PermissionError("no alter table")
        yield  # pragma: no cover

    monkeypatch.setattr(postgres, "_TENANT_TABLES", ["goals"])
    monkeypatch.setattr(world, "_tx", failing_tx)
    monkeypatch.setattr(world, "_rls_policy_is_active", lambda table: False)

    with pytest.raises(RuntimeError, match="could not be installed or verified on goals"):
        world._apply_rls()

def test_rls_covers_tables_added_after_the_v10_tenant_migration():
    # Council #7: projects (v15) and fact_history (v18) carry tenant_id but were
    # absent from the RLS set, so a PG deployment left them app-layer-only. They
    # must be RLS-scoped, while staying OUT of the v10 column-add migration list.
    assert "projects" in postgres._RLS_TABLES
    assert "fact_history" in postgres._RLS_TABLES
    assert "erasure_receipts" in postgres._RLS_TABLES
    assert "approval_audit_outbox" in postgres._RLS_TABLES
    assert "projects" not in postgres._TENANT_TABLES      # not in the v10 ALTER set
    assert "fact_history" not in postgres._TENANT_TABLES
    assert "erasure_receipts" not in postgres._TENANT_TABLES
    assert "approval_audit_outbox" not in postgres._TENANT_TABLES
    # Every v10 tenant table is still RLS-covered.
    assert set(postgres._TENANT_TABLES).issubset(set(postgres._RLS_TABLES))


# --- #51/#57: enterprise auto-on for strict isolation + RLS ----------------

@pytest.fixture
def _clean_toggle_env(monkeypatch):
    for var in ("MAVERICK_STRICT_TENANT_ISOLATION", "MAVERICK_PG_RLS"):
        monkeypatch.delenv(var, raising=False)
    # No config and not enterprise unless a test opts in.
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("maverick.enterprise.enterprise_enabled", lambda: False)


def test_toggles_default_off_without_enterprise(_clean_toggle_env):
    assert postgres._strict_tenant_isolation() is False
    assert postgres._rls_enabled() is False
    assert postgres._rls_explicitly_set() is False


def test_toggles_auto_on_under_enterprise(_clean_toggle_env, monkeypatch):
    monkeypatch.setattr("maverick.enterprise.enterprise_enabled", lambda: True)
    assert postgres._strict_tenant_isolation() is True
    assert postgres._rls_enabled() is True
    # Auto-on is NOT an explicit operator choice -> the boot preflight applies.
    assert postgres._rls_explicitly_set() is False


def test_explicit_env_off_overrides_enterprise(_clean_toggle_env, monkeypatch):
    monkeypatch.setattr("maverick.enterprise.enterprise_enabled", lambda: True)
    monkeypatch.setenv("MAVERICK_PG_RLS", "0")
    monkeypatch.setenv("MAVERICK_STRICT_TENANT_ISOLATION", "off")
    assert postgres._rls_enabled() is False
    assert postgres._strict_tenant_isolation() is False
    assert postgres._rls_explicitly_set() is True


def test_explicit_env_on_is_explicit(_clean_toggle_env, monkeypatch):
    monkeypatch.setenv("MAVERICK_PG_RLS", "1")
    assert postgres._rls_enabled() is True
    assert postgres._rls_explicitly_set() is True


def test_config_toggle_resolves_when_env_absent(_clean_toggle_env, monkeypatch):
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {"world_model": {"rls": True, "strict_tenant_isolation": "yes"}},
    )
    assert postgres._rls_enabled() is True
    assert postgres._strict_tenant_isolation() is True
    assert postgres._rls_explicitly_set() is True


def test_preflight_refuses_boot_on_legacy_null_rows_when_auto_on(monkeypatch):
    # Enterprise auto-on (not explicit) + a table with NULL-tenant rows -> refuse.
    monkeypatch.setattr(postgres, "_rls_explicitly_set", lambda: False)
    monkeypatch.setattr(
        "maverick.world_model_backends.pg_rls.preflight",
        lambda conn: {"role": "app", "ready": False, "tables": {
            "goals": {"null_tenant_rows": 3, "owned_by_current_role": True},
            "facts": {"null_tenant_rows": 0, "owned_by_current_role": True},
        }},
    )
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    world._pool = None
    world.conn = object()
    with pytest.raises(RuntimeError, match="tenant backfill"):
        world._preflight_rls_or_die()


def test_preflight_passes_when_no_null_rows(monkeypatch):
    monkeypatch.setattr(postgres, "_rls_explicitly_set", lambda: False)
    monkeypatch.setattr(
        "maverick.world_model_backends.pg_rls.preflight",
        lambda conn: {"role": "app", "ready": True, "tables": {
            "goals": {"null_tenant_rows": 0, "owned_by_current_role": True},
        }},
    )
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    world._pool = None
    world.conn = object()
    world._preflight_rls_or_die()  # no raise


def test_preflight_skipped_when_explicitly_opted_in(monkeypatch):
    # Explicit MAVERICK_PG_RLS=1 keeps the old fail-closed path: no boot refusal
    # even with NULL rows present (operator knowingly opted in).
    monkeypatch.setattr(postgres, "_rls_explicitly_set", lambda: True)
    called = {"preflight": False}

    def _should_not_run(conn):  # pragma: no cover -- asserted not called
        called["preflight"] = True
        return {"tables": {}}

    monkeypatch.setattr(
        "maverick.world_model_backends.pg_rls.preflight", _should_not_run)
    world = PostgresWorldModel.__new__(PostgresWorldModel)
    world._pool = None
    world.conn = object()
    world._preflight_rls_or_die()
    assert called["preflight"] is False
