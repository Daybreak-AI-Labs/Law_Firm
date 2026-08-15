"""World-DB backend for the self-harness learning stores (fleet-shared state).

Phase 1 of ``docs/proposals/fleet-learning-state.md``: the three LEARNING
stores -- the addenda map, the provenance/outcome line-meta sidecar, and the
transfer tried-memory -- can live as world-database tables instead of per-host
JSON files, so a fleet spanning hosts shares one learning state (transfer
sweeps see every host's guidance, outcome evidence aggregates, and two hosts
can't promote conflicting lines). The eval corpus and its harvest sidecars
stay on files this phase (operator data keyed by a configurable path).

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
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# One row per logical file the store replaces.
_TABLES = ("harness_addenda", "harness_line_meta", "harness_transfer_tried")

_SQLITE_DDL = (
    "CREATE TABLE IF NOT EXISTS harness_addenda ("
    " key TEXT PRIMARY KEY, block TEXT NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS harness_line_meta ("
    " line_id TEXT PRIMARY KEY, record TEXT NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS harness_transfer_tried ("
    " line_id TEXT PRIMARY KEY, ts REAL NOT NULL)",
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
_PG_RMW_LOCK = 0x6D766B6C726E  # 'mvklrn'


def enabled() -> bool:
    """True when ``[self_harness] store = "world"`` selects this backend."""
    try:
        from .self_harness import settings
        return str(settings().get("store") or "files").strip().lower() == "world"
    except Exception:  # pragma: no cover -- config trouble means file store
        return False


def _postgres() -> bool:
    try:
        from .world_model_backends import is_postgres_configured
        return is_postgres_configured()
    except Exception:  # pragma: no cover -- backends extra absent
        return False


def _sqlite_path() -> Path:
    """The same tenant-aware world DB location ``open_world()`` resolves
    (``data_dir`` scopes to the active tenant) -- computed at CALL time, so
    env/tenant changes and per-test isolation apply, unlike the import-frozen
    ``world_model.DEFAULT_DB`` constant."""
    from .paths import data_dir
    return data_dir("world.db")


def _pg_dsn() -> str:
    dsn = os.environ.get("MAVERICK_PG_DSN") or ""
    if not dsn:
        try:
            from .config import load_config
            dsn = str(((load_config() or {}).get("world_model") or {})
                      .get("dsn") or "")
        except Exception:  # pragma: no cover
            dsn = ""
    if not dsn:
        raise RuntimeError("world learning store: postgres selected but no "
                           "MAVERICK_PG_DSN / [world_model] dsn configured")
    return dsn


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


@contextlib.contextmanager
def _pg_conn():
    import psycopg  # the [postgres] extra; same dependency rule as the world
    conn = psycopg.connect(_pg_dsn(), autocommit=False)
    try:
        with conn.cursor() as cur:
            for ddl in _SQLITE_DDL:  # DDL is portable: TEXT/REAL map fine
                cur.execute(ddl.replace(" REAL ", " DOUBLE PRECISION "))
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conn():
    return _pg_conn() if _postgres() else _sqlite_conn()


@contextlib.contextmanager
def rmw_lock():
    """DB-side serialization for a whole load-modify-save. A no-op on SQLite
    (the callers' flock already serializes the single host that can reach a
    SQLite file); on Postgres, a session advisory lock held for the critical
    section so two HOSTS can't interleave a read-modify-write."""
    if not _postgres():
        yield
        return
    import psycopg
    conn = psycopg.connect(_pg_dsn(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_PG_RMW_LOCK,))
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_PG_RMW_LOCK,))
        finally:
            conn.close()


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
    """Placeholder list for the active backend's paramstyle."""
    mark = "%s" if _postgres() else "?"
    return ", ".join([mark] * n)


def load_addenda_db() -> dict[str, str]:
    """``{key: block}`` -- the world-store addenda map ({} on any error)."""
    try:
        with _conn() as c:
            return {str(k): str(b) for k, b in
                    _rows(c, "SELECT key, block FROM harness_addenda")}
    except Exception:
        log.debug("learning_store: addenda read failed", exc_info=True)
        return {}


