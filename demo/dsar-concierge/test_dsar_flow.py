"""The standalone DSAR Concierge end to end over HTTP: intake → verify →
fulfill/handoff → close, the license evaluation cap, and the ops surface
(/health, /value.json) the partner fleet console polls."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

HERE = Path(__file__).resolve().parent

# The two agent SKUs share module NAMES (backend, capabilities, ...) by
# design -- each directory is a self-contained deployable. In one pytest
# process that collides in sys.modules, so every loader purges the shared
# names before executing its app.
_SHARED_MODULES = ("backend", "capabilities", "license_kit", "mailsink",
                   "value_ledger", "store", "pia_engine", "dsar_engine",
                   "ot_mock", "onetrust_client", "notice_check",
                   "contract_guard", "serve_standalone")


def _purge_shared_modules():
    for _m in _SHARED_MODULES:
        sys.modules.pop(_m, None)



@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DSAR_STANDALONE", "1")
    monkeypatch.setenv("DSAR_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LIGHTWORK_LICENSE", raising=False)
    monkeypatch.setenv("DSAR_OPERATOR_TOKEN", "test-operator-token")
    sys.path.insert(0, str(HERE))
    _purge_shared_modules()
    try:
        spec = importlib.util.spec_from_file_location(
            "dsar_concierge_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.INBOX.clear()
        # No SMTP listener in tests; backend inserts into INBOX directly.
        yield TestClient(module.app), module, {
            "auth": ("operator", "test-operator-token")}
    finally:
        sys.path.remove(str(HERE))


def _mail_link(module, needle="/verify/"):
    for m in module.INBOX:
        for line in m.body.splitlines():
            if needle in line:
                return line.strip()
    return ""


def test_full_access_flow(client):
    tc, module, operator = client
    r = tc.post("/request", data={"subject_id": "jordan@example.test",
                                  "kind": "access", "note": "everything"})
    assert r.status_code == 200 and "opened" in r.text
    link = _mail_link(module)
    assert link, "verification email not delivered"
    token = link.rsplit("/", 1)[-1]
    assert "Identity confirmed" in tc.get(f"/verify/{token}").text
    rid = next(iter(module.engine.list_requests()))["id"]
    r = tc.post(f"/case/{rid}/fulfill",
                data={"extracts": "CRM: name, email\nBilling: 3 invoices"},
                follow_redirects=True, **operator)
    assert r.status_code == 200
    pkg = tc.get(f"/case/{rid}/package.json", **operator)
    assert pkg.status_code == 200
    assert set(pkg.json()["data"]) == {"CRM", "Billing"}
    # The subject got the cover note.
    assert any("is ready" in m.subject for m in module.INBOX)
    v = tc.get("/value.json").json()
    assert v["agent"] == "dsar-concierge" and v["cases"] == 1
    assert v["dollars"] > 0
    assert tc.get("/health").json()["ok"] is True


def test_webhook_detector_and_erasure_handoff(client):
    tc, module, operator = client
    r = tc.post("/webhook/message", json={
        "text": "Under Article 17 please delete my data.",
        "sender": "sam@example.test"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["kind"] == "erasure"
    # Nonsense never opens a case.
    assert tc.post("/webhook/message",
                   json={"text": "lovely weather"}).status_code == 422
    token = _mail_link(module).rsplit("/", 1)[-1]
    tc.get(f"/verify/{token}")
    r = tc.post(f"/case/{rid}/erasure", data={"systems": "CRM\nBilling"},
                follow_redirects=True, **operator)
    assert "deliberately" in r.text or "non-destructive" in r.text
    rec = module.engine.get(rid)
    assert rec["status"] == "awaiting_erasure"
    tc.post(f"/case/{rid}/close", data={"reason": "confirmed"},
            follow_redirects=True, **operator)
    assert module.engine.get(rid)["status"] == "closed"


def test_operator_console_rejects_unauthenticated_clients(client):
    tc, module, operator = client
    opened = tc.post("/request", data={
        "subject_id": "privacy@example.test", "kind": "access"})
    assert opened.status_code == 200
    rid = next(iter(module.engine.list_requests()))["id"]
    for path in ("/", f"/case/{rid}", f"/case/{rid}/package.json"):
        response = tc.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")
    for path in (f"/case/{rid}/fulfill", f"/case/{rid}/erasure",
                 f"/case/{rid}/close"):
        assert tc.post(path, data={}).status_code == 401
    assert tc.get("/", **operator).status_code == 200


def test_evaluation_mode_caps_open_requests(client):
    tc, module, _operator = client
    assert module.LICENSE["mode"] == "evaluation"
    cap = module.LICENSE["open_case_cap"]
    for i in range(cap):
        assert tc.post("/request", data={
            "subject_id": f"s{i}@example.test", "kind": "access"
        }).status_code == 200
    over = tc.post("/request", data={"subject_id": "x@example.test",
                                     "kind": "access"})
    assert "Evaluation mode" in over.text
    assert len(module.engine.list_requests()) == cap
    # The webhook refuses with the same message, as a 403.
    r = tc.post("/webhook/message", json={
        "text": "access request please — a@b.test", "sender": "a@b.test"})
    assert r.status_code == 403


def test_license_key_lifts_the_cap(tmp_path, monkeypatch):
    from maverick.licensing import generate_keypair, make_license
    private_pem, public_pem = generate_keypair()
    token = make_license(customer="Acme", sku="dsar_standalone",
                         private_pem=private_pem, days=30)
    monkeypatch.setenv("LIGHTWORK_LICENSE", token)
    monkeypatch.setenv("LIGHTWORK_LICENSE_PUBKEY", public_pem)
    monkeypatch.setenv("DSAR_STANDALONE", "1")
    monkeypatch.setenv("DSAR_DATA_DIR", str(tmp_path / "store"))
    sys.path.insert(0, str(HERE))
    _purge_shared_modules()
    try:
        spec = importlib.util.spec_from_file_location(
            "dsar_concierge_licensed", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.LICENSE["mode"] == "licensed"
        assert module.LICENSE["customer"] == "Acme"
        tc = TestClient(module.app)
        for i in range(7):   # comfortably past the evaluation cap
            assert tc.post("/request", data={
                "subject_id": f"s{i}@example.test", "kind": "access"
            }).status_code == 200
        assert len(module.engine.list_requests()) == 7
        assert "licensed to <b>Acme</b>" in tc.get("/about").text
    finally:
        sys.path.remove(str(HERE))
