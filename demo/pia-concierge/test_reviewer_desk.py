"""The reviewer desk: assigned queue, the paper question, and the vendor-paper
round that files a versioned analysis memo + tracked-changes redline."""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

HERE = Path(__file__).resolve().parent

pytest.importorskip("maverick.paper_review",
                    reason="vendor-paper redline is a platform capability")

# The two agent SKUs share module NAMES by design, so every loader purges the
# shared names before executing its app (see test_onetrust_protocol). paper_desk
# is on the list because it binds STORE at import: left cached across a purge it
# would keep writing to the previous test module's store.
_SHARED_MODULES = ("backend", "capabilities", "license_kit", "mailsink",
                   "value_ledger", "store", "pia_engine", "dsar_engine",
                   "ot_mock", "onetrust_client", "notice_check",
                   "contract_guard", "serve_standalone", "paper_desk")

VENDOR_DPA = """DATA PROCESSING AGREEMENT
This Data Processing Agreement is entered into pursuant to Article 28 GDPR.
1. Processor shall process personal data only on documented instructions from the Controller.
2. Personnel are bound by appropriate confidentiality undertakings.
3. Processor may engage sub-processors at its sole discretion without notice to the Controller.
4. Personal data may be transferred to any third country at Processor's sole discretion.
"""


