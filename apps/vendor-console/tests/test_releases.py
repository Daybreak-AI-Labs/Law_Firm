"""Release publishing: signed manifests (verifiable with the product's code),
newest/yank resolution, the serve API, and RBAC."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick import release_update
from vendor_console import licensing, releases, store
from vendor_console.util import hash_token

ART = [{"name": "maverick-macos-arm64", "sha256": "0" * 64, "size": 10}]


def test_publish_signs_a_verifiable_manifest(conn):
    rel = releases.publish_release(conn, version="v0.1.7", channel="stable",
                                   min_from="v0.1.5", notes="notes",
                                   migrations=["m6"], artifacts=ART, actor="o@d.co")
    ok, why = release_update.verify_manifest(rel.manifest, [licensing.public_key_hex()])
    assert ok and why == "ok"                      # verifies with the customer's code
    assert rel.manifest["version"] == "v0.1.7" and rel.manifest["migrations"] == ["m6"]


def test_newest_falls_back_after_yank(conn):
    releases.publish_release(conn, version="v0.1.6", channel="stable", min_from="",
                             notes="", migrations=[], artifacts=[], actor="a")
    r2 = releases.publish_release(conn, version="v0.1.7", channel="stable", min_from="",
                                  notes="", migrations=[], artifacts=[], actor="a")
    assert store.newest_release(conn, "stable").version == "v0.1.7"
    assert releases.yank_release(conn, r2.id, actor="a") is True
    assert store.newest_release(conn, "stable").version == "v0.1.6"   # prior serves


def test_channels_are_isolated(conn):
    releases.publish_release(conn, version="v0.2.0", channel="edge", min_from="",
                             notes="", migrations=[], artifacts=[], actor="a")
    assert store.newest_release(conn, "stable") is None
    assert store.newest_release(conn, "edge").version == "v0.2.0"


def test_publish_rejects_bad_channel(conn):
    with pytest.raises(ValueError):
        releases.publish_release(conn, version="v1", channel="beta", min_from="",
                                 notes="", migrations=[], artifacts=[], actor="a")


def test_serve_release_api(app):
    conn = app.state.conn
    token = "svc_relfeed_aaaa"    # pragma: allowlist secret
    store.create_customer(conn, name="Acme", serve_token_hash=hash_token(token))
    c = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/v1/release", headers=hdr).status_code == 404      # nothing yet
    releases.publish_release(conn, version="v0.1.7", channel="stable", min_from="v0.1.5",
                             notes="", migrations=[], artifacts=ART, actor="a")
    r = c.get("/api/v1/release", headers=hdr, params={"version": "v0.1.6"})
    assert r.status_code == 200 and r.json()["version"] == "v0.1.7"
    assert c.get("/api/v1/release",
                 headers={"Authorization": "Bearer nope"}).status_code == 401


def test_admin_publishes_via_ui(admin, app):
    r = admin.post("/releases", data={
        "version": "v0.1.7", "channel": "stable", "min_from": "v0.1.5",
        "migrations": "m6, m7", "artifacts": "bin deadbeefcafe 100"},
        follow_redirects=False)
    assert r.status_code == 303
    rels = store.list_releases(app.state.conn)
    assert len(rels) == 1 and rels[0].version == "v0.1.7"
    assert rels[0].migrations == ["m6", "m7"] and rels[0].artifacts[0]["name"] == "bin"


def test_release_publish_requires_write(client, make_staff, login_cookie):
    sid = make_staff(email="v@daybreak.co", role="viewer")
    client.cookies.update(login_cookie(sid, "viewer"))
    assert client.get("/releases").status_code == 200                   # can view
    r = client.post("/releases", data={"version": "v0.1.7"}, follow_redirects=False)
    assert r.status_code == 403                                         # cannot publish
