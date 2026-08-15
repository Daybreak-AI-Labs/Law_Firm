"""Repository layer — thin, explicit SQL over the SQLite rows. No ORM, no magic;
every query is visible so an internal tool holding signing keys is easy to audit.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .models import Customer, License, Release, Staff, Ticket, TicketComment

# ---- staff -----------------------------------------------------------------

def create_staff(conn: sqlite3.Connection, *, email: str, name: str, role: str,
                 pw_hash: str, totp_secret: str = "", at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO staff(email, name, role, pw_hash, totp_secret, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (email.strip().lower(), name, role, pw_hash, totp_secret,
         at if at is not None else time.time()))
    conn.commit()
    return cur.lastrowid


def create_first_owner(conn: sqlite3.Connection, *, email: str, name: str,
                       pw_hash: str, at: float | None = None) -> int | None:
    """Create the bootstrap owner ONLY while the staff table is empty, atomically
    (INSERT … WHERE NOT EXISTS) so two racing /setup posts can't both win.
    Returns the new id, or None if someone already claimed owner."""
    cur = conn.execute(
        "INSERT INTO staff(email, name, role, pw_hash, created_at) "
        "SELECT ?, ?, 'owner', ?, ? WHERE NOT EXISTS (SELECT 1 FROM staff)",
        (email.strip().lower(), name, pw_hash, at if at is not None else time.time()))
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def get_staff_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM staff WHERE email = ?",
                        (email.strip().lower(),)).fetchone()


def get_staff(conn: sqlite3.Connection, staff_id: int) -> Staff | None:
    r = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return Staff.from_row(r) if r else None


def get_staff_row(conn: sqlite3.Connection, staff_id: int) -> sqlite3.Row | None:
    """Raw row incl. secrets (pw_hash, totp_secret) — for the auth flow only."""
    return conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()


def list_staff(conn: sqlite3.Connection) -> list[Staff]:
    return [Staff.from_row(r) for r in
            conn.execute("SELECT * FROM staff ORDER BY email").fetchall()]


def count_staff(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM staff").fetchone()["n"]


def set_totp(conn: sqlite3.Connection, staff_id: int, secret: str, enrolled: bool) -> None:
    conn.execute("UPDATE staff SET totp_secret = ?, totp_enrolled = ? WHERE id = ?",
                 (secret, 1 if enrolled else 0, staff_id))
    conn.commit()


def set_totp_last_step(conn: sqlite3.Connection, staff_id: int, step: int) -> None:
    """Record the last-consumed TOTP time-step so a code can't be replayed."""
    conn.execute("UPDATE staff SET totp_last_step = ? WHERE id = ?", (step, staff_id))
    conn.commit()


def bump_session_epoch(conn: sqlite3.Connection, staff_id: int) -> None:
    """Invalidate every live session for a staff member (logout / forced sign-out)."""
    conn.execute("UPDATE staff SET session_epoch = session_epoch + 1 WHERE id = ?",
                 (staff_id,))
    conn.commit()


# ---- customers -------------------------------------------------------------

def create_customer(conn: sqlite3.Connection, *, name: str, primary_contact: str = "",
                    contact_email: str = "", posture: str = "connected",
                    status: str = "trial", notes: str = "",
                    serve_token_hash: str = "", at: float | None = None) -> int:
    now = at if at is not None else time.time()
    cur = conn.execute(
        "INSERT INTO customers(name, primary_contact, contact_email, posture, status, "
        "notes, serve_token_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name.strip(), primary_contact, contact_email, posture, status, notes,
         serve_token_hash, now, now))
    conn.commit()
    return cur.lastrowid


def get_customer(conn: sqlite3.Connection, customer_id: int) -> Customer | None:
    r = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return Customer.from_row(r) if r else None


def list_customers(conn: sqlite3.Connection, *, search: str = "") -> list[Customer]:
    if search.strip():
        rows = conn.execute(
            "SELECT * FROM customers WHERE name LIKE ? OR contact_email LIKE ? "
            "ORDER BY name", (f"%{search}%", f"%{search}%")).fetchall()
    else:
        rows = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    return [Customer.from_row(r) for r in rows]


