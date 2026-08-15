"""Storage + the tamper-evident vendor audit chain."""
from __future__ import annotations

from vendor_console import audit, store


def test_customer_crud_and_search(conn):
    cid = store.create_customer(conn, name="Acme Bank", contact_email="x@acme.example")
    assert store.get_customer(conn, cid).name == "Acme Bank"
    store.update_customer(conn, cid, status="active", notes="POC")
    assert store.get_customer(conn, cid).status == "active"
    assert [c.name for c in store.list_customers(conn, search="acme")] == ["Acme Bank"]
    assert store.list_customers(conn, search="zzz") == []


def test_serve_token_lookup(conn):
    cid = store.create_customer(conn, name="Acme", serve_token_hash="deadbeef")
    assert store.customer_by_serve_token_hash(conn, "deadbeef").id == cid
    assert store.customer_by_serve_token_hash(conn, "nope") is None
    assert store.customer_by_serve_token_hash(conn, "") is None   # empty never matches


def test_checkins_latest_per_customer(conn):
    cid = store.create_customer(conn, name="Acme")
    store.record_checkin(conn, customer_id=cid, version="v0.1.6", at=100)
    store.record_checkin(conn, customer_id=cid, version="v0.1.7", at=200)
    latest = store.latest_checkins(conn)
    assert len(latest) == 1 and latest[0]["version"] == "v0.1.7"


def test_audit_chain_detects_tampering(conn):
    audit.record(conn, actor="a", action="customer.create", target="Acme")
    audit.record(conn, actor="a", action="license.issue", target="Acme",
                 detail={"tier": "gold"})
    ok, broken = audit.verify_chain(conn)
    assert ok and broken == 0
    # Tamper: rewrite a past event's target; the chain must break at that row.
    conn.execute("UPDATE audit SET target = 'Evil' WHERE id = 1")
    conn.commit()
    ok, broken = audit.verify_chain(conn)
    assert not ok and broken == 1
