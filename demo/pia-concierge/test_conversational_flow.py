"""The partner-feedback demo flow: chat/voice interpretation, OneTrust-first
review (file as Under Review, approve inside OneTrust), and appending
documents to a vendor that has already been assessed."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
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



def _load_demo_app():
    _purge_shared_modules()
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_concierge_demo_flow_app", HERE / "app.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HERE))


@pytest.fixture(scope="module")
def demo_app():
    return _load_demo_app()


@pytest.fixture(autouse=True)
def _fresh_world(tmp_path, monkeypatch, demo_app):
    from maverick import config, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.reset_config_cache()
    demo_app.STORE.tickets.clear()
    demo_app.STORE.cases.clear()
    demo_app.STORE.onetrust.clear()
    demo_app.STORE._paper_seq.clear()
    demo_app._ot_mock.reset_mock()
    demo_app.paper_desk._REDLINES.clear()

    async def _no_email(to, subject, body):
        return "ok (stubbed)"

    monkeypatch.setattr(demo_app, "_send_email", _no_email)
    yield
    config.reset_config_cache()


def _request(method: str = "GET", path: str = "/", body: bytes = b"") -> Request:
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", b"application/json")] if body else []
    return Request(
        {"type": "http", "method": method, "path": path, "headers": headers,
         "query_string": b"", "scheme": "http",
         "server": ("127.0.0.1", 8890), "client": ("127.0.0.1", 1)},
        _receive,
    )


def _json_request(payload: dict) -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/x",
         "headers": [(b"content-type", b"application/json")]},
        _receive_of(json.dumps(payload).encode()),
    )


def _receive_of(body: bytes):
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return _receive


def _mkcase(demo_app, subject="Acme CRM", case_id="LW-PIA-T001",
            ticket="PRV-T-1"):
    case = demo_app.Case(
        id=case_id, ticket_number=ticket, subject=subject,
        requester="Jordan Diaz", requester_email="jordan@example.test",
        data_types="contact data")
    demo_app.STORE.cases[case_id] = case
    demo_app.STORE.tickets[ticket] = demo_app.Ticket(
        number=ticket, short_description=f"Assess {subject}",
        requester="Jordan Diaz", requester_email="jordan@example.test",
        system_name=subject, data_types="contact data", case_id=case_id)
    return case


def _wire_onetrust(demo_app, monkeypatch):
    """Bind the wire client to the in-process mock tenant over ASGI — the
    protocol run then exercises every verified shape end-to-end."""
    from starlette.testclient import TestClient
    sys.path.insert(0, str(HERE))
    try:
        import ot_mock
        from onetrust_client import OneTrustClient
    finally:
        sys.path.remove(str(HERE))
    ot_mock.reset_mock()
    tc = TestClient(demo_app.app)
    monkeypatch.setattr(demo_app.backend, "onetrust_client",
                        lambda: OneTrustClient(base_url="/ot-api", http=tc))


def _upload(name: str, data: bytes, mime: str = "application/pdf") -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name,
                      headers=Headers({"content-type": mime}))


# --------------------------------------------------------------------------- #
# Scripted interpretation: typed/spoken words -> the template vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("Yes, we do.", "yes"),
    ("we have SCCs signed already", "yes"),
    ("It's encrypted in transit and at rest.", "yes"),
    ("Nope.", "no"),
    ("We don't have a countersigned DPA yet.", "no"),
    ("There's no retention period defined.", "no"),
    ("Not applicable to this system.", "na"),
    ("I'd have to check with IT.", "unknown"),
    ("not sure, honestly", "unknown"),
    ("purple monkey dishwasher", None),
])
def test_parse_free_answer_vocabulary(demo_app, text, expected):
    assert demo_app._parse_free_answer(text) == expected


def test_interpret_records_answer_with_verbatim_note(demo_app):
    case = _mkcase(demo_app)
    said = "No — Acme hasn't countersigned the Art. 28 agreement yet."
    resp = asyncio.run(demo_app.intake_interpret(
        case.id, _json_request({"question_id": "pia_processors",
                                "text": said})))
    payload = json.loads(resp.body)
    assert payload["ok"] is True and payload["answer"] == "no"
    assert payload["method"] == "scripted"
    # The requester's own words survive into the record for the reviewer.
    assert case.answers["pia_processors"]["answer"] == "no"
    assert case.answers["pia_processors"]["note"] == said


def test_interpret_asks_for_clarification_instead_of_guessing(demo_app):
    case = _mkcase(demo_app)
    resp = asyncio.run(demo_app.intake_interpret(
        case.id, _json_request({"question_id": "pia_security",
                                "text": "the weather is nice today"})))
    payload = json.loads(resp.body)
    assert payload["ok"] is True and payload["answer"] is None
    assert payload["method"] == "clarify"
    assert "pia_security" not in case.answers


# --------------------------------------------------------------------------- #
# OneTrust-first: submit files Under Review; the decision happens in OneTrust
# --------------------------------------------------------------------------- #
def _submit(demo_app, monkeypatch, case):
    _wire_onetrust(demo_app, monkeypatch)
    for qid, (ans, note) in demo_app._DEMO_ANSWERS.items():
        case.answers[qid] = {"answer": ans, "note": note}
    asyncio.run(demo_app.intake_submit(_request("POST"), case.id))
    return demo_app.STORE.onetrust[case.onetrust_id]


def test_submit_files_to_onetrust_as_under_review(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    assert case.stage == "in_onetrust_review"
    assert ot.status == "Under Review"
    assert ot.case_id == case.id
    # The review screen carries the interview verbatim + the controls.
    assert ot.result["answers"], "answers travel with the filed record"
    assert any(a["note"] for a in ot.result["answers"])
    assert ot.controls, "control mapping travels with the filed record"
    # The governance mirror exists but nothing is completed yet.
    approval = demo_app._world().get_approval(int(case.approval_id))
    assert approval is not None and approval.status == "pending"


def test_onetrust_approve_completes_everything(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    resp = asyncio.run(demo_app.onetrust_decide(_json_request(
        {"assessment_id": ot.assessment_id, "decision": "approve",
         "reviewer": "GT Reviewer"})))
    assert json.loads(resp.body)["ok"] is True
    assert ot.status == "Completed" and ot.decided_by == "GT Reviewer"
    assert case.stage == "filed"
    # The decision is mirrored onto the governed machinery in the background:
    # the world approval row and the real assessment record both carry it.
    approval = demo_app._world().get_approval(int(case.approval_id))
    assert approval.status == "approved"
    from maverick.assessment import load_saved
    record = load_saved(case.assessment_id)
    assert record["status"] == "approved"
    assert demo_app.STORE.tickets[case.ticket_number].state == "Resolved"


def test_onetrust_send_back(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    resp = asyncio.run(demo_app.onetrust_decide(_json_request(
        {"assessment_id": ot.assessment_id, "decision": "send_back",
         "reviewer": "GT Reviewer"})))
    assert json.loads(resp.body)["ok"] is True
    assert ot.status == "Sent back"
    assert case.stage == "rejected"
    # A second decision on a decided record is refused.
    resp = asyncio.run(demo_app.onetrust_decide(_json_request(
        {"assessment_id": ot.assessment_id, "decision": "approve"})))
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# "We've already done the assessment — we're just adding stuff"
# --------------------------------------------------------------------------- #
def _dpa_pdf(demo_app) -> bytes:
    return demo_app._mini_pdf(
        "Acme CRM — updated Data Processing Agreement",
        ["Art. 28 GDPR data processing agreement with Acme CRM.",
         "Processor acts on documented instructions from the controller.",
         "Retention: personal data deleted 12 months after termination."])


def test_add_document_to_existing_onetrust_record(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    resp = asyncio.run(demo_app.onetrust_add_document(
        ot.assessment_id, _upload("acme-dpa-v2.pdf", _dpa_pdf(demo_app))))
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert len(ot.addenda) == 1
    entry = ot.addenda[0]
    assert entry["filename"] == "acme-dpa-v2.pdf"
    # A DPA-looking document goes through the real Art. 28 clause review.
    assert "clause review" in entry["summary"]
    assert entry["dpa_review_id"]
    from maverick import privacy_ops
    vendors = {r["vendor"] for r in privacy_ops.list_dpa_reviews()}
    assert "Acme CRM" in vendors
    # Mostly-missing clauses on a fresh mini-DPA => flagged for re-review.
    assert entry["re_review"] is True and ot.needs_re_review is True


def test_chat_side_append_resolves_prior_vendor_and_closes_case(
        demo_app, monkeypatch):
    first = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, first)
    # A NEW request for the same vendor: append-only, no second interview.
    second = _mkcase(demo_app, case_id="LW-PIA-T002", ticket="PRV-T-2")
    resp = asyncio.run(demo_app.intake_append_doc(
        second.id, _upload("acme-dpa-v2.pdf", _dpa_pdf(demo_app))))
    payload = json.loads(resp.body)
    assert payload["ok"] is True and payload["ot_id"] == ot.assessment_id
    assert second.stage == "addendum_filed"
    assert demo_app.STORE.tickets["PRV-T-2"].state == "Resolved"
    assert len(ot.addenda) == 1


def test_append_without_prior_assessment_is_refused(demo_app):
    case = _mkcase(demo_app, subject="Never Assessed Inc")
    resp = asyncio.run(demo_app.intake_append_doc(
        case.id, _upload("doc.pdf", _dpa_pdf(demo_app))))
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Prefills: skip questions the agent can answer with quotable provenance
# --------------------------------------------------------------------------- #
def test_document_evidence_prefills_with_negation_guard(demo_app):
    case = _mkcase(demo_app)
    text = ("Art. 28 GDPR data processing agreement with Acme CRM. "
            "Standard contractual clauses (module 2) are annexed. "
            "There is no retention period defined by the vendor.")
    hits = demo_app._prefill_from_document(case, "acme-dpa.pdf", text)
    by_q = {h["id"]: h for h in hits}
    assert by_q["pia_processors"]["answer"] == "yes"
    assert by_q["pia_transfers"]["answer"] == "no"     # safeguard exists
    # "no retention period" must NOT read as evidence FOR retention.
    assert "pia_retention" not in by_q
    # Provenance survives into the record the reviewer reads.
    note = case.answers["pia_processors"]["note"]
    assert "acme-dpa.pdf" in note and "data processing agreement" in note.lower()
    assert case.answers["pia_processors"]["auto"] is True


def test_prefill_never_overwrites_a_human_or_delegate_answer(demo_app):
    case = _mkcase(demo_app)
    case.answers["pia_security"] = {"answer": "no", "note": "typed by human"}
    case.delegations["tok"] = {"qids": ["pia_processors"], "to_name": "Alex",
                               "to_email": "a@x.test", "note": "",
                               "answered": []}
    hits = demo_app._prefill_from_document(
        case, "sec.pdf",
        "Encryption at rest (AES-256). Data processing agreement attached.")
    assert {h["id"] for h in hits} == set()  # security human-owned, processors delegated
    assert case.answers["pia_security"]["note"] == "typed by human"


def test_ticket_prefills_special_category_both_ways(demo_app):
    plain = _mkcase(demo_app)
    assert demo_app._prefill_from_ticket(plain)[0]["answer"] == "no"
    assert plain.answers["pia_special_category"]["auto"] is True
    health = _mkcase(demo_app, subject="MedTrack",
                     case_id="LW-PIA-T010", ticket="PRV-T-10")
    health.data_types = "employee health records and biometrics"
    assert demo_app._prefill_from_ticket(health)[0]["answer"] == "yes"


def test_upload_endpoint_returns_prefills(demo_app):
    case = _mkcase(demo_app)
    pdf = demo_app._mini_pdf(
        "Acme CRM — Security Overview",
        ["Encryption at rest (AES-256), TLS 1.2+, SOC 2 Type II attested."])
    resp = asyncio.run(demo_app.intake_upload(
        case.id, _upload("sec-overview.pdf", pdf)))
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert {p["id"] for p in payload["prefilled"]} == {"pia_security"}
    assert case.answers["pia_security"]["answer"] == "yes"


def test_carry_forward_prefills_from_the_last_review(demo_app, monkeypatch):
    first = _mkcase(demo_app)
    _submit(demo_app, monkeypatch, first)
    second = _mkcase(demo_app, case_id="LW-PIA-T011", ticket="PRV-T-11")
    resp = asyncio.run(demo_app.intake_carry_forward(second.id))
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert len(payload["prefilled"]) == len(demo_app._DEMO_ANSWERS)
    assert payload["prior"]["ot_id"] == first.onetrust_id
    rec = second.answers["pia_transfers"]
    assert rec["auto"] is True and "last review" in rec["source"]
    # Nothing to carry -> clean empty, not an error.
    lone = _mkcase(demo_app, subject="Globex HRIS",
                   case_id="LW-PIA-T012", ticket="PRV-T-12")
    assert json.loads(asyncio.run(
        demo_app.intake_carry_forward(lone.id)).body)["prefilled"] == []


def test_addendum_reports_gaps_closed_since_last_review(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    # First document: sparse DPA -> plenty of missing clauses recorded.
    asyncio.run(demo_app.onetrust_add_document(
        ot.assessment_id, _upload("acme-dpa-v1.pdf", _dpa_pdf(demo_app))))
    # Second document: fuller DPA -> closes some of those gaps.
    fuller = demo_app._mini_pdf(
        "Acme CRM — countersigned Data Processing Agreement v2",
        ["Art. 28 GDPR data processing agreement with Acme CRM.",
         "Processor acts only on documented instructions from the controller.",
         "Audit rights: controller may audit annually.",
         "Sub-processor list maintained; prior written authorisation required.",
         "Personnel are bound by confidentiality obligations.",
         "Breach notification to the controller without undue delay.",
         "Deletion or return of all personal data at contract end.",
         "Assists the controller with data subject requests.",
         "Technical and organisational security measures per Art. 32."])
    resp = asyncio.run(demo_app.onetrust_add_document(
        ot.assessment_id, _upload("acme-dpa-v2.pdf", fuller)))
    entry = json.loads(resp.body)["addendum"]
    assert entry["closed_gaps"], "the fuller DPA closes earlier gaps"
    assert "Closes" in entry["summary"]


def test_prior_offer_survives_ticket_prefill(demo_app, monkeypatch):
    first = _mkcase(demo_app)
    _submit(demo_app, monkeypatch, first)
    second = _mkcase(demo_app, case_id="LW-PIA-T013", ticket="PRV-T-13")
    demo_app._prefill_from_ticket(second)   # auto answers must not hide the offer
    html = asyncio.run(demo_app.intake(_request(), second.id)).body.decode()
    assert 'id="appendCard"' in html
    assert "Already on file" not in html or first.uploads  # only with docs
    assert "carry-forward" in html


# --------------------------------------------------------------------------- #
# The speed story: counted value, never invented
# --------------------------------------------------------------------------- #
def test_case_value_counts_answer_sources(demo_app):
    case = _mkcase(demo_app)
    demo_app._prefill_from_ticket(case)                       # 1 auto
    case.answers["pia_security"] = {"answer": "yes", "note": "typed"}
    case.answers["pia_rights"] = {"answer": "yes", "note": "", "by": "Alex"}
    v = demo_app._case_value(case)
    assert v["auto_answered"] == 1
    assert v["asked"] == 1
    assert v["delegated"] == 1
    assert v["questions_total"] == 10
    assert "filed_seconds" not in v  # nothing filed yet


def test_value_surfaces_after_full_flow(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    ot = _submit(demo_app, monkeypatch, case)
    v = demo_app._case_value(case)
    assert v["intake_seconds"] >= 0 and v["filed_seconds"] >= 0
    asyncio.run(demo_app.onetrust_decide(_json_request(
        {"assessment_id": ot.assessment_id, "decision": "approve",
         "reviewer": "GT Reviewer"})))
    assert "decided_elapsed" in demo_app._case_value(case)
    program = demo_app._program_value()
    assert program["filed"] == 1 and program["completed"] == 1
    assert program["saved_hours"] > 0
    # The landing page shows the strip; the OneTrust record carries speed.
    html = asyncio.run(demo_app.index(_request())).body.decode()
    assert 'id="value-strip"' in html
    assert "stated assumption, configurable" in html
    ot_html = asyncio.run(demo_app.onetrust_viewer(_request())).body.decode()
    assert "Agent speed" in ot_html and '"speed"' in ot_html
    # The intake done panel tells the requester how fast it went.
    intake_html = asyncio.run(
        demo_app.intake(_request(), case.id)).body.decode()
    assert "Ticket to filed took" in intake_html


def test_value_strip_hidden_before_any_filing(demo_app):
    html = asyncio.run(demo_app.index(_request())).body.decode()
    assert 'id="value-strip"' not in html


# --------------------------------------------------------------------------- #
# Lived-in tenant fixtures (PIA_SEED_TENANT)
# --------------------------------------------------------------------------- #
def test_tenant_seed_is_opt_in_and_idempotent(demo_app, monkeypatch):
    monkeypatch.delenv("PIA_SEED_TENANT", raising=False)
    demo_app._seed_tenant()
    assert not demo_app.STORE.onetrust
    monkeypatch.setenv("PIA_SEED_TENANT", "1")
    demo_app._seed_tenant()
    n = len(demo_app.STORE.onetrust)
    assert n == len(demo_app._TENANT_VENDORS)
    demo_app._seed_tenant()   # second call must not double-seed
    assert len(demo_app.STORE.onetrust) == n


def test_tenant_seed_shapes_a_years_history(demo_app, monkeypatch):
    monkeypatch.setenv("PIA_SEED_TENANT", "1")
    demo_app._seed_tenant()
    records = list(demo_app.STORE.onetrust.values())
    by_status: dict[str, int] = {}
    for a in records:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    assert by_status["Under Review"] == 3
    assert by_status["Sent back"] == 1
    assert by_status["Completed"] == len(records) - 4
    # Every record has a matching case and ticket; nothing touched the world.
    for a in records:
        assert a.case_id in demo_app.STORE.cases
    assert any(a.addenda for a in records)
    assert any(a.needs_re_review for a in records)
    assert demo_app._world().pending_approvals() == []
    # The value strip has program-scale numbers to show.
    v = demo_app._program_value()
    assert v["filed"] == len(records) and v["saved_hours"] > 0
    html = asyncio.run(demo_app.index(_request())).body.decode()
    assert 'id="value-strip"' in html


def test_seeded_tenant_documents_are_clickable(demo_app, monkeypatch):
    """The seeded tenant is not just rows: every vendor record carries the
    two protocol deliverables as OPENABLE documents, and two vendors carry a
    filed paper round whose redline is a real tracked-changes .docx."""
    monkeypatch.setenv("PIA_SEED_TENANT", "1")
    demo_app._seed_tenant()
    docs = demo_app._vendor_documents("Wayne Payments")
    names = [d["filename"] for d in docs]
    # Legible, versioned names — never case-id soup.
    assert "Wayne-Payments-risk-analysis-v1.docx" in names
    assert "Wayne-Payments-notice-crosscheck-v1.docx" in names
    assert any(n.endswith("-redline.docx") for n in names)
    assert any(n.endswith("-analysis.txt") for n in names)
    assert all(d["available"] for d in docs)
    assert all(d["at"] for d in docs)     # dated, so the tab reads as history
    # The risk analysis is a REAL report, not a four-line stub.
    import io as _io
    import zipfile as _zip
    att = next(a for a in demo_app._ot_mock.ATTACHMENTS.values()
               if a["filename"] == "Wayne-Payments-risk-analysis-v1.docx")
    assert att["bytes"] > 2500
    doc = _zip.ZipFile(_io.BytesIO(att["content"])).read(
        "word/document.xml").decode()
    for section in ("EXECUTIVE SUMMARY", "FINDINGS", "INTERVIEW BASIS",
                    "GOVERNANCE"):
        assert section in doc, section
    # The round sits on the case too, so the reviewer desk shows history,
    # and the redline carries real revisions (produced by the real engine).
    case = next(c for c in demo_app.STORE.cases.values()
                if c.subject == "Wayne Payments")
    assert case.paper_reviews and case.paper_reviews[0]["has_redline"]
    entry = case.paper_reviews[0]
    content = demo_app.paper_desk.redline_bytes(
        case.id, entry["instrument"], entry["version"])
    from maverick import docx_redline as dr
    ins, dels = dr.revision_count(content)
    assert ins >= 1 and dels >= 1


def test_seeded_vendor_supports_memory_and_carry_forward(demo_app, monkeypatch):
    monkeypatch.setenv("PIA_SEED_TENANT", "1")
    demo_app._seed_tenant()
    case = _mkcase(demo_app, subject="Wayne Payments",
                   case_id="LW-PIA-T020", ticket="PRV-T-20")
    html = asyncio.run(demo_app.intake(_request(), case.id)).body.decode()
    assert 'id="appendCard"' in html   # the vendor is known
    resp = asyncio.run(demo_app.intake_carry_forward(case.id))
    payload = json.loads(resp.body)
    assert len(payload["prefilled"]) == 10   # seeded interview carries over
    assert all("last review" in p["source"] for p in payload["prefilled"])


# --------------------------------------------------------------------------- #
# Pages: the selectable chat/guided experience; suggestions panel is gone
# --------------------------------------------------------------------------- #
def test_intake_page_offers_both_modes_and_voice_without_suggestions(demo_app):
    case = _mkcase(demo_app)
    html = asyncio.run(demo_app.intake(_request(), case.id)).body.decode()
    assert 'id="mode-chat"' in html and 'id="mode-guided"' in html
    assert "SpeechRecognition" in html          # voice input
    assert "speechSynthesis" in html            # optional voice replies
    assert "/interpret" in html
    # The "we've seen systems like this" suggestions panel is deleted.
    assert "seen systems like this" not in html
    assert "/insights" not in html
    assert "Use suggested" not in html


def test_intake_page_offers_append_when_vendor_already_assessed(
        demo_app, monkeypatch):
    first = _mkcase(demo_app)
    _submit(demo_app, monkeypatch, first)
    second = _mkcase(demo_app, case_id="LW-PIA-T003", ticket="PRV-T-3")
    html = asyncio.run(demo_app.intake(_request(), second.id)).body.decode()
    assert 'id="appendCard"' in html
    assert "Just adding documents" in html
    # No prior vendor -> no append offer.
    fresh = _mkcase(demo_app, subject="Globex HRIS",
                    case_id="LW-PIA-T004", ticket="PRV-T-4")
    html = asyncio.run(demo_app.intake(_request(), fresh.id)).body.decode()
    assert 'id="appendCard"' not in html


def test_onetrust_page_is_a_review_surface(demo_app, monkeypatch):
    case = _mkcase(demo_app)
    _submit(demo_app, monkeypatch, case)
    html = asyncio.run(demo_app.onetrust_viewer(_request())).body.decode()
    assert "Under Review" in html
    assert "/onetrust/decide" in html
    assert "Add document to this vendor" in html
    assert "reviews and approves it right here" in html


# --------------------------------------------------------------------------- #
# Repeat review: v2 is a delta, and the vendor's paper is never skipped
# --------------------------------------------------------------------------- #
def _docx_text(data: bytes) -> str:
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode("utf-8", "replace")


def test_repeat_review_files_a_delta_and_rereviews_the_paper(
        demo_app, monkeypatch):
    # One live tenant for BOTH rounds (_submit re-wires and RESETS the mock,
    # which would wipe round 1's filings between rounds).
    _wire_onetrust(demo_app, monkeypatch)

    def _run(case):
        for qid, (ans, note) in demo_app._DEMO_ANSWERS.items():
            case.answers[qid] = {"answer": ans, "note": note}
        asyncio.run(demo_app.intake_submit(_request("POST"), case.id))
        return demo_app.STORE.onetrust[case.onetrust_id]

    # Round 1: first assessment for the vendor, decided; their DPA lands on
    # the record as a counterparty upload. Unique vendor name: the module
    # fixture shares the store across tests, and the delta looks up priors
    # by subject.
    case1 = _mkcase(demo_app, subject="Rekall Deux Analytics",
                    case_id="LW-PIA-T001R", ticket="PRV-T-1R")
    ot1 = _run(case1)
    asyncio.run(demo_app.onetrust_decide(_json_request(
        {"assessment_id": ot1.assessment_id, "decision": "approve",
         "reviewer": "GT Reviewer"})))
    demo_app._ot_mock.seed_attachment(
        case1.subject, "acme-dpa.docx", demo_app._seed_paper_docx(case1.subject),
        demo_app.paper_desk.DOCX_MIME, by="Vendor upload")

    # Round 2: a fresh case for the same vendor.
    case2 = _mkcase(demo_app, subject="Rekall Deux Analytics",
                    case_id="LW-PIA-T002R", ticket="PRV-T-2R")
    _run(case2)

    # The v2 risk analysis reads as a DELTA against round 1, not a duplicate.
    docs = demo_app._vendor_documents(case2.subject)
    risk = [d for d in docs if "-risk-analysis-" in d["filename"]]
    assert risk, "second review files its own analysis version"
    assert any("-v2" in d["filename"] for d in risk)
    newest = max(risk, key=lambda d: d["at"])
    xml = _docx_text(
        demo_app._ot_mock.ATTACHMENTS[newest["id"]]["content"])
    assert "CHANGES SINCE THE LAST REVIEW" in xml
    assert "Risk movement" in xml and ot1.assessment_id in xml
    # And the FIRST review never claims a delta: at filing time the run's own
    # record is already mirrored in the store, and it must not read as its
    # own "prior" (the self-reference bug this test pinned down).
    v1 = next(d for d in risk if "-v1" in d["filename"])
    assert "CHANGES SINCE THE LAST REVIEW" not in _docx_text(
        demo_app._ot_mock.ATTACHMENTS[v1["id"]]["content"])

    # The DPA on record was re-reviewed in the same run: a clause round with
    # a filed memo + tracked-changes redline, never silently skipped.
    assert case2.paper_reviews, "counterparty paper must be re-reviewed"
    round1 = case2.paper_reviews[-1]
    assert round1.get("attachment_id") or round1.get("version")
    filed = demo_app._vendor_documents(case2.subject)
    assert any("redline" in d["filename"] for d in filed)
