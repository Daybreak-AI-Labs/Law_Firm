"""Support desk: bundle intake → triaged ticket, correlation de-dupe, the intake
API, status/comment flow, and RBAC."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from vendor_console import routes_api, store, support
from vendor_console.util import hash_token

BUNDLE = {
    "correlation_id": "sup_abc123",
    "generated_at": "2026-07-03T00:00:00Z",
    "entitlement": {"customer": "Cedar Valley Bank", "tier": "gold",
                    "status": "licensed", "suites": ["fleet"]},
    "versions": {"maverick-agent": "0.1.7"},
    "readiness": {"client_binding": "ok", "shield": "fail: required but unavailable"},
    "recent_failures": {"failure_modes": {"timeout": 3},
                        "failed_jobs": [{"id": 1}, {"id": 2}]},
}


def test_intake_creates_a_triaged_ticket(conn):
    cid = store.create_customer(conn, name="Cedar Valley Bank")
    t = support.intake_bundle(conn, customer_id=cid, bundle=BUNDLE)
    assert t.correlation_id == "sup_abc123" and t.tier == "gold"
    assert t.agent_version == "0.1.7"
    assert t.priority == "high"                       # failing readiness → high
    assert "shield" in t.subject
    assert t.summary["readiness_failing"] == ["shield"]


def test_intake_dedupes_by_correlation_id(conn):
    cid = store.create_customer(conn, name="Acme")
    t1 = support.intake_bundle(conn, customer_id=cid, bundle=BUNDLE)
    t2 = support.intake_bundle(conn, customer_id=cid, bundle=BUNDLE)   # resend
    assert t2.id == t1.id                             # no duplicate ticket
    assert len(store.list_tickets(conn)) == 1
    assert len(store.list_comments(conn, t1.id)) == 1  # appended a note instead


def test_intake_api(app):
    conn = app.state.conn
    token = "svc_support_bbbb"    # pragma: allowlist secret
    store.create_customer(conn, name="Acme", serve_token_hash=hash_token(token))
    c = TestClient(app)
    r = c.post("/api/v1/support", headers={"Authorization": f"Bearer {token}"},
               json=BUNDLE)
    assert r.status_code == 201
    body = r.json()
    assert body["correlation_id"] == "sup_abc123" and body["status"] == "open"
    assert len(store.list_tickets(conn)) == 1
    # bad token, and a non-object body
    assert c.post("/api/v1/support", headers={"Authorization": "Bearer no"},
                  json={}).status_code == 401
    assert c.post("/api/v1/support", headers={"Authorization": f"Bearer {token}"},
                  json=["not", "an", "object"]).status_code == 400


def test_status_and_comment_flow(admin, app):
    conn = app.state.conn
    cid = store.create_customer(conn, name="Acme")
    t = support.intake_bundle(conn, customer_id=cid, bundle=BUNDLE)
    admin.post(f"/support/{t.id}/comment", data={"body": "looking into it"})
    admin.post(f"/support/{t.id}/status",
               data={"status": "resolved", "note": "fixed in v0.1.8"})
    tkt = store.get_ticket(conn, t.id)
    assert tkt.status == "resolved"
    kinds = [c.kind for c in store.list_comments(conn, t.id)]
    assert "note" in kinds and "status" in kinds


def test_support_role_can_triage_but_viewer_cannot(app, client, make_staff, login_cookie):
    conn = app.state.conn
    cid = store.create_customer(conn, name="Acme")
    t = support.intake_bundle(conn, customer_id=cid, bundle=BUNDLE)
    # viewer: read yes, mutate no
    vid = make_staff(email="v@daybreak.co", role="viewer")
    client.cookies.update(login_cookie(vid, "viewer"))
    assert client.get(f"/support/{t.id}").status_code == 200
    assert client.post(f"/support/{t.id}/comment", data={"body": "x"},
                       follow_redirects=False).status_code == 403
    # support: can triage
    sid = make_staff(email="s@daybreak.co", role="support")
    client.cookies.update(login_cookie(sid, "support"))
    r = client.post(f"/support/{t.id}/status", data={"status": "in_progress"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.get_ticket(conn, t.id).status == "in_progress"


def test_intake_rejects_an_oversize_bundle(app):
    conn = app.state.conn
    token = "svc_big_cccc"    # pragma: allowlist secret
    store.create_customer(conn, name="Acme", serve_token_hash=hash_token(token))
    c = TestClient(app)
    big = {"correlation_id": "sup_big", "junk": "x" * 1_100_000}   # > 1 MB
    r = c.post("/api/v1/support", headers={"Authorization": f"Bearer {token}"}, json=big)
    assert r.status_code == 413
    assert store.list_tickets(conn) == []                          # nothing stored


def test_support_body_limit_stops_streaming_before_buffering_everything():
    class StreamingRequest:
        headers = {}

        def __init__(self):
            self.chunks_read = 0

        async def stream(self):
            for _ in range(10):
                self.chunks_read += 1
                yield b"x" * 250_000

    req = StreamingRequest()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_api._read_limited_body(req))

    assert exc.value.status_code == 413
    assert req.chunks_read == 5


def test_support_body_limit_rejects_oversized_content_length_without_streaming():
    class StreamingRequest:
        headers = {"content-length": "1000001"}

        def __init__(self):
            self.stream_called = False

        async def stream(self):
            self.stream_called = True
            yield b""

    req = StreamingRequest()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_api._read_limited_body(req))

    assert exc.value.status_code == 413
    assert req.stream_called is False
