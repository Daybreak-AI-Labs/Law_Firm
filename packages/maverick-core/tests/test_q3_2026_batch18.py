"""Q3 2026 batch 18.

  - sql_query tool: permanently read-only SQLite querying and write rejection
    (keyword guard + engine mode=ro + authorizer), params, and row caps.
    Tested against a real stdlib sqlite3 db (no mocks).
"""
from __future__ import annotations

import sqlite3

from maverick.tools.sql_query import sql_query


def _mkdb(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO users VALUES(?, ?)", [(1, "alice"), (2, "bob"), (3, "carol")])
    c.commit()
    c.close()
    return db


def test_select_returns_rows(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({"database": str(db), "query": "SELECT name FROM users ORDER BY id"})
    assert "alice" in out and "bob" in out and "carol" in out
    assert "(3 row(s))" in out


def test_readonly_rejects_write_by_keyword(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({"database": str(db), "query": "DELETE FROM users"})
    assert out.startswith("ERROR") and "read-only" in out.lower()
    # data untouched
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    c.close()


def test_readonly_rejects_commented_write_by_keyword(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({
        "database": str(db),
        "query": "/* sneaky */ UPDATE users SET name='x'",
    })
    assert out.startswith("ERROR") and "read-only" in out.lower()
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM users WHERE name='x'").fetchone()[0] == 0
    c.close()


def test_readonly_blocks_commented_attach_outside_workspace(tmp_path):
    db = _mkdb(tmp_path)
    outside = tmp_path.parent / "outside-attach.db"
    outside.unlink(missing_ok=True)
    out = sql_query().fn({
        "database": str(db),
        "query": f"/* sneaky */ ATTACH DATABASE '{outside}' AS escaped",
    })
    assert out.startswith("ERROR") and "read-only" in out.lower()
    assert not outside.exists()


def test_readonly_authorizer_blocks_commented_vacuum_into(tmp_path):
    db = _mkdb(tmp_path)
    outside = tmp_path.parent / "outside-vacuum.db"
    outside.unlink(missing_ok=True)
    out = sql_query().fn({
        "database": str(db),
        "query": f"/* sneaky */ VACUUM INTO '{outside}'",
    })
    assert out.startswith("ERROR")
    assert not outside.exists()


def test_model_cannot_enable_write_mode_with_read_only_false(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({
        "database": str(db),
        "query": "DELETE FROM users WHERE id = 1",
        "read_only": False,
    })
    assert out.startswith("ERROR") and "permanently read-only" in out
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    c.close()


def test_write_mode_authorizer_blocks_attach_outside_workspace(tmp_path):
    db = _mkdb(tmp_path)
    outside = tmp_path.parent / "outside-write-attach.db"
    outside.unlink(missing_ok=True)

    out = sql_query().fn({
        "database": str(db),
        "query": f"ATTACH DATABASE '{outside}' AS escaped",
        "read_only": False,
    })

    assert out.startswith("ERROR")
    assert not outside.exists()


def test_write_mode_authorizer_blocks_vacuum_into_outside_workspace(tmp_path):
    db = _mkdb(tmp_path)
    outside = tmp_path.parent / "outside-write-vacuum.db"
    outside.unlink(missing_ok=True)

    out = sql_query().fn({
        "database": str(db),
        "query": f"VACUUM INTO '{outside}'",
        "read_only": False,
    })

    assert out.startswith("ERROR")
    assert not outside.exists()


def test_write_with_returning_is_refused(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({
        "database": str(db),
        "query": "INSERT INTO users(id, name) VALUES(4, 'dave') RETURNING id",
        "read_only": False,
    })
    assert out.startswith("ERROR")
    c = sqlite3.connect(db)
    assert c.execute("SELECT name FROM users WHERE id = 4").fetchone() is None
    c.close()


def test_delete_with_returning_is_refused(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({
        "database": str(db),
        "query": "DELETE FROM users WHERE id = 1 RETURNING name",
        "read_only": False,
    })
    c = sqlite3.connect(db)
    assert out.startswith("ERROR")
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    c.close()


def test_params_binding(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({
        "database": str(db),
        "query": "SELECT name FROM users WHERE name = ?",
        "params": ["bob"],
    })
    assert "bob" in out and "alice" not in out and "(1 row(s))" in out


def test_max_rows_truncation(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({"database": str(db), "query": "SELECT * FROM users", "max_rows": 1})
    assert "truncated at 1" in out


def test_missing_database(tmp_path):
    out = sql_query().fn({"database": str(tmp_path / "nope.db"), "query": "SELECT 1"})
    assert "not found" in out.lower()


def test_bad_sql_returns_error(tmp_path):
    db = _mkdb(tmp_path)
    out = sql_query().fn({"database": str(db), "query": "SELECT * FROM does_not_exist"})
    assert out.startswith("ERROR")


def test_multi_statement_does_not_execute_injected_write(tmp_path):
    db = _mkdb(tmp_path)
    # A piggy-backed write must never run -- regardless of how the running
    # Python version handles a multi-statement execute() (<=3.10 raises
    # sqlite3.Warning, 3.11+ raises ProgrammingError, and a hypothetical
    # future could run only the first statement). Asserting the data
    # invariant instead of a version-specific error message is what keeps
    # this test from breaking across the 3.10/3.11/3.12 matrix.
    sql_query().fn({
        "database": str(db),
        "query": "SELECT 1; DROP TABLE users",
        "read_only": False,
    })
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert "users" in tables  # the injected DROP never executed


def test_requires_database_and_query():
    assert "database is required" in sql_query().fn({"query": "SELECT 1"}).lower()
    assert "query is required" in sql_query().fn({"database": "x.db"}).lower()


def test_registered_by_default():
    from maverick.tools import base_registry

    class _FakeSandbox:
        pass

    class _FakeWorld:
        pass

    reg = base_registry(world=_FakeWorld(), sandbox=_FakeSandbox())
    assert "sql_query" in {t.name for t in reg.all()}


def test_schema_requires_database_and_query():
    schema = sql_query().input_schema
    assert schema["required"] == ["database", "query"]
    assert "read_only" not in schema["properties"]
