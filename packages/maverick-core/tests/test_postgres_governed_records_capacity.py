"""Live coverage for cross-replica governed-record capacity admission.

The unit suite proves the emitted SQL with a transactional double.  This test
uses independent real psycopg connections so PostgreSQL itself must serialize
the count-and-insert critical section with ``pg_advisory_xact_lock``.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from maverick.governed_records import (
    GovernedRecordCapacityError,
    PostgresGovernedRecordBackend,
)
from maverick.paths import tenant_scope

_DSN = os.environ.get("MAVERICK_PG_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="MAVERICK_PG_DSN not set (no Postgres service)",
)

_CAPACITY = 3
_ATTEMPTS = 10


@pytest.fixture
def shared_encryption_authority(tmp_path, monkeypatch):
    """Pin one fleet key without consulting developer-machine key material."""
    from maverick import crypto_at_rest, governed_records
    from maverick.config import reset_config_cache

    key = hashlib.sha256(
        b"live-postgres-governed-record-capacity-regression"
    ).digest()
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_PER_TENANT", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", key.hex())
    monkeypatch.setenv(
        "MAVERICK_ENCRYPTION_KEY_DIGEST",
        "sha256:" + hashlib.sha256(key).hexdigest(),
    )
    monkeypatch.setattr(
        crypto_at_rest,
        "_KEY_PATH",
        tmp_path / "keys" / "at_rest.key",
    )
    reset_config_cache()
    crypto_at_rest._reset_shared_key_identity_for_testing()
    governed_records._reset_process_authority_pin_for_testing()
    try:
        yield
    finally:
        crypto_at_rest._reset_shared_key_identity_for_testing()
        governed_records._reset_process_authority_pin_for_testing()
        reset_config_cache()


@pytest.fixture
def governed_record_dsn():
    """Return a real least-privilege DSN and remove any authority we create.

    The repository's Postgres CI service exposes a superuser DSN, while the
    governed backend correctly refuses superuser and BYPASSRLS identities.
    Mirror the existing live RLS test pattern by provisioning a unique
    non-superuser role and schema in that case.  A production-style
    least-privilege DSN is used directly.
    """
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    suffix = uuid.uuid4().hex[:16]
    role = f"mvk_gr_cap_{suffix}"
    schema = f"mvk_gr_cap_{suffix}"
    password = secrets.token_urlsafe(32)
    provisioned = False
    try:
        with (
            psycopg.connect(_DSN, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            identity = cur.fetchone()
            assert identity is not None
            requires_test_role = bool(identity[0]) or bool(identity[1])
            if requires_test_role:
                cur.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                        "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                provisioned = True
                cur.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        sql.Identifier(schema),
                        sql.Identifier(role),
                    )
                )

        app_dsn = (
            make_conninfo(
                _DSN,
                user=role,
                password=password,
                options=f"-c search_path={schema}",
            )
            if provisioned
            else _DSN
        )
        yield app_dsn
    finally:
        if provisioned:
            with (
                psycopg.connect(_DSN, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(role)
                    )
                )


def _authoritative_count(
    dsn: str,
    *,
    tenant: str,
    namespace: str,
) -> int:
    """Read the real table under the same forced-RLS tenant identity."""
    import psycopg

    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('maverick.tenant', %s, true)",
            (tenant,),
        )
        cur.execute(
            "SELECT COUNT(*) FROM governed_records "
            "WHERE tenant_id=%s AND namespace=%s",
            (tenant, namespace),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _delete_test_rows(
    dsn: str,
    *,
    tenant: str,
    namespace: str,
) -> None:
    """Delete only this test's unique tenant/namespace from a shared schema."""
    import psycopg

    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(current_schema() || '.governed_records')"
        )
        table = cur.fetchone()
        if table is None or table[0] is None:
            return
        cur.execute(
            "SELECT set_config('maverick.tenant', %s, true)",
            (tenant,),
        )
        cur.execute(
            "DELETE FROM governed_records "
            "WHERE tenant_id=%s AND namespace=%s",
            (tenant, namespace),
        )


def test_concurrent_distinct_creates_never_exceed_postgres_capacity(
    governed_record_dsn,
    shared_encryption_authority,
):
    """Independent backends admit exactly the cap across concurrent inserts."""
    del shared_encryption_authority
    suffix = uuid.uuid4().hex
    tenant = f"live-pg-capacity-{suffix}"
    namespace = f"live_pg_capacity_{suffix}"
    prefix = "LGC"
    start = threading.Barrier(_ATTEMPTS, timeout=20)
    backends = [
        PostgresGovernedRecordBackend(
            namespace,
            prefix,
            dsn=governed_record_dsn,
            require_bound_tenant=True,
        )
        for _ in range(_ATTEMPTS)
    ]

    def create(index: int) -> tuple[str, str]:
        # One backend per caller models independent application replicas.  No
        # injected connection seam or fake connection participates.
        backend = backends[index]
        record_id = f"{prefix}-{index:02d}"
        now = time.time()
        with tenant_scope(tenant=tenant):
            start.wait()
            try:
                backend.save(
                    {
                        "id": record_id,
                        "created_at": now,
                        "updated_at": now,
                        "worker": index,
                    },
                    expected_revision=0,
                    max_records=_CAPACITY,
                )
            except GovernedRecordCapacityError:
                return "capacity", record_id
        return "created", record_id

    try:
        # Provision and verify the schema before the rendezvous.  Otherwise the
        # schema-migration advisory lock would serialize first use and weaken
        # the capacity-race regression this test is intended to exercise.
        with tenant_scope(tenant=tenant):
            assert all(backend.list() == [] for backend in backends)

        with ThreadPoolExecutor(max_workers=_ATTEMPTS) as pool:
            outcomes = list(pool.map(create, range(_ATTEMPTS)))

        created = {record_id for outcome, record_id in outcomes
                   if outcome == "created"}
        refused = {record_id for outcome, record_id in outcomes
                   if outcome == "capacity"}
        assert len(created) == _CAPACITY
        assert len(refused) == _ATTEMPTS - _CAPACITY
        assert created.isdisjoint(refused)

        # No row is deleted before this query, so the final database count is
        # also this test's authoritative high-water mark.
        count = _authoritative_count(
            governed_record_dsn,
            tenant=tenant,
            namespace=namespace,
        )
        assert count <= _CAPACITY, (
            "database-authoritative governed-record count exceeded its cap"
        )
        assert count == len(created) == _CAPACITY

        with tenant_scope(tenant=tenant):
            persisted = {record["id"] for record in backends[0].list()}
        assert persisted == created
    finally:
        _delete_test_rows(
            governed_record_dsn,
            tenant=tenant,
            namespace=namespace,
        )