def write_addenda_db(data: dict[str, str]) -> None:
    """Whole-map replace, mirroring the file store's atomic rewrite."""
    now = time.time()
    with _conn() as c:
        _exec(c, "DELETE FROM harness_addenda")
        for k, block in (data or {}).items():
            _exec(c, f"INSERT INTO harness_addenda (key, block, updated_at) "
                     f"VALUES ({_ph(3)})", (str(k), str(block), now))


def load_line_meta_db() -> dict[str, dict]:
    """``{line_id: record}`` provenance sidecar ({} on any error)."""
    out: dict[str, dict] = {}
    try:
        with _conn() as c:
            for lid, rec in _rows(
                    c, "SELECT line_id, record FROM harness_line_meta"):
                try:
                    parsed = json.loads(rec)
                    if isinstance(parsed, dict):
                        out[str(lid)] = parsed
                except ValueError:
                    continue
    except Exception:
        log.debug("learning_store: line-meta read failed", exc_info=True)
    return out


def write_line_meta_db(meta: dict[str, dict]) -> None:
    now = time.time()
    with _conn() as c:
        _exec(c, "DELETE FROM harness_line_meta")
        for lid, rec in (meta or {}).items():
            _exec(c, f"INSERT INTO harness_line_meta (line_id, record, "
                     f"updated_at) VALUES ({_ph(3)})",
                  (str(lid), json.dumps(rec, sort_keys=True), now))


def load_transfer_tried_db() -> dict[str, float]:
    """``{line_id: ts}`` transfer tried-memory ({} on any error)."""
    try:
        with _conn() as c:
            return {str(k): float(ts) for k, ts in
                    _rows(c, "SELECT line_id, ts FROM harness_transfer_tried")}
    except Exception:
        log.debug("learning_store: tried read failed", exc_info=True)
        return {}


def write_transfer_tried_db(tried: dict[str, float]) -> None:
    with _conn() as c:
        _exec(c, "DELETE FROM harness_transfer_tried")
        for lid, ts in (tried or {}).items():
            _exec(c, f"INSERT INTO harness_transfer_tried (line_id, ts) "
                     f"VALUES ({_ph(2)})", (str(lid), float(ts)))


def _corpus_row_sensitive(kind: str) -> bool:
    """True for machine-owned corpus rows that mirror sealed file sidecars."""
    return str(kind) in {"pending", "rejected"}


def _encode_corpus_row(kind: str, row: object) -> str:
    text = json.dumps(row, sort_keys=True)
    if _corpus_row_sensitive(kind):
        from .crypto_at_rest import at_rest_enabled
        if at_rest_enabled():
            from .crypto_at_rest import seal_to_str
            return seal_to_str(text)
    return text


def _decode_corpus_row(kind: str, row: str) -> object:
    if _corpus_row_sensitive(kind):
        from .crypto_at_rest import at_rest_enabled, is_sealed_str, strict_at_rest, unseal_from_str
        if at_rest_enabled():
            if is_sealed_str(row):
                row = unseal_from_str(row)
            elif strict_at_rest():
                log.error(
                    "at-rest strict: withholding an unsealed harness_corpus.%s row",
                    kind,
                )
                raise ValueError("unsealed sensitive corpus row withheld")
            else:
                log.warning(
                    "at-rest: unsealed harness_corpus.%s row "
                    "(pre-migration legacy or tampering); run 'maverick encryption migrate'",
                    kind,
                )
    return json.loads(row)


def load_corpus_db(kind: str) -> dict:
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
                    continue
            if kind == "live":
                for key, row in _rows(
                        c, f"SELECT key, row FROM harness_corpus "
                           f"WHERE kind = {_ph(1)} ORDER BY key, seq",
                        ("extra",)):
                    try:
                        out[str(key)] = _decode_corpus_row("extra", row)
                    except ValueError:
                        continue
    except Exception:
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
    "load_transfer_tried_db", "write_transfer_tried_db",
    "load_corpus_db", "write_corpus_db",
]
