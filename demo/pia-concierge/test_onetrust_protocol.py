"""Wire-protocol conformance: the OneTrust client against the mock tenant
that enforces the field-verified shapes (traps included). A client that
passes here speaks the same protocol the shapes were verified on live."""
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
                   "contract_guard", "serve_standalone", "paper_desk")


def _purge_shared_modules():
    for _m in _SHARED_MODULES:
        sys.modules.pop(_m, None)



@pytest.fixture(scope="module")
def demo():
    _purge_shared_modules()
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_protocol_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import contract_guard
        import notice_check
        import ot_mock
        import value_ledger
        from onetrust_client import OneTrustClient, OneTrustError
        return {"app": module, "ot_mock": ot_mock,
                "Client": OneTrustClient, "Error": OneTrustError,
                "notice_check": notice_check,
                "contract_guard": contract_guard,
                "value_ledger": value_ledger}
    finally:
        sys.path.remove(str(HERE))


@pytest.fixture()
def client(demo, monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    demo["ot_mock"].reset_mock()
    demo["app"].STORE.onetrust.clear()
    demo["app"].STORE._paper_seq.clear()
    tc = TestClient(demo["app"].app)
    return demo["Client"](base_url="/ot-api", token="demo-bearer-token",
                          http=tc)


def _launch(client):
    return client.launch("tmpl-pia", org_group_id="og-demo",
                         respondent="jordan@example.test",
                         name="PIA — Acme CRM")


# --- launch + read ---------------------------------------------------------
def test_launch_requires_org_group_and_respondent(demo, client):
    with pytest.raises(demo["Error"]) as err:
        client.launch("tmpl-pia", org_group_id="", respondent="",
                      name="nope")
    assert err.value.status == 400
    aid = _launch(client)
    assert aid.startswith("OT-ASMT-")


def test_mock_sensitive_endpoints_require_bearer(demo, client):
    aid = _launch(client)
    tc = client._http

    assert tc.get(f"/ot-api/api/assessment/v2/assessments/{aid}/export"
                  ).status_code == 401
    assert tc.post(f"/ot-api/api/assessment/v2/assessments/{aid}/responses",
                   json={"responses": []}).status_code == 401
    assert tc.post(
        f"/ot-api/api/assessment/v2/assessments/{aid}/submit",
        params={"disclaimerAccepted": "true"}).status_code == 401
    assert tc.post("/ot-api/api/document/v2/attachments",
                   data={"attachment": "{}"}).status_code == 401
    assert tc.post("/ot-api/api/inventory/v2/inventories/ven-1/attachments",
                   json=[]).status_code == 401
    assert tc.post("/ot-api/api/assessment/v2/assessments/assessment-links",
                   json={"fromId": aid, "toIds": []}).status_code == 401


def test_plain_read_403s_but_export_works(demo, client):
    aid = _launch(client)
    with pytest.raises(demo["Error"]) as err:
        client._get(f"/api/assessment/v2/assessments/{aid}")
    assert err.value.status == 403
    exp = client.export(aid)
    assert exp["status"] == "In Progress"
    assert {q["questionId"] for q in client.questions(aid)} >= {
        "q-name", "q-risk", "q-vendor", "q-personal-data"}


# --- the write shapes ------------------------------------------------------
def test_option_write_needs_option_id_and_response_key(demo, client):
    aid = _launch(client)
    qs = {q["questionId"]: q for q in client.questions(aid)}
    bare = [{"questionId": "q-risk", "sectionId": "sec-risk",
             "responseId": None, "response": "High", "type": "DEFAULT"}]
    with pytest.raises(demo["Error"]) as err:
        client.write_responses(aid, bare)
    assert "INVALID_REQUEST_INPUT" in str(err.value)
    client.write_responses(aid, client.answer_option(qs["q-risk"], "High"))
    stored = [q for q in client.questions(aid)
              if q["questionId"] == "q-risk"][0]["responses"]
    assert stored[0]["response"] == "High"
    assert stored[0]["responseKey"] == "t.q-risk.option.option"


def test_top_level_justification_is_silently_dropped(demo, client):
    """The tenant behavior that breaks naive integrations: the field is
    accepted, dropped, and submit then fails with no server-side hint."""
    aid = _launch(client)
    qs = {q["questionId"]: q for q in client.questions(aid)}
    entries = (client.answer_text(qs["q-name"], "Acme CRM")
               + client.answer_option(qs["q-risk"], "High")
               + client.answer_option(qs["q-ai"], "No")
               + client.answer_record(qs["q-vendor"],
                                      client.ensure_vendor("Awardco"))
               + client.personal_data_rows(
                   qs["q-personal-data"],
                   client.resolve_personal_data("email address")))
    # The trap: justification as a top-level field, not an entry.
    client._post(f"/api/assessment/v2/assessments/{aid}/responses",
                 {"responses": entries, "justification": "<p>because</p>"})
    sub = client.submit(aid)
    assert sub["advanced"] is False
    # The server's list UNDER-reports: the justification miss is invisible.
    reasons = {o["questionId"]: o["reason"] for o in sub["outstanding"]}
    assert reasons == {"q-risk": "missing justification"}
    # A proper JUSTIFICATION entry fixes it.
    client.write_responses(aid, client.justification(qs["q-risk"],
                                                     "documented rationale"))
    assert client.submit(aid)["advanced"] is True


def test_personal_data_mirror_nodes_are_rejected_and_writes_replace(
        demo, client):
    aid = _launch(client)
    qs = {q["questionId"]: q for q in client.questions(aid)}
    triples = client.resolve_personal_data("name and email address")
    assert len(triples) == 2
    rows = client.personal_data_rows(qs["q-personal-data"], triples)
    dirty = [dict(rows[0], element={"id": "el-1"})]   # a mirror node
    with pytest.raises(demo["Error"]) as err:
        client.write_responses(aid, dirty)
    assert "mirror nodes" in str(err.value)
    client.write_responses(aid, rows)
    # A second write REPLACES the whole row set (no accumulation).
    one = client.personal_data_rows(qs["q-personal-data"], triples[:1])
    client.write_responses(aid, one)
    pd = [q for q in client.questions(aid)
          if q["questionId"] == "q-personal-data"][0]["responses"]
    assert len(pd) == 1


def test_submit_self_check_reads_the_actual_stage(demo, client):
    aid = _launch(client)
    qs = {q["questionId"]: q for q in client.questions(aid)}
    entries = (client.answer_text(qs["q-name"], "Acme CRM")
               + client.answer_option(qs["q-risk"], "Medium")
               + client.justification(qs["q-risk"], "controls in place")
               + client.answer_option(qs["q-ai"], "No")
               + client.answer_record(qs["q-vendor"],
                                      client.ensure_vendor("Cvent"))
               + client.personal_data_rows(
                   qs["q-personal-data"],
                   client.resolve_personal_data("phone number")))
    client.write_responses(aid, entries)
    sub = client.submit(aid)
    assert sub["advanced"] is True and sub["status"] == "Under Review"
    assert client.export(aid)["status"] == "Under Review"


def test_session_gated_operations_fail_with_guidance(demo, client):
    aid = _launch(client)
    for call in (
            lambda: client._post(
                f"/api/assessment/v2/assessments/{aid}/reopen"),
            lambda: client._post(
                f"/api/assessment/v2/assessments/{aid}/attachments")):
        with pytest.raises(demo["Error"]) as err:
            call()
        assert err.value.status == 403
        assert "session-gated" in str(err.value)


def test_attachments_are_two_step_onto_the_vendor_record(demo, client):
    vendor = client.ensure_vendor("Medallia")
    att_id = client.attach_to_record(vendor["id"], "risk-analysis.txt",
                                     b"findings...")
    state = demo["ot_mock"].mock_state()
    assert state["attachments"][att_id]["linked_to"] == vendor["id"]
    assert state["attachments"][att_id]["filename"] == "risk-analysis.txt"
    # The tenant RETAINS the bytes (so the demo's Documents tab can open
    # them) and serves them back, bearer-gated like every other resource.
    assert state["attachments"][att_id]["content"] == b"findings..."
    tc = client._http
    r = tc.get(f"/ot-api/api/document/v2/attachments/{att_id}/file",
               headers={"Authorization": "Bearer demo-bearer-token"})
    assert r.status_code == 200 and r.content == b"findings..."
    assert tc.get(f"/ot-api/api/document/v2/attachments/{att_id}/file"
                  ).status_code == 401
    assert tc.get("/ot-api/api/document/v2/attachments/att-none/file",
                  headers={"Authorization": "Bearer demo-bearer-token"}
                  ).status_code == 404


def test_list_dedupes_the_overlapping_pages(client):
    for i in range(5):
        client.launch("tmpl-pia", org_group_id="og", respondent="r@x.test",
                      name=f"PIA {i}")
    rows = client.list_assessments()
    ids = [r["assessmentId"] for r in rows]
    assert len(ids) == len(set(ids)) == 5


# --- inventory: the typo-tolerant ladder + dedup-not-duplicate -------------
def test_match_record_ladder_and_partial_input(demo, client):
    rows = client.records("vendors")
    assert client.match_record("Awardco", rows)["name"] == "Awardco"
    assert client.match_record("award", rows)["name"] == "Awardco"
    assert client.match_record("brand", rows)["name"] == \
        "Brand and Digital Services"
    assert client.match_record("Awardcoo", rows)["name"] == "Awardco"
    assert client.match_record("zzz-nothing", rows) is None


def test_ensure_vendor_dedups_and_entities_are_never_created(demo, client):
    before = len(client.records("vendors"))
    linked = client.ensure_vendor("awardco")
    assert linked["created"] is False
    assert len(client.records("vendors")) == before
    created = client.ensure_vendor("Brand-New Vendor LLC")
    assert created["created"] is True
    assert client.find_entity("Global Vacation Clubs")["id"] == "ent-1"
    assert client.find_entity("Unknown Entity Co") is None
    with pytest.raises(demo["Error"]):
        client._post("/api/inventory/v2/inventories/entities",
                     {"name": "Rogue Entity"})


def test_personal_data_resolver_uses_live_catalogs(client):
    triples = client.resolve_personal_data(
        "HR system holding employee salary and postal address")
    names = {t["element"]["name"] for t in triples}
    assert names == {"salary", "postal address"}
    assert all(t["subject"]["name"] == "Employees" for t in triples)
    assert {t["category"]["name"] for t in triples} == {"HR Data",
                                                        "Contact Data"}


# --- notices (Enterprise Policy) + cross-check -----------------------------
def test_notice_api_shapes(demo, client):
    with pytest.raises(demo["Error"]) as err:
        client._get("/api/enterprise-policy/v1/privacynotices/list")
    assert err.value.status == 400        # lastPublishedDate is required
    notices = client.notices()
    assert {n["guid"] for n in notices} == {"ntc-default", "ntc-brand"}
    with pytest.raises(demo["Error"]) as err:
        client._get("/api/enterprise-policy/v1/privacynotices/ntc-default")
    assert err.value.status == 403        # the bare guid is the edit resource
    text = client.notice_text("ntc-default")
    assert "do not sell" in text.lower()


def test_notice_pick_is_brand_first_then_default(demo, client):
    nc = demo["notice_check"]
    notices = client.notices()
    picked, siblings = nc.pick_notice("Brand and Digital", notices)
    assert picked["guid"] == "ntc-brand" and len(siblings) == 1
    picked, _ = nc.pick_notice("", notices,
                               default_name="Global Vacation Clubs")
    assert picked["guid"] == "ntc-default"


def test_cross_check_contradiction_and_gap(demo, client):
    nc = demo["notice_check"]
    text = client.notice_text("ntc-default")
    result = nc.cross_check(notice_text=text,
                            facts={"data_sold": True},
                            data_categories=["Contact Data",
                                             "Behavioural Data"])
    assert result["escalate"] is True
    assert result["contradictions"][0]["key"] == "no_sale"
    assert "do not sell" in result["contradictions"][0]["notice_quote"].lower()
    gaps = {g["category"] for g in result["gaps"]}
    assert gaps == {"Behavioural Data"}   # contact data IS disclosed
    doc = nc.recommendations_doc("Acme CRM", {"name": "N", "organizationName":
                                              "B"}, result, [])
    assert "CONTRADICTIONS" in doc and "Behavioural Data" in doc


# --- the deterministic contract guard --------------------------------------
@pytest.mark.parametrize("regions,expected", [
    ("customers in Germany and France under GDPR", "DataProcessingAgreement"),
    ("California residents only, CCPA applies", "PrivacyAddendum"),
    ("EU and US customer base", "DataProcessingAgreement"),
    ("somewhere unspecified", "DataProcessingAgreement"),
])
def test_decide_instrument_matrix(demo, regions, expected):
    out = demo["contract_guard"].decide_instrument(
        regions_text=regions, external_processing=True, personal_data=True)
    assert out["instrument"] == expected


def test_no_external_processing_means_no_instrument_and_guard_overrides(demo):
    cg = demo["contract_guard"]
    none = cg.decide_instrument(regions_text="EU", external_processing=False,
                                personal_data=True)
    assert none["instrument"] is None
    # A model's comparative-GDPR pick can't flip a US-only activity to a DPA.
    us = cg.decide_instrument(regions_text="California CCPA",
                              external_processing=True, personal_data=True)
    final = cg.guard_instrument("DataProcessingAgreement", us)
    assert final["instrument"] == "PrivacyAddendum"
    assert final["guard_overrode_model"] is True


# --- value ledger ----------------------------------------------------------
def test_value_ledger_splits_audiences_without_double_count(demo):
    v = demo["value_ledger"].case_value(kind="privacy_assessment",
                                        answers_autofilled=10,
                                        contract_drafted=True,
                                        notice_checked=True)
    assert v["privacy_hours"] == 11.25 + 7.0 + 3.0
    assert v["business_hours"] == 0.5 + 1.0
    assert v["dollars"] == round(v["hours"] * v["rate"], 2)


# --- the whole protocol driven by the concierge flow -----------------------
def test_flow_files_under_review_with_ai_branch_and_escalation(
        demo, client, monkeypatch):
    app = demo["app"]
    monkeypatch.setattr(app.backend, "onetrust_client", lambda: client)
    case = app.Case(
        id="LW-PIA-P001", ticket_number="PRV-P-1", subject="Acme Analytics",
        requester="Jordan", requester_email="jordan@example.test",
        data_types="browsing history and email address; data is sold to "
                   "an AI model vendor")
    case.answers["pia_necessity"] = {"answer": "yes", "note": "", "auto": True}
    case.result = {"risk_rating": "medium", "answered": 1, "total": 10,
                   "findings": [{"severity": "high", "section": "Transfers",
                                 "question": "Cross-border flows"}]}
    app.STORE.cases[case.id] = case
    run = app._run_onetrust_protocol(case)
    assert run["ok"] is True
    rec = app.STORE.onetrust[run["assessment_id"]]
    assert rec.status == "Under Review"
    # The published notice says "we do not sell" and the intake says sold:
    # the contradiction escalates the FILED rating to High before submit.
    assert rec.risk_level == "high"
    assert run["notice"]["escalate"] is True
    # AI detected -> a linked AI Model Assessment, also Under Review.
    assert run["ai_assessment_id"]
    ai = app.STORE.onetrust[run["ai_assessment_id"]]
    assert ai.status == "Under Review"
    links = demo["ot_mock"].mock_state()["links"]
    assert links and links[-1]["toIds"] == [run["ai_assessment_id"]]
    # Existing vendor linked, not duplicated; deliverables on its record —
    # Word documents on the platform (standalone files the honest .txt),
    # named legibly and versioned per vendor, never by case id.
    assert run["vendor"]["created"] is False
    attached = [a for a in demo["ot_mock"].mock_state()
                ["attachments"].values()
                if a["linked_to"] == run["vendor"]["id"]]
    assert {a["filename"] for a in attached} == {
        "Acme-Analytics-risk-analysis-v1.docx",
        "Acme-Analytics-notice-crosscheck-v1.docx"}
    # Openable, not stubs: real OPC bytes carrying the FULL report — the
    # executive summary, findings, and the verbatim interview basis.
    assert all(a["content"][:2] == b"PK" and a["bytes"] > 1500
               for a in attached)
    import io as _io
    import zipfile as _zip
    risk = next(a for a in attached if "risk-analysis" in a["filename"])
    doc = _zip.ZipFile(_io.BytesIO(risk["content"])).read(
        "word/document.xml").decode()
    for section in ("SUMMARY OF RECORD", "EXECUTIVE SUMMARY", "FINDINGS",
                    "GOVERNANCE"):
        assert section in doc, section
    # The actual finding made it into the document, not just a count.
    assert "Cross-border flows" in doc
    # Value ledger counted the AI-assessment work product.
    assert run["value"]["privacy_hours"] >= 10.0