def update_customer(conn: sqlite3.Connection, customer_id: int, **fields) -> None:
    allowed = {"primary_contact", "contact_email", "posture", "status", "notes",
               "serve_token_hash", "channel"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE customers SET {cols}, updated_at = ? WHERE id = ?",
                 (*sets.values(), time.time(), customer_id))
    conn.commit()


def customer_by_serve_token_hash(conn: sqlite3.Connection, token_hash: str) -> Customer | None:
    if not token_hash:
        return None
    r = conn.execute("SELECT * FROM customers WHERE serve_token_hash = ?",
                     (token_hash,)).fetchone()
    return Customer.from_row(r) if r else None


# ---- licenses --------------------------------------------------------------

def add_license(conn: sqlite3.Connection, *, customer_id: int, license_id: str,
                tier: str, suites: list[str], features: list[str], seats: int | None,
                issued_at: str, expires_at: str | None, grace_days: int, key_id: str,
                doc: dict, note: str, created_by: str, at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO licenses(customer_id, license_id, tier, suites_json, features_json, "
        "seats, issued_at, expires_at, grace_days, key_id, doc_json, note, created_by, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (customer_id, license_id, tier, json.dumps(suites), json.dumps(features),
         seats, issued_at, expires_at, grace_days, key_id, json.dumps(doc), note,
         created_by, at if at is not None else time.time()))
    conn.commit()
    return cur.lastrowid


def get_license(conn: sqlite3.Connection, license_id: str) -> License | None:
    r = conn.execute("SELECT * FROM licenses WHERE license_id = ?",
                     (license_id,)).fetchone()
    return License.from_row(r) if r else None


