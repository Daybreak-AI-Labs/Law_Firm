"""End-to-end through the FastAPI app: auth gate, the real setup→TOTP→login
flow, customer + license issuance, the serve API, download, and RBAC."""
from __future__ import annotations

import re

from maverick import entitlements
from vendor_console import licensing, security, store


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_unauthenticated_is_redirected_to_login(client):
    r = client.get("/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_full_setup_totp_login_flow(client, app):
    conn = app.state.conn
    # first visit → setup
    assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
    # create the owner
    r = client.post("/setup", data={"email": "boss@daybreak.co", "name": "Boss",
                                    "password": "supersecret1"},  # pragma: allowlist secret
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/enroll-totp"
    # enrollment shows a secret; confirm with a real code
    client.get("/enroll-totp")
    secret = store.get_staff_by_email(conn, "boss@daybreak.co")["totp_secret"]
    code = security.totp_now(secret)
    r = client.post("/enroll-totp", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    # now the console is reachable
    assert client.get("/customers").status_code == 200
    # a wrong TOTP at login is refused
    client.cookies.clear()
    client.post("/login", data={"email": "boss@daybreak.co",
                                "password": "supersecret1"})  # pragma: allowlist secret
    bad = client.post("/totp", data={"code": "000001"})
    assert "Wrong or expired code" in bad.text


def _create_customer(admin, app, name="Cedar Valley Bank") -> tuple[int, str]:
    r = admin.post("/customers", data={"name": name, "contact_email": "c@cvb.example",
                                       "posture": "connected"})
    token = re.search(r"svc_[A-Za-z0-9_\-]+", r.text).group(0)
    cid = store.list_customers(app.state.conn, search=name)[0].id
    return cid, token


def test_customer_issue_and_serve(admin, app):
    cid, token = _create_customer(admin, app)
    # no license yet → serve API 404
    assert admin.get("/api/v1/license",
                     headers={"Authorization": f"Bearer {token}"}).status_code == 404
    # issue a Gold+fleet license from the feature-access checkboxes (all the
    # gold-tier boxes stay checked, as the form renders them)
    gold = [f for f, t in entitlements.GATED_FEATURES.items() if t == "gold"]
    admin.post(f"/customers/{cid}/licenses",
               data={"tier": "gold", "suites_on": ["fleet"], "features_on": gold,
                     "expires": "2027-01-01", "grace_days": "14"})
    # the serve API now returns a verifiable Gold license
    r = admin.get("/api/v1/license", headers={"Authorization": f"Bearer {token}"},
                  params={"deployment": "dep-1", "version": "v0.1.6"})
    assert r.status_code == 200
    doc = r.json()
    pub = licensing.public_key_hex()
    ok, _ = entitlements.verify_license(doc, [pub])
    assert ok and doc["tier"] == "gold" and doc["suites"] == ["fleet"]
    assert not doc.get("features_denied")   # nothing was unchecked
    # the poll recorded a fleet check-in
    assert store.latest_checkins(app.state.conn)[0]["version"] == "v0.1.6"


def test_serve_api_rejects_bad_token(admin, app):
    _create_customer(admin, app)
    assert admin.get("/api/v1/license",
                     headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert admin.get("/api/v1/license").status_code == 401   # missing token


def test_license_download(admin, app):
    cid, _ = _create_customer(admin, app)
    admin.post(f"/customers/{cid}/licenses", data={"tier": "platinum"})
    lic = store.list_licenses(app.state.conn, cid)[0]
    r = admin.get(f"/licenses/{lic.license_id}/download")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.json()["tier"] == "platinum"


def test_rbac_viewer_cannot_write(client, make_staff, login_cookie):
    sid = make_staff(email="view@daybreak.co", role="viewer")
    client.cookies.update(login_cookie(sid, "viewer"))
    assert client.get("/customers").status_code == 200          # can read
    r = client.post("/customers", data={"name": "Nope"}, follow_redirects=False)
    assert r.status_code == 403                                  # cannot write
