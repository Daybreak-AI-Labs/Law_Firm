"""OneTrust wire client — the field-verified assessment protocol.

Every endpoint and write shape here was verified against a live OneTrust
tenant (see ONETRUST-INTEGRATION.md for the full reference, including the
traps): read via /export never /{id}; option answers carry the optionId AND a
responseKey; justifications are an ADDITIONAL entry in the same responses
array (a top-level "justification" field is silently dropped); PERSONAL_DATA
writes are full category+subject+element triples and REPLACE the whole row
set; submit's 403 completeness gate under-reports (misses justifications and
inventory dropdowns), so completeness is computed locally from the export;
attachments go on the VENDOR inventory record in two steps (assessment-level
attach, reopen, and delete are browser-session-gated — a static API key gets
403 ACCESS-MANAGEMENT-ASSERTIONS, which is not a scope problem and cannot be
fixed with permissions).

Standalone-safe: httpx + stdlib only, no platform imports. The agent stays
autonomous only up to "Under Review" — that stage IS the human gate; nothing
here can mark an assessment Complete.
"""
from __future__ import annotations

import difflib
import json
import os
from typing import Any

_SESSION_GATED = (
    "browser-session-gated on this tenant (the static API key gets 403 "
    "ACCESS-MANAGEMENT-ASSERTIONS; it is not a permission or scope issue). "
    "Do it in the OneTrust UI instead.")