def list_licenses(conn: sqlite3.Connection, customer_id: int) -> list[License]:
    rows = conn.execute(
        "SELECT * FROM licenses WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,)).fetchall()
    return [License.from_row(r) for r in rows]


def revoke_license(conn: sqlite3.Connection, license_id: str, *, note: str = "",
                   at: float | None = None) -> bool:
    cur = conn.execute(
        "UPDATE licenses SET revoked_at = ?, note = ? WHERE license_id = ? "
        "AND revoked_at IS NULL",
        (at if at is not None else time.time(), note, license_id))
    conn.commit()
    return cur.rowcount > 0


# ---- fleet check-ins -------------------------------------------------------

def record_checkin(conn: sqlite3.Connection, *, customer_id: int, deployment_id: str = "",
                   version: str = "", license_status: str = "", ip: str = "",
                   at: float | None = None) -> None:
    conn.execute(
        "INSERT INTO checkins(customer_id, deployment_id, version, license_status, ip, at) "
        "VALUES (?,?,?,?,?,?)",
        (customer_id, deployment_id, version, license_status, ip,
         at if at is not None else time.time()))
    conn.commit()


def latest_checkins(conn: sqlite3.Connection, *, limit: int = 500) -> list[dict]:
    """One row per customer: their most recent check-in (fleet health board).
    Keyed on MAX(id) — strictly increasing — so two check-ins sharing the same
    ``at`` can't double-count a customer."""
    rows = conn.execute(
        "SELECT c.name AS customer, k.* FROM checkins k "
        "JOIN customers c ON c.id = k.customer_id "
        "JOIN (SELECT customer_id, MAX(id) AS mx FROM checkins GROUP BY customer_id) t "
        "  ON t.mx = k.id "
        "ORDER BY k.at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def latest_checkin_for(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    """The single most recent check-in for one customer (targeted — avoids
    scanning the whole fleet on a detail page)."""
    r = conn.execute(
        "SELECT * FROM checkins WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
        (customer_id,)).fetchone()
    return dict(r) if r else None


# ---- releases --------------------------------------------------------------

def add_release(conn: sqlite3.Connection, *, version: str, channel: str, min_from: str,
                notes: str, migrations: list[str], artifacts: list[dict], manifest: dict,
                key_id: str, published_by: str, at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO releases(version, channel, min_from, notes, migrations_json, "
        "artifacts_json, manifest_json, key_id, published_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (version, channel, min_from, notes, json.dumps(migrations),
         json.dumps(artifacts), json.dumps(manifest), key_id, published_by,
         at if at is not None else time.time()))
    conn.commit()
    return cur.lastrowid


def get_release(conn: sqlite3.Connection, release_id: int) -> Release | None:
    r = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    return Release.from_row(r) if r else None


def list_releases(conn: sqlite3.Connection, *, channel: str = "") -> list[Release]:
    if channel:
        rows = conn.execute("SELECT * FROM releases WHERE channel = ? ORDER BY id DESC",
                            (channel,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM releases ORDER BY id DESC").fetchall()
    return [Release.from_row(r) for r in rows]


def newest_release(conn: sqlite3.Connection, channel: str) -> Release | None:
    """The most recently published, non-yanked release on ``channel``."""
    r = conn.execute(
        "SELECT * FROM releases WHERE channel = ? AND yanked_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (channel,)).fetchone()
    return Release.from_row(r) if r else None


def yank_release(conn: sqlite3.Connection, release_id: int, *,
                 at: float | None = None) -> bool:
    cur = conn.execute(
        "UPDATE releases SET yanked_at = ? WHERE id = ? AND yanked_at IS NULL",
        (at if at is not None else time.time(), release_id))
    conn.commit()
    return cur.rowcount > 0


# ---- support tickets -------------------------------------------------------

def create_ticket(conn: sqlite3.Connection, *, customer_id: int | None,
                  correlation_id: str, subject: str, priority: str = "normal",
                  tier: str = "", agent_version: str = "", summary: dict | None = None,
                  bundle: dict | None = None, at: float | None = None) -> int:
    now = at if at is not None else time.time()
    cur = conn.execute(
        "INSERT INTO tickets(customer_id, correlation_id, subject, status, priority, "
        "tier, agent_version, summary_json, bundle_json, created_at, updated_at) "
        "VALUES (?,?,?,'open',?,?,?,?,?,?,?)",
        (customer_id, correlation_id, subject, priority, tier, agent_version,
         json.dumps(summary or {}), json.dumps(bundle or {}), now, now))
    conn.commit()
    return cur.lastrowid


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> Ticket | None:
    r = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return Ticket.from_row(r) if r else None


def ticket_by_correlation(conn: sqlite3.Connection, correlation_id: str) -> Ticket | None:
    if not correlation_id:
        return None
    r = conn.execute(
        "SELECT * FROM tickets WHERE correlation_id = ? ORDER BY id DESC LIMIT 1",
        (correlation_id,)).fetchone()
    return Ticket.from_row(r) if r else None


def list_tickets(conn: sqlite3.Connection, *, status: str = "",
                 customer_id: int | None = None) -> list[Ticket]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if customer_id is not None:
        clauses.append("customer_id = ?")
        params.append(customer_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tickets{where} ORDER BY id DESC", params).fetchall()
    return [Ticket.from_row(r) for r in rows]


def update_ticket(conn: sqlite3.Connection, ticket_id: int, **fields) -> None:
    allowed = {"status", "priority", "assignee", "subject"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE tickets SET {cols}, updated_at = ? WHERE id = ?",
                 (*sets.values(), time.time(), ticket_id))
    conn.commit()


def add_comment(conn: sqlite3.Connection, *, ticket_id: int, author: str, body: str,
                kind: str = "note", at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO ticket_comments(ticket_id, author, body, kind, created_at) "
        "VALUES (?,?,?,?,?)",
        (ticket_id, author, body, kind, at if at is not None else time.time()))
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?",
                 (at if at is not None else time.time(), ticket_id))
    conn.commit()
    return cur.lastrowid


def list_comments(conn: sqlite3.Connection, ticket_id: int) -> list[TicketComment]:
    rows = conn.execute(
        "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id ASC",
        (ticket_id,)).fetchall()
    return [TicketComment.from_row(r) for r in rows]


def ticket_status_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
