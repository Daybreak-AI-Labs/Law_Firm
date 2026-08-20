"""World-DB backend for matter-scoped self-harness learning stores.

The addenda map, provenance/outcome line metadata, and evaluated corpus may use
the world database instead of file sidecars. Cross-model transfer state is not
part of the firm product.

Selection is the ``[self_harness] store`` knob (``"files"`` default,
``"world"`` opt-in) and the seam lives in :mod:`maverick.self_harness`: an
EXPLICIT ``path`` argument always means the file store at that path (tests
and tenant redirection are unchanged), while the default location resolves to
whichever store is configured. Semantics here mirror the file store exactly:
whole-map reads and whole-map replaces, serialized by the caller's existing
in-process + flock critical section -- plus :func:`rmw_lock`, which adds the
DB-side serialization ``flock`` cannot provide across hosts (a Postgres
advisory lock; SQLite is same-host and needs nothing extra).

Backends ride the world model's own rails: SQLite uses the same
tenant-floored ``world.db`` file, Postgres the same DSN resolution as
``PostgresWorldModel``. Tables are in each backend's migration ladder (v25)
and are also created lazily here, so the store works even if nothing opened
the world first. Reads are tolerant (``{}`` on any error, like the file
loaders); writes propagate so a failed promotion is never silently dropped.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SQLITE_DDL = (
    "CREATE TABLE IF NOT EXISTS harness_addenda ("
    " key TEXT PRIMARY KEY, block TEXT NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS harness_line_meta ("
    " line_id TEXT PRIMARY KEY, record TEXT NOT NULL, updated_at REAL NOT NULL)",
    # Phase 2: the eval-corpus family. ``kind`` is live|pending|rejected plus
    # ``extra`` for a live file's non-list top-level entries (operator
    # annotations like "_meta"), so an export/import round-trip loses nothing.
    # ``seq`` preserves row order -- review indexes are positional.
    "CREATE TABLE IF NOT EXISTS harness_corpus ("
    " kind TEXT NOT NULL, key TEXT NOT NULL, seq INTEGER NOT NULL,"
    " row TEXT NOT NULL, PRIMARY KEY (kind, key, seq))",
)

# Session-scoped Postgres advisory lock serializing the store's whole
# load-modify-save across HOSTS (flock only serializes one host). Fixed
# arbitrary key, distinct from the migration lock.


def enabled() -> bool:
    """True when ``[self_harness] store = "world"`` selects this backend."""
    try:
        from .self_harness import settings
        return str(settings().get("store") or "files").strip().lower() == "world"
    except Exception:  # pragma: no cover -- config trouble means file store
        return False


def _sqlite_path() -> Path:
    """The same tenant-aware world DB location ``open_world()`` resolves
    (``data_dir`` scopes to the active tenant) -- computed at CALL time, so
    env/tenant changes and per-test isolation apply, unlike the import-frozen
    ``world_model.DEFAULT_DB`` constant."""
    from .paths import data_dir
    return data_dir("world.db")


@contextlib.contextmanager
def _sqlite_conn():
    p = _sqlite_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for ddl in _SQLITE_DDL:
            conn.execute(ddl)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conn():
    return _sqlite_conn()


@contextlib.contextmanager
def rmw_lock():
    """DB-side serialization for a whole load-modify-save. A no-op on SQLite:
    the callers' flock already serializes the single host that can reach a
    SQLite file."""
    yield


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    if hasattr(conn, "cursor") and not isinstance(conn, sqlite3.Connection):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    return conn.execute(sql, params).fetchall()


def _exec(conn, sql: str, params: tuple = ()) -> None:
    if hasattr(conn, "cursor") and not isinstance(conn, sqlite3.Connection):
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return
    conn.execute(sql, params)


def _ph(n: int) -> str:
    """Placeholder list for the SQLite paramstyle."""
    return ", ".join(["?"] * n)


def load_addenda_db(*, strict: bool = False) -> dict[str, str]:
    """``{key: block}`` -- the world-store addenda map ({} on any error)."""
    try:
        from .learning_crypto import decode_text, protected_learning_enabled

        with _conn() as c:
            out: dict[str, str] = {}
            for key, block in _rows(c, "SELECT key, block FROM harness_addenda"):
                decoded = decode_text(str(block))
                if decoded is None:
                    if strict or protected_learning_enabled():
                        raise ValueError("unsealed or unreadable harness addendum")
                    continue
                out[str(key)] = decoded
            return out
    except Exception:
        if strict:
            raise
        log.debug("learning_store: addenda read failed", exc_info=True)
        return {}


def write_addenda_db(data: dict[str, str]) -> None:
    """Whole-map replace, mirroring the file store's atomic rewrite."""
    from .learning_crypto import encode_text

    now = time.time()
    with _conn() as c:
        _exec(c, "DELETE FROM harness_addenda")
        for k, block in (data or {}).items():
            _exec(c, f"INSERT INTO harness_addenda (key, block, updated_at) "
                     f"VALUES ({_ph(3)})", (str(k), encode_text(str(block)), now))


