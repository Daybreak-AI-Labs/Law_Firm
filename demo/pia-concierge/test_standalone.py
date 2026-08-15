"""The standalone agent SKU: PIA Concierge running with PIA_STANDALONE=1 and
no Lightwork platform. The engine is the vendored pia_engine, there is no
world governance and no audit trail, and the whole intake -> score -> file ->
review -> approve flow still works."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

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



@pytest.fixture(scope="module")
def sa_app(tmp_path_factory):
    """Load the concierge in standalone mode in an isolated module namespace."""
    data = tmp_path_factory.mktemp("sa-data")
    os.environ["PIA_STANDALONE"] = "1"
    os.environ["PIA_DATA_DIR"] = str(data)
    sys.path.insert(0, str(HERE))
    # Fresh import so capabilities.STANDALONE is evaluated with the env set.
    _purge_shared_modules()
    sys.modules.pop("app", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_standalone_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pia_standalone_app"] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(HERE))
        os.environ.pop("PIA_STANDALONE", None)


@pytest.fixture(autouse=True)
def _clean_store(sa_app, monkeypatch):
    sa_app.STORE.tickets.clear()
    sa_app.STORE.cases.clear()
    sa_app.STORE.onetrust.clear()

    async def _no_email(to, subject, body):
        return "ok (stub)"

    monkeypatch.setattr(sa_app, "_send_email", _no_email)

    # Bind the wire client to the in-process mock tenant over ASGI (a live
    # deployment takes the same calls over real HTTP; the protocol is
    # identical — that is the point of the mock enforcing the shapes).
    from starlette.testclient import TestClient
    sys.path.insert(0, str(HERE))
    try:
        import ot_mock
        from onetrust_client import OneTrustClient
    finally:
        sys.path.remove(str(HERE))
    ot_mock.reset_mock()
    tc = TestClient(sa_app.app)
    monkeypatch.setattr(sa_app.backend, "onetrust_client",
                        lambda: OneTrustClient(base_url="/ot-api", http=tc))
    yield


def _req(method="GET", body=b""):
    async def _recv():
        return {"type": "http.request", "body": body, "more_body": False}
    headers = [(b"content-type", b"application/json")] if body else []
    return Request({"type": "http", "method": method, "path": "/",
                    "headers": headers, "query_string": b"", "scheme": "http",
                    "server": ("127.0.0.1", 8890), "client": ("127.0.0.1", 1)},
                   _recv)


def _json_req(payload):
    body = json.dumps(payload).encode()

    async def _recv():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/x",
                    "headers": [(b"content-type", b"application/json")]}, _recv)


def _upload(name, data, mime="application/pdf"):
    return UploadFile(io.BytesIO(data), filename=name,
                      headers=Headers({"content-type": mime}))


# --------------------------------------------------------------------------- #
def test_capabilities_report_standalone(sa_app):
    from capabilities import CAPS, STANDALONE, caps_summary
    assert STANDALONE is True
    # Agent features on; platform features off.
    for on in ("risk_scoring", "onetrust_filing", "onetrust_review",
               "chat_voice_intake", "vendor_memory"):
        assert CAPS[on] is True
    for off in ("signed_audit", "world_governance", "control_catalog",
                "privacy_workspace", "dpa_clause_review", "assessment_memory"):
        assert CAPS[off] is False
    s = caps_summary()
    assert s["standalone"] and s["gated"] and s["active"]


def test_engine_is_the_vendored_one(sa_app):
    # The scorer is pia_engine, not maverick.assessment.
    assert sa_app.get_template.__module__ == "pia_engine"
    assert sa_app.backend.world() is None          # no governed world
    assert sa_app.backend.find_controls("x") == []  # no control catalog


import asyncio  # noqa: E402


def test_full_flow_intake_score_file_review_approve(sa_app):
    # 1. Intake via webhook: no governed goal in standalone.
    resp = json.loads(asyncio.run(sa_app.webhook_pia(
        _json_req({"short_description": "CRM rollout", "requester": "Jordan",
                   "requester_email": "j@x.test", "system_name": "Acme CRM",
                   "data_types": "contact data", "source": "servicenow"}))).body)
    case_id = resp["case_id"]
    assert resp["goal_id"] is None
    case = sa_app.STORE.cases[case_id]

    # 2. Answer the interview (a high-risk mix) and submit.
    for qid, (ans, note) in sa_app._DEMO_ANSWERS.items():
        case.answers[qid] = {"answer": ans, "note": note}
    asyncio.run(sa_app.intake_submit(_req("POST"), case_id))

    # 3. Filed into OneTrust as Under Review; no world approval row exists.
    assert case.stage == "in_onetrust_review"
    assert case.approval_id == ""
    ot = sa_app.STORE.onetrust[case.onetrust_id]
    assert ot.status == "Under Review" and ot.result["answers"]
    assert case.result["risk_rating"] == "high"

    # 4. The saved assessment record is the vendored store (no maverick).
    rec = sa_app.backend.load_saved(case.assessment_id)
    assert rec and rec["status"] == "pending_review"

    # 5. Approve INSIDE OneTrust — the only decision surface standalone.
    d = json.loads(asyncio.run(sa_app.onetrust_decide(_json_req(
        {"assessment_id": ot.assessment_id, "decision": "approve",
         "reviewer": "L. Haller"}))).body)
    assert d["ok"] and d["status"] == "Completed"
    assert case.stage == "filed"
    assert sa_app.backend.load_saved(case.assessment_id)["status"] == "approved"
    assert sa_app.STORE.tickets[case.ticket_number].state == "Resolved"


def test_append_document_degrades_without_clause_engine(sa_app):
    # File one assessment, then append a DPA: standalone recognises it but the
    # Art. 28 clause engine is a Lightwork feature.
    resp = json.loads(asyncio.run(sa_app.webhook_pia(
        _json_req({"system_name": "Acme CRM", "requester": "Jo",
                   "requester_email": "j@x.test", "source": "servicenow",
                   "short_description": "x", "data_types": "y"}))).body)
    case = sa_app.STORE.cases[resp["case_id"]]
    for qid, (ans, note) in sa_app._DEMO_ANSWERS.items():
        case.answers[qid] = {"answer": ans, "note": note}
    asyncio.run(sa_app.intake_submit(_req("POST"), case.id))
    ot_id = case.onetrust_id
    pdf = sa_app._mini_pdf("Acme DPA", ["Art. 28 data processing agreement."])
    r = json.loads(asyncio.run(sa_app.onetrust_add_document(
        ot_id, _upload("dpa.pdf", pdf))).body)
    assert r["ok"]
    entry = sa_app.STORE.onetrust[ot_id].addenda[0]
    assert "Lightwork" in entry["summary"] and entry["re_review"] is True


def test_pages_hide_platform_and_show_capability_matrix(sa_app):
    landing = asyncio.run(sa_app.index(_req())).body.decode()
    assert "Standalone agent" in landing
    assert "/goals" not in landing and "/audit" not in landing
    about = asyncio.run(sa_app.about(_req())).body.decode()
    assert "Ed25519 tamper-evident audit chain" in about   # listed as gated
    assert "Deterministic risk scoring" in about           # listed as active
