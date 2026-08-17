"""Shared governed-record primitive for product and evidence stores."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

import pytest
from maverick import governed_records
from maverick.governed_records import (
    GovernedRecordBackendError,
    GovernedRecordCapacityError,
    GovernedRecordStore,
)
from maverick.privacy_ops import PrivacyStateError, RecordConflict


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    from maverick import crypto_at_rest

    crypto_at_rest._reset_shared_key_identity_for_testing()
    governed_records._reset_process_authority_pin_for_testing()
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_PG_RLS", "0")
    for name in (
        "MAVERICK_GOVERNED_RECORDS_BACKEND",
        "MAVERICK_ENCRYPTION_KEY",
        "MAVERICK_ENCRYPTION_KEY_DIGEST",
        "MAVERICK_PG_DSN",
        "MAVERICK_REPLICA_COUNT",
        "MAVERICK_WORLD_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    key = b"k" * 32
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", key.hex())
    monkeypatch.setenv(
        "MAVERICK_ENCRYPTION_KEY_DIGEST",
        "sha256:" + hashlib.sha256(key).hexdigest(),
    )
    yield
    crypto_at_rest._reset_shared_key_identity_for_testing()
    governed_records._reset_process_authority_pin_for_testing()


def _pin_shared_encryption_key(monkeypatch, key: bytes = b"k" * 32) -> str:
    digest = "sha256:" + hashlib.sha256(key).hexdigest()
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", key.hex())
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY_DIGEST", digest)
    return digest


class _MemoryPg:
    """Small transactional psycopg double; no live service is required."""

    def __init__(self):
        self.rows: dict[tuple[str, str, str], tuple[int, str, float, float]] = {}
        self.sql: list[tuple[str, tuple]] = []
        self.lock = threading.RLock()
        self.table_exists = True
        self.columns = (
            ("tenant_id", "text", "NO"),
            ("namespace", "text", "NO"),
            ("record_id", "text", "NO"),
            ("revision", "bigint", "NO"),
            ("record_json", "text", "NO"),
            ("created_at", "double precision", "NO"),
            ("updated_at", "double precision", "NO"),
        )
        self.primary_key = ["tenant_id", "namespace", "record_id"]
        self.checks = [
            "CHECK ((char_length(tenant_id) >= 1) AND "
            "(char_length(tenant_id) <= 201))",
            "CHECK ((char_length(namespace) >= 1) AND "
            "(char_length(namespace) <= 128))",
            "CHECK ((record_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'::text))",
            "CHECK ((revision >= 1))",
            "CHECK ((octet_length(record_json) <= 25165824))",
        ]
        self.rls_enabled = True
        self.rls_forced = True
        self.superuser = False
        self.bypassrls = False
        self.allow_ddl = True
        self.clock_time = 1_700_000_123.25
        expression = (
            "tenant_id = nullif(current_setting('maverick.tenant', true), '')"
        )
        self.policies: list[tuple] = [(
            "mvk_tenant_isolation",
            "PERMISSIVE",
            ["public"],
            "ALL",
            expression,
            expression,
        )]

    def connect(self, _dsn, *, autocommit=False):
        assert autocommit is False
        return _MemoryConnection(self)


class _MemoryConnection:
    def __init__(self, database: _MemoryPg):
        self.database = database
        self.database.lock.acquire()
        self.snapshot = copy.deepcopy({
            "rows": database.rows,
            "table_exists": database.table_exists,
            "columns": database.columns,
            "primary_key": database.primary_key,
            "checks": database.checks,
            "rls_enabled": database.rls_enabled,
            "rls_forced": database.rls_forced,
            "policies": database.policies,
        })
        self.closed = False

    def cursor(self):
        return _MemoryCursor(self.database)

    def commit(self):
        return None

    def rollback(self):
        self.database.rows.clear()
        self.database.rows.update(self.snapshot["rows"])
        self.database.table_exists = self.snapshot["table_exists"]
        self.database.columns = self.snapshot["columns"]
        self.database.primary_key = self.snapshot["primary_key"]
        self.database.checks = self.snapshot["checks"]
        self.database.rls_enabled = self.snapshot["rls_enabled"]
        self.database.rls_forced = self.snapshot["rls_forced"]
        self.database.policies = self.snapshot["policies"]

    def close(self):
        if not self.closed:
            self.closed = True
            self.database.lock.release()


class _MemoryCursor:
    def __init__(self, database: _MemoryPg):
        self.database = database
        self.result: list[tuple] = []
        self.rowcount = -1

    def execute(self, query, params=()):  # noqa: C901 - explicit SQL contract double
        sql = " ".join(str(query).split())
        values = tuple(params or ())
        self.database.sql.append((sql, values))
        self.result = []
        self.rowcount = -1
        if sql.startswith("SELECT extract(epoch FROM clock_timestamp())"):
            self.result = [(self.database.clock_time,)]
            self.rowcount = 1
            return
        if sql.startswith("SELECT rolsuper, rolbypassrls"):
            self.result = [(
                self.database.superuser,
                self.database.bypassrls,
            )]
            self.rowcount = 1
            return
        if sql.startswith("SELECT to_regclass("):
            self.result = [(
                "governed_records" if self.database.table_exists else None,
            )]
            self.rowcount = 1
            return
        if sql.startswith("SELECT column_name, data_type, is_nullable"):
            self.result = list(self.database.columns)
            self.rowcount = len(self.result)
            return
        if sql.startswith("SELECT array_agg(a.attname"):
            self.result = [(list(self.database.primary_key),)]
            self.rowcount = 1
            return
        if sql.startswith("SELECT pg_get_constraintdef(oid)"):
            self.result = [(check,) for check in self.database.checks]
            self.rowcount = len(self.result)
            return
        if sql.startswith("CREATE TABLE "):
            if not self.database.allow_ddl:
                raise PermissionError("runtime role cannot create tables")
            self.database.table_exists = True
            self.database.rls_enabled = False
            self.database.rls_forced = False
            self.database.policies = []
            self.rowcount = 1
            return
        if sql.startswith("CREATE INDEX "):
            if not self.database.allow_ddl:
                raise PermissionError("runtime role cannot create indexes")
            self.rowcount = 1
            return
        if sql.startswith("ALTER TABLE "):
            if not self.database.allow_ddl:
                raise PermissionError("runtime role cannot alter tables")
            if "ENABLE ROW LEVEL SECURITY" in sql:
                self.database.rls_enabled = True
            if "FORCE ROW LEVEL SECURITY" in sql:
                self.database.rls_forced = True
            self.rowcount = 1
            return
        if sql.startswith("DROP POLICY "):
            if not self.database.allow_ddl:
                raise PermissionError("runtime role cannot drop policies")
            self.database.policies = [
                policy
                for policy in self.database.policies
                if policy[0] != "mvk_tenant_isolation"
            ]
            self.rowcount = 1
            return
        if sql.startswith("CREATE POLICY "):
            if not self.database.allow_ddl:
                raise PermissionError("runtime role cannot create policies")
            expression = (
                "tenant_id = nullif(current_setting('maverick.tenant', true), '')"
            )
            self.database.policies.append((
                "mvk_tenant_isolation",
                "PERMISSIVE",
                ["public"],
                "ALL",
                expression,
                expression,
            ))
            self.rowcount = 1
            return
        if sql.startswith("SELECT relrowsecurity"):
            self.result = [(
                self.database.rls_enabled,
                self.database.rls_forced,
            )]
            self.rowcount = 1
            return
        if sql.startswith("SELECT policyname, permissive"):
            self.result = list(self.database.policies)
            self.rowcount = len(self.result)
            return
        if sql.startswith((
            "SELECT set_config(",
            "SELECT pg_advisory_xact_lock(",
        )):
            self.rowcount = 1
            return
        if sql.startswith("SELECT COUNT(*) FROM governed_records"):
            tenant, namespace = str(values[0]), str(values[1])
            count = sum(
                row_tenant == tenant and row_namespace == namespace
                for row_tenant, row_namespace, _record_id in self.database.rows
            )
            self.result = [(count,)]
            self.rowcount = 1
            return
        if sql.startswith("SELECT revision, record_json"):
            key = (str(values[0]), str(values[1]), str(values[2]))
            row = self.database.rows.get(key)
            self.result = [row] if row is not None else []
            self.rowcount = len(self.result)
            return
        if sql.startswith("INSERT INTO governed_records"):
            key = (str(values[0]), str(values[1]), str(values[2]))
            if key in self.database.rows:
                self.rowcount = 0
            else:
                self.database.rows[key] = (
                    int(values[3]),
                    str(values[4]),
                    float(values[5]),
                    float(values[6]),
                )
                self.rowcount = 1
            return
        if sql.startswith("UPDATE governed_records"):
            key = (str(values[4]), str(values[5]), str(values[6]))
            current = self.database.rows.get(key)
            if current is None or current[0] != int(values[7]):
                self.rowcount = 0
            else:
                self.database.rows[key] = (
                    int(values[0]),
                    str(values[1]),
                    float(values[2]),
                    float(values[3]),
                )
                self.rowcount = 1
            return
        if sql.startswith("SELECT record_id, revision"):
            tenant, namespace = str(values[0]), str(values[1])
            matches = [
                (record_id, *row)
                for (row_tenant, row_namespace, record_id), row
                in self.database.rows.items()
                if row_tenant == tenant and row_namespace == namespace
            ]
            self.result = sorted(matches, key=lambda row: (-row[3], row[0]))
            if " LIMIT %s" in sql:
                self.result = self.result[:int(values[2])]
            self.rowcount = len(self.result)
            return
        if sql.startswith("SELECT record_id FROM governed_records"):
            tenant, namespace, cursor, limit = (
                str(values[0]),
                str(values[1]),
                str(values[2]),
                int(values[3]),
            )
            ids = [
                record_id
                for row_tenant, row_namespace, record_id in self.database.rows
                if row_tenant == tenant and row_namespace == namespace
            ]
            ids.sort(key=lambda record_id: (record_id <= cursor, record_id))
            self.result = [(record_id,) for record_id in ids[:limit]]
            self.rowcount = len(self.result)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def close(self):
        return None


def test_create_update_and_stale_cas_are_audited(monkeypatch):
    import maverick.audit as audit

    # Local single-replica compatibility does not impose the Postgres
    # application-encryption floor.
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    events: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        audit,
        "record",
        lambda kind, **payload: events.append((kind, payload)) or True,
    )
    store = GovernedRecordStore("test_governed", "TGR", "test_record")

    long_actor = "alice@example.com/" + "a" * 300
    created = store.create(
        {"id": store.new_id(), "status": "open", "title": "Observed condition"},
        action="create",
        actor=long_actor,
    )
    assert created["revision"] == 1
    assert "_audit_pending" not in created
    assert events[-1][1]["record_type"] == "test_record"
    assert events[-1][1]["action"] == "create"
    assert events[-1][1]["record_sha256"]
    assert events[-1][1]["actor"] != long_actor
    assert "#sha256:" in events[-1][1]["actor"]

    updated = store.update(
        created["id"],
        lambda row: row.update(status="closed"),
        expected_revision=created["revision"],
        action="close",
        actor="bob@example.com",
    )
    assert updated is not None
    assert updated["revision"] == 2
    assert updated["status"] == "closed"

    with pytest.raises(RecordConflict):
        store.update(
            created["id"],
            lambda row: row.update(status="stale"),
            expected_revision=created["revision"],
            action="stale_update",
            actor="mallory@example.com",
        )


def test_local_bounded_create_refuses_before_namespace_oversize(monkeypatch):
    import maverick.audit as audit

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_capacity", "TCP", "test_record")
    first = store.create(
        {"id": "TCP-first", "status": "open"},
        action="create",
        actor="operator",
        max_records=1,
    )

    with pytest.raises(
        GovernedRecordCapacityError,
        match="capacity has been reached",
    ):
        store.create(
            {"id": "TCP-second", "status": "open"},
            action="create",
            actor="operator",
            max_records=1,
        )
    assert [row["id"] for row in store.list()] == [first["id"]]


def test_public_record_id_scan_is_bounded_and_cursor_rotates(monkeypatch):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_scan", "TGR", "test_record")
    for record_id in ("TGR-charlie", "TGR-alpha", "TGR-bravo"):
        store.create(
            {"id": record_id, "status": "open"},
            action="create",
            actor="operator",
        )

    first = list(store.iter_record_ids(limit=2))
    assert [record_id for _cursor, record_id in first] == [
        "TGR-alpha",
        "TGR-bravo",
    ]
    second = list(store.iter_record_ids(start_after=first[-1][0], limit=2))
    assert [record_id for _cursor, record_id in second] == [
        "TGR-charlie",
        "TGR-alpha",
    ]
    assert all(store.get(record_id) is not None for _cursor, record_id in first)


def test_local_backend_rejects_oversized_record_before_creating_a_file(
    tmp_path,
    monkeypatch,
):
    from maverick import privacy_ops
    from maverick.paths import data_dir

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.setattr(privacy_ops, "_MAX_PRIVACY_JSON_BYTES", 1_024)
    store = GovernedRecordStore("test_oversized", "TGR", "test_record")
    record_id = store.new_id()

    with pytest.raises(PrivacyStateError, match="16 MiB JSON limit"):
        store.create(
            {"id": record_id, "status": "open", "payload": "x" * 2_000},
            action="create",
            actor="operator",
        )

    assert not (data_dir("test_oversized") / f"{record_id}.json").exists()


@pytest.mark.parametrize("authority_scope", ["tenant", "deployment_global"])
def test_local_bounded_list_reads_no_more_than_limit(monkeypatch, authority_scope):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(audit, "record_global", lambda *_args, **_kwargs: True)
    namespace = f"test_bounded_{authority_scope}"
    store = GovernedRecordStore(
        namespace,
        "TBL",
        "test_record",
        authority_scope=authority_scope,
    )
    for index in range(5):
        store.create(
            {"id": f"TBL-record{index}", "status": "open", "index": index},
            action="create",
            actor="operator",
        )

    disk_store = store._local_backend._store
    original_read = disk_store._read_path
    reads = []

    def counted_read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(disk_store, "_read_path", counted_read)
    rows = store.list(limit=2)

    assert len(rows) == 2
    assert len(reads) == 2
    assert all(row["id"].startswith("TBL-") for row in rows)


def test_local_bounded_list_validates_unselected_paths_before_reads(monkeypatch):
    import maverick.audit as audit
    from maverick.file_lock import atomic_write_text

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_bounded_paths", "TBP", "test_record")
    for index in range(2):
        store.create(
            {"id": f"TBP-record{index}", "status": "open"},
            action="create",
            actor="operator",
        )

    disk_store = store._local_backend._store
    atomic_write_text(
        disk_store._dir() / "wrong-prefix.json",
        json.dumps({"id": "wrong-prefix", "revision": 1}),
        mode=0o600,
    )
    original_read = disk_store._read_path
    reads = []

    def counted_read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(disk_store, "_read_path", counted_read)
    with pytest.raises(PermissionError, match="invalid record path"):
        store.list(limit=1)
    assert reads == []


def test_failed_audit_is_durable_and_retryable(monkeypatch):
    import maverick.audit as audit

    accepted = False

    def _record(*_args, **_kwargs):
        return accepted

    monkeypatch.setattr(audit, "record", _record)
    store = GovernedRecordStore("test_retry", "TGR", "test_record")
    created = store.create(
        {"id": store.new_id(), "status": "open"},
        action="create",
        actor="operator",
    )
    assert created["_audit_pending"]

    accepted = True
    assert store.retry_pending(limit=10) == 1
    loaded = store.get(created["id"])
    assert loaded is not None
    assert loaded["revision"] == created["revision"]
    assert "_audit_pending" not in loaded


def test_public_update_cannot_bypass_revision_or_audit(monkeypatch):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_metadata", "TGR", "test_record")
    created = store.create(
        {"id": store.new_id(), "status": "open"},
        action="create",
        actor="operator",
    )
    with pytest.raises(TypeError, match="metadata_only"):
        store.update(
            created["id"],
            lambda row: row.update(status="closed"),
            expected_revision=created["revision"],
            action="metadata",
            actor="system",
            metadata_only=True,
            queue_audit=False,
        )
    loaded = store.get(created["id"])
    assert loaded is not None
    assert loaded["revision"] == created["revision"]
    assert loaded["status"] == "open"


@pytest.mark.parametrize(
    "reserved",
    [
        {"_audit_pending": []},
        {"created_at": 1.0},
        {"updated_at": 1.0},
        {"revision": 99},
    ],
)
def test_create_rejects_store_reserved_fields(monkeypatch, reserved):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_reserved", "TGR", "test_record")
    with pytest.raises(ValueError, match="store-reserved"):
        store.create(
            {"id": store.new_id(), "status": "open", **reserved},
            action="create",
            actor="operator",
        )
    assert store.list() == []


def test_actor_is_required_for_governed_mutations(monkeypatch):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_actor", "TGR", "test_record")
    with pytest.raises(ValueError, match="actor"):
        store.create(
            {"id": store.new_id(), "status": "open"},
            action="create",
            actor="",
        )


def test_over_limit_actor_and_action_are_rejected_without_truncation(monkeypatch):
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)
    store = GovernedRecordStore("test_bounded_actor", "TGR", "test_record")
    record_id = store.new_id()
    with pytest.raises(ValueError, match="actor exceeds"):
        store.create(
            {"id": record_id, "status": "open"},
            action="create",
            actor="a" * 4097,
        )
    with pytest.raises(ValueError, match="action exceeds"):
        store.create(
            {"id": record_id, "status": "open"},
            action="a" * 65,
            actor="operator",
        )
    assert store.list() == []


def test_enterprise_and_multiple_replicas_fail_closed_without_shared_state(
    monkeypatch,
):
    store = GovernedRecordStore("test_selection", "TGS", "test_record")
    monkeypatch.setenv("MAVERICK_GOVERNED_RECORDS_BACKEND", "local")
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    with pytest.raises(GovernedRecordBackendError, match="require a shared"):
        store.new_id()

    monkeypatch.setenv("MAVERICK_ENTERPRISE", "0")
    monkeypatch.setenv("MAVERICK_GOVERNED_RECORDS_BACKEND", "auto")
    monkeypatch.setenv("MAVERICK_REPLICA_COUNT", "2")
    with pytest.raises(GovernedRecordBackendError, match="require a shared"):
        store.new_id()