class OneTrustError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class OneTrustClient:
    """Thin, deliberate client for the verified protocol. ``http`` may be any
    httpx.Client-compatible object (the tests pass a TestClient bound to the
    mock tenant); by default a real httpx.Client hits ONETRUST_HOSTNAME."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 http: Any = None):
        self.base = (base_url if base_url is not None
                     else os.environ.get("ONETRUST_HOSTNAME",
                                         "http://127.0.0.1:8890/ot-api"))
        self.base = self.base.rstrip("/")
        token = token or os.environ.get("ONETRUST_TOKEN", "demo-bearer-token")
        self._headers = {"Authorization": f"Bearer {token}"}
        if http is None:
            import httpx
            http = httpx.Client(timeout=15)
        self._http = http
        self._catalogs: dict | None = None

    # -- transport ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _fail(self, r: Any, doing: str) -> None:
        try:
            body = r.json()
        except Exception:
            body = {"message": (r.text or "")[:200]}
        code = str(body.get("errorCode") or body.get("error") or "")
        msg = f"{doing}: HTTP {r.status_code} {code} {body.get('message', '')}"
        if code.startswith("ACCESS-MANAGEMENT-ASSERTIONS"):
            msg += f" — {_SESSION_GATED}"
        raise OneTrustError(msg.strip(), status=r.status_code, code=code)

    def _get(self, path: str, **params: Any) -> Any:
        r = self._http.get(self._url(path), headers=self._headers,
                           params=params or None)
        if r.status_code >= 400:
            self._fail(r, f"GET {path}")
        return r.json()

    def _post(self, path: str, body: Any = None, *, doing: str = "",
              **params: Any) -> Any:
        r = self._http.post(self._url(path), headers=self._headers,
                            json=body, params=params or None)
        if r.status_code >= 400:
            self._fail(r, doing or f"POST {path}")
        return r.json() if (r.text or "").strip() else {}

    # -- assessments -------------------------------------------------------
    def launch(self, template_id: str, *, org_group_id: str,
               respondent: str, name: str) -> str:
        """POST /api/assessment/v3/assessments — needs the org group AND a
        respondent email, or the tenant refuses."""
        out = self._post("/api/assessment/v3/assessments", {
            "templateId": template_id, "orgGroupId": org_group_id,
            "respondentEmail": respondent, "name": name,
        }, doing="launch assessment")
        return str(out.get("assessmentId") or out.get("id") or "")

    def list_assessments(self, *, statuses: str = "",
                         max_pages: int = 20) -> list[dict]:
        """POST .../assessments/list. The tenant's pagination is inconsistent
        (pages can overlap), so rows are deduped by assessmentId."""
        seen: dict[str, dict] = {}
        for page in range(max_pages):
            params: dict[str, Any] = {"page": page, "size": 50}
            if statuses:
                params["assessmentStatuses"] = statuses
            out = self._post("/api/assessment/v3/assessments/list",
                             {"visibleColumns": ["assessmentId", "name",
                                                 "status", "templateName",
                                                 "submittedOn"],
                              "filterCriteria": []},
                             doing="list assessments", **params)
            rows = out.get("content") or []
            for row in rows:
                rid = str(row.get("assessmentId") or "")
                if rid:
                    seen.setdefault(rid, row)
            if not rows or page + 1 >= int(out.get("totalPages") or 1):
                break
        return list(seen.values())

    def export(self, assessment_id: str) -> dict:
        """GET .../{id}/export — the readable representation. Plain /{id}
        403s even for records the key can otherwise touch."""
        return self._get(f"/api/assessment/v2/assessments/"
                         f"{assessment_id}/export")

    def questions(self, assessment_id: str) -> list[dict]:
        """Flatten the export into writable question descriptors: id, section,
        type, required flags, options, and any existing responseId."""
        exp = self.export(assessment_id)
        out: list[dict] = []
        for section in exp.get("sections", []):
            for q in section.get("questions", []):
                responses = q.get("responses") or []
                out.append({
                    "questionId": q.get("questionId"),
                    "sectionId": section.get("sectionId"),
                    "text": q.get("text", ""),
                    "type": q.get("questionType", "TEXT"),
                    "required": bool(q.get("required")),
                    "requires_justification":
                        bool(q.get("requiresJustification")),
                    "options": q.get("options") or [],
                    "translationId": q.get("translationId", ""),
                    "responseId": (responses[0].get("responseId")
                                   if responses else None),
                    "responses": responses,
                })
        return out

    # -- answer shapes (the verified wire formats) -------------------------
    @staticmethod
    def _stamp(q: dict, entry: dict) -> dict:
        entry["questionId"] = q["questionId"]
        entry["sectionId"] = q["sectionId"]
        return entry

    @classmethod
    def answer_option(cls, q: dict, option_text: str) -> list[dict]:
        """Select / Yes-No: the option's id AND its responseKey — a bare text
        answer is rejected by the tenant."""
        for opt in q["options"]:
            if str(opt.get("optionText", "")).strip().lower() \
                    == option_text.strip().lower():
                return [cls._stamp(q, {
                    "responseId": opt["id"],
                    "response": opt["optionText"],
                    "responseKey":
                        f"{opt.get('translationId', '')}.option.option",
                    "type": "DEFAULT"})]
        raise OneTrustError(
            f"question {q['questionId']!r} has no option matching "
            f"{option_text!r} (options: "
            f"{[o.get('optionText') for o in q['options']]})")

    @classmethod
    def answer_text(cls, q: dict, value: str) -> list[dict]:
        return [cls._stamp(q, {"responseId": q.get("responseId"),
                               "response": value, "type": "DEFAULT"})]

    @classmethod
    def answer_record(cls, q: dict, record: dict) -> list[dict]:
        """Inventory dropdown: the picked record's id+name IS the question
        answer (this also writes the link, not just the primary record)."""
        return [cls._stamp(q, {"responseId": record["id"],
                               "response": record["name"],
                               "type": "DEFAULT"})]

    @classmethod
    def justification(cls, q: dict, html: str) -> list[dict]:
        """An ADDITIONAL entry in the same responses array. A top-level
        "justification" field is silently dropped by the tenant — required
        questions then fail submit with no server-side hint."""
        return [cls._stamp(q, {"responseId": None,
                               "response": f"<p>{html}</p>",
                               "type": "JUSTIFICATION"})]

    @classmethod
    def personal_data_rows(cls, q: dict, triples: list[dict]) -> list[dict]:
        """PERSONAL_DATA: one row per (category, subject, element) triple.
        A write REPLACES the whole row set — send everything at once, and do
        NOT send the mirror element/subject/category nodes the export shows."""
        rows = []
        for t in triples:
            rows.append(cls._stamp(q, {
                "response": None, "responseId": None, "type": "DEFAULT",
                "responseMap": {
                    "DATA_CATEGORIES": t["category"],
                    "DATA_SUBJECTS": t["subject"],
                    "DATA_ELEMENTS": t["element"]}}))
        return rows

    def write_responses(self, assessment_id: str,
                        entries: list[dict]) -> None:
        """POST all response entries (answers + justifications + personal-data
        rows) grouped per question in one call."""
        self._post(f"/api/assessment/v2/assessments/"
                   f"{assessment_id}/responses",
                   {"responses": entries}, doing="write responses")

    # -- completeness + submit (the human gate stays sacred) ---------------
    def outstanding(self, assessment_id: str) -> list[dict]:
        """The COMPLETE list of what submit will refuse on, computed locally:
        the server's requiredUnansweredQuestionIds under-reports (it misses
        missing justifications and blank inventory dropdowns)."""
        out = []
        for q in self.questions(assessment_id):
            if not q["required"]:
                continue
            answers = [r for r in q["responses"]
                       if r.get("type") != "JUSTIFICATION"
                       and (r.get("response") or r.get("responseMap"))]
            justs = [r for r in q["responses"]
                     if r.get("type") == "JUSTIFICATION"
                     and r.get("response")]
            if not answers:
                out.append({"questionId": q["questionId"], "text": q["text"],
                            "reason": "unanswered"})
            elif q["requires_justification"] and not justs:
                out.append({"questionId": q["questionId"], "text": q["text"],
                            "reason": "missing justification"})
        return out

    def submit(self, assessment_id: str) -> dict:
        """Submit → Under Review, then SELF-CHECK the actual stage by
        re-reading the export (never trust that the POST returning means the
        stage advanced). Never marks anything Complete — Under Review is the
        human gate."""
        try:
            self._post(f"/api/assessment/v2/assessments/"
                       f"{assessment_id}/submit",
                       doing="submit assessment", disclaimerAccepted="true")
        except OneTrustError as exc:
            if exc.status == 403:
                return {"advanced": False,
                        "outstanding": self.outstanding(assessment_id),
                        "error": str(exc)}
            raise
        status = str(self.export(assessment_id).get("status", ""))
        return {"advanced": status.lower().replace(" ", "_")
                == "under_review",
                "status": status, "outstanding": []}

    def link_assessments(self, from_id: str, to_ids: list[str]) -> None:
        """POST .../assessment-links — ties the AI Model Assessment to its
        primary so reviewers see both in one place."""
        self._post("/api/assessment/v2/assessments/assessment-links",
                   {"fromId": from_id, "toIds": to_ids},
                   doing="link assessments")

    # -- inventory ---------------------------------------------------------
    def records(self, schema: str, *, max_pages: int = 10) -> list[dict]:
        """Page the inventory (the server-side name filter is ignored for
        static keys — match client-side, bounded)."""
        rows: list[dict] = []
        for page in range(max_pages):
            out = self._get(f"/api/inventory/v2/inventories/{schema}",
                            page=page, size=50)
            batch = out.get("content") or out.get("data") or []
            rows.extend(batch)
            if not batch or page + 1 >= int(out.get("totalPages") or 1):
                break
        return rows

    @staticmethod
    def match_record(name: str, rows: list[dict]) -> dict | None:
        """The typo-tolerant ladder: exact → unique prefix → unique substring
        → unique fuzzy. Ambiguity returns None (re-list, never guess)."""
        want = name.strip().lower()
        if not want:
            return None
        names = [(str(r.get("name", "")), r) for r in rows]
        exact = [r for n, r in names if n.strip().lower() == want]
        if len(exact) == 1:
            return exact[0]
        prefix = [r for n, r in names if n.strip().lower().startswith(want)]
        if len(prefix) == 1:
            return prefix[0]
        sub = [r for n, r in names
               if want in n.lower() or n.strip().lower() in want]
        if len(sub) == 1:
            return sub[0]
        scored = sorted(
            ((difflib.SequenceMatcher(None, want, n.lower()).ratio(), r)
             for n, r in names), key=lambda p: -p[0])
        if scored and scored[0][0] >= 0.75 and \
                (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
            return scored[0][1]
        return None

    def ensure_vendor(self, name: str) -> dict:
        """Dedup-first: link the existing vendor record when one matches;
        create only on a real no-match. Entities are NEVER created."""
        found = self.match_record(name, self.records("vendors"))
        if found:
            return {**found, "created": False}
        out = self._post("/api/inventory/v2/inventories/vendors",
                         {"name": name,
                          "type": os.environ.get("ONETRUST_VENDOR_TYPE",
                                                 "Vendor")},
                         doing="create vendor record")
        return {**out, "created": True}

    def find_entity(self, name: str) -> dict | None:
        """Entities are pick-only. No match → None; the caller re-lists.
        There is deliberately no create path."""
        return self.match_record(name, self.records("entities"))

    # -- personal-data resolver --------------------------------------------
    def data_catalogs(self) -> dict:
        if self._catalogs is None:
            self._catalogs = {
                "elements": self._get("/api/inventory/v2/data-elements"
                                      ).get("content", []),
                "subjects": self._get("/api/inventory/v2/data-subjects"
                                      ).get("content", []),
                "categories": self._get("/api/inventory/v2/data-categories"
                                        ).get("content", []),
            }
        return self._catalogs

    def resolve_personal_data(self, text: str, *,
                              limit: int = 8) -> list[dict]:
        """Resolve a free-text "what data is involved" answer into catalog
        triples (category + subject + element) using the LIVE catalogs, so a
        write never invents ids."""
        cats = self.data_catalogs()
        low = f" {text.lower()} "
        subject = None
        for s in cats["subjects"]:
            if str(s.get("name", "")).lower() in low:
                subject = s
                break
        if subject is None:
            employees = [s for s in cats["subjects"]
                         if "employee" in str(s.get("name", "")).lower()]
            customers = [s for s in cats["subjects"]
                         if "customer" in str(s.get("name", "")).lower()]
            subject = (employees[0] if "employee" in low or "hr" in low
                       else customers[0] if customers
                       else (cats["subjects"][0] if cats["subjects"]
                             else None))
        triples: list[dict] = []
        for el in cats["elements"]:
            name = str(el.get("name", "")).lower()
            if not name or name not in low:
                continue
            category = (el.get("categories") or [{}])[0]
            if subject is None or not category.get("id"):
                continue
            triples.append({
                "element": {"id": el["id"], "name": el["name"],
                            "nameKey": el.get("nameKey", "")},
                "subject": {"id": subject["id"], "name": subject["name"],
                            "nameKey": subject.get("nameKey", "")},
                "category": {"id": category["id"],
                             "name": category.get("name", ""),
                             "nameKey": category.get("nameKey", "")},
            })
            if len(triples) >= limit:
                break
        return triples

    # -- attachments (two-step, on the VENDOR record) ----------------------
    def attach_to_record(self, record_id: str, filename: str,
                         content: bytes, *, mime: str = "text/plain") -> str:
        """(1) upload the document, (2) link it to the inventory record.
        Assessment-question attach is session-gated — deliverables live on
        the vendor's Documents tab instead."""
        r = self._http.post(
            self._url("/api/document/v2/attachments"),
            headers=self._headers,
            files={"file": (filename, content, mime)},
            data={"attachment": json.dumps({
                "FileName": filename, "Type": "10",
                "RefIds": [record_id], "IsInternal": True,
                "Encrypt": False})})
        if r.status_code >= 400:
            self._fail(r, "upload attachment")
        attachment_id = str(r.json().get("Id", ""))
        self._post(f"/api/inventory/v2/inventories/{record_id}/attachments",
                   [{"attachmentId": attachment_id, "name": filename}],
                   doing="link attachment to record")
        return attachment_id

    # -- privacy notices (Enterprise Policy module) ------------------------
    def notices(self, *, compliance_type: str = "privacynotices",
                max_pages: int = 10) -> list[dict]:
        """The REAL published notices live in the Enterprise Policy module
        (NOT /api/privacynotice/v2, which is the consent module).
        lastPublishedDate is required — 1970-01-01 means "all"."""
        rows: list[dict] = []
        for page in range(max_pages):
            out = self._get(
                f"/api/enterprise-policy/v1/{compliance_type}/list",
                lastPublishedDate="1970-01-01", page=page, size=50)
            rows.extend(out.get("data") or [])
            total = ((out.get("meta") or {}).get("page")
                     or {}).get("totalPages", 1)
            if page + 1 >= int(total or 1):
                break
        return rows

    def notice_text(self, guid: str) -> str:
        """Published content via /details (the bare /{guid} and /versions are
        the 403-gated edit resources). HTML stripped to text."""
        detail = self._get(
            f"/api/enterprise-policy/v1/privacynotices/{guid}/details")
        versions = detail.get("versions") or []
        published = [v for v in versions
                     if str(v.get("versionStatus", "")).lower()
                     == "published"] or versions
        parts = []
        for section in (published[0].get("sections") if published else []) or []:
            parts.append(str(section.get("content", "")))
        import re
        return re.sub(r"<[^>]+>", " ", " ".join(parts)).strip()
