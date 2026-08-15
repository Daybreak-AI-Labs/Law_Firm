"""Security-hardening behaviours added after the adversarial review:
single-use TOTP, session revocation, rate-limit lockout, header-only serve
token, atomic first-owner, CSRF Origin check, and the audit int/float fix."""
from __future__ import annotations

from fastapi.testclient import TestClient
from vendor_console import audit, security, store
from vendor_console.util import hash_token


def test_totp_single_use_rejects_replay():
    secret = security.new_totp_secret()
    at = 1_700_000_000
    step = security.totp_step(at)
    code = security.totp_now(secret, at=at)
    assert security.totp_match_step(secret, code, at=at, after_step=-1) == step
    # once the step is consumed, the same code (same step) is refused
    assert security.totp_match_step(secret, code, at=at, after_step=step) is None


def test_logout_revokes_the_live_session(admin, app):
    assert admin.get("/customers").status_code == 200
    stolen = dict(admin.cookies)                 # capture a pre-logout copy
    admin.post("/logout")
    thief = TestClient(app)
    thief.cookies.update(stolen)
    r = thief.get("/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"   # epoch bumped


def test_login_locks_out_after_repeated_failures(client, make_staff):
    make_staff(email="boss@daybreak.co")    # password-1234
    for _ in range(5):
        client.post("/login", data={"email": "boss@daybreak.co",
                                    "password": "wrong"})  # pragma: allowlist secret
    # even the CORRECT password is now refused during the lockout window
    r = client.post("/login", data={"email": "boss@daybreak.co",
                                    "password": "password-1234"})  # pragma: allowlist secret
    assert "Too many attempts" in r.text


def test_serve_token_not_accepted_in_query_string(app):
    conn = app.state.conn
    token = "svc_secret"
    store.create_customer(conn, name="Acme", serve_token_hash=hash_token(token))
    c = TestClient(app)
    assert c.get("/api/v1/license", params={"token": token}).status_code == 401
    # header auth still works (404 = no license yet, but auth passed)
    assert c.get("/api/v1/license",
                 headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_create_first_owner_is_atomic(conn):
    sid = store.create_first_owner(conn, email="a@x.co", name="A",
                                   pw_hash=security.hash_password("x"))
    assert sid is not None
    # a second bootstrap (even a different email) is refused once staff exist
    assert store.create_first_owner(conn, email="b@x.co", name="B",
                                    pw_hash=security.hash_password("y")) is None
    assert store.count_staff(conn) == 1


def test_cross_origin_mutation_is_refused(admin):
    r = admin.post("/customers", data={"name": "X"},
                   headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_audit_chain_ok_with_integer_timestamps(conn):
    audit.record(conn, actor="a", action="x", at=1_700_000_000)   # int, not float
    audit.record(conn, actor="a", action="y", at=1_700_000_030)
    ok, broken = audit.verify_chain(conn)
    assert ok and broken == 0


def test_latest_checkins_no_duplicate_on_equal_at(conn):
    cid = store.create_customer(conn, name="Acme")
    store.record_checkin(conn, customer_id=cid, version="a", at=100)
    store.record_checkin(conn, customer_id=cid, version="b", at=100)   # same timestamp
    rows = store.latest_checkins(conn)
    assert len(rows) == 1 and rows[0]["version"] == "b"    # newest by id, deduped


def test_session_secret_too_short_is_refused(monkeypatch):
    monkeypatch.setenv("VENDOR_CONSOLE_SECRET", "short")
    import pytest
    with pytest.raises(RuntimeError, match="at least 16"):
        security.session_key()