def load_line_meta_db(*, strict: bool = False) -> dict[str, dict]:
    """``{line_id: record}`` provenance sidecar ({} on any error)."""
    out: dict[str, dict] = {}
    try:
        from .learning_crypto import decode_text, protected_learning_enabled

        with _conn() as c:
            for lid, rec in _rows(
                    c, "SELECT line_id, record FROM harness_line_meta"):
                try:
                    decoded = decode_text(str(rec))
                    if decoded is None:
                        raise ValueError("unsealed or unreadable harness line metadata")
                    parsed = json.loads(decoded)
                    if isinstance(parsed, dict):
                        out[str(lid)] = parsed
                except ValueError:
                    if strict or protected_learning_enabled():
                        raise
                    continue
    except Exception:
        if strict:
            raise
        log.debug("learning_store: line-meta read failed", exc_info=True)
    return out


def write_line_meta_db(meta: dict[str, dict]) -> None:
    from .learning_crypto import encode_text

    now = time.time()
    with _conn() as c:
        _exec(c, "DELETE FROM harness_line_meta")
        for lid, rec in (meta or {}).items():
            _exec(c, f"INSERT INTO harness_line_meta (line_id, record, "
                     f"updated_at) VALUES ({_ph(3)})",
                  (str(lid), encode_text(json.dumps(rec, sort_keys=True)), now))


def _encode_corpus_row(kind: str, row: object) -> str:
    del kind
    text = json.dumps(row, sort_keys=True)
    from .learning_crypto import encode_text

    return encode_text(text)


def _decode_corpus_row(kind: str, row: str) -> object:
    from .learning_crypto import decode_text

    decoded = decode_text(str(row))
    if decoded is None:
        raise ValueError(f"unsealed or unreadable harness_corpus.{kind} row")
    return json.loads(decoded)


def load_corpus_db(kind: str, *, strict: bool = False) -> dict:
    """The corpus family from the world store, in the FILE shape:
    ``kind="live"`` returns ``{key: [row-dicts]}`` merged with any preserved
    non-list top-level entries (stored under kind ``extra``); ``"pending"``
    returns ``{key: [row-dicts]}``; ``"rejected"`` returns ``{key: [goals]}``.
    ``{}`` on any error, like the tolerant file loaders."""
    out: dict = {}
    try:
        with _conn() as c:
            for key, row in _rows(
                    c, f"SELECT key, row FROM harness_corpus WHERE kind = {_ph(1)}"
                       " ORDER BY key, seq", (str(kind),)):
                try:
                    out.setdefault(str(key), []).append(_decode_corpus_row(str(kind), row))
                except ValueError:
                    from .learning_crypto import protected_learning_enabled

                    if protected_learning_enabled():
                        raise
                    continue
            if kind == "live":
                for key, row in _rows(
                        c, f"SELECT key, row FROM harness_corpus "
                           f"WHERE kind = {_ph(1)} ORDER BY key, seq",
                        ("extra",)):
                    try:
                        out[str(key)] = _decode_corpus_row("extra", row)
                    except ValueError:
                        from .learning_crypto import protected_learning_enabled

                        if protected_learning_enabled():
                            raise
                        continue
    except Exception:
        if strict:
            raise
        log.debug("learning_store: corpus read failed (%s)", kind, exc_info=True)
        return {}
    return out


def write_corpus_db(kind: str, data: dict) -> None:
    """Whole-family replace for ``kind``, mirroring the file store's atomic
    rewrite. For ``"live"``, non-list top-level values are preserved under the
    ``extra`` kind so operator annotations survive the round-trip."""
    with _conn() as c:
        _exec(c, f"DELETE FROM harness_corpus WHERE kind = {_ph(1)}",
              (str(kind),))
        if kind == "live":
            _exec(c, f"DELETE FROM harness_corpus WHERE kind = {_ph(1)}",
                  ("extra",))
        for key, val in (data or {}).items():
            if kind == "live" and not isinstance(val, list):
                _exec(c, f"INSERT INTO harness_corpus (kind, key, seq, row) "
                         f"VALUES ({_ph(4)})",
                      ("extra", str(key), 0, _encode_corpus_row("extra", val)))
                continue
            for i, row in enumerate(val if isinstance(val, list) else []):
                _exec(c, f"INSERT INTO harness_corpus (kind, key, seq, row) "
                         f"VALUES ({_ph(4)})",
                      (str(kind), str(key), i, _encode_corpus_row(str(kind), row)))


__all__ = [
    "enabled", "rmw_lock",
    "load_addenda_db", "write_addenda_db",
    "load_line_meta_db", "write_line_meta_db",
    "load_corpus_db", "write_corpus_db",
]
