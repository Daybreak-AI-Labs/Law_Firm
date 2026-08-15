"""Mock OneTrust tenant — speaks the VERIFIED wire protocol, traps included.

This is not a friendly stub: it enforces the exact shapes a live OneTrust
tenant accepted (see ONETRUST-INTEGRATION.md), including the traps that make
naive integrations fail there:

* plain GET /assessments/{id} → 403 (read via /export only),
* option answers must carry the optionId AND responseKey,
* a top-level "justification" field is SILENTLY DROPPED (the tenant's
  behavior — required questions then fail submit with no hint),
* PERSONAL_DATA rows must be clean triples (the mirror nodes the export
  shows are rejected on write) and a write replaces the whole row set,
* submit's 403 under-reports: requiredUnansweredQuestionIds misses missing
  justifications and blank inventory dropdowns,
* reopen / delete / assessment-level attach → 403
  ACCESS-MANAGEMENT-ASSERTIONS (browser-session-gated, not a scope issue),
* the inventory list ignores server-side name filters,
* the assessment list paginates with overlaps (dedupe or double-count),
* enterprise-policy list requires lastPublishedDate; the bare notice guid
  is the 403-gated edit resource (/details is the read view).

A test suite that passes against this mock is wire-compatible with the
tenant the shapes were verified on.
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from store import STORE, OneTrustAssessment

router = APIRouter(prefix="/ot-api")

# Attachment content is RETAINED so the demo tenant can show the documents the
# agent filed (the whole point of a Documents tab is opening them). Bounded on
# both axes so a long-running demo can't grow without limit: oversized uploads
# are rejected like a tenant would, and once the retained pool exceeds the
# budget the OLDEST content is evicted (metadata stays, so the row still lists;
# the download honestly 410s).
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_RETAINED_BYTES = 64 * 1024 * 1024

_SESSION_403 = {"errorCode": "ACCESS-MANAGEMENT-ASSERTIONS_ACCESS_VIOLATION",
                "message": "assertion failed for token fingerprint"}


# --------------------------------------------------------------------------- #
# Tenant fixtures: templates, inventories, catalogs, notices.
# --------------------------------------------------------------------------- #
def _opt(qid: str, n: int, text: str) -> dict:
    return {"id": f"opt-{qid}-{n}", "optionText": text,
            "translationId": f"t.{qid}"}


def _select(qid: str, text: str, options: list[str], *, required: bool,
            justification: bool = False) -> dict:
    return {"questionId": qid, "text": text, "questionType": "SELECT",
            "required": required, "requiresJustification": justification,
            "translationId": f"t.{qid}",
            "options": [_opt(qid, i, o) for i, o in enumerate(options)],
            "responses": []}


def _text_q(qid: str, text: str, *, required: bool = False) -> dict:
    return {"questionId": qid, "text": text, "questionType": "TEXT",
            "required": required, "requiresJustification": False,
            "translationId": f"t.{qid}", "options": [], "responses": []}


TEMPLATES: dict[str, dict] = {
    "tmpl-pia": {
        "templateId": "tmpl-pia", "name": "Privacy Impact Assessment",
        "sections": [
            {"sectionId": "sec-overview", "name": "Overview", "questions": [
                _text_q("q-name", "Name of the processing activity",
                        required=True),
                _text_q("q-summary", "Executive summary"),
            ]},
            {"sectionId": "sec-risk", "name": "Risk", "questions": [
                _select("q-risk", "Overall risk rating",
                        ["Low", "Medium", "High"], required=True,
                        justification=True),
                _select("q-ai", "Do any Artificial Intelligence (AI) tools "
                                "support this process?", ["Yes", "No"],
                        required=True),
                _select("q-sold", "Is personal data sold or shared for "
                                  "value?", ["Yes", "No"], required=False),
            ]},
            {"sectionId": "sec-data", "name": "Data", "questions": [
                {"questionId": "q-vendor", "text": "Primary vendor record",
                 "questionType": "INVENTORY", "required": True,
                 "requiresJustification": False,
                 "translationId": "t.q-vendor", "options": [],
                 "responses": []},
                {"questionId": "q-personal-data",
                 "text": "What personal data is involved?",
                 "questionType": "PERSONAL_DATA", "required": True,
                 "requiresJustification": False,
                 "translationId": "t.q-personal-data", "options": [],
                 "responses": []},
            ]},
        ],
    },
    "tmpl-ai": {
        "templateId": "tmpl-ai", "name": "AI Model Assessment",
        "sections": [
            {"sectionId": "sec-ai", "name": "Model", "questions": [
                _text_q("q-ai-name", "AI system name", required=True),
                _select("q-ai-oversight", "Is meaningful human oversight in "
                                          "place?", ["Yes", "No"],
                        required=True),
            ]},
        ],
    },
}

VENDORS: list[dict] = []
ENTITIES: list[dict] = []
CATALOGS: dict[str, list[dict]] = {"elements": [], "subjects": [],
                                   "categories": []}
NOTICES: list[dict] = []
NOTICE_BODIES: dict[str, list[dict]] = {}
LINKS: list[dict] = []
ATTACHMENTS: dict[str, dict] = {}
_SEQ = {"vendor": 100, "att": 0}


def reset_mock() -> None:
    """Reseed the tenant fixtures (tests call this; import seeds once)."""
    VENDORS.clear()
    VENDORS.extend({"id": f"ven-{i}", "name": n, "type": "Vendor"}
                   for i, n in enumerate(
                       ["Awardco", "Cvent", "Medallia", "Acme Analytics",
                        "Brand and Digital Services"], start=1))
    ENTITIES.clear()
    ENTITIES.extend({"id": f"ent-{i}", "name": n, "type": "Entity"}
                    for i, n in enumerate(
                        ["Global Vacation Clubs", "Corporate"], start=1))
    cats = {"Contact Data": "cat-1", "Financial Data": "cat-2",
            "HR Data": "cat-3", "Behavioural Data": "cat-4"}
    CATALOGS["categories"] = [
        {"id": cid, "name": name, "nameKey": f"k.{cid}"}
        for name, cid in cats.items()]
    CATALOGS["subjects"] = [
        {"id": "sub-1", "name": "Customers", "nameKey": "k.sub-1"},
        {"id": "sub-2", "name": "Employees", "nameKey": "k.sub-2"},
        {"id": "sub-3", "name": "Prospects", "nameKey": "k.sub-3"}]
    def _el(i, name, cat):
        return {"id": f"el-{i}", "name": name, "nameKey": f"k.el-{i}",
                "categories": [{"id": cats[cat], "name": cat,
                                "nameKey": f"k.{cats[cat]}"}]}
    CATALOGS["elements"] = [
        _el(1, "name", "Contact Data"),
        _el(2, "email address", "Contact Data"),
        _el(3, "phone number", "Contact Data"),
        _el(4, "postal address", "Contact Data"),
        _el(5, "payment card number", "Financial Data"),
        _el(6, "salary", "HR Data"),
        _el(7, "browsing history", "Behavioural Data"),
        _el(8, "location", "Behavioural Data")]
    NOTICES.clear()
    NOTICES.extend([
        {"guid": "ntc-default",
         "organizationName": "Global Vacation Clubs",
         "name": "Global Vacation Clubs Privacy Notice",
         "description": "Customer-facing privacy notice",
         "owners": ["privacy-office@company.com"],
         "defaultLanguageCode": "en"},
        {"guid": "ntc-brand",
         "organizationName": "Brand and Digital Services",
         "name": "Brand and Digital Privacy Notice",
         "description": "Sibling brand notice", "owners": [],
         "defaultLanguageCode": "en"},
    ])
    NOTICE_BODIES.clear()
    NOTICE_BODIES["ntc-default"] = [
        {"name": "Your privacy", "order": 1, "content":
         "<p>We collect contact data (name, email address, phone number) "
         "and payment details to run your membership.</p>"},
        {"name": "Sharing", "order": 2, "content":
         "<p>We do not sell your personal information. We share it only "
         "with service providers under contract.</p>"}]
    NOTICE_BODIES["ntc-brand"] = [
        {"name": "Overview", "order": 1, "content":
         "<p>Brand and Digital processes contact data for marketing.</p>"}]
    LINKS.clear()
    ATTACHMENTS.clear()


reset_mock()


def _bearer_ok(request: Request) -> bool:
    return request.headers.get("authorization", "").startswith("Bearer ")


def _missing_bearer() -> JSONResponse:
    return JSONResponse({"error": "missing bearer token"}, status_code=401)


def _rec(aid: str) -> OneTrustAssessment | None:
    return STORE.onetrust.get(aid)


def _questions(rec: OneTrustAssessment) -> dict[str, dict]:
    out = {}
    for section in rec.questions:
        for q in section["questions"]:
            out[q["questionId"]] = q
    return out


# --------------------------------------------------------------------------- #
# Assessments: launch (v3), list (v3, overlapping pages), export (v2).
# --------------------------------------------------------------------------- #
@router.post("/api/assessment/v3/assessments")
async def ot_launch(request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    body = await request.json()
    template = TEMPLATES.get(str(body.get("templateId", "")))
    if template is None or not body.get("orgGroupId") \
            or not body.get("respondentEmail"):
        return JSONResponse(
            {"errorCode": "INVALID_REQUEST_INPUT",
             "message": "templateId, orgGroupId and respondentEmail are "
                        "required"}, status_code=400)
    ot_id = STORE.next_onetrust_id()
    STORE.onetrust[ot_id] = OneTrustAssessment(
        assessment_id=ot_id,
        name=str(body.get("name") or template["name"]),
        template=template["name"], subject="", status="In Progress",
        risk_level="unknown", result={},
        questions=copy.deepcopy(template["sections"]),
        respondent=str(body.get("respondentEmail")),
        org_group=str(body.get("orgGroupId")))
    return JSONResponse({"assessmentId": ot_id, "status": "In Progress"},
                        status_code=201)


@router.post("/api/assessment/v3/assessments/list")
async def ot_list(request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    page = int(request.query_params.get("page", 0))
    size = int(request.query_params.get("size", 50))
    statuses = request.query_params.get("assessmentStatuses", "")
    rows = [{"assessmentId": a.assessment_id, "name": a.name,
             "status": a.status, "templateName": a.template,
             "submittedOn": a.submitted_on}
            for a in STORE.onetrust.values()
            if not statuses or a.status in statuses.split(",")]
    # The tenant's pagination is inconsistent: pages overlap by one row.
    # Clients that don't dedupe by assessmentId double-count.
    start = max(0, page * size - (1 if page else 0))
    total_pages = max(1, (len(rows) + size - 1) // size)
    return JSONResponse({"content": rows[start:start + size],
                         "totalPages": total_pages})


@router.get("/api/assessment/v2/assessments/{aid}/export")
async def ot_export(aid: str, request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    rec = _rec(aid)
    if rec is None:
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    sections = copy.deepcopy(rec.questions)
    for section in sections:
        for q in section["questions"]:
            entries = copy.deepcopy(rec.responses.get(q["questionId"], []))
            if q["questionType"] == "PERSONAL_DATA":
                # The export decorates PD rows with mirror nodes a write
                # must NOT send back.
                for e in entries:
                    if e.get("responseMap"):
                        e["element"] = e["responseMap"]["DATA_ELEMENTS"]
                        e["personalDataDetailId"] = f"pdd-{aid}"
            q["responses"] = entries
    return JSONResponse({"assessmentId": aid, "name": rec.name,
                         "status": rec.status, "sections": sections})


@router.get("/api/assessment/v2/assessments/{aid}")
async def ot_plain_read(aid: str) -> JSONResponse:
    # The trap: the plain resource 403s even for records the key owns.
    return JSONResponse({"errorCode": "ACCESS_DENIED",
                         "message": "use /export"}, status_code=403)


# --------------------------------------------------------------------------- #
# Responses: the verified write shapes, validated hard.
# --------------------------------------------------------------------------- #
def _validate_entry(rec: OneTrustAssessment, entry: dict,
                    questions: dict[str, dict]) -> str | None:
    q = questions.get(str(entry.get("questionId", "")))
    if q is None:
        return f"unknown questionId {entry.get('questionId')!r}"
    if entry.get("sectionId") not in {s["sectionId"] for s in rec.questions}:
        return f"bad sectionId for {q['questionId']}"
    kind = str(entry.get("type", ""))
    if kind == "JUSTIFICATION":
        return None if entry.get("response") else "empty justification"
    if q["questionType"] == "SELECT":
        opts = {o["id"]: o for o in q["options"]}
        opt = opts.get(str(entry.get("responseId", "")))
        if opt is None:
            return (f"{q['questionId']}: responseId must be an optionId "
                    f"(INVALID_REQUEST_INPUT)")
        want_key = f"{opt['translationId']}.option.option"
        if entry.get("responseKey") != want_key:
            return f"{q['questionId']}: responseKey must be {want_key!r}"
        return None
    if q["questionType"] == "INVENTORY":
        known = {r["id"] for r in VENDORS} | {r["id"] for r in ENTITIES}
        if str(entry.get("responseId", "")) not in known:
            return f"{q['questionId']}: responseId must be a record id"
        return None
    if q["questionType"] == "PERSONAL_DATA":
        rm = entry.get("responseMap")
        if not isinstance(rm, dict) or set(rm) != {
                "DATA_CATEGORIES", "DATA_SUBJECTS", "DATA_ELEMENTS"}:
            return (f"{q['questionId']}: PERSONAL_DATA rows need a "
                    f"responseMap triple (INVALID_REQUEST_INPUT)")
        extra = set(entry) - {"response", "responseId", "type", "responseMap",
                              "questionId", "sectionId"}
        if extra:
            return (f"{q['questionId']}: mirror nodes {sorted(extra)} must "
                    f"not be sent on write (INVALID_REQUEST_INPUT)")
        ok_ids = {c["id"] for c in CATALOGS["categories"]}
        if rm["DATA_CATEGORIES"].get("id") not in ok_ids:
            return f"{q['questionId']}: unknown data category id"
        return None
    # TEXT
    return None if isinstance(entry.get("response"), str) else \
        f"{q['questionId']}: text response required"


@router.post("/api/assessment/v2/assessments/{aid}/responses")
async def ot_write_responses(aid: str, request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    rec = _rec(aid)
    if rec is None:
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    body = await request.json()
    # The tenant behavior: a top-level "justification" field is accepted and
    # silently dropped. Only type:"JUSTIFICATION" entries count.
    entries = body.get("responses") or []
    questions = _questions(rec)
    for entry in entries:
        problem = _validate_entry(rec, entry, questions)
        if problem:
            return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT",
                                 "message": problem}, status_code=400)
    pd_written: set[str] = set()
    for entry in entries:
        qid = str(entry["questionId"])
        q = questions[qid]
        stored = {k: v for k, v in entry.items()
                  if k not in ("questionId", "sectionId")}
        if q["questionType"] == "PERSONAL_DATA":
            # A PD write REPLACES the whole row set.
            if qid not in pd_written:
                rec.responses[qid] = []
                pd_written.add(qid)
            rec.responses[qid].append(stored)
        elif stored.get("type") == "JUSTIFICATION":
            rec.responses.setdefault(qid, [])
            rec.responses[qid] = [r for r in rec.responses[qid]
                                  if r.get("type") != "JUSTIFICATION"]
            rec.responses[qid].append(stored)
        else:
            rec.responses.setdefault(qid, [])
            rec.responses[qid] = [r for r in rec.responses[qid]
                                  if r.get("type") == "JUSTIFICATION"]
            rec.responses[qid].insert(0, stored)
    return JSONResponse({"written": len(entries)})


# --------------------------------------------------------------------------- #
# Submit: the completeness gate (with the under-reporting quirk).
# --------------------------------------------------------------------------- #
@router.post("/api/assessment/v2/assessments/{aid}/submit")
async def ot_submit(aid: str, request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    rec = _rec(aid)
    if rec is None:
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    if request.query_params.get("disclaimerAccepted") != "true":
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT",
                             "message": "disclaimerAccepted required"},
                            status_code=400)
    missing_plain: list[str] = []
    blocked = False
    for q in _questions(rec).values():
        if not q["required"]:
            continue
        entries = rec.responses.get(q["questionId"], [])
        answers = [e for e in entries if e.get("type") != "JUSTIFICATION"
                   and (e.get("response") or e.get("responseMap"))]
        justs = [e for e in entries if e.get("type") == "JUSTIFICATION"]
        if not answers:
            blocked = True
            # The quirk: inventory dropdowns are missing from the server's
            # requiredUnansweredQuestionIds list.
            if q["questionType"] != "INVENTORY":
                missing_plain.append(q["questionId"])
        elif q["requiresJustification"] and not justs:
            blocked = True   # ...and justification-only misses never appear.
    if blocked:
        return JSONResponse(
            {"errorCode": "INVALID_USER_OPERATION",
             "message": "assessment is not complete",
             "requiredUnansweredQuestionIds": missing_plain},
            status_code=403)
    rec.status = "Under Review"
    rec.submitted_on = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    answers = {qid: rows for qid, rows in rec.responses.items()}
    name_rows = answers.get("q-name", [])
    rec.subject = str(name_rows[0].get("response", "")) if name_rows else ""
    risk_rows = [r for r in answers.get("q-risk", [])
                 if r.get("type") != "JUSTIFICATION"]
    rec.risk_level = str(risk_rows[0].get("response", "unknown")).lower() \
        if risk_rows else "unknown"
    return JSONResponse({"status": "Under Review"})


@router.post("/api/assessment/v2/assessments/{aid}/reopen")
async def ot_reopen(aid: str) -> JSONResponse:
    return JSONResponse(_SESSION_403, status_code=403)


@router.delete("/api/assessment/v2/assessments/{aid}")
async def ot_delete(aid: str) -> JSONResponse:
    return JSONResponse(_SESSION_403, status_code=403)


@router.post("/api/assessment/v2/assessments/{aid}/attachments")
async def ot_assessment_attach(aid: str) -> JSONResponse:
    # Assessment-level attach is browser-session-gated (fgpt cookie).
    return JSONResponse(_SESSION_403, status_code=403)


@router.post("/api/assessment/v2/assessments/assessment-links")
async def ot_link(request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    body = await request.json()
    if not body.get("fromId") or not isinstance(body.get("toIds"), list):
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT"},
                            status_code=400)
    LINKS.append({"fromId": body["fromId"], "toIds": body["toIds"]})
    return JSONResponse({"linked": len(body["toIds"])}, status_code=201)


# --------------------------------------------------------------------------- #
# Inventory + catalogs.
# --------------------------------------------------------------------------- #
@router.get("/api/inventory/v2/inventories/{schema}")
async def ot_inventory(schema: str, request: Request) -> JSONResponse:
    rows = VENDORS if schema == "vendors" else \
        ENTITIES if schema == "entities" else []
    # The trap: the server-side name filter is IGNORED for static keys.
    page = int(request.query_params.get("page", 0))
    size = int(request.query_params.get("size", 50))
    total_pages = max(1, (len(rows) + size - 1) // size)
    return JSONResponse({"content": rows[page * size:(page + 1) * size],
                         "totalPages": total_pages})


@router.post("/api/inventory/v2/inventories/vendors")
async def ot_create_vendor(request: Request) -> JSONResponse:
    body = await request.json()
    if not body.get("name"):
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT"},
                            status_code=400)
    _SEQ["vendor"] += 1
    rec = {"id": f"ven-{_SEQ['vendor']}", "name": str(body["name"]),
           "type": str(body.get("type", "Vendor"))}
    VENDORS.append(rec)
    return JSONResponse(rec, status_code=201)


@router.post("/api/inventory/v2/inventories/entities")
async def ot_create_entity() -> JSONResponse:
    return JSONResponse({"errorCode": "ACCESS_DENIED",
                         "message": "entities are managed centrally"},
                        status_code=403)


@router.post("/api/inventory/v2/inventories/{record_id}/attachments")
async def ot_record_attach(record_id: str, request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    body = await request.json()
    if not isinstance(body, list) or not body \
            or not body[0].get("attachmentId"):
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT"},
                            status_code=400)
    for row in body:
        att = ATTACHMENTS.get(str(row["attachmentId"]))
        if att is None:
            return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
        att["linked_to"] = record_id
    return JSONResponse({"linked": len(body)}, status_code=201)


@router.post("/api/document/v2/attachments")
async def ot_upload(request: Request) -> JSONResponse:
    if not _bearer_ok(request):
        return _missing_bearer()
    form = await request.form()
    blob = form.get("file")
    meta = form.get("attachment")
    if blob is None or meta is None:
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT",
                             "message": "multipart file + attachment JSON"},
                            status_code=400)
    try:
        parsed = json.loads(meta if isinstance(meta, str) else str(meta))
    except ValueError:
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT"},
                            status_code=400)
    content = await blob.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        return JSONResponse(
            {"errorCode": "INVALID_REQUEST_INPUT",
             "message": "attachment exceeds the size limit"}, status_code=400)
    _SEQ["att"] += 1
    att_id = f"att-{_SEQ['att']}"
    ATTACHMENTS[att_id] = {"id": att_id,
                           "filename": parsed.get("FileName", ""),
                           "bytes": len(content),
                           "mime": getattr(blob, "content_type", "") or
                           "application/octet-stream",
                           "content": content,
                           "at": time.time(),
                           "by": "Lightwork Concierge",
                           "linked_to": ""}
    _evict_over_budget()
    return JSONResponse({"Id": att_id}, status_code=201)


def _evict_over_budget() -> None:
    retained = sum(len(a.get("content", b"")) for a in ATTACHMENTS.values())
    if retained <= MAX_RETAINED_BYTES:
        return
    for att in ATTACHMENTS.values():        # dict order = upload order
        if not att.get("content"):
            continue
        retained -= len(att["content"])
        att["content"] = b""
        att["evicted"] = True
        if retained <= MAX_RETAINED_BYTES:
            return


@router.get("/api/document/v2/attachments/{att_id}/file")
async def ot_attachment_file(att_id: str, request: Request) -> Response:
    """Serve a stored attachment's bytes back. This route exists for the demo
    viewer (the agent itself never downloads its own deliverables); it is not
    part of the field-verified write protocol above."""
    if not _bearer_ok(request):
        return _missing_bearer()
    att = ATTACHMENTS.get(att_id)
    if att is None:
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    if not att.get("content"):
        return JSONResponse(
            {"errorCode": "GONE",
             "message": "attachment content was evicted from the demo "
                        "tenant's retained pool"}, status_code=410)
    return Response(att["content"], media_type=att.get("mime") or
                    "application/octet-stream")


@router.get("/api/inventory/v2/data-elements")
async def ot_elements() -> JSONResponse:
    return JSONResponse({"content": CATALOGS["elements"]})


@router.get("/api/inventory/v2/data-subjects")
async def ot_subjects() -> JSONResponse:
    return JSONResponse({"content": CATALOGS["subjects"]})


@router.get("/api/inventory/v2/data-categories")
async def ot_categories() -> JSONResponse:
    return JSONResponse({"content": CATALOGS["categories"]})


# --------------------------------------------------------------------------- #
# Templates + Enterprise Policy (the real notices).
# --------------------------------------------------------------------------- #
@router.get("/api/template/v1/templates")
async def ot_templates() -> JSONResponse:
    return JSONResponse({"content": [
        {"templateId": t["templateId"], "name": t["name"]}
        for t in TEMPLATES.values()]})


@router.get("/api/enterprise-policy/v1/{ctype}/list")
async def ot_notice_list(ctype: str, request: Request) -> JSONResponse:
    if ctype not in ("privacynotices", "policies", "standards", "procedures"):
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    if not request.query_params.get("lastPublishedDate"):
        return JSONResponse({"errorCode": "INVALID_REQUEST_INPUT",
                             "message": "lastPublishedDate is required"},
                            status_code=400)
    data = NOTICES if ctype == "privacynotices" else []
    return JSONResponse({"data": data,
                         "meta": {"page": {"totalPages": 1}}})


@router.get("/api/enterprise-policy/v1/privacynotices/{guid}/details")
async def ot_notice_details(guid: str) -> JSONResponse:
    sections = NOTICE_BODIES.get(guid)
    if sections is None:
        return JSONResponse({"errorCode": "NOT_FOUND"}, status_code=404)
    return JSONResponse({"versions": [{
        "id": f"{guid}-v1", "versionStatus": "Published",
        "publishedDate": "2026-01-01", "sections": sections}]})


@router.get("/api/enterprise-policy/v1/privacynotices/{guid}")
async def ot_notice_edit_resource(guid: str) -> JSONResponse:
    # The bare guid (and /versions) is the edit resource — 403 for API keys.
    return JSONResponse(_SESSION_403, status_code=403)


@router.get("/api/enterprise-policy/v1/privacynotices/{guid}/versions")
async def ot_notice_versions(guid: str) -> JSONResponse:
    return JSONResponse(_SESSION_403, status_code=403)


def seed_attachment(vendor_name: str, filename: str, content: bytes,
                    mime: str, at: float | None = None,
                    by: str = "Vendor upload") -> str:
    """Direct (no-HTTP) placement of a document on a vendor's record,
    creating the vendor row when the name isn't in the base fixture set.

    Two callers, neither of which is the agent: the tenant seeder (fixture
    history) and the demo UI's addenda upload (a browser action on the
    viewer, not a wire-protocol filing). The agent's own filings always go
    through the verified upload → link protocol above."""
    name = (vendor_name or "").strip()
    vendor = next((v for v in VENDORS
                   if v["name"].strip().lower() == name.lower()), None)
    if vendor is None:
        _SEQ["vendor"] += 1
        vendor = {"id": f"ven-{_SEQ['vendor']}", "name": name,
                  "type": "Vendor"}
        VENDORS.append(vendor)
    _SEQ["att"] += 1
    att_id = f"att-{_SEQ['att']}"
    ATTACHMENTS[att_id] = {"id": att_id, "filename": filename,
                           "bytes": len(content), "mime": mime,
                           "content": content,
                           "at": at if at is not None else time.time(),
                           "by": by,
                           "linked_to": vendor["id"]}
    _evict_over_budget()
    return att_id


def mock_state() -> dict[str, Any]:
    """Test hook: the tenant-side state the protocol should have produced."""
    return {"links": LINKS, "attachments": ATTACHMENTS, "vendors": VENDORS}