def _docx(paragraphs: list[str]) -> bytes:
    from maverick import docx_redline as dr
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{dr._W_NS}"><w:body>{body}{dr._SECT_PR}'
           "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", dr._CONTENT_TYPES)
        z.writestr("_rels/.rels", dr._ROOT_RELS)
        z.writestr("word/_rels/document.xml.rels", dr._DOCUMENT_RELS)
        z.writestr("word/settings.xml", dr._SETTINGS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")


@pytest.fixture(scope="module")
def demo():
    # Pin PLATFORM mode for the load: capabilities.STANDALONE is evaluated at
    # import time, and vendor-paper redlining is a platform capability. Another
    # module importing the standalone launcher sets PIA_STANDALONE process-wide,
    # so this fixture states which SKU it is testing rather than inheriting it.
    prev = os.environ.get("PIA_STANDALONE")
    os.environ.pop("PIA_STANDALONE", None)
    for name in _SHARED_MODULES:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_reviewer_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import ot_mock
        import paper_desk
        import store
        from capabilities import CAPS
        from onetrust_client import OneTrustClient
        assert CAPS["paper_redline"], (
            "loaded in standalone mode — the platform redline capability is "
            "off, so these tests would assert against the wrong SKU")
        return {"app": module, "ot_mock": ot_mock, "paper_desk": paper_desk,
                "store": store, "Client": OneTrustClient}
    finally:
        sys.path.remove(str(HERE))
        if prev is not None:
            os.environ["PIA_STANDALONE"] = prev


@pytest.fixture()
def desk(demo, tmp_path, monkeypatch):
    """The demo app with an isolated home, two seeded cases on different
    reviewers, and the OneTrust client bound to the in-process mock."""
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("PIA_SEED_TENANT", "0")
    app_mod = demo["app"]
    STORE = demo["store"].STORE
    Case = demo["store"].Case

    demo["ot_mock"].reset_mock()
    STORE.cases.clear()
    STORE.tickets.clear()
    STORE.onetrust.clear()
    STORE._paper_seq.clear()
    demo["paper_desk"]._REDLINES.clear()

    client = TestClient(app_mod.app)
    monkeypatch.setattr(
        app_mod.backend, "onetrust_client",
        lambda: demo["Client"](base_url="/ot-api", token="demo-bearer-token",
                               http=client))

    case = Case(id="LW-PIA-0001", ticket_number="PRV0001001",
                subject="Acme CRM", requester="Dana Reed",
                requester_email="dana@example.com", data_types="contact data")
    case.assigned_to = "A. Novak (Privacy)"
    STORE.cases[case.id] = case
    other = Case(id="LW-PIA-0002", ticket_number="PRV0001002",
                 subject="Globex HRIS", requester="Sam Lee",
                 requester_email="sam@example.com", data_types="HR data")
    other.assigned_to = "L. Chen (Privacy)"
    STORE.cases[other.id] = other
    return {"client": client, "case": case, "other": other,
            "paper_desk": demo["paper_desk"], "store": STORE,
            "ot_mock": demo["ot_mock"]}


# --- the queue ------------------------------------------------------------

def test_reviewer_sees_only_their_own_numbered_queue(desk):
    pd = desk["paper_desk"]
    mine = pd.queue_for("A. Novak (Privacy)")
    assert [r["n"] for r in mine] == [1]
    assert mine[0]["case_id"] == "LW-PIA-0001"
    # Another reviewer's numbering is independent and cannot reach my case.
    theirs = pd.queue_for("L. Chen (Privacy)")
    assert theirs[0]["case_id"] == "LW-PIA-0002"
    assert pd.case_by_number("A. Novak (Privacy)", 1).id == "LW-PIA-0001"
    assert pd.case_by_number("L. Chen (Privacy)", 1).id == "LW-PIA-0002"
    # A number outside my queue resolves to nothing, never to someone else's.
    assert pd.case_by_number("A. Novak (Privacy)", 2) is None


def test_desk_page_lists_the_queue_on_sign_in(desk):
    r = desk["client"].get("/reviewer", params={"who": "A. Novak (Privacy)"})
    assert r.status_code == 200
    assert "Acme CRM" in r.text and "Globex HRIS" not in r.text


# --- question one ---------------------------------------------------------

def test_our_paper_ends_the_round(desk):
    r = desk["client"].post("/reviewer/case/LW-PIA-0001/paper-source",
                            data={"source": "ours"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["upload"] is False
    assert desk["case"].paper_source == "ours"


def test_their_paper_asks_for_the_document(desk):
    body = desk["client"].post("/reviewer/case/LW-PIA-0001/paper-source",
                               data={"source": "theirs"}).json()
    assert body["ok"] and body["upload"] is True
    assert desk["case"].paper_source == "theirs"


def test_a_bogus_answer_is_rejected(desk):
    r = desk["client"].post("/reviewer/case/LW-PIA-0001/paper-source",
                            data={"source": "maybe"})
    assert r.status_code == 400


# --- the paper round ------------------------------------------------------

def _upload(client, filename, data, mime):
    return client.post("/reviewer/case/LW-PIA-0001/paper-upload",
                       files={"file": (filename, data, mime)},
                       data={"who": "A. Novak (Privacy)", "use_model": "0"})


def test_docx_upload_produces_a_versioned_redline_and_memo(desk):
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    r = _upload(client, "Acme-DPA.docx", original, DOCX_MIME)
    assert r.status_code == 200, r.text
    rd = r.json()["round"]

    assert rd["instrument"] == "dpa"
    assert rd["version"] == 1
    assert rd["gaps"] >= 1 and rd["high"] >= 1
    assert rd["recommendation"] == "do_not_sign_without_changes"
    assert rd["redline_filename"] == "Acme-CRM-dpa-v1-redline.docx"
    assert rd["report_filename"] == "Acme-CRM-dpa-v1-analysis.txt"
    # Both artifacts reached the vendor's Documents tab.
    assert rd["filed"] and len(rd["attachment_ids"]) == 2 and not rd["error"]


def test_filed_attachments_land_on_the_vendor_record(desk):
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(desk["client"], "Acme-DPA.docx", original, DOCX_MIME)
    attachments = list(desk["ot_mock"].mock_state()["attachments"].values())
    names = [a["filename"] for a in attachments]
    assert "Acme-CRM-dpa-v1-redline.docx" in names
    assert "Acme-CRM-dpa-v1-analysis.txt" in names
    # Linked to a record, not left dangling after the upload step.
    assert attachments and all(a["linked_to"] for a in attachments)
    # The redline carried real bytes, not an empty placeholder.
    redline = next(a for a in attachments
                   if a["filename"].endswith("-redline.docx"))
    assert redline["bytes"] > 500


def test_versions_increment_per_vendor_and_instrument(desk):
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    v1 = _upload(client, "Acme-DPA.docx", original, DOCX_MIME).json()["round"]
    v2 = _upload(client, "Acme-DPA-rev2.docx", original,
                 DOCX_MIME).json()["round"]
    assert (v1["version"], v2["version"]) == (1, 2)
    assert "v2" in v2["redline_filename"]
    # A different instrument for the same vendor numbers independently, so the
    # DPA history and the addendum history each read v1, v2, ...
    assert desk["store"].paper_version_count("Acme CRM", "dpa") == 2
    assert desk["store"].paper_version_count("Acme CRM", "addendum") == 0


def test_pdf_upload_is_accepted_and_reconstructed(desk):
    # A digital PDF the stdlib extractor can read.
    from maverick import privacy_ops
    pdf = _tiny_pdf(VENDOR_DPA)
    if not privacy_ops._document_text(pdf, "application/pdf").strip():
        pytest.skip("stdlib PDF extractor could not read the fixture")
    r = _upload(desk["client"], "Acme-DPA.pdf", pdf, "application/pdf")
    assert r.status_code == 200, r.text
    assert r.json()["round"]["version"] == 1


def test_unsupported_file_type_is_refused(desk):
    r = _upload(desk["client"], "notes.txt", b"hello", "text/plain")
    assert r.status_code == 400
    assert "Word" in r.json()["error"]


def test_unreadable_document_is_refused_not_silently_reviewed(desk):
    # An empty .docx: the package parses but yields no text. Reviewing it would
    # report "10 clauses missing" about a document we never actually read.
    r = _upload(desk["client"], "scanned.docx", _docx([]), DOCX_MIME)
    assert r.status_code == 400
    assert "no text" in r.json()["error"]


def test_memo_is_downloadable_and_states_its_limits(desk):
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(client, "Acme-DPA.docx", original, DOCX_MIME)
    memo = client.get("/reviewer/case/LW-PIA-0001/round/1/report").text
    assert "VENDOR PAPER REVIEW — Acme CRM  (v1)" in memo
    assert "Data Processing Agreement" in memo
    assert "CONFLICTS WITH OUR POSITION" in memo
    assert "sole discretion" in memo          # quotes their actual wording
    # It is explicit that findings are deterministic, not model-invented.
    assert "a model cannot create or clear a finding" in memo
    assert client.get("/reviewer/case/LW-PIA-0001/round/9/report"
                      ).status_code == 404


def test_redline_attached_is_a_real_tracked_changes_docx(desk, monkeypatch):
    """The bytes filed must actually carry w:ins/w:del revisions -- a redline
    that renders as ordinary text is worthless to the counsel receiving it."""
    from maverick import docx_redline as dr
    paper_desk = desk["paper_desk"]
    captured = {}
    real = paper_desk.file_paper_review

    def spy(case, review, redline_result, **kw):
        captured["content"] = redline_result.content
        return real(case, review, redline_result, **kw)

    monkeypatch.setattr(paper_desk, "file_paper_review", spy)
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(desk["client"], "Acme-DPA.docx", original, DOCX_MIME)
    ins, dels = dr.revision_count(captured["content"])
    assert ins >= 1 and dels >= 1


def test_redline_is_downloadable_from_the_desk(desk):
    """The rounds table's Redline button serves the exact tracked-changes
    .docx that was filed — the reviewer opens it in Word without a trip
    through the tenant."""
    from maverick import docx_redline as dr
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(client, "Acme-DPA.docx", original, DOCX_MIME)
    r = client.get("/reviewer/case/LW-PIA-0001/round/1/redline")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(DOCX_MIME)
    assert "Acme-CRM-dpa-v1-redline.docx" in r.headers["content-disposition"]
    ins, dels = dr.revision_count(r.content)
    assert ins >= 1 and dels >= 1
    assert client.get("/reviewer/case/LW-PIA-0001/round/9/redline"
                      ).status_code == 404
    assert client.get("/reviewer/case/LW-PIA-9999/round/1/redline"
                      ).status_code == 404


def test_mock_tenant_retains_and_serves_the_filed_bytes(desk):
    """What lands on the tenant must be openable there: the mock keeps the
    uploaded bytes and serves them back bearer-gated."""
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(client, "Acme-DPA.docx", original, DOCX_MIME)
    atts = desk["ot_mock"].mock_state()["attachments"]
    redline = next(a for a in atts.values()
                   if a["filename"].endswith("-redline.docx"))
    assert redline["content"] and len(redline["content"]) == redline["bytes"]
    r = client.get(f"/ot-api/api/document/v2/attachments/{redline['id']}/file",
                   headers={"Authorization": "Bearer demo-bearer-token"})
    assert r.status_code == 200 and r.content == redline["content"]
    assert client.get(
        f"/ot-api/api/document/v2/attachments/{redline['id']}/file"
    ).status_code == 401


def test_documents_open_from_the_onetrust_viewer(desk):
    """The OneTrust-look record pop-out lists the vendor's documents and each
    one opens: the memo renders as text, the redline downloads for Word."""
    client = desk["client"]
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    _upload(client, "Acme-DPA.docx", original, DOCX_MIME)
    # The viewer keys documents off a record's subject: give Acme CRM one.
    OneTrustAssessment = desk["ot_mock"].OneTrustAssessment
    ot = OneTrustAssessment(
        assessment_id="OT-ASMT-9001", name="PIA — Acme CRM",
        template="Privacy Impact Assessment", subject="Acme CRM",
        status="Completed", risk_level="high", result={})
    desk["store"].onetrust[ot.assessment_id] = ot
    html = client.get("/onetrust").text
    assert "Acme-CRM-dpa-v1-redline.docx" in html
    assert "Acme-CRM-dpa-v1-analysis.txt" in html

    atts = desk["ot_mock"].mock_state()["attachments"]
    memo = next(a for a in atts.values()
                if a["filename"].endswith("-analysis.txt"))
    redline = next(a for a in atts.values()
                   if a["filename"].endswith("-redline.docx"))
    r = client.get(f"/onetrust/attachment/{memo['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "VENDOR PAPER REVIEW — Acme CRM" in r.text
    r = client.get(f"/onetrust/attachment/{redline['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(DOCX_MIME)
    assert "attachment" in r.headers["content-disposition"]
    assert client.get("/onetrust/attachment/att-none").status_code == 404


def _tiny_pdf(text: str) -> bytes:
    """A minimal single-page PDF with an uncompressed text stream."""
    lines = [ln for ln in text.split("\n") if ln.strip()][:6]
    shown = "\n".join(
        f"({ln.replace(chr(92), '').replace('(', '').replace(')', '')}) Tj 0 -14 Td"
        for ln in lines)
    content = f"BT /F1 10 Tf 40 750 Td\n{shown}\nET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


# --- our paper: the drafter -----------------------------------------------

def test_our_paper_answer_files_a_red_filled_draft(desk):
    client = desk["client"]
    r = client.post("/reviewer/case/LW-PIA-0001/paper-source",
                    data={"source": "ours", "who": "A. Novak (Privacy)"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["upload"] is False
    rd = body["round"]
    assert rd["kind"] == "our_paper" and rd["version"] == 1
    assert rd["filed"] and len(rd["attachment_ids"]) == 2 and not rd["error"]
    assert rd["redline_filename"] == "Acme-CRM-dpa-v1-our-paper.docx"
    assert any("Acme CRM" in f for f in rd["filled"])

    # Filed to the tenant: cover note + the draft, which carries red fills.
    atts = desk["ot_mock"].mock_state()["attachments"]
    draft = next(a for a in atts.values()
                 if a["filename"].endswith("-our-paper.docx"))
    doc = zipfile.ZipFile(io.BytesIO(draft["content"])).read(
        "word/document.xml").decode()
    from maverick.docx_redline import FILL_COLOR, revision_count
    assert doc.count(f'w:val="{FILL_COLOR}"') >= 4
    assert "Acme CRM" in doc
    assert revision_count(draft["content"]) == (0, 0)   # a draft, not a redline

    # The desk serves the same bytes; the note names every red value.
    dl = client.get("/reviewer/case/LW-PIA-0001/round/1/redline")
    assert dl.status_code == 200 and dl.content == draft["content"]
    note = client.get("/reviewer/case/LW-PIA-0001/round/1/report").text
    assert "AUTO-FILLED VALUES" in note and "Acme CRM" in note
    assert "Verify every red value before signature" in note


def test_our_paper_and_their_paper_share_one_version_stream(desk):
    client = desk["client"]
    client.post("/reviewer/case/LW-PIA-0001/paper-source",
                data={"source": "ours", "who": "A. Novak (Privacy)"})
    original = _docx([p for p in VENDOR_DPA.split("\n") if p.strip()])
    rd = _upload(client, "Acme-DPA.docx", original, DOCX_MIME).json()["round"]
    # The vendor came back with their own paper: the negotiation history for
    # this instrument keeps counting -- v1 our draft, v2 their redlined paper.
    assert rd["version"] == 2


# --- the negotiation round-trip -------------------------------------------

def test_round_trip_reports_which_demands_were_accepted(desk):
    client = desk["client"]
    lines = [p for p in VENDOR_DPA.split("\n") if p.strip()]
    v1 = _upload(client, "Acme-DPA.docx", _docx(lines),
                 DOCX_MIME).json()["round"]
    assert v1["previous_version"] == 0 and v1["gap_reqs"]

    # Their v2 accepts our sub-processor clause verbatim; the rest unchanged.
    from maverick.paper_review import OUR_POSITIONS
    v2_doc = _docx(lines[:4] + [OUR_POSITIONS["subprocessors"]])
    v2 = _upload(client, "Acme-DPA-rev2.docx", v2_doc,
                 DOCX_MIME).json()["round"]
    assert v2["version"] == 2 and v2["previous_version"] == 1
    assert ("Sub-processor authorization and flow-down"
            in v2["closed_from_previous"])
    assert v2["gaps"] < v1["gaps"]
    memo = client.get("/reviewer/case/LW-PIA-0001/round/2/report").text
    assert "NEGOTIATION PROGRESS" in memo
    assert "+ Sub-processor authorization and flow-down" in memo


# --- addenda are openable too ---------------------------------------------

def test_addendum_upload_is_openable_from_the_tenant(desk):
    client = desk["client"]
    OneTrustAssessment = desk["ot_mock"].OneTrustAssessment
    ot = OneTrustAssessment(
        assessment_id="OT-ASMT-9100", name="PIA — Acme CRM",
        template="Privacy Impact Assessment", subject="Acme CRM",
        status="Completed", risk_level="high", result={})
    desk["store"].onetrust[ot.assessment_id] = ot
    pdf = _tiny_pdf("Amendment to the data processing agreement terms.")
    r = client.post(f"/onetrust/record/{ot.assessment_id}/add-document",
                    files={"file": ("amendment.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    entry = r.json()["addendum"]
    assert entry["attachment_id"]
    served = client.get(f"/onetrust/attachment/{entry['attachment_id']}")
    assert served.status_code == 200 and served.content == pdf
