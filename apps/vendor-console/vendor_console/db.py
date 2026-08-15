"""SQLite storage for the vendor console.

One file, WAL mode, foreign keys on. SQLite is right for an internal, low-
concurrency control plane; the schema is Postgres-portable (a later migration
to the platform's postgres backend is a swap of this module, not the callers).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'viewer',   -- owner|admin|support|viewer
    pw_hash       TEXT NOT NULL,
    totp_secret   TEXT NOT NULL DEFAULT '',
    totp_enrolled INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0,  -- single-use TOTP: reject step <= this
    session_epoch INTEGER NOT NULL DEFAULT 0,   -- bumped on logout to revoke live sessions
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    primary_contact TEXT NOT NULL DEFAULT '',
    contact_email   TEXT NOT NULL DEFAULT '',
    posture         TEXT NOT NULL DEFAULT 'connected',  -- connected|vpc|airgapped
    status          TEXT NOT NULL DEFAULT 'trial',      -- trial|active|suspended|churned
    notes           TEXT NOT NULL DEFAULT '',
    serve_token_hash TEXT NOT NULL DEFAULT '',          -- sha256 of the deployment serve token
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    license_id   TEXT NOT NULL UNIQUE,
    tier         TEXT NOT NULL,
    suites_json  TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL DEFAULT '[]',
    seats        INTEGER,
    issued_at    TEXT NOT NULL,
    expires_at   TEXT,
    grace_days   INTEGER NOT NULL DEFAULT 14,
    key_id       TEXT NOT NULL DEFAULT '',
    doc_json     TEXT NOT NULL,                        -- the full signed license
    revoked_at   REAL,
    note         TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_licenses_customer ON licenses(customer_id);

CREATE TABLE IF NOT EXISTS checkins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    deployment_id TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    license_status TEXT NOT NULL DEFAULT '',
    ip            TEXT NOT NULL DEFAULT '',
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_checkins_customer ON checkins(customer_id, at);

CREATE TABLE IF NOT EXISTS releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version        TEXT NOT NULL,
    channel        TEXT NOT NULL DEFAULT 'stable',   -- stable | edge
    min_from       TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    migrations_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    manifest_json  TEXT NOT NULL,                    -- the full signed release manifest
    key_id         TEXT NOT NULL DEFAULT '',
    published_by   TEXT NOT NULL DEFAULT '',
    yanked_at      REAL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_releases_channel ON releases(channel, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_releases_ver_chan ON releases(version, channel);

CREATE TABLE IF NOT EXISTS tickets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    subject        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open',     -- open|in_progress|waiting|resolved|closed
    priority       TEXT NOT NULL DEFAULT 'normal',   -- low|normal|high|urgent
    tier           TEXT NOT NULL DEFAULT '',
    agent_version  TEXT NOT NULL DEFAULT '',
    summary_json   TEXT NOT NULL DEFAULT '{}',       -- redacted ticket_summary
    bundle_json    TEXT NOT NULL DEFAULT '{}',       -- the redacted support bundle
    assignee       TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status, id);
CREATE INDEX IF NOT EXISTS ix_tickets_customer ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS ix_tickets_correlation ON tickets(correlation_id);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author     TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'note',         -- note | status
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comments_ticket ON ticket_comments(ticket_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL DEFAULT '',
    hash        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_db_path() -> Path:
    env = os.environ.get("VENDOR_CONSOLE_DB")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("DAYBREAK_HOME", Path.home() / ".daybreak")) / "vendor-console.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Additive column migrations (CREATE TABLE IF NOT EXISTS won't alter an
    # existing table). Each is idempotent — a duplicate-column error means it's
    # already there. Add new columns here as the schema grows.
    for stmt in (
        "ALTER TABLE customers ADD COLUMN channel TEXT NOT NULL DEFAULT 'stable'",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),))
    conn.commit()
