"""Relational `database` tool (SQLAlchemy). Driver/network-free paths."""
from __future__ import annotations


def test_database_registers(tmp_path):
    from maverick.sandbox.local import LocalBackend
    from maverick.tools import base_registry

    class _W:
        def open_questions(self, gid):
            return []

    names = {t.name for t in base_registry(_W(), LocalBackend(workdir=tmp_path)).all()}
    assert "database" in names


def test_requires_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from maverick.tools.database_tool import database_tool
    out = database_tool().fn({"op": "query", "sql": "select 1"})
    assert "ERROR" in out and "DATABASE_URL" in out


def test_write_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool
    out = database_tool().fn({"op": "query", "sql": "DELETE FROM t WHERE 1=1"})
    assert "DRY RUN" in out


def test_read_returns_error_gracefully_without_server(monkeypatch):
    # No reachable DB / maybe no driver: must return an ERROR string, not raise.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
    from maverick.tools.database_tool import database_tool
    out = database_tool().fn({"op": "query", "sql": "SELECT 1"})
    assert out.startswith("ERROR")


def test_cte_prefixed_write_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({
        "op": "query",
        "sql": "WITH victims AS (SELECT id FROM users) DELETE FROM users USING victims",
    })

    assert "DRY RUN" in out


def test_explain_prefixed_sql_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({"op": "query", "sql": "EXPLAIN ANALYZE DELETE FROM users"})

    assert "DRY RUN" in out


def test_pragma_is_always_governed_as_potentially_mutating(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from maverick.tools.database_tool import database_tool

    for sql in (
        "PRAGMA user_version",
        "PRAGMA user_version = 7",
        "PRAGMA writable_schema=ON",
        "PRAGMA journal_mode=WAL",
    ):
        assert "DRY RUN" in database_tool().fn({"op": "query", "sql": sql}), sql


def test_select_forms_with_known_side_effects_need_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    for sql in (
        "SELECT 'owned' INTO OUTFILE '/tmp/pwn'",
        "SELECT * INTO copied_accounts FROM accounts",
        "SELECT * FROM accounts FOR UPDATE",
        "SELECT GET_LOCK('nightly', 30)",
        "SELECT nextval('invoice_seq')",
    ):
        assert "DRY RUN" in database_tool().fn({"op": "query", "sql": sql}), sql


def test_comment_prefixed_write_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    for sql in (
        "/* select */ DELETE FROM users",
        "-- SELECT\nDROP TABLE users",
        "  (/* select */ UPDATE users SET admin = true)",
    ):
        out = database_tool().fn({"op": "query", "sql": sql})
        assert "DRY RUN" in out


def test_returning_write_is_committed(monkeypatch, tmp_path):
    # A confirmed write with a RETURNING clause reports returns_rows, so it took
    # the row path -- SQLAlchemy 2.0 is commit-as-you-go, so without an explicit
    # commit the connection rolls the write back on block exit and the mutation
    # is silently lost. Use a file-backed sqlite URL (no driver/network needed).
    import pytest
    pytest.importorskip("sqlalchemy")
    db = tmp_path / "d.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    from maverick.tools.database_tool import database_tool
    t = database_tool()
    assert "ok" in t.fn({"op": "query",
                         "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)",
                         "confirm": True}).lower()
    out = t.fn({"op": "query",
                "sql": "INSERT INTO t(id, v) VALUES(1, 'x') RETURNING id",
                "confirm": True})
    assert "1" in out
    # A fresh read (new engine/connection) must see the committed row.
    check = t.fn({"op": "query", "sql": "SELECT v FROM t WHERE id = 1"})
    assert "x" in check


def test_row_returning_read_classified_sql_is_not_committed(monkeypatch):
    import sys
    import types

    committed = {"called": False}

    class _Result:
        returns_rows = True

        def keys(self):
            return ["n"]

        def fetchmany(self, _size):
            return [(1,)]

    class _Conn:
        def execute(self, _stmt):
            return _Result()

        def commit(self):
            committed["called"] = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

        def dispose(self):
            pass

    fake_sa = types.ModuleType("sqlalchemy")
    fake_sa.create_engine = lambda url: _Engine()
    fake_sa.text = lambda s: s
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake_sa)

    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({
        "op": "query",
        "sql": "SELECT perform_side_effect()",
        "url": "postgresql+psycopg://u:p@localhost/db",
    })

    assert "columns: ['n']" in out
    assert committed["called"] is False


def test_mysql_executable_comment_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    for sql in (
        "/*! INSERT INTO audit_log */ SELECT * FROM users",
        "/*!80000 INSERT INTO audit_log */ SELECT * FROM users",
        "/*M! INSERT INTO audit_log */ SELECT * FROM users",
        "/*M!100100 INSERT INTO audit_log */ SELECT * FROM users",
    ):
        out = database_tool().fn({"op": "query", "sql": sql})
        assert "DRY RUN" in out


def test_comment_prefixed_read_is_allowed(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({"op": "query", "sql": "/* allowed */ -- still allowed\nSELECT 1"})

    assert "DATABASE_URL" in out


def test_stacked_write_behind_select_needs_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    from maverick.tools.database_tool import database_tool

    for sql in (
        "SELECT 1; DELETE FROM users",
        "SELECT * FROM t WHERE x=1; DROP TABLE t",
    ):
        out = database_tool().fn({"op": "query", "sql": sql})
        assert "DRY RUN" in out, sql


def test_host_param_in_query_string_is_denied(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from maverick.tools.database_tool import database_tool

    # libpq honors ?host=; the netloc host is empty so the old check failed open.
    for url in (
        "postgresql:///db?host=evil.com",
        "postgresql+psycopg://u@/db?host=evil.com",
    ):
        out = database_tool().fn({
            "op": "query",
            "sql": "SELECT 1",
            "url": url,
            "_capability_allow_hosts": ("*.corp.internal",),
        })
        assert "DENIED by capability" in out, url


def test_hostless_url_fails_closed_under_allowlist(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({
        "op": "query",
        "sql": "SELECT 1",
        "url": "postgresql:///db",
        "_capability_allow_hosts": ("*.corp.internal",),
    })
    assert "DENIED by capability" in out


def test_row_limit_is_clamped(monkeypatch):
    monkeypatch.delenv("MAVERICK_DATABASE_MAX_ROWS", raising=False)
    import types

    captured = {}

    class _Result:
        returns_rows = True

        def keys(self):
            return ["n"]

        def fetchmany(self, size):
            captured["size"] = size
            return []

    class _Conn:
        def execute(self, _stmt):
            return _Result()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    disposed = {"called": False}

    class _Engine:
        def connect(self):
            return _Conn()

        def dispose(self):
            disposed["called"] = True

    fake_sa = types.ModuleType("sqlalchemy")
    fake_sa.create_engine = lambda url: _Engine()
    fake_sa.text = lambda s: s
    monkeypatch.setitem(__import__("sys").modules, "sqlalchemy", fake_sa)

    from maverick.tools.database_tool import database_tool

    database_tool().fn({
        "op": "query",
        "sql": "SELECT 1",
        "url": "postgresql+psycopg://u:p@localhost/db",
        "limit": 100_000_000,
    })
    assert captured["size"] == 1000  # clamped to _DEFAULT_MAX_ROWS
    assert disposed["called"] is True  # engine pool released


def test_database_url_host_scope_applies_to_env_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@evil.com/db")
    from maverick.tools.database_tool import database_tool

    out = database_tool().fn({
        "op": "query",
        "sql": "SELECT 1",
        "_capability_allow_hosts": ("*.example.com",),
    })

    assert "DENIED by capability" in out
    assert "evil.com" in out
