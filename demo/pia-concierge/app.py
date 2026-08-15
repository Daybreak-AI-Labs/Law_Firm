"""PIA Concierge — external-world harness for the Lightwork PIA demo.

THE PLATFORM IS THE DEMO. This process serves only what exists OUTSIDE
Lightwork in a real deployment:

  * a simulated ServiceNow tenant (any ticketing system) that fires a webhook,
  * the requester's mailbox (real SMTP delivery, captured locally),
  * the requester-facing intake questionnaire the emailed link opens,
  * a simulated OneTrust tenant that the REAL onetrust integration files into.

Everything platform-side is written into the real Lightwork world so the REAL
dashboard (`maverick dashboard`, port 8765) is where the demo happens:

  * the intake becomes a real goal — watch it on /goals and its live timeline,
  * scoring uses the real maverick.assessment PIA engine and is saved where
    `maverick assess list` and the /assessments register read,
  * control mapping uses the real maverick.controls catalog (GDPR/ISO/SOC 2/
    NIST citations),
  * the human decision is a real world-model approval — approved on the REAL
    /approvals page, quorum and identity recorded,
  * every step lands in the real Ed25519-signed audit chain — /audit page,
    `maverick audit verify`.

Both processes share MAVERICK_HOME (world.db + audit chain + keys).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import time
from pathlib import Path
from typing import Any

# The agent talks to the platform (or its standalone stand-ins) through the
# backend seam only -- so the same app is the Lightwork-bundled build AND the
# sold-on-its-own standalone agent, depending on capabilities.
import backend
import contract_guard
import httpx
import notice_check
import paper_desk
import value_ledger
from backend import (
    AssessmentSession,
    find_controls,
    get_template,
    save_session,
)
from capabilities import CAPS, STANDALONE, caps_summary
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from mailsink import INBOX, start_mailsink
from store import STORE, Case, OneTrustAssessment, Ticket

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["timestamp_hm"] = (
    lambda ts: _dt.datetime.fromtimestamp(float(ts or 0)).strftime("%H:%M"))

BASE_URL = os.environ.get("PIA_BASE_URL", "http://127.0.0.1:8890")
DASHBOARD_URL = os.environ.get("LIGHTWORK_DASHBOARD_URL", "http://127.0.0.1:8765")
AGENT = "pia-concierge"
ANALYST = "privacy-analyst-agent"

app = FastAPI(title="PIA Concierge — external world (ServiceNow / mail / OneTrust)")

# The concierge's own assets (the natural-voice module). Standalone-safe:
# starlette ships StaticFiles; the directory travels in the deployment kit.
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory=str(HERE / "static")),
          name="static")


def _world():
    """The governed world model, or None in the standalone agent (no goals /
    approval queue without the platform)."""
    return backend.world()


def _audit(kind: str, agent: str, goal_id: int | None, **payload: Any) -> None:
    """Write to the signed audit chain when the platform is present; a no-op
    in the standalone agent (audit is a Lightwork feature)."""
    backend.audit_record(kind, agent=agent, goal_id=goal_id, **payload)


def _post_event(goal_id: int, agent: str, kind: str, content: str) -> None:
    """Mirror a step onto the goal's timeline (platform only)."""
    w = _world()
    if w is None:
        return
    try:
        w.append_event(goal_id, agent, kind, content)
    except Exception as exc:  # pragma: no cover
        print(f"[goal-event] failed: {exc}")


async def _send_email(to: str, subject: str, body: str) -> str:
    """Send off the event loop (the in-process capture sink shares this loop;
    a blocking send from a handler would deadlock)."""
    return await asyncio.to_thread(backend.send_email, to, subject, body)


# Plain-language phrasing for the intake interview; answers map onto the real
# PIA template's yes/no/na/unknown vocabulary.
_INTAKE_PHRASING = {
    "pia_necessity": "Is every field of personal data you're collecting actually needed for the purpose? (No = collecting more than needed)",
    "pia_lawful_basis": "Have you documented a lawful basis for this processing under GDPR Art. 6?",
    "pia_special_category": "Will this process special-category data (health, biometrics, race, etc.)?",
    "pia_transparency": "Are data subjects told about this processing via a privacy notice (Art. 13/14)?",
    "pia_rights": "Can data subjects exercise access / erasure / portability rights against this system?",
    "pia_retention": "Is there a defined retention period after which the data is deleted?",
    "pia_security": "Is the personal data encrypted in transit and at rest?",
    "pia_transfers": "Will personal data leave the EU/EEA without a Chapter V transfer safeguard?",
    "pia_processors": "Is every third-party processor covered by a signed Art. 28 data-processing agreement?",
    "pia_automated": "Does the system make automated decisions or profile people with legal/significant effects?",
}

# Realistic presenter auto-fill: yields a HIGH-risk result (over-collection,
# unsafeguarded transfer, no retention, missing DPA, profiling).
_DEMO_ANSWERS = {
    "pia_necessity": ("no", "Marketing wants full contact + browsing history; only email+name needed."),
    "pia_lawful_basis": ("yes", "Legitimate-interest assessment on file."),
    "pia_special_category": ("no", "No special-category data."),
    "pia_transparency": ("yes", "Covered by the customer privacy notice."),
    "pia_rights": ("yes", "DSAR workflow already wired to this store."),
    "pia_retention": ("no", "No retention period defined by the vendor yet."),
    "pia_security": ("yes", "TLS in transit; vendor claims AES-256 at rest."),
    "pia_transfers": ("yes", "Vendor hosts in us-east-1; no SCCs / transfer safeguard in place."),
    "pia_processors": ("no", "Acme CRM is a processor; Art. 28 DPA not yet countersigned."),
    "pia_automated": ("yes", "Large-scale lead scoring / profiling of customers."),
}


# ===========================================================================
# Vendor memory + evidence prefills — the agent answers what it can PROVE,
# with quotable provenance: an attached document, the ticket itself, or the
# vendor's last review. Every auto-answer keeps source + excerpt as the answer
# note the reviewer reads. Nothing is silently invented.
# ===========================================================================
_ANSWER_LABELS = {"yes": "Yes", "no": "No", "na": "N/A", "unknown": "Not sure"}

_DOC_ANSWER_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pia_processors", "yes",
     ("data processing agreement", "art. 28", "article 28")),
    ("pia_lawful_basis", "yes",
     ("lawful basis", "legitimate interest assessment",
      "legitimate-interest assessment")),
    ("pia_security", "yes",
     ("encryption at rest", "aes-256", "encrypted in transit", "tls 1.")),
    ("pia_retention", "yes",
     ("retention period", "retention:", "deletion schedule",
      "deleted 12 months")),
    ("pia_transfers", "no",
     ("standard contractual clauses", "scc module", "scc annex")),
)

_SPECIAL_CATEGORY_TERMS = ("health", "medical", "biometric", "genetic",
                           "race", "ethnic", "religio", "union membership",
                           "sexual", "political")


def _negated_before(low: str, pos: int) -> bool:
    """"no audit rights", "without SCCs" must not read as evidence FOR."""
    return bool(re.search(r"\b(?:no|not|without|never)\b\W*$",
                          low[max(0, pos - 24):pos]))


def _term_excerpt(text: str, pos: int, term: str) -> str:
    lo = max(0, pos - 40)
    hi = min(len(text), pos + len(term) + 60)
    return " ".join(text[lo:hi].split())


def _record_auto_answer(case: Case, qid: str, answer: str, source: str,
                        excerpt: str) -> dict | None:
    """Answer on the requester's behalf — only if nobody (human, delegate, or
    an earlier source) owns the question yet."""
    if qid in case.answers:
        return None
    if any(qid in d["qids"] for d in case.delegations.values()):
        return None
    note = f"Auto-answered from {source}"
    if excerpt:
        note += f': "{excerpt}"'
    case.answers[qid] = {"answer": answer, "note": note, "auto": True,
                         "source": source}
    return {"id": qid, "answer": answer, "label": _ANSWER_LABELS[answer],
            "source": source, "excerpt": excerpt}


def _prefill_from_document(case: Case, filename: str, text: str) -> list[dict]:
    """Evidence rules: what an attached document proves, quoted."""
    if not (text or "").strip():
        return []
    low = text.lower()
    hits: list[dict] = []
    for qid, answer, terms in _DOC_ANSWER_RULES:
        for term in terms:
            pos = low.find(term)
            if pos < 0 or _negated_before(low, pos):
                continue
            hit = _record_auto_answer(case, qid, answer, filename,
                                      _term_excerpt(text, pos, term))
            if hit:
                hits.append(hit)
            break
    if hits:
        gid = getattr(case, "goal_id", None)
        _audit("INTAKE_PREFILL", ANALYST, gid, case=case.id, source=filename,
               questions=[h["id"] for h in hits])
        if gid:
            _post_event(gid, ANALYST, "observation",
                        f"{filename} answered {len(hits)} intake question(s) "
                        f"with quoted evidence; the requester won't be asked.")
    return hits


def _prefill_from_ticket(case: Case) -> list[dict]:
    """The ticket already declares the data types — settle the special-
    category question from the requester's own declaration."""
    dt = (case.data_types or "").strip()
    if not dt:
        return []
    low = dt.lower()
    source = f"ServiceNow ticket {case.ticket_number}"
    if any(t in low for t in _SPECIAL_CATEGORY_TERMS):
        hit = _record_auto_answer(case, "pia_special_category", "yes", source,
                                  f"data types declared: {dt}")
    else:
        hit = _record_auto_answer(case, "pia_special_category", "no", source,
                                  f"no special-category types among: {dt}")
    return [hit] if hit else []


def _doc_text_for_prefill(data: bytes, mime: str) -> str:
    return backend.extract_text(data, mime)


def _prefills_from_bytes(case: Case, filename: str, data: bytes,
                         mime: str) -> list[dict]:
    return _prefill_from_document(case, filename,
                                  _doc_text_for_prefill(data, mime))


def _latest_counterparty_paper(vendor_id: str) -> dict | None:
    """The newest document on the vendor's record that came from the OTHER
    side of the table (vendor or reviewer upload, never our own filings),
    still retained, in a reviewable format."""
    ours = ("Lightwork Concierge",)
    docs = [a for a in _ot_mock.ATTACHMENTS.values()
            if a.get("linked_to") == vendor_id and a.get("content")
            and a.get("by") not in ours
            and str(a.get("filename", "")).lower().endswith(
                (".docx", ".pdf", ".txt"))]
    return max(docs, key=lambda a: a.get("at", 0)) if docs else None


def _latest_prior_record(subject: str) -> OneTrustAssessment | None:
    matches = [a for a in STORE.onetrust.values()
               if a.subject.strip().lower() == subject.strip().lower()
               and a.status in ("Completed", "Under Review")]
    return max(matches, key=lambda a: a.filed_at) if matches else None


def _fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _case_value(case: Case) -> dict:
    """The speed story, counted rather than claimed: how much of this
    assessment the agent answered from evidence, and how fast it moved."""
    tpl_total = len(get_template("pia").questions)
    auto = sum(1 for rec in case.answers.values() if rec.get("auto"))
    delegated = sum(1 for rec in case.answers.values()
                    if rec.get("by") and not rec.get("auto"))
    out: dict[str, Any] = {
        "questions_total": tpl_total,
        "auto_answered": auto,
        "delegated": delegated,
        "asked": max(0, len(case.answers) - auto - delegated),
        "documents": len(case.uploads),
    }
    if case.submitted_at:
        out["intake_seconds"] = int(case.submitted_at - case.created_at)
        out["intake_elapsed"] = _fmt_elapsed(out["intake_seconds"])
    ot = _ot_record_for(case)
    if ot:
        out["filed_seconds"] = int(ot.filed_at - case.created_at)
        out["filed_elapsed"] = _fmt_elapsed(out["filed_seconds"])
        if ot.decided_at:
            out["decided_seconds"] = int(ot.decided_at - case.created_at)
            out["decided_elapsed"] = _fmt_elapsed(out["decided_seconds"])
    return out


def _program_value() -> dict:
    """Aggregate value strip for the landing page. The manual baseline is an
    explicit, configurable assumption (PIA_MANUAL_BASELINE_HOURS, default 6),
    never presented as a measurement."""
    records = list(STORE.onetrust.values())
    minutes: list[float] = []
    auto_total = 0
    answered_total = 0
    for a in records:
        c = STORE.cases.get(a.case_id)
        if not c:
            continue
        end = a.decided_at or a.filed_at
        minutes.append(max(0.0, (end - c.created_at) / 60))
        auto_total += sum(1 for r in c.answers.values() if r.get("auto"))
        answered_total += len(c.answers)
    baseline_hours = float(os.environ.get("PIA_MANUAL_BASELINE_HOURS", "6"))
    return {
        "filed": len(records),
        "completed": sum(1 for a in records if a.status == "Completed"),
        "median_minutes": (round(sorted(minutes)[len(minutes) // 2], 1)
                           if minutes else 0.0),
        "auto_answered": auto_total,
        "answered": answered_total,
        "baseline_hours": baseline_hours,
        "saved_hours": round(max(
            0.0, len(records) * baseline_hours - sum(minutes) / 60), 1),
    }


def _prior_summary(ot: OneTrustAssessment) -> dict:
    """What we already know about this vendor — shown to the requester."""
    prior_case = STORE.cases.get(ot.case_id)
    doc_names = []
    if prior_case:
        doc_names += [u.split(" (")[0] for u in prior_case.uploads]
    doc_names += [a["filename"] for a in ot.addenda]
    when = ot.decided_at or ot.filed_at
    return {
        "ot_id": ot.assessment_id, "status": ot.status,
        "risk": (ot.result or {}).get("risk_rating", ot.risk_level),
        "decided_by": ot.decided_by,
        "when": _dt.datetime.fromtimestamp(when).strftime("%b %d, %Y"),
        "findings": len((ot.result or {}).get("findings", [])),
        "docs": doc_names[:8],
    }


# The privacy reviewers work is assigned to. The demo has no identity
# provider, so a reviewer picks their name at the desk; on the platform the
# dashboard's verified principal plays this role.
_REVIEWERS: tuple[str, ...] = (
    "A. Novak (Privacy)", "L. Chen (Privacy)",
    "R. Okafor (Legal)", "Privacy Reviewer",
)


# ---- Lived-in tenant history (opt-out fixtures, PIA_SEED_TENANT) -----------
_TENANT_VENDORS: tuple[tuple[str, str], ...] = (
    ("Acme CRM", "high"), ("Globex HRIS", "medium"), ("Initech Payroll", "low"),
    ("Umbrella Health Portal", "high"), ("Stark Analytics", "medium"),
    ("Wayne Payments", "high"), ("Pied Piper Storage", "low"),
    ("Hooli Ads Platform", "medium"), ("Soylent Survey Tool", "low"),
    ("Tyrell Recruiting", "high"), ("Aperture Support Desk", "medium"),
    ("Cyberdyne Vision API", "high"), ("Oceanic Booking", "low"),
    ("Duff Loyalty", "medium"), ("Vandelay Imports EDI", "low"),
    ("Wonka Logistics", "medium"), ("Gringotts Billing", "high"),
    ("Prestige Telecom CDR", "medium"), ("Oscorp Labs LIMS", "high"),
    ("Bluth Docs eSign", "low"), ("Dunder Mifflin Print", "low"),
    ("Sirius Chat Widget", "medium"), ("MomCorp IoT Fleet", "high"),
    ("Planet Express Tracking", "low"), ("Massive Dynamic A/B", "medium"),
    ("Virtucon Badging", "medium"), ("Rekall VR Training", "high"),
)

_FINDINGS_BY_RISK = {
    "high": ["Unsafeguarded transfer to us-east-1 (Chapter V).",
             "Art. 28 DPA not countersigned.",
             "No defined retention period."],
    "medium": ["Privacy notice does not cover this processing.",
               "Sub-processor list is stale."],
    "low": ["Access review cadence undocumented."],
}

_SEED_REQUESTERS = ("Jordan Diaz", "Sam Patel", "Alex Kim",
                    "Riley Moore", "Casey Lee")

# A short vendor DPA with real departures from our standard positions, so the
# seeded paper round below is produced by the REAL deterministic engine
# rather than hand-written fixture findings.
_SEED_PAPER_PARAGRAPHS = (
    "DATA PROCESSING AGREEMENT",
    "This Data Processing Agreement is entered into between {vendor} (the "
    "Processor) and the Customer (the Controller) pursuant to Article 28 "
    "GDPR.",
    "1. The Processor shall process personal data only on documented "
    "instructions from the Controller.",
    "2. Processor personnel are bound by appropriate obligations of "
    "confidentiality.",
    "3. The Processor may engage sub-processors at its sole discretion "
    "without prior notice to the Controller.",
    "4. The Processor shall notify the Controller of a personal data breach "
    "without undue delay.",
    "5. Upon termination of services, the Processor may retain personal data "
    "for as long as it deems appropriate.",
)


def _risk_report_text(*, subject: str, ticket: str, requester: str,
                      result: dict, controls: list[dict],
                      instrument_line: str, rationale: str,
                      at: float, prior_lines: list[str] | None = None) -> str:
    """The full risk-analysis report filed to the vendor record — a document
    a reviewer can actually defend from: what was found, what it means, what
    must change, and exactly what the assessment was based on. One composer
    for the live run and the seeded history, so both read identically."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))
    findings = result.get("findings") or []
    answers = result.get("answers") or []
    high = sum(1 for f in findings if f.get("severity") == "high")
    rating = str(result.get("risk_rating", "unknown")).upper()
    inherent = str(result.get("inherent_risk",
                              result.get("risk_rating", "?"))).upper()
    residual = str(result.get("residual_risk", "?")).upper()
    cip = result.get("controls_in_place", 0)
    ris = result.get("risks_in_scope", 0)

    lines = [
        "SUMMARY OF RECORD",
        "-" * 68,
        f"System / vendor : {subject}",
        f"Ticket          : {ticket} — requested by {requester}",
        "Prepared by     : Lightwork PIA Concierge (agent input + analysis; "
        "the approval decision is human)",
        f"Prepared at     : {stamp}",
        f"Interview       : {result.get('answered', 0)} of "
        f"{result.get('total', 0)} questions answered",
        f"Rating filed    : {rating} — inherent {inherent} → residual "
        f"{residual} ({cip} of {ris} risk areas controlled)",
        f"Instrument      : {instrument_line}",
        "",
        *(["CHANGES SINCE THE LAST REVIEW", "-" * 68,
           *prior_lines, ""] if prior_lines else []),
        "EXECUTIVE SUMMARY",
        "-" * 68,
        (f"{subject} was assessed at {rating} risk. "
         + (f"{len(findings)} finding(s) — {high} high severity — require "
            "remediation before contract renewal. "
            if findings else
            "No question in scope produced an adverse finding. ")
         + f"Rationale: {rationale} "
         + f"Controls are in place for {cip} of {ris} risk areas, leaving "
           f"residual risk {residual}. The assessment was filed to OneTrust "
           "as Under Review; nothing is Completed until the privacy team "
           "approves it."),
        "",
        f"FINDINGS ({len(findings)})",
        "-" * 68,
    ]
    if not findings:
        lines.append("None. Standard processing against the questionnaire "
                     "in force.")
    for i, f in enumerate(findings, start=1):
        lines.append(f"{i}. [{str(f.get('severity', '?')).upper()}] "
                     f"{f.get('section', 'Assessment')}: "
                     f"{f.get('text') or f.get('question', '')}")
        if f.get("guidance"):
            lines.append(f"   Remediation: {f['guidance']}")
    lines.append("")
    if controls:
        lines += [f"REQUIRED CONTROLS ({len(controls)})", "-" * 68]
        for c in controls:
            lines.append(f"- {c.get('id', '?')} — {c.get('title', '')}")
            if c.get("frameworks"):
                lines.append(f"  Frameworks: {c['frameworks']}")
        lines.append("")
    if answers:
        lines += [f"INTERVIEW BASIS ({len(answers)} answers, verbatim)",
                  "-" * 68]
        for i, a in enumerate(answers, start=1):
            lines.append(f"{i}. {a.get('question', '')}")
            by = f" — {a['by']}" if a.get("by") else ""
            lines.append(f"   Answer: {a.get('answer', '?')}{by}")
            if a.get("note"):
                lines.append(f"   Note: {a['note']}")
        lines.append("")
    lines += [
        "GOVERNANCE & METHOD",
        "-" * 68,
        "Scoring is deterministic from the questionnaire template in force — "
        "a model can neither create nor clear a finding. The agent files the "
        "assessment as Under Review and cannot approve it; the reviewer's "
        "decision is recorded on Lightwork's Ed25519-signed audit chain.",
    ]
    return "\n".join(lines)


def _deliverable(vendor: str, kind: str, title: str,
                 body: str) -> tuple[str, bytes, str]:
    """A filed deliverable: Word on the platform, honest plain text in
    standalone; named legibly and versioned per vendor+kind so the Documents
    tab reads as a history. Returns (filename, content, mime)."""
    version = STORE.next_doc_version(vendor, kind)
    stem = f"{paper_desk._safe(vendor)}-{kind}-v{version}"
    subtitle = (f"{vendor} \u00b7 {kind.replace('-', ' ')} "
                f"\u00b7 v{version} \u00b7 {time.strftime('%Y-%m-%d')}")
    docx = backend.report_docx(body, title=title, subtitle=subtitle)
    if docx is not None:
        return f"{stem}.docx", docx, paper_desk.DOCX_MIME
    return f"{stem}.txt", f"{title}\n{body}".encode(), "text/plain"


def _seed_notice_result(vendor: str, risk: str) -> dict:
    """Synthetic cross-check inputs for the seeded history, rendered through
    the REAL notice_check composer so seeded and live documents read alike."""
    if risk == "high":
        return {"contradictions": [], "gaps": [
            {"finding": f"The published notice does not describe the "
                        f"{vendor} processing (behavioural data).",
             "category": "Behavioural Data"}]}
    return {"contradictions": [], "gaps": []}


_SEED_NOTICE_PICKED = {"name": "Global Vacation Clubs Privacy Notice",
                       "organizationName": "Global Vacation Clubs"}


def _seed_paper_docx(vendor: str) -> bytes:
    """The vendor's fixture DPA as a minimal valid .docx, so the seeded
    redline is their document edited in place with tracked changes."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    from maverick import docx_redline as dr
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">'
        f'{escape(p.format(vendor=vendor))}</w:t></w:r></w:p>'
        for p in _SEED_PAPER_PARAGRAPHS)
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


def _seed_paper_round(case: Case, reviewer: str) -> None:
    """A filed vendor-paper round as tenant history: run the real engine
    (deterministic, no model) over the fixture DPA and file it through the
    same code path the desk uses. Attachments land on the mock tenant
    directly — the app isn't serving HTTP yet at seed time."""
    text = "\n".join(p.format(vendor=case.subject)
                     for p in _SEED_PAPER_PARAGRAPHS)
    review = backend.review_vendor_paper(
        text, vendor=case.subject,
        document_name=f"{case.subject} — DPA (vendor draft).docx",
        use_model=False)
    if review is None:
        return
    redline = backend.build_redline(
        review, original=_seed_paper_docx(case.subject),
        mime=paper_desk.DOCX_MIME,
        date=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        title=f"{case.subject} — DPA (Lightwork redline)")

    class _SeedFiler:
        """ensure_vendor hands back the NAME as the record id;
        seed_attachment resolves it against the mock's vendor rows."""

        def ensure_vendor(self, name: str) -> dict:
            return {"id": name, "name": name}

        def attach_to_record(self, record_id: str, filename: str,
                             content: bytes, *,
                             mime: str = "text/plain") -> str:
            return _ot_mock.seed_attachment(record_id, filename, content,
                                            mime, by="Lightwork Concierge")

    entry = paper_desk.file_paper_review(case, review, redline,
                                         reviewer=reviewer,
                                         client=_SeedFiler())
    entry["at"] = case.created_at + 3600     # history, not "just now"
    for att_id in entry["attachment_ids"]:
        att = _ot_mock.ATTACHMENTS.get(att_id)
        if att:
            att["at"] = entry["at"]


def _seed_tenant() -> None:
    """A year of backdated OneTrust history (PIA_SEED_TENANT=1, the launcher
    default): completed reviews, a live Under Review queue, one sent-back, a
    couple of addenda — so the tenant, the ticket list, and the speed strip
    read as sustained use. Pure demo-store fixtures: nothing is written to
    the world model or the signed audit chain. PIA_SEED_TENANT=0 for a
    clean-slate run."""
    if os.environ.get("PIA_SEED_TENANT", "0") != "1" or STORE.onetrust:
        return
    import random
    rng = random.Random(42)
    now = time.time()
    tpl = get_template("pia")
    reviewers = _REVIEWERS
    n = len(_TENANT_VENDORS)
    for i, (vendor, risk) in enumerate(_TENANT_VENDORS):
        # Oldest first; the newest three are still in the review queue and
        # one mid-history record was sent back.
        frac = (n - 1 - i) / (n - 1)
        age_days = 1.0 + frac * 420 * rng.uniform(0.85, 1.15)
        status = ("Under Review" if i >= n - 3
                  else "Sent back" if i == 9 else "Completed")
        created = now - age_days * 86400
        case_id = STORE.next_case_id()
        ticket_no = STORE.next_ticket_number()
        auto_n = rng.randint(2, 7)
        answers: dict[str, dict] = {}
        result_answers: list[dict] = []
        for j, q in enumerate(tpl.questions):
            ans = "no" if (risk == "high" and j in (0, 7, 8)) else "yes"
            rec: dict[str, Any] = {"answer": ans, "note": ""}
            if j < auto_n:
                src = rng.choice((f"{vendor} — DPA (signed).pdf",
                                  f"{vendor} — Security Overview.pdf",
                                  f"ServiceNow ticket {ticket_no}",
                                  "last review"))
                rec.update({"auto": True, "source": src,
                            "note": f"Auto-answered from {src}"})
            answers[q.id] = rec
            result_answers.append({"question": q.text, "answer": ans,
                                   "note": rec["note"], "by": ""})
        findings = [{"severity": risk, "section": "Assessment", "text": t,
                     "guidance": "Remediate before contract renewal."}
                    for t in _FINDINGS_BY_RISK[risk]]
        case = Case(id=case_id, ticket_number=ticket_no, subject=vendor,
                    requester=rng.choice(_SEED_REQUESTERS),
                    requester_email="requester@example.test",
                    data_types="customer contact data")
        case.created_at = created
        case.submitted_at = created + rng.uniform(90, 420)
        case.answers = answers
        filed_at = case.submitted_at + rng.uniform(15, 40)
        decided_at = (filed_at + rng.uniform(300, 7200)
                      if status != "Under Review" else 0.0)
        ot = OneTrustAssessment(
            assessment_id=STORE.next_onetrust_id(),
            name=f"PIA — {vendor}", template="Privacy Impact Assessment",
            subject=vendor, status=status, risk_level=risk,
            result={"risk_rating": risk, "inherent_risk": risk,
                    "residual_risk": "medium" if risk == "high" else "low",
                    "risks_in_scope": 10,
                    "controls_in_place": 10 - len(findings),
                    "answered": 10, "total": 10,
                    "findings": findings, "answers": result_answers},
            case_id=case_id, decided_at=decided_at,
            decided_by=rng.choice(reviewers) if decided_at else "")
        ot.filed_at = filed_at
        if status == "Completed" and i in (4, 12):
            re_review = i == 12
            ot.addenda.append({
                "at": decided_at + 6 * 86400,
                "filename": f"{vendor} — DPA amendment.pdf",
                "summary": ("Art. 28 clause review: 8 of 10 clauses present, "
                            "residual risk "
                            + ("MEDIUM. Missing clauses flagged — re-review "
                               "recommended." if re_review else "LOW. No new "
                               "gaps — filed for the record.")),
                "gaps": (["audit rights", "retention schedule"]
                         if re_review else []),
                "re_review": re_review, "dpa_review_id": ""})
            ot.needs_re_review = re_review
        case.stage = ("in_onetrust_review" if status == "Under Review"
                      else "filed" if status == "Completed" else "rejected")
        case.onetrust_id = ot.assessment_id
        case.decided_by = ot.decided_by
        STORE.cases[case_id] = case
        STORE.onetrust[ot.assessment_id] = ot
        ticket = Ticket(
            number=ticket_no,
            short_description=f"Privacy assessment: {vendor}",
            requester=case.requester,
            requester_email=case.requester_email,
            system_name=vendor, data_types=case.data_types,
            state="Resolved" if status == "Completed" else "In Progress",
            case_id=case_id)
        ticket.created_at = created
        STORE.tickets[ticket_no] = ticket
        # The deliverables the live run attaches, retained as OPENABLE
        # documents on the vendor's mock record — same composers, names,
        # and format as the live run, backdated to the filing.
        risk_body = _risk_report_text(
            subject=vendor, ticket=ticket_no, requester=case.requester,
            result=ot.result, controls=[],
            instrument_line="dpa — external processing of personal data on "
                            "the controller's behalf",
            rationale=("; ".join(f["text"] for f in findings)
                       or "No adverse findings."),
            at=filed_at)
        notice_body = notice_check.recommendations_doc(
            vendor, _SEED_NOTICE_PICKED, _seed_notice_result(vendor, risk),
            [])
        for name, content, mime in (
            _deliverable(vendor, "risk-analysis",
                         f"Privacy Risk Analysis — {vendor}", risk_body),
            _deliverable(vendor, "notice-crosscheck",
                         f"Privacy-Notice Cross-Check — {vendor}",
                         notice_body),
        ):
            _ot_mock.seed_attachment(vendor, name, content, mime,
                                     at=filed_at, by="Lightwork Concierge")
    n_docs = len(_ot_mock.ATTACHMENTS)
    if CAPS["paper_redline"]:
        # Two vendors also carry a filed vendor-paper round (analysis memo +
        # tracked-changes redline), so the Documents tab and the reviewer
        # desk both open with history to click through.
        for subject in ("Wayne Payments", "Rekall VR Training"):
            case = next((c for c in STORE.cases.values()
                         if c.subject == subject), None)
            if case is not None:
                _seed_paper_round(case, rng.choice(reviewers))
        n_docs = len(_ot_mock.ATTACHMENTS)
    print(f"[seed] tenant history: {len(STORE.onetrust)} OneTrust records, "
          f"{n_docs} vendor documents (disable with PIA_SEED_TENANT=0)")


@app.on_event("startup")
async def _startup() -> None:
    # This public demo route explicitly opts into ambient document credentials,
    # so make that ambient state unconditionally mock-only before startup does
    # any async work. Never inherit real Graph credentials into a process that
    # exposes unauthenticated intake endpoints. The launcher binds this server
    # to loopback.
    graph_sim_base = (
        f"http://127.0.0.1:{int(os.environ.get('WORLD_PORT', '8890'))}/graph-sim"
    )
    os.environ["MSGRAPH_ACCESS_TOKEN"] = GRAPH_SIM_TOKEN
    os.environ["MSGRAPH_BASE_URL"] = graph_sim_base
    os.environ.pop("MAVERICK_FETCH_ALLOW_PRIVATE", None)
    for inherited_source_setting in (
        "SLACK_SEARCH_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_BASE_URL",
        "GDRIVE_ACCESS_TOKEN",
        "GDRIVE_BASE_URL",
    ):
        os.environ.pop(inherited_source_setting, None)

    host = os.environ.get("EMAIL_SMTP_HOST", "127.0.0.1")
    port = int(os.environ.get("EMAIL_SMTP_PORT", "1025"))
    try:
        app.state.mailserver = await start_mailsink(host, port)
        print(f"[mailsink] capturing SMTP on {host}:{port}")
    except Exception as exc:  # pragma: no cover
        print(f"[mailsink] could not bind {host}:{port}: {exc}")
    _seed_tenant()
    app.state.watcher = asyncio.create_task(_approval_watcher())
    print(f"[graph-sim] simulated SharePoint tenant at {graph_sim_base}")


# ===========================================================================
# Simulated SharePoint/OneDrive tenant (Microsoft Graph shapes). Only the
# TENANT is simulated — search + fetch go through the real
# maverick.doc_discovery module, exactly as they would against graph.microsoft
# .com. Docs are seeded per case so "find my documents" has something to find.
# ===========================================================================
GRAPH_SIM_TOKEN = "demo-graph-token"  # pragma: allowlist secret
GRAPH_DOCS: dict[str, dict[str, Any]] = {}


def _mini_pdf(title: str, lines: list[str]) -> bytes:
    """A tiny, valid one-page PDF (passes the attachment mime + magic checks)."""
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    body = ["BT /F1 14 Tf 54 760 Td (" + esc(title) + ") Tj ET"]
    y = 730
    for ln in lines:
        body.append("BT /F1 10 Tf 54 " + str(y) + " Td (" + esc(ln) + ") Tj ET")
        y -= 16
    stream = "\n".join(body).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode()
            + b"\n%%EOF\n")
    return bytes(out)


def _seed_corp_docs(subject: str) -> None:
    """Drop the documents a real org would already have for this vendor into
    the simulated tenant: the SOW, MSA, DPA, and security overview — plus an
    unrelated doc so ranking visibly works."""
    docs = [
        (f"{subject} — Statement of Work (SOW) FY26.pdf",
         [f"Statement of Work between the Company and {subject}.",
          "Scope: managed service, personal data of customers in scope.",
          "Term: 12 months. Data locations: us-east-1."]),
        (f"{subject} — Data Processing Agreement (DPA, signed).pdf",
         [f"Art. 28 GDPR data processing agreement with {subject}.",
          "Processor obligations, sub-processor list, SCC module 2 annex."]),
        (f"{subject} — Master Services Agreement (MSA).pdf",
         [f"Master services agreement governing all {subject} orders.",
          "Liability caps, security exhibit, audit rights."]),
        (f"{subject} — Security Overview & Architecture.pdf",
         [f"{subject} platform security whitepaper.",
          "Encryption at rest (AES-256), TLS 1.2+, SOC 2 Type II attested."]),
        ("Company holiday calendar FY26.pdf",
         ["All-hands calendar. Nothing to do with any vendor."]),
    ]
    for name, lines in docs:
        doc_id = f"doc-{abs(hash((subject, name))) % 10**10:010d}"
        if doc_id not in GRAPH_DOCS:
            GRAPH_DOCS[doc_id] = {
                "name": name, "subject": subject,
                "bytes": _mini_pdf(name, lines), "mime": "application/pdf",
            }


def _graph_auth_ok(request: Request) -> bool:
    tok = request.headers.get("authorization", "")
    return tok == f"Bearer {os.environ.get('MSGRAPH_ACCESS_TOKEN', GRAPH_SIM_TOKEN)}"


def _discover_mock_documents(subject: str):
    """Run real discovery with a task-local allowance for the mock loopback."""
    from maverick import doc_discovery
    from maverick.tools._ssrf import allow_private_hosts

    with allow_private_hosts({"127.0.0.1"}):
        return doc_discovery.discover(
            subject,
            sources=["msgraph"],
            allow_ambient_credentials=True,
        )


def _fetch_mock_document(source: str, doc_id: str, ref: dict):
    """Fetch only from the fixed mock base under the same task-local policy."""
    from maverick import doc_discovery
    from maverick.tools._ssrf import allow_private_hosts

    with allow_private_hosts({"127.0.0.1"}):
        return doc_discovery.fetch(
            source,
            doc_id,
            ref,
            allow_ambient_credentials=True,
        )


@app.post("/graph-sim/search/query")
async def graph_sim_search(request: Request) -> JSONResponse:
    if not _graph_auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    try:
        q = str(body["requests"][0]["query"]["queryString"])
    except (KeyError, IndexError, TypeError):
        return JSONResponse({"error": "bad request"}, status_code=400)
    tokens = {t.lower() for t in q.split() if len(t) > 2}
    hits = []
    for doc_id, doc in GRAPH_DOCS.items():
        name = doc["name"].lower()
        subj = doc["subject"].lower()
        if any(t in name or t in subj for t in tokens):
            hits.append({
                "summary": f"…{doc['name']}…",
                "resource": {
                    "id": doc_id, "name": doc["name"],
                    "webUrl": f"{BASE_URL}/graph-sim/web/{doc_id}",
                    "size": len(doc["bytes"]),
                    "file": {"mimeType": doc["mime"]},
                    "parentReference": {"driveId": "demo-drive"},
                },
            })
    return JSONResponse({"value": [{"hitsContainers": [{"hits": hits}]}]})


@app.get("/graph-sim/drives/{drive_id}/items/{item_id}/content")
async def graph_sim_content(request: Request, drive_id: str, item_id: str):
    if not _graph_auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    doc = GRAPH_DOCS.get(item_id)
    if not doc:
        return JSONResponse({"error": "not found"}, status_code=404)
    from fastapi.responses import Response
    return Response(content=doc["bytes"], media_type=doc["mime"])


def _page(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    base = {
        "request": request,
        "base_url": BASE_URL,
        "dashboard_url": DASHBOARD_URL,
        "n_mail": len(INBOX),
        "n_filed": len(STORE.onetrust),
        # Capability context: templates hide platform-only links (the
        # dashboard, signed audit) and can show the standalone banner.
        "standalone": STANDALONE,
        "caps": CAPS,
        "caps_summary": caps_summary(),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base)


# ===========================================================================
# Landing
# ===========================================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(request, "index.html", tickets=list(STORE.tickets.values()),
                 cases=list(STORE.cases.values()), value=_program_value())


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request) -> HTMLResponse:
    """What the standalone agent does on its own vs. what Lightwork adds."""
    return _page(request, "about.html")


# ===========================================================================
# Mock ServiceNow — the "any ticketing system" intake surface
# ===========================================================================
@app.get("/servicenow", response_class=HTMLResponse)
async def servicenow(request: Request) -> HTMLResponse:
    tickets = sorted(STORE.tickets.values(), key=lambda t: t.created_at, reverse=True)
    return _page(request, "servicenow.html", tickets=tickets)


@app.post("/servicenow/create")
async def servicenow_create(
    request: Request,
    short_description: str = Form(...),
    requester: str = Form(...),
    requester_email: str = Form(...),
    system_name: str = Form(...),
    data_types: str = Form(...),
) -> RedirectResponse:
    number = STORE.next_ticket_number()
    ticket = Ticket(
        number=number,
        short_description=short_description,
        requester=requester,
        requester_email=requester_email,
        system_name=system_name,
        data_types=data_types,
    )
    STORE.tickets[number] = ticket
    ticket.note("Ticket created by requester.", by=requester)

    # A ServiceNow Business Rule would POST to the platform webhook. We make a
    # real HTTP hop so the decoupling is genuine, not simulated in-process.
    payload = {
        "ticket_number": number,
        "short_description": short_description,
        "requester": requester,
        "requester_email": requester_email,
        "system_name": system_name,
        "data_types": data_types,
        "source": "servicenow",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{BASE_URL}/webhook/pia", json=payload)
    except Exception as exc:  # pragma: no cover
        ticket.note(f"Webhook delivery failed: {exc}", by="ServiceNow")

    return RedirectResponse(f"/servicenow/ticket/{number}", status_code=303)


@app.get("/servicenow/ticket/{number}", response_class=HTMLResponse)
async def servicenow_ticket(request: Request, number: str) -> HTMLResponse:
    ticket = STORE.tickets.get(number)
    if not ticket:
        return _page(request, "notfound.html", what=f"ticket {number}")
    case = STORE.cases.get(ticket.case_id) if ticket.case_id else None
    return _page(request, "ticket.html", ticket=ticket, case=case)


# ===========================================================================
# Intake webhook — "submitted via anything" lands here, becomes a REAL goal
# ===========================================================================
AGENT_VERSION = "1.1.0"


def _license_gate() -> str | None:
    """Evaluation-mode cap (standalone only): a friendly refusal when a NEW
    case would exceed the open-case cap, else None. A licensed key or the
    platform build lifts the cap — never a mid-flight cripple."""
    from capabilities import LICENSE
    if not STANDALONE or LICENSE.get("mode") == "licensed":
        return None
    cap = LICENSE.get("open_case_cap") or 0
    open_cases = sum(1 for c in STORE.cases.values()
                     if c.stage not in ("approved", "rejected"))
    if cap and open_cases >= cap:
        return (f"Evaluation mode ({LICENSE.get('reason', 'no license')}): "
                f"{open_cases} cases are open and the evaluation cap is "
                f"{cap}. Close a case, or set LIGHTWORK_LICENSE to lift "
                f"the cap — a license is a key swap, not a reinstall.")
    return None


@app.post("/webhook/pia")
async def webhook_pia(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _license_gate()
    if gate:
        return JSONResponse({"ok": False, "error": gate}, status_code=403)
    number = body.get("ticket_number", "")
    ticket = STORE.tickets.get(number)
    if not ticket:
        # Intake from a system we haven't seen (email, Jira, curl...).
        number = number or STORE.next_ticket_number()
        ticket = Ticket(
            number=number,
            short_description=body.get("short_description", "Privacy assessment request"),
            requester=body.get("requester", "Unknown"),
            requester_email=body.get("requester_email", ""),
            system_name=body.get("system_name", "Unknown system"),
            data_types=body.get("data_types", ""),
        )
        STORE.tickets[number] = ticket

    # A governed goal in the shared world (platform only) — appears on the
    # dashboard /goals page. Standalone has no world: gid stays None and the
    # agent runs ungoverned but fully functional.
    def _mk_goal() -> int | None:
        w = _world()
        if w is None:
            return None
        gid = w.create_goal(
            f"Privacy assessment: {ticket.system_name} ({number})",
            description=(f"{ticket.short_description} — requested by {ticket.requester} "
                         f"via {body.get('source', 'webhook')}. Data: {ticket.data_types}"),
            owner=AGENT, domain="itgrc_dpia",
        )
        w.set_goal_status(gid, "running")
        return gid

    gid = await asyncio.to_thread(_mk_goal)

    case_id = STORE.next_case_id()
    case = Case(
        id=case_id,
        ticket_number=number,
        subject=ticket.system_name,
        requester=ticket.requester,
        requester_email=ticket.requester_email,
        data_types=ticket.data_types,
    )
    case.goal_id = gid  # type: ignore[attr-defined]
    STORE.cases[case_id] = case
    ticket.case_id = case_id
    # The org "already has" this vendor's paperwork in the simulated tenant.
    _seed_corp_docs(ticket.system_name)
    # The ticket itself already answers what it can (quoted provenance).
    _prefill_from_ticket(case)

    _audit("GOAL_START", AGENT, gid, case=case_id, ticket=number,
           subject=ticket.system_name, source=body.get("source", "webhook"))
    _post_event(gid, AGENT, "plan",
                f"PIA request {number} received from {body.get('source', 'webhook')}. "
                f"Plan: interview requester → score risk → map controls → "
                f"file to OneTrust for review → human approves in OneTrust.")
    case.log(f"Intake received from {body.get('source', 'webhook')} ticket {number}.")

    ticket.state = "In Progress"
    ticket.note(
        f"Privacy assessment {case_id} opened in Lightwork (goal #{gid}). Requester "
        f"emailed an intake link; awaiting questionnaire completion.")

    intake_link = f"{BASE_URL}/intake/{case_id}"
    result = await _send_email(
        ticket.requester_email,
        f"[Action needed] Privacy assessment for {ticket.system_name} ({case_id})",
        (
            f"Hi {ticket.requester},\n\n"
            f"Thanks for requesting a privacy assessment for {ticket.system_name}.\n\n"
            f"Please complete the short intake questionnaire and attach any vendor "
            f"documents (DPA, security overview) here:\n\n    {intake_link}\n\n"
            f"It takes about two minutes — answer in the chat (typing or "
            f"voice) or use the guided buttons. Once you submit, the analysis "
            f"is filed straight into OneTrust for the privacy team to review "
            f"and approve.\n\n"
            f"— Privacy Office (automated)\n"
        ),
    )
    _audit("TOOL_CALL", ANALYST, gid, tool="email", to=ticket.requester_email,
           case=case_id, outcome=result[:80])
    _post_event(gid, ANALYST, "observation",
                f"Intake interview link emailed to {ticket.requester_email}. Waiting on requester.")
    case.log(f"Intake email sent to {ticket.requester_email}.")

    return JSONResponse({"ok": True, "case_id": case_id, "goal_id": gid,
                         "intake": intake_link})


# ===========================================================================
# Mail inbox (the requester's mailbox, on screen)
# ===========================================================================
@app.get("/mail", response_class=HTMLResponse)
async def mail(request: Request) -> HTMLResponse:
    # Newest-first payload for the reading-pane pop-out; the intake link is
    # pulled out of the body so the pane can render it as a proper button.
    mail_json = []
    for m in reversed(INBOX):
        link = next((t for t in m.body.split() if t.startswith("http")), "")
        mail_json.append({
            "to": m.to, "subject": m.subject, "body": m.body,
            "when": _dt.datetime.fromtimestamp(m.at).strftime("%H:%M"),
            "link": link,
        })
    return _page(request, "mail.html", inbox=INBOX, mail_json=mail_json)


# ===========================================================================
# Intake questionnaire — requester-facing (the emailed link)
# ===========================================================================
@app.get("/intake/{case_id}", response_class=HTMLResponse)
async def intake(request: Request, case_id: str, d: str = "") -> HTMLResponse:
    case = STORE.cases.get(case_id)
    if not case:
        return _page(request, "notfound.html", what=f"case {case_id}")
    tpl = get_template("pia")
    questions = [{
        "id": q.id,
        "section": q.section,
        "severity": q.severity,
        "prompt": _INTAKE_PHRASING.get(q.id, q.text),
    } for q in tpl.questions]

    # Delegate mode: an emailed ?d=<token> link scopes the interview to the
    # questions a colleague was asked to answer.
    delegation = None
    if d:
        rec = (getattr(case, "delegations", None) or {}).get(d)
        if rec:
            qids = set(rec["qids"])
            questions = [q for q in questions if q["id"] in qids]
            delegation = {"token": d, "to_name": rec["to_name"],
                          "note": rec.get("note", ""),
                          "from_name": case.requester}

    # Follow-up mode: the privacy team sent questions back; the requester
    # answers ONLY those (free text), then the case returns to review. A
    # delegate link stays scoped to its delegated questions even mid-followup.
    followups = (_open_followups(case)
                 if case.stage == "followup" and delegation is None else [])

    # A vendor we've already assessed? Offer "just add documents" instead of
    # a second full interview — the append-to-vendor flow. Auto-answers (from
    # the ticket/docs) don't count as having started the interview.
    human_answers = any(not rec.get("auto") for rec in case.answers.values())
    prior = None
    if case.stage == "intake_sent" and not human_answers and delegation is None:
        prior_ot = _latest_prior_record(case.subject)
        if prior_ot:
            prior = _prior_summary(prior_ot)

    # Already-resolved questions (human, delegate, or auto with provenance):
    # the interview skips them and shows where each answer came from.
    answered = {qid: {"answer": rec.get("answer", ""),
                      "auto": bool(rec.get("auto")),
                      "source": rec.get("source", ""),
                      "by": rec.get("by", "")}
                for qid, rec in case.answers.items()}

    done = (case.stage not in ("intake_sent", "followup")
            and delegation is None)
    return _page(request, "intake.html", case=case, questions=questions,
                 delegation=delegation, followups=followups, prior=prior,
                 answered=answered, done=done,
                 value=_case_value(case) if done else None)


@app.post("/intake/{case_id}/followup-answer")
async def intake_followup_answer(
    case_id: str,
    followup_id: str = Form(...),
    answer: str = Form(...),
) -> JSONResponse:
    """The requester answers one reviewer follow-up. Writes onto the REAL
    assessment record; when the last one is answered the case returns to the
    dashboard review queue."""
    case = STORE.cases.get(case_id)
    if not case or case.stage != "followup":
        return JSONResponse({"ok": False}, status_code=404)
    aid = getattr(case, "assessment_id", "")
    record = await asyncio.to_thread(
        backend.answer_followup, aid, followup_id.strip(), answer.strip(),
        case.requester)
    if record is None:
        return JSONResponse({"ok": False, "error": "unknown question"},
                            status_code=404)
    gid = getattr(case, "goal_id", None)
    _audit("FOLLOWUP_ANSWERED", ANALYST, gid, case=case_id,
           followup=followup_id.strip())
    remaining = [f for f in (record.get("followups") or []) if not f.get("answer")]
    if not remaining:
        case.stage = "pending_review"
        case.log("All follow-up answers received; back with the privacy team.")
        if gid:
            _post_event(gid, ANALYST, "observation",
                        "Requester answered every follow-up question. "
                        "Assessment is back in the review queue.")
    return JSONResponse({"ok": True, "remaining": len(remaining)})


@app.post("/intake/{case_id}/delegate")
async def intake_delegate(case_id: str, request: Request) -> JSONResponse:
    """Hand specific questions (or the whole remainder) to a colleague: a
    scoped intake link is emailed; their answers merge into the same case,
    attributed to them."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    body = await request.json()
    qids = [str(q) for q in (body.get("questions") or []) if q][:20]
    to_name = str(body.get("to_name", "")).strip()[:80] or "a colleague"
    to_email = str(body.get("to_email", "")).strip()[:200]
    note = str(body.get("note", "")).strip()[:500]
    if not qids or not to_email or "@" not in to_email:
        return JSONResponse({"ok": False, "error": "questions and a valid "
                             "colleague email are required"}, status_code=400)
    import uuid
    token = uuid.uuid4().hex[:10]
    dele = getattr(case, "delegations", None)
    if dele is None:
        dele = {}
        case.delegations = dele  # type: ignore[attr-defined]
    dele[token] = {"qids": qids, "to_name": to_name, "to_email": to_email,
                   "note": note, "answered": []}
    gid = getattr(case, "goal_id", None)
    _audit("INTAKE_DELEGATED", ANALYST, gid, case=case_id, to=to_email,
           questions=len(qids))
    if gid:
        _post_event(gid, ANALYST, "observation",
                    f"{case.requester} delegated {len(qids)} question(s) "
                    f"to {to_name} ({to_email}).")
    case.log(f"Delegated {len(qids)} question(s) to {to_name}.")
    link = f"{BASE_URL}/intake/{case_id}?d={token}"
    await _send_email(
        to_email,
        f"[Action needed] {case.requester} needs your input — privacy "
        f"assessment for {case.subject}",
        (
            f"Hi {to_name},\n\n"
            f"{case.requester} is completing a privacy assessment for "
            f"{case.subject} and delegated {len(qids)} question(s) to you"
            + (f':\n\n    "{note}"\n' if note else ".\n")
            + f"\nAnswer them here (takes a minute):\n\n    {link}\n\n"
            f"— Privacy Office (automated)\n"
        ),
    )
    return JSONResponse({"ok": True, "token": token,
                         "delegated": {t: {"to_name": r["to_name"],
                                           "qids": r["qids"]}
                                       for t, r in dele.items()}})


@app.post("/intake/{case_id}/answer")
async def intake_answer(
    case_id: str,
    question_id: str = Form(...),
    answer: str = Form(...),
    note: str = Form(""),
    d: str = Form(""),
) -> JSONResponse:
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False, "error": "unknown case"}, status_code=404)
    rec = {"answer": answer.strip().lower(), "note": note.strip()}
    # A delegate's answer is attributed and only accepted for their questions.
    if d:
        dele = case.delegations.get(d)
        if not dele or question_id not in dele["qids"]:
            return JSONResponse({"ok": False, "error": "not your question"},
                                status_code=403)
        rec["by"] = dele["to_name"]
        if question_id not in dele["answered"]:
            dele["answered"].append(question_id)
        gid = getattr(case, "goal_id", None)
        if gid and len(dele["answered"]) == len(dele["qids"]):
            _post_event(gid, ANALYST, "observation",
                        f"{dele['to_name']} answered all {len(dele['qids'])} "
                        f"delegated question(s).")
    case.answers[question_id] = rec
    return JSONResponse({"ok": True, "answered": len(case.answers)})


@app.post("/intake/{case_id}/autofill")
async def intake_autofill(case_id: str) -> JSONResponse:
    """Presenter convenience: fill realistic demo answers."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    for qid, (ans, note) in _DEMO_ANSWERS.items():
        case.answers[qid] = {"answer": ans, "note": note}
    return JSONResponse({"ok": True, "answered": len(case.answers)})


# ---- Conversational answers: free text / voice -> the template vocabulary --
_ANSWER_WORDS = {
    "yes": "yes", "yeah": "yes", "yep": "yes", "yup": "yes", "correct": "yes",
    "absolutely": "yes", "definitely": "yes", "affirmative": "yes",
    "no": "no", "nope": "no", "nah": "no", "negative": "no",
    "unknown": "unknown", "unsure": "unknown", "maybe": "unknown", "dunno": "unknown",
}
_NA_PHRASES = ("not applicable", "n/a", "na for", "doesn't apply",
               "does not apply", "doesnt apply", "irrelevant",
               "no such thing here")
_UNKNOWN_PHRASES = ("not sure", "don't know", "dont know", "do not know",
                    "no idea", "have to check", "need to check", "not certain",
                    "i'd have to", "id have to", "would have to ask", "tbd",
                    "can't say", "cant say", "hard to say")
_NO_PHRASES = ("we don't", "we do not", "we dont", "there is no", "there's no",
               "theres no", "not yet", "never", "haven't", "hasn't", "hasnt",
               "havent", "isn't", "isnt", "aren't", "arent", "won't", "wont",
               "nothing in place", "no dpa", "no agreement", "no retention",
               "unencrypted", "not encrypted", "not documented", "no notice",
               "without a", "still waiting on", "missing")
_YES_PHRASES = ("we do", "we have", "we've", "weve got", "it is", "it does",
                "they do", "in place", "signed", "covered", "documented",
                "encrypted", "always", "of course", "already", "that's right",
                "thats right", "there is a", "there's a", "theres a")


def _parse_free_answer(text: str) -> str | None:
    """Deterministic mapping of a typed/spoken reply onto yes/no/na/unknown.

    Scripted on purpose: the demo must behave identically with no API key
    and no network. Returns None when the reply is genuinely ambiguous so
    the agent can ask for a clarification instead of guessing."""
    low = " ".join((text or "").lower().split())
    if not low:
        return None
    if any(p in low for p in _NA_PHRASES):
        return "na"
    if any(p in low for p in _UNKNOWN_PHRASES):
        return "unknown"
    first = low.split()[0].strip(".,!?:;")
    if first in _ANSWER_WORDS:
        return _ANSWER_WORDS[first]
    # Phrase scan: negations are checked first because "no, it's encrypted
    # in transit only" should read as the correction it is.
    if any(p in low for p in _NO_PHRASES):
        return "no"
    if any(p in low for p in _YES_PHRASES):
        return "yes"
    return None


def _llm_interpret(question: str, text: str) -> str | None:
    """Optional upgrade: on the platform, with a provider key, a model reads
    an ambiguous reply. Standalone (no LLM assist) and any error degrade to
    None -- the scripted clarification -- so the agent never needs the
    network."""
    return backend.llm_interpret(question, text)


@app.post("/intake/{case_id}/carry-forward")
async def intake_carry_forward(case_id: str) -> JSONResponse:
    """Fresh assessment for a vendor we've reviewed before: pre-answer the
    interview from the last review's saved record. The requester only covers
    what's new; the reviewer sees where every carried answer came from."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    prior_ot = _latest_prior_record(case.subject)
    prior_case = STORE.cases.get(prior_ot.case_id) if prior_ot else None
    if prior_ot is None or prior_case is None:
        return JSONResponse({"ok": True, "prefilled": []})
    prior_answers: dict[str, dict] = {}
    if getattr(prior_case, "assessment_id", ""):
        record = await asyncio.to_thread(backend.load_saved,
                                         prior_case.assessment_id)
        prior_answers = (record or {}).get("answers") or {}
    if not prior_answers:
        # Seeded/legacy history has no saved platform record; the demo case
        # still remembers the interview.
        prior_answers = prior_case.answers
    source = f"last review {prior_ot.assessment_id}"
    hits: list[dict] = []
    for qid, rec in prior_answers.items():
        ans = (rec.get("answer") or "").strip()
        if ans not in _ANSWER_LABELS:
            continue
        hit = _record_auto_answer(case, qid, ans, source,
                                  (rec.get("note") or "")[:160])
        if hit:
            hits.append(hit)
    if hits:
        gid = getattr(case, "goal_id", None)
        _audit("INTAKE_PREFILL", ANALYST, gid, case=case.id, source=source,
               questions=[h["id"] for h in hits])
        if gid:
            _post_event(gid, ANALYST, "observation",
                        f"Carried {len(hits)} answer(s) forward from "
                        f"{prior_ot.assessment_id}; only new or changed "
                        f"ground gets asked.")
    return JSONResponse({"ok": True, "prefilled": hits,
                         "prior": _prior_summary(prior_ot)})


@app.post("/intake/{case_id}/interpret")
async def intake_interpret(case_id: str, request: Request) -> JSONResponse:
    """Chat/voice mode: turn a free-text reply into a recorded answer.

    The respondent's own words are preserved as the answer note (they land in
    the real assessment record and the reviewer sees them verbatim)."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    body = await request.json()
    question_id = str(body.get("question_id", ""))
    text = str(body.get("text", "")).strip()[:2000]
    d = str(body.get("d", ""))
    if not question_id or not text:
        return JSONResponse({"ok": False, "error": "question_id and text "
                             "are required"}, status_code=400)
    prompt = _INTAKE_PHRASING.get(question_id, question_id)
    answer = _parse_free_answer(text)
    method = "scripted"
    if answer is None:
        answer = await asyncio.to_thread(_llm_interpret, prompt, text)
        method = "llm"
    if answer is None:
        return JSONResponse({"ok": True, "answer": None, "method": "clarify",
                             "reply": "Sorry — I couldn't tell. Is that a yes, "
                                      "a no, not applicable, or are you unsure?"})
    rec = {"answer": answer, "note": text}
    if d:
        dele = case.delegations.get(d)
        if not dele or question_id not in dele["qids"]:
            return JSONResponse({"ok": False, "error": "not your question"},
                                status_code=403)
        rec["by"] = dele["to_name"]
        if question_id not in dele["answered"]:
            dele["answered"].append(question_id)
    case.answers[question_id] = rec
    labels = {"yes": "Yes", "no": "No", "na": "N/A", "unknown": "Not sure"}
    return JSONResponse({"ok": True, "answer": answer, "method": method,
                         "label": labels[answer],
                         "answered": len(case.answers)})


def _store_evidence(case: Case, filename: str, data: bytes, mime: str,
                    *, via: str) -> tuple[bool, str]:
    """Store evidence bytes as a REAL goal attachment (size cap, mime
    allowlist, executable/archive deny — the platform's own rules), mirror it
    onto the case's upload list, and audit. Returns (ok, message)."""
    gid = getattr(case, "goal_id", None)
    label = f"{filename} ({len(data):,} bytes)"
    if gid:
        try:
            from maverick.attachments import AttachmentRejected, store
            w = _world()
            existing = sum(a.size_bytes for a in w.list_attachments(gid))
            stored = store(gid, filename=filename, mime=mime, data=data,
                           existing_total=existing)
            w.add_attachment(goal_id=gid, filename=stored.filename,
                             mime=stored.mime, size_bytes=stored.size_bytes,
                             sha256=stored.sha256, path=str(stored.path))
            label = f"{stored.filename} ({stored.size_bytes:,} bytes)"
        except AttachmentRejected as exc:
            return False, str(exc)
        except Exception as exc:  # pragma: no cover - demo resilience
            print(f"[attach] real store failed, keeping demo record: {exc}")
    case.uploads.append(f"{label} — via {via}")
    _audit("EVIDENCE_CAPTURE", ANALYST, gid, case=case.id, artifact=filename,
           bytes=len(data), via=via)
    if gid:
        _post_event(gid, ANALYST, "artifact",
                    f"Evidence attached ({via}): {label}")
    return True, label


@app.post("/intake/{case_id}/upload")
async def intake_upload(case_id: str, file: UploadFile) -> JSONResponse:
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    # Bounded read: the intake link is unauthenticated by design, so an
    # oversized body must fail fast instead of buffering without limit.
    data = await file.read(backend.MAX_FILE_BYTES + 1)
    if len(data) > backend.MAX_FILE_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"file too large (limit "
                                   f"{backend.MAX_FILE_BYTES} bytes)",
             "uploads": case.uploads},
            status_code=400)
    mime = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"
    ok, msg = await asyncio.to_thread(
        _store_evidence, case, filename, data, mime, via="upload")
    if not ok:
        return JSONResponse({"ok": False, "error": msg,
                             "uploads": case.uploads}, status_code=400)
    prefilled = await asyncio.to_thread(
        _prefills_from_bytes, case, filename, data, mime)
    return JSONResponse({"ok": True, "uploads": case.uploads,
                         "prefilled": prefilled})


@app.post("/intake/{case_id}/discover")
async def intake_discover(case_id: str) -> JSONResponse:
    """Search the connected document sources (here: the simulated SharePoint
    tenant, via the REAL maverick.doc_discovery module) for this case's
    SOW/contract/DPA/security paperwork."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    if not backend.discovery_available():
        # Connected-source discovery ships with Lightwork's connectors.
        return JSONResponse({"ok": True, "hits": [], "unavailable": True})
    hits = await asyncio.to_thread(_discover_mock_documents, case.subject)
    gid = getattr(case, "goal_id", None)
    _audit("TOOL_CALL", ANALYST, gid, tool="doc_discovery", case=case_id,
           subject=case.subject, hits=len(hits))
    if gid and hits:
        _post_event(gid, ANALYST, "observation",
                    f"Doc discovery found {len(hits)} candidate document(s) "
                    f"for {case.subject} in connected sources.")
    return JSONResponse({"ok": True, "hits": [h.to_dict() for h in hits]})


@app.post("/intake/{case_id}/attach-found")
async def intake_attach_found(case_id: str, request: Request) -> JSONResponse:
    """One-click attach of a discovered document: fetch through the real
    doc_discovery path, store as real goal evidence."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    if not backend.discovery_available():
        return JSONResponse({"ok": False, "error": "document discovery "
                             "requires Lightwork connectors"}, status_code=400)
    body = await request.json()
    source = str(body.get("source", ""))
    if source != "msgraph":
        return JSONResponse(
            {"ok": False, "error": "document source is not available in this demo"},
            status_code=400,
        )
    doc_id = str(body.get("doc_id", ""))
    name = str(body.get("name", "")) or f"{source}-{doc_id[:16]}"
    ref = body.get("ref") or {}
    from maverick import doc_discovery
    try:
        data, mime = await asyncio.to_thread(
            _fetch_mock_document,
            source,
            doc_id,
            ref,
        )
    except ValueError as exc:  # our own clean validation messages
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001 -- no transport/URL detail to the caller
        return JSONResponse({"ok": False, "error": "could not fetch that "
                             "document from the source"}, status_code=400)
    resolved_mime = doc_discovery.resolve_mime(name, mime)
    ok, msg = await asyncio.to_thread(
        _store_evidence, case, name, data, resolved_mime,
        via=f"found in {source}")
    if not ok:
        return JSONResponse({"ok": False, "error": msg,
                             "uploads": case.uploads}, status_code=400)
    prefilled = await asyncio.to_thread(
        _prefills_from_bytes, case, name, data, resolved_mime)
    return JSONResponse({"ok": True, "error": None, "uploads": case.uploads,
                         "prefilled": prefilled})


@app.post("/intake/{case_id}/submit")
async def intake_submit(request: Request, case_id: str) -> RedirectResponse:
    case = STORE.cases.get(case_id)
    if not case:
        return RedirectResponse("/", status_code=303)
    gid = getattr(case, "goal_id", None)
    case.submitted_at = time.time()

    def _score_and_park() -> tuple[dict, list[dict], int]:
        # REAL scoring engine.
        session = AssessmentSession(type="pia", subject=case.subject)
        for qid, rec in case.answers.items():
            try:
                session.record(qid, rec.get("answer", ""), rec.get("note", ""))
                if rec.get("by"):  # delegate attribution, shown in the review pop-out
                    session.answers[qid]["by"] = rec["by"]
            except Exception:
                continue
        result = session.evaluate()
        # Persist where `maverick assess list` reads; remember the record id
        # so the dashboard review pop-out and the follow-up loop can find it.
        try:
            save_session(session)
            case.assessment_id = session.id  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"[assess] save_session failed: {exc}")

        # REAL control mapping with framework citations.
        controls: list[dict] = []
        seen: set[str] = set()
        for f in result.findings:
            for ctrl in find_controls(f"{f.section} {f.question}", limit=2):
                if ctrl.id in seen:
                    continue
                seen.add(ctrl.id)
                controls.append({
                    "id": ctrl.id, "title": ctrl.title, "theme": ctrl.theme,
                    "frameworks": "; ".join(ctrl.frameworks),
                    "for_finding": f.question_id,
                })

        result_dict = {
            "type": result.type, "subject": result.subject,
            "risk_rating": result.risk_rating,
            "inherent_risk": result.inherent_risk,
            "residual_risk": result.residual_risk,
            "risks_in_scope": result.risks_in_scope,
            "controls_in_place": result.controls_in_place,
            "answered": result.answered, "total": result.total,
            "findings": [{
                "id": f.question_id, "section": f.section, "text": f.question,
                "severity": f.severity, "answer": f.answer, "kind": f.kind,
                "guidance": f.recommendation,
            } for f in result.findings],
        }
        # The OneTrust review screen shows the interview verbatim — the
        # reviewer approves there, so the record must carry the answers.
        qtext = {q.id: _INTAKE_PHRASING.get(q.id, q.text)
                 for q in get_template("pia").questions}
        result_dict["answers"] = [{
            "question": qtext.get(qid, qid),
            "answer": rec.get("answer", ""), "note": rec.get("note", ""),
            "by": rec.get("by", ""),
        } for qid, rec in case.answers.items()]

        # Governed approval row -> the dashboard's /approvals queue (platform
        # only). Standalone has no world queue: approval_id stays "" and the
        # human decision happens purely in the OneTrust review screen.
        risk = result.risk_rating if result.risk_rating in ("high", "medium", "low") else "medium"
        findings_txt = "; ".join(
            f"[{f.severity}] {f.section}: {f.question}" for f in result.findings) or "no findings"
        controls_txt = "; ".join(f"{c['id']} {c['title']}" for c in controls) or "none"
        w = _world()
        approval_id: int | str = ""
        if w is not None:
            approval_id = w.create_approval(
                f"Approve PIA for {case.subject} (risk: {result.risk_rating.upper()})",
                risk=risk,
                scope=f"onetrust:/api/assessment/v2/assessments · ticket {case.ticket_number}",
                detail=(f"First-pass assessment {case.id} — inherent {result.inherent_risk.upper()} "
                        f"→ residual {result.residual_risk.upper()} "
                        f"({result.controls_in_place} of {result.risks_in_scope} risk areas controlled), "
                        f"{result.answered}/{result.total} answered, "
                        f"{len(result.findings)} findings. Findings: {findings_txt}. "
                        f"Required controls: {controls_txt}. Uploads: {', '.join(case.uploads) or 'none'}. "
                        # The token the dashboard's review pop-out keys on.
                        f"[assessment:{getattr(case, 'assessment_id', '') or 'unsaved'}]"),
                provenance="goal",
                requested_by=AGENT,
            )
        return result_dict, controls, approval_id

    result_dict, controls, approval_id = await asyncio.to_thread(_score_and_park)

    case.result = result_dict
    case.controls = controls
    case.approval_id = str(approval_id)

    _audit("ASSESSMENT_REVIEW", ANALYST, gid, case=case_id, subject=case.subject,
           risk=result_dict["risk_rating"], findings=len(result_dict["findings"]),
           controls=len(controls), status="pending_human_review")
    _audit("AUTONOMY_GATED", AGENT, gid, case=case_id, approval_id=approval_id,
           reason="assessment_requires_human_approval_before_completion")
    if gid:
        _post_event(gid, ANALYST, "finding",
                    f"First-pass assessment: risk {result_dict['risk_rating'].upper()}, "
                    f"{len(result_dict['findings'])} findings, {len(controls)} required controls "
                    f"(GDPR/ISO 27001/SOC 2/NIST citations attached).")

    # OneTrust-first: file immediately as UNDER REVIEW. The human decision
    # happens inside OneTrust (or the Lightwork queue — either surface decides
    # approval #{id}); nothing is marked Completed until a person approves.
    await _file_under_review(case)
    if gid:
        _post_event(gid, AGENT, "verify",
                    f"Autonomy gated: filed to OneTrust as UNDER REVIEW. Approval "
                    f"#{approval_id} mirrors the human decision — approve in "
                    f"OneTrust and the signed audit chain records it.")
    case.log(f"Scored risk={result_dict['risk_rating']}; filed to OneTrust as "
             f"Under Review (approval #{approval_id} tracks the decision).")

    ticket = STORE.tickets.get(case.ticket_number)
    if ticket:
        ticket.note(f"First-pass assessment complete — risk "
                    f"{result_dict['risk_rating'].upper()}, {len(result_dict['findings'])} findings. "
                    f"Filed to OneTrust as Under Review; awaiting the privacy "
                    f"team's decision there (approval #{approval_id}).")

    return RedirectResponse(f"/intake/{case_id}?submitted=1", status_code=303)


def _protocol_facts(case: Case) -> dict:
    """The deterministic facts the wire answers are built from — intake
    answers and the scored result only, never model prose."""
    result = case.result or {}
    answer_text = " ".join(
        f"{rec.get('answer', '')} {rec.get('note', '')}"
        for rec in case.answers.values()) + f" {case.data_types}"
    low = f" {answer_text.lower()} "
    risk = {"minimal": "Low", "low": "Low", "medium": "Medium",
            "high": "High"}.get(str(result.get("risk_rating", "medium")),
                                "Medium")
    return {
        "risk_option": risk,
        "ai": bool(re.search(r"\b(ai|artificial intelligence|machine "
                             r"learning|llm|model)\b", low)),
        "sold": bool(re.search(r"\b(sell|sold|sale of data)\b", low)),
        "autofilled": sum(1 for rec in case.answers.values()
                          if rec.get("auto")),
        "rationale": "; ".join(
            f"[{f.get('severity', '?')}] {f.get('section', '?')}: "
            f"{f.get('question', '?')}"
            for f in (result.get("findings") or [])[:6]) or
            "No adverse findings; risk from in-scope severities.",
        "regions_text": answer_text,
    }


def _run_onetrust_protocol(case: Case) -> dict:
    """Drive the VERIFIED OneTrust protocol end-to-end (sync; runs in a
    thread): launch → dedup-link the vendor → resolve personal-data triples
    against the live catalogs → notice cross-check (contradictions escalate
    the filed rating BEFORE submit) → write every answer shape in one call →
    submit → SELF-CHECK the stage → attach deliverables to the vendor record.
    Autonomy stops at Under Review — the human gate."""
    client = backend.onetrust_client()
    facts = _protocol_facts(case)
    steps: list[str] = []

    aid = client.launch(
        "tmpl-pia",
        org_group_id=os.environ.get("ONETRUST_ORG_GROUP", "og-demo"),
        respondent=case.requester_email or "requester@example.com",
        name=f"PIA — {case.subject}")
    steps.append(f"Launched {aid} (v3, org group + respondent)")

    vendor = client.ensure_vendor(case.subject)
    steps.append(("Created vendor record " if vendor.get("created")
                  else "Linked existing vendor record ")
                 + f"{vendor['id']} ({vendor['name']}) — dedup-first, "
                   "entities never created")

    triples = client.resolve_personal_data(
        f"{case.data_types} name email address")
    categories = sorted({t["category"]["name"] for t in triples})

    # Notice cross-check BEFORE the write: a contradiction with a published
    # promise escalates the rating that gets filed.
    notices = client.notices()
    picked, siblings = notice_check.pick_notice(
        os.environ.get("PIA_CONTRACTING_ENTITY", ""), notices,
        default_name=os.environ.get("ONETRUST_DEFAULT_NOTICE",
                                    "Global Vacation Clubs"))
    check = {"contradictions": [], "gaps": [], "escalate": False}
    if picked:
        check = notice_check.cross_check(
            notice_text=client.notice_text(picked["guid"]),
            facts={"data_sold": facts["sold"],
                   "shared_with_third_parties": False},
            data_categories=categories)
        steps.append(f"Cross-checked notice {picked['name']!r}: "
                     f"{len(check['contradictions'])} contradiction(s), "
                     f"{len(check['gaps'])} gap(s)")
    risk_option = "High" if check["escalate"] else facts["risk_option"]
    rationale = facts["rationale"]
    if check["escalate"]:
        rationale = (check["contradictions"][0]["finding"] + " " + rationale)

    qs = {q["questionId"]: q for q in client.questions(aid)}
    entries: list[dict] = []
    entries += client.answer_text(qs["q-name"], case.subject)
    entries += client.answer_text(
        qs["q-summary"],
        f"{(case.result or {}).get('answered', 0)}/"
        f"{(case.result or {}).get('total', 0)} answered; "
        f"{len((case.result or {}).get('findings') or [])} findings; "
        f"source ticket {case.ticket_number}.")
    entries += client.answer_option(qs["q-risk"], risk_option)
    entries += client.justification(qs["q-risk"], rationale)
    entries += client.answer_option(qs["q-ai"],
                                    "Yes" if facts["ai"] else "No")
    entries += client.answer_option(qs["q-sold"],
                                    "Yes" if facts["sold"] else "No")
    entries += client.answer_record(qs["q-vendor"], vendor)
    entries += client.personal_data_rows(qs["q-personal-data"], triples)
    client.write_responses(aid, entries)
    steps.append(f"Wrote {len(entries)} response entries in one call "
                 "(answers + justification + personal-data triples)")

    sub = client.submit(aid)
    if not sub.get("advanced"):
        # Self-advance once: the only honest auto-fix is a justification we
        # actually have; unanswered compliance questions are never invented.
        fixes: list[dict] = []
        for o in sub.get("outstanding", []):
            if o["reason"] == "missing justification" \
                    and o["questionId"] in qs:
                fixes += client.justification(qs[o["questionId"]], rationale)
        if fixes:
            client.write_responses(aid, fixes)
            sub = client.submit(aid)
            steps.append("Retried once after writing the missing "
                         "justification")
    advanced = bool(sub.get("advanced"))
    if advanced:
        steps.append("Self-check: export status is Under Review (verified "
                     "by re-read, not by the POST returning)")

    # AI branch: an AI-supported process gets its own linked AI Model
    # Assessment, finalized to Under Review in the same run.
    ai_id = ""
    if advanced and facts["ai"]:
        ai_id = client.launch(
            "tmpl-ai",
            org_group_id=os.environ.get("ONETRUST_ORG_GROUP", "og-demo"),
            respondent=case.requester_email or "requester@example.com",
            name=f"AI Model Assessment — {case.subject}")
        aqs = {q["questionId"]: q for q in client.questions(ai_id)}
        a_entries = client.answer_text(aqs["q-ai-name"], case.subject)
        # Oversight is a fact of this process: the Under-Review human gate.
        a_entries += client.answer_option(aqs["q-ai-oversight"], "Yes")
        client.write_responses(ai_id, a_entries)
        client.link_assessments(aid, [ai_id])
        client.submit(ai_id)
        steps.append(f"AI detected → launched {ai_id}, linked to {aid}, "
                     "finalized to Under Review")

    # Contract instrument: decided from the answers, never from prose.
    instrument = contract_guard.decide_instrument(
        regions_text=facts["regions_text"],
        external_processing=True, personal_data=bool(triples))

    # Deliverables land on the VENDOR record (assessment-level attach is
    # session-gated on real tenants).
    # The full risk-analysis report and notice cross-check — real documents
    # with the findings, controls, and interview basis, named legibly and
    # versioned per vendor. Word on the platform; standalone files the
    # honest plain text.
    instrument_line = (f"{instrument['instrument'] or 'none required'} — "
                       f"{instrument['reason']}")
    # Repeat review of a known vendor: v2+ documents must read as a DELTA,
    # not a duplicate — what changed, what carried, how the risk moved.
    prior_lines: list[str] = []
    # Exclude THIS run's record by assessment id, not just case id: on the
    # wire path the mock mirrors the new filing into the store at submit()
    # time with its case link still unset, so a case_id check alone would
    # pick the run's own record as its "prior".
    prior_rec = next(
        (a for a in sorted(STORE.onetrust.values(),
                           key=lambda a: a.filed_at, reverse=True)
         if a.subject.strip().lower() == case.subject.strip().lower()
         and a.assessment_id != aid and a.case_id != case.id
         and a.status in ("Completed", "Under Review")), None)
    if prior_rec is not None:
        carried = sum(1 for rec in case.answers.values()
                      if rec.get("auto") and "last review"
                      in str(rec.get("source") or ""))
        fresh_n = sum(1 for rec in case.answers.values()
                      if not rec.get("auto"))
        prior_risk = str((prior_rec.result or {}).get(
            "risk_rating", prior_rec.risk_level or "?")).upper()
        new_risk = str((case.result or {}).get("risk_rating", "?")).upper()
        when = time.strftime("%Y-%m-%d", time.gmtime(
            prior_rec.decided_at or prior_rec.filed_at))
        prior_lines = [
            f"Prior review    : {prior_rec.assessment_id} — "
            f"{prior_rec.status}, decided {when}",
            f"Carried forward : {carried} answer(s) unchanged from the "
            "prior review (source shown on each)",
            f"Fresh this run  : {fresh_n} answer(s) newly established "
            "or changed",
            f"Risk movement   : {prior_risk} → {new_risk}"
            + (" (unchanged)" if prior_risk == new_risk else ""),
        ]
    report_body = _risk_report_text(
        subject=case.subject, ticket=case.ticket_number,
        requester=case.requester, result=case.result or {},
        controls=case.controls, instrument_line=instrument_line,
        rationale=rationale, at=time.time(), prior_lines=prior_lines)
    notice_body = notice_check.recommendations_doc(case.subject, picked,
                                                   check, siblings)
    risk_name, risk_bytes, risk_mime = _deliverable(
        case.subject, "risk-analysis",
        f"Privacy Risk Analysis — {case.subject}", report_body)
    notice_name, notice_bytes, notice_mime = _deliverable(
        case.subject, "notice-crosscheck",
        f"Privacy-Notice Cross-Check — {case.subject}", notice_body)
    client.attach_to_record(vendor["id"], risk_name, risk_bytes,
                            mime=risk_mime)
    client.attach_to_record(vendor["id"], notice_name, notice_bytes,
                            mime=notice_mime)
    steps.append(f"Attached {risk_name} + {notice_name} to the vendor "
                 "record (two-step upload → link)")

    # The vendor's own paper never gets skipped: if no clause round has run
    # on this case but the record carries counterparty paper (their DPA from
    # this or a prior round), re-run the playbook review and file the memo +
    # tracked-changes redline in the same protocol run. Deterministic path
    # (operator playbook), so it needs no model and cannot stall the filing.
    if CAPS.get("paper_redline") and not case.paper_reviews:
        paper = _latest_counterparty_paper(vendor["id"])
        if paper is not None:
            try:
                text, _ext = backend.document_text(paper["content"],
                                                   paper["mime"])
                if (text or "").strip():
                    review = backend.review_vendor_paper(
                        text, vendor=case.subject,
                        document_name=paper["filename"], use_model=False)
                    redline = backend.build_redline(
                        review, original=paper["content"], mime=paper["mime"],
                        date=_dt.datetime.now(_dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        title=f"{case.subject} — "
                              f"{review.to_dict()['instrument_label']} "
                              "(Lightwork redline)")
                    entry = paper_desk.file_paper_review(
                        case, review, redline, reviewer="Lightwork Concierge")
                    steps.append(
                        f"Re-reviewed {paper['filename']} against the clause "
                        f"playbook — {entry['gaps']} departure(s); memo + "
                        f"tracked-changes redline filed (v{entry['version']})")
            except Exception as exc:  # pragma: no cover — filing never dies on paper
                steps.append(f"Paper re-review skipped: {exc}"[:160])

    value = value_ledger.case_value(
        kind="ai_assessment" if facts["ai"] else "privacy_assessment",
        answers_autofilled=facts["autofilled"],
        contract_drafted=bool(instrument["instrument"]),
        notice_checked=bool(picked))
    return {"ok": advanced, "assessment_id": aid, "ai_assessment_id": ai_id,
            "vendor": vendor, "steps": steps,
            "outstanding": sub.get("outstanding", []),
            "instrument": instrument, "notice": check, "value": value}


async def _file_under_review(case: Case) -> None:
    """File the scored assessment into OneTrust as "Under Review" through the
    verified wire protocol — input + analysis are the agent's job; the human
    approves inside OneTrust. The agent can never mark anything Complete."""
    gid = getattr(case, "goal_id", None)
    from onetrust_client import OneTrustError  # cached by the module import
    try:
        run = await asyncio.to_thread(_run_onetrust_protocol, case)
    except OneTrustError as exc:
        case.stage = "pending_review"
        case.log(f"OneTrust protocol run failed: {exc}")
        _audit("TOOL_CALL", ANALYST, gid, tool="onetrust", op="protocol",
               case=case.id, outcome=f"error: {exc}"[:200])
        return
    _audit("TOOL_CALL", ANALYST, gid, tool="onetrust", op="protocol",
           path="/api/assessment/v3/assessments", case=case.id,
           status="Under Review" if run["ok"] else "incomplete",
           outcome="; ".join(run["steps"])[:400])
    if run["ok"]:
        case.onetrust_id = run["assessment_id"]
        case.stage = "in_onetrust_review"
        for step in run["steps"]:
            case.log(f"OneTrust: {step}.")
        v = run["value"]
        case.value = v
        case.log(f"Value ledger: {v['hours']}h saved "
                 f"(privacy team {v['privacy_hours']}h · business "
                 f"{v['business_hours']}h) ≈ ${v['dollars']:,.0f} at "
                 f"${v['rate']:,.0f}/h.")
        # Demo-side linkage so the tenant viewer shows the case's analysis.
        rec = STORE.onetrust.get(run["assessment_id"])
        if rec is not None:
            rec.case_id = case.id
            rec.result = case.result or {}
            rec.controls = case.controls
        if gid:
            _post_event(gid, ANALYST, "artifact",
                        f"Filed in OneTrust as {case.onetrust_id} — status "
                        f"UNDER REVIEW via the verified protocol (launch → "
                        f"responses incl. justification + personal-data "
                        f"triples → submit → stage self-check). Review & "
                        f"approve happens in OneTrust.")
    else:
        case.stage = "pending_review"
        outstanding = "; ".join(
            f"{o['questionId']} ({o['reason']})"
            for o in run["outstanding"]) or "unknown"
        case.log("OneTrust submit did not advance — outstanding required "
                 f"questions: {outstanding}. Nothing was fabricated; the "
                 "case stays reviewable from Lightwork.")


# ===========================================================================
# Approval watcher — mirrors a decision made in the REAL Lightwork queue
# (the primary review surface is now INSIDE OneTrust; both converge here)
# ===========================================================================
_REVIEWABLE_STAGES = ("in_onetrust_review", "pending_review")


async def _approval_watcher() -> None:
    while True:
        try:
            pending = [c for c in STORE.cases.values()
                       if c.stage in _REVIEWABLE_STAGES]
            for case in pending:
                # Reviewer asked for more detail (dashboard pop-out)? Reopen
                # the interview before looking at the approve/deny state.
                if await asyncio.to_thread(_open_followups, case):
                    await _reopen_for_followups(case)
                    continue
                status, decided_by = await asyncio.to_thread(_approval_state, case)
                if status == "approved":
                    await _complete_case(case, decided_by, via="lightwork")
                elif status in ("denied", "rejected"):
                    await _send_back_case(case, decided_by, via="lightwork")
        except Exception as exc:  # pragma: no cover
            print(f"[watcher] {exc}")
        await asyncio.sleep(2)


def _open_followups(case: Case) -> list[dict]:
    """Unanswered reviewer follow-ups on this case's assessment record."""
    aid = getattr(case, "assessment_id", "")
    if not aid:
        return []
    try:
        record = backend.load_saved(aid) or {}
        return [f for f in (record.get("followups") or []) if not f.get("answer")]
    except Exception:  # pragma: no cover - demo resilience
        return []


async def _reopen_for_followups(case: Case) -> None:
    """The privacy team sent questions back: re-interview the requester."""
    gid = getattr(case, "goal_id", None)
    case.stage = "followup"
    n = len(_open_followups(case))
    case.log(f"Privacy team asked {n} follow-up question(s); re-interviewing.")
    _audit("FOLLOWUP_REQUESTED", ANALYST, gid, case=case.id, questions=n)
    if gid:
        _post_event(gid, ANALYST, "observation",
                    f"Reviewer sent {n} follow-up question(s) back to "
                    f"{case.requester}. Awaiting answers.")
    intake_link = f"{BASE_URL}/intake/{case.id}"
    await _send_email(
        case.requester_email,
        f"[Action needed] Follow-up questions on your privacy assessment ({case.id})",
        (
            f"Hi {case.requester},\n\n"
            f"The privacy team reviewed your assessment for {case.subject} and "
            f"needs a bit more detail — {n} quick question(s):\n\n    {intake_link}\n\n"
            f"Your earlier answers are saved; this only covers the new questions.\n\n"
            f"— Privacy Office (automated)\n"
        ),
    )


def _approval_state(case: Case) -> tuple[str, str]:
    w = _world()
    if w is None or not case.approval_id:
        # Standalone: no world approval row. Decisions come straight through
        # the OneTrust screen (onetrust_decide), not this poll.
        return "none", ""
    try:
        a = w.get_approval(int(case.approval_id))
        if a is None:
            return "missing", ""
        return a.status, a.decided_by or ""
    except Exception:
        return "error", ""


def _ot_record_for(case: Case) -> OneTrustAssessment | None:
    return STORE.onetrust.get(case.onetrust_id) if case.onetrust_id else None


def _decide_saved_assessment(case: Case, decision: str, decided_by: str) -> None:
    """Mirror the decision onto the REAL assessment record (revision-checked;
    the record may already carry the decision if it was made in the dashboard
    pop-out — that's fine, the state converges)."""
    aid = getattr(case, "assessment_id", "")
    if not aid:
        return
    try:
        rec = backend.load_saved(aid)
        if rec is None or rec.get("status") == decision:
            return
        backend.decide_assessment(aid, decision, decided_by=decided_by,
                                  cadence_days=365,
                                  expected_revision=rec["revision"])
    except Exception as exc:  # pragma: no cover - demo resilience
        print(f"[decide] assessment record mirror failed: {exc}")


async def _complete_case(case: Case, decided_by: str, *, via: str) -> None:
    """One approval, wherever it was clicked (OneTrust screen or the Lightwork
    queue): flip the OneTrust record to Completed, decide the real assessment
    record, close the goal + ticket, notify the requester — all on the signed
    audit chain."""
    if case.stage not in _REVIEWABLE_STAGES:
        return
    gid = getattr(case, "goal_id", None)
    case.stage = "filed"
    case.decided_by = decided_by or "privacy-reviewer"
    case.log(f"Approved by {case.decided_by} (via {via}).")
    _audit("APPROVAL_DECISION", case.decided_by, gid, case=case.id,
           decision="approved", approval_id=case.approval_id, via=via,
           risk=(case.result or {}).get("risk_rating"))
    await asyncio.to_thread(_decide_saved_assessment, case, "approved",
                            case.decided_by)
    ot = _ot_record_for(case)
    if ot:
        ot.status = "Completed"
        ot.decided_by = case.decided_by
        ot.decided_at = time.time()
    if gid:
        _post_event(gid, AGENT, "observation",
                    f"Approval #{case.approval_id} granted by {case.decided_by} "
                    f"(via {via}). {case.onetrust_id or 'The assessment'} is now "
                    f"COMPLETED in OneTrust.")
        await asyncio.to_thread(
            _world().set_goal_status, gid, "done",
            f"PIA complete: risk {(case.result or {}).get('risk_rating', '?').upper()}, "
            f"approved by {case.decided_by} in OneTrust review, "
            f"ticket {case.ticket_number} resolved.")
    ticket = STORE.tickets.get(case.ticket_number)
    if ticket:
        ticket.state = "Resolved"
        ticket.note(
            f"Privacy assessment APPROVED (risk "
            f"{(case.result or {}).get('risk_rating', '?').upper()}) by {case.decided_by} "
            f"in OneTrust ({case.onetrust_id}). "
            f"{len(case.controls)} required controls attached. Closing ticket.")
    await _send_email(
        case.requester_email,
        f"Privacy assessment complete — {case.subject} ({case.id})",
        (
            f"Hi {case.requester},\n\n"
            f"Your privacy assessment for {case.subject} is complete and approved.\n\n"
            f"Risk rating: {(case.result or {}).get('risk_rating', '?').upper()}\n"
            f"OneTrust record: {case.onetrust_id}\n"
            f"Required controls: {len(case.controls)}\n\n"
            f"The privacy team will follow up on any open controls.\n\n"
            f"— Privacy Office\n"
        ),
    )


async def _send_back_case(case: Case, decided_by: str, *, via: str) -> None:
    if case.stage not in _REVIEWABLE_STAGES:
        return
    gid = getattr(case, "goal_id", None)
    case.stage = "rejected"
    case.decided_by = decided_by or "privacy-reviewer"
    case.log(f"Sent back by {case.decided_by} (via {via}).")
    _audit("APPROVAL_DECISION", case.decided_by, gid, case=case.id,
           decision="denied", approval_id=case.approval_id, via=via)
    await asyncio.to_thread(_decide_saved_assessment, case, "rejected",
                            case.decided_by)
    ot = _ot_record_for(case)
    if ot:
        ot.status = "Sent back"
        ot.decided_by = case.decided_by
        ot.decided_at = time.time()
    if gid:
        _post_event(gid, AGENT, "error",
                    f"Approval #{case.approval_id} denied by {case.decided_by} "
                    f"(via {via}); the OneTrust record is marked Sent back.")
        await asyncio.to_thread(_world().set_goal_status, gid, "blocked",
                                "Assessment sent back by privacy review.")
    ticket = STORE.tickets.get(case.ticket_number)
    if ticket:
        ticket.state = "In Progress"
        ticket.note(f"Assessment sent back by privacy team ({case.decided_by}).")


# ===========================================================================
# Mock OneTrust — tenant API (the verified wire protocol) + viewer
# ===========================================================================
import ot_mock as _ot_mock  # noqa: E402
from ot_mock import router as _ot_router  # noqa: E402

app.include_router(_ot_router)


def _vendor_documents(subject: str) -> list[dict]:
    """The documents sitting on a vendor's mock-tenant record — what the
    agent filed there (risk analysis, notice cross-check, paper-review memo
    + redline), plus anything seeded. Matched by vendor NAME because the
    viewer's records only know their subject."""
    name = (subject or "").strip().lower()
    if not name:
        return []
    vendor = next((v for v in _ot_mock.VENDORS
                   if v["name"].strip().lower() == name), None)
    if vendor is None:
        return []
    docs = [a for a in _ot_mock.ATTACHMENTS.values()
            if a.get("linked_to") == vendor["id"]]
    return [{
        "id": a["id"], "filename": a.get("filename", ""),
        "bytes": a.get("bytes", 0),
        "at": a.get("at", 0),
        "kind": ("docx" if str(a.get("filename", "")).endswith(".docx")
                 else "text" if str(a.get("filename", "")).endswith(".txt")
                 else "pdf" if str(a.get("filename", "")).endswith(".pdf")
                 else "other"),
        "by": a.get("by", ""),
        "available": bool(a.get("content")),
    } for a in docs]


_ATT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]")


@app.get("/onetrust/attachment/{att_id}")
async def onetrust_attachment(att_id: str) -> Response:
    """Open a document from the tenant's Documents tab: text renders in the
    browser, a .docx downloads for Word (that's where tracked changes live)."""
    att = _ot_mock.ATTACHMENTS.get(att_id)
    if att is None:
        return PlainTextResponse("unknown attachment", status_code=404)
    content = att.get("content") or b""
    if not content:
        return PlainTextResponse(
            "attachment content is no longer retained by the demo tenant",
            status_code=410)
    filename = _ATT_FILENAME_RE.sub("-", att.get("filename") or "document")
    if filename.endswith(".docx"):
        return Response(
            content, media_type=paper_desk.DOCX_MIME,
            headers={"Content-Disposition":
                     f'attachment; filename="{filename}"'})
    if filename.endswith(".txt"):
        return Response(content, media_type="text/plain; charset=utf-8")
    return Response(
        content, media_type=att.get("mime") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/onetrust", response_class=HTMLResponse)
async def onetrust_viewer(request: Request) -> HTMLResponse:
    items = sorted(STORE.onetrust.values(), key=lambda a: a.filed_at, reverse=True)
    # Serializable payload for the record-detail / review pop-out.
    def _speed(a: OneTrustAssessment) -> dict:
        c = STORE.cases.get(a.case_id)
        if not c:
            return {}
        out = {"filed_elapsed": _fmt_elapsed(a.filed_at - c.created_at),
               "auto_answered": sum(1 for r in c.answers.values()
                                    if r.get("auto"))}
        if a.decided_at:
            out["decided_elapsed"] = _fmt_elapsed(a.decided_at - c.created_at)
        return out

    records_json = [{
        "assessment_id": a.assessment_id, "name": a.name,
        "template": a.template, "subject": a.subject, "status": a.status,
        "risk_level": a.risk_level,
        "inherent_risk": (a.result or {}).get("inherent_risk", a.risk_level),
        "residual_risk": (a.result or {}).get("residual_risk", a.risk_level),
        "controls_in_place": (a.result or {}).get("controls_in_place", 0),
        "risks_in_scope": (a.result or {}).get("risks_in_scope", 0),
        "answered": (a.result or {}).get("answered", 0),
        "total": (a.result or {}).get("total", 0),
        "findings": (a.result or {}).get("findings", []),
        "answers": (a.result or {}).get("answers", []),
        "controls": a.controls,
        "documents": _vendor_documents(a.subject),
        "addenda": a.addenda,
        "needs_re_review": a.needs_re_review,
        "decided_by": a.decided_by,
        "speed": _speed(a),
    } for a in items]
    n_review = sum(1 for a in items if a.status == "Under Review")
    return _page(request, "onetrust.html", assessments=items,
                 records_json=records_json, n_review=n_review)


@app.post("/onetrust/decide")
async def onetrust_decide(request: Request) -> JSONResponse:
    """The reviewer's decision, made INSIDE OneTrust. It drives the same
    governed machinery as the Lightwork queue: the world approval row is
    decided, the real assessment record is decided, and the signed audit
    chain records who clicked."""
    body = await request.json()
    ot_id = str(body.get("assessment_id", ""))
    decision = str(body.get("decision", ""))
    reviewer = str(body.get("reviewer", "")).strip()[:80] or "OneTrust reviewer"
    ot = STORE.onetrust.get(ot_id)
    if not ot:
        return JSONResponse({"ok": False, "error": "unknown assessment"},
                            status_code=404)
    if ot.status != "Under Review":
        return JSONResponse({"ok": False, "error": f"already {ot.status}"},
                            status_code=409)
    if decision not in ("approve", "send_back"):
        return JSONResponse({"ok": False, "error": "decision must be approve "
                             "or send_back"}, status_code=422)
    case = STORE.cases.get(ot.case_id)
    if case is None:
        # Record predates a demo restart: flip it locally, honestly labeled.
        ot.status = "Completed" if decision == "approve" else "Sent back"
        ot.decided_by = reviewer
        ot.decided_at = time.time()
        return JSONResponse({"ok": True, "status": ot.status})
    if decision == "approve" and await asyncio.to_thread(_open_followups, case):
        return JSONResponse(
            {"ok": False, "error": "follow-up questions are still open with "
             "the requester — answers land back here automatically"},
            status_code=409)
    status = "approved" if decision == "approve" else "denied"
    w = _world()
    if w is not None and case.approval_id:
        await asyncio.to_thread(
            w.decide_approval, int(case.approval_id), status,
            decided_by=reviewer)
    if decision == "approve":
        await _complete_case(case, reviewer, via="onetrust")
    else:
        await _send_back_case(case, reviewer, via="onetrust")
    return JSONResponse({"ok": True, "status": ot.status})


# ---- Append documents to a vendor we've already assessed -------------------
def _vendor_previous_gaps(subject: str) -> set[str]:
    """Missing clauses from the vendor's most recent DPA review (if any) —
    so a newly appended document can be reported as closing them. Empty in
    standalone (the Art. 28 clause engine is a Lightwork feature)."""
    return backend.previous_dpa_gaps(subject)


def _analyze_addendum(ot: OneTrustAssessment, case: Case | None,
                      filename: str, data: bytes, mime: str) -> dict:
    """First-pass analysis of a document added AFTER the assessment existed.
    On the platform, DPA-looking text runs the real Art. 28 clause review and
    the record lands in the /privacy workspace. In the standalone agent that
    engine isn't present, so the addendum is attached with an honest
    metadata-only summary and flagged for human review."""
    gid = getattr(case, "goal_id", None) if case else None
    text = backend.extract_text(data, mime)
    entry: dict[str, Any] = {"at": time.time(), "filename": filename,
                             "summary": "", "gaps": [], "re_review": False,
                             "dpa_review_id": ""}
    low = f"{filename} {text[:4000]}".lower()
    dpa_like = any(k in low for k in ("data processing", "dpa", "processor",
                                      "art. 28", "article 28"))
    review = None
    if dpa_like and CAPS["dpa_clause_review"]:
        # It knows we reviewed this vendor before: diff the new document
        # against the gaps the LAST review left open.
        prev_missing = _vendor_previous_gaps(ot.subject)
        try:
            review = backend.dpa_clause_review(
                ot.subject, text, filename=filename, mime=mime, data=data)
        except Exception as exc:
            print(f"[addendum] clause review unavailable: {exc}")
            review = None
    if review is not None:
        missing = [c["requirement"] for c in review.get("clauses", [])
                   if c.get("status") == "missing"]
        entry["gaps"] = missing[:6]
        entry["dpa_review_id"] = review.get("id", "")
        entry["re_review"] = (bool(missing)
                              or review.get("residual_risk") in ("high",
                                                                 "medium"))
        entry["summary"] = (
            f"Art. 28 clause review: {review.get('clauses_present', 0)} of "
            f"{review.get('clauses_total', 0)} clauses present, residual "
            f"risk {str(review.get('residual_risk', '?')).upper()}."
            + (" Missing clauses flagged — re-review recommended."
               if entry["re_review"] else " No gaps — filed for the record."))
        closed = sorted(prev_missing - set(missing))
        if closed:
            entry["closed_gaps"] = closed[:6]
            entry["summary"] += (
                f" Closes {len(closed)} gap(s) from the last review: "
                f"{'; '.join(closed[:3])}.")
    elif dpa_like and not CAPS["dpa_clause_review"]:
        # Standalone: recognise the document type, flag for human review.
        entry["re_review"] = True
        entry["summary"] = ("Looks like a DPA — the Art. 28 clause-by-clause "
                            "review comes with Lightwork. Attached for human "
                            "review.")
    elif text.strip():
        entry["summary"] = (f"Text extracted ({len(text):,} chars); no DPA "
                            "clause set detected — attached for the record.")
    else:
        entry["re_review"] = True
        entry["summary"] = ("Not machine-readable (scanned or unsupported "
                            "format) — attached; human review required.")
    # Retain the uploaded bytes on the vendor's mock record so the addendum
    # row in the pop-out is openable, like every other filed document.
    try:
        entry["attachment_id"] = _ot_mock.seed_attachment(
            ot.subject, filename, data, mime, by="Reviewer upload")
    except Exception:  # pragma: no cover -- viewer convenience, never fatal
        entry["attachment_id"] = ""
    ot.addenda.append(entry)
    if entry["re_review"]:
        ot.needs_re_review = True
    _audit("ASSESSMENT_ADDENDUM", ANALYST, gid, subject=ot.subject,
           ot_id=ot.assessment_id, artifact=filename,
           re_review=entry["re_review"], gaps=len(entry["gaps"]))
    if gid:
        _post_event(gid, ANALYST, "artifact",
                    f"Addendum on {ot.assessment_id}: {filename} — "
                    f"{entry['summary']}")
    return entry


async def _read_upload_bounded(file: UploadFile) -> tuple[bytes | None, str]:
    data = await file.read(backend.MAX_FILE_BYTES + 1)
    if len(data) > backend.MAX_FILE_BYTES:
        return None, f"file too large (limit {backend.MAX_FILE_BYTES} bytes)"
    return data, ""


@app.post("/onetrust/record/{ot_id}/add-document")
async def onetrust_add_document(ot_id: str, file: UploadFile) -> JSONResponse:
    """"We've already done the assessment — we're just adding stuff": append
    a document to an existing OneTrust record, analyze it, and flag whether
    the addition warrants a re-review."""
    ot = STORE.onetrust.get(ot_id)
    if not ot:
        return JSONResponse({"ok": False, "error": "unknown assessment"},
                            status_code=404)
    data, err = await _read_upload_bounded(file)
    if data is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    mime = file.content_type or "application/octet-stream"
    filename = file.filename or "document"
    case = STORE.cases.get(ot.case_id)
    if case is not None:
        ok, msg = await asyncio.to_thread(
            _store_evidence, case, filename, data, mime, via="onetrust addendum")
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
    entry = await asyncio.to_thread(_analyze_addendum, ot, case, filename,
                                    data, mime)
    return JSONResponse({"ok": True, "addendum": entry,
                         "needs_re_review": ot.needs_re_review})


@app.post("/intake/{case_id}/append-doc")
async def intake_append_doc(case_id: str, file: UploadFile) -> JSONResponse:
    """Chat-side append: the requester says they're just adding documents to
    a vendor we've already assessed. The file lands on the EXISTING OneTrust
    record as an analyzed addendum; no second interview."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False}, status_code=404)
    ot = _latest_prior_record(case.subject)
    if ot is None:
        return JSONResponse({"ok": False, "error": "no prior assessment for "
                             f"{case.subject}"}, status_code=404)
    data, err = await _read_upload_bounded(file)
    if data is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    mime = file.content_type or "application/octet-stream"
    filename = file.filename or "document"
    ok, msg = await asyncio.to_thread(
        _store_evidence, case, filename, data, mime, via="append to "
        + ot.assessment_id)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    entry = await asyncio.to_thread(_analyze_addendum, ot, case, filename,
                                    data, mime)
    gid = getattr(case, "goal_id", None)
    if case.stage == "intake_sent":
        case.stage = "addendum_filed"
        case.log(f"Append-only: document(s) added to {ot.assessment_id}.")
        if gid:
            await asyncio.to_thread(
                _world().set_goal_status, gid, "done",
                f"Append-only request: document(s) analyzed and attached to "
                f"{ot.assessment_id}; no new interview needed.")
        ticket = STORE.tickets.get(case.ticket_number)
        if ticket:
            ticket.state = "Resolved"
            ticket.note(f"Document appended to existing assessment "
                        f"{ot.assessment_id} — {entry['summary']}")
    return JSONResponse({"ok": True, "ot_id": ot.assessment_id,
                         "addendum": entry,
                         "needs_re_review": ot.needs_re_review})


# ===========================================================================
# Reviewer desk — a reviewer's own queue, and the vendor-paper round
# ===========================================================================
@app.get("/reviewer", response_class=HTMLResponse)
async def reviewer_desk(request: Request, who: str = "") -> HTMLResponse:
    """Sign in as a reviewer and get straight to your numbered queue."""
    paper_desk.assign_unassigned(list(_REVIEWERS))
    names = paper_desk.reviewers() or list(_REVIEWERS)
    who = (who or "").strip()
    return _page(request, "reviewer.html", reviewers=names, who=who,
                 queue=paper_desk.queue_for(who) if who else [],
                 paper_caps=CAPS["paper_redline"])


@app.get("/reviewer/case/{case_id}", response_class=HTMLResponse)
async def reviewer_case(request: Request, case_id: str,
                        who: str = "") -> HTMLResponse:
    case = STORE.cases.get(case_id)
    if not case:
        return _page(request, "notfound.html")
    return _page(request, "reviewer_case.html", case=case,
                 who=(who or case.assigned_to),
                 rounds=case.paper_reviews,
                 paper_caps=CAPS["paper_redline"])


@app.post("/reviewer/case/{case_id}/paper-source")
async def reviewer_paper_source(case_id: str, source: str = Form(...),
                                who: str = Form("")) -> JSONResponse:
    """Question one: our paper, or theirs? On OUR paper the desk drafts our
    template for this vendor (values filled in red) and files it; on theirs
    it asks for the document to review."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False, "error": "unknown case"},
                            status_code=404)
    choice = (source or "").strip().lower()
    if choice not in paper_desk.PAPER_SOURCES:
        return JSONResponse(
            {"ok": False, "error": "answer must be 'ours' or 'theirs'"},
            status_code=400)
    case.paper_source = choice
    if choice == "ours":
        if not CAPS["paper_redline"]:
            case.log("Vendor is on OUR paper — no third-party document to "
                     "review.")
            return JSONResponse(
                {"ok": True, "source": choice, "upload": False,
                 "message": "On our paper — nothing to redline. Our template "
                            "already governs."})
        reviewer = (who or case.assigned_to or "Privacy Reviewer").strip()
        entry = await asyncio.to_thread(paper_desk.file_our_paper, case,
                                        reviewer=reviewer)
        if entry is None:
            return JSONResponse(
                {"ok": True, "source": choice, "upload": False,
                 "message": "On our paper — nothing to redline. Our template "
                            "already governs."})
        gid = getattr(case, "goal_id", None)
        _audit("TOOL_CALL", ANALYST, gid, tool="paper_draft", case=case_id,
               vendor=case.subject, instrument=entry["instrument"],
               version=entry["version"], filled=len(entry["filled"]))
        if gid:
            _post_event(gid, ANALYST, "artifact",
                        f"Drafted our {entry['instrument_label']} for "
                        f"{case.subject} (v{entry['version']}): "
                        f"{len(entry['filled'])} value(s) auto-filled in red, "
                        f"filed to the vendor record.")
        return JSONResponse(
            {"ok": True, "source": choice, "upload": False, "round": entry,
             "message": f"On our paper — I've drafted our "
                        f"{entry['instrument_label']} for {case.subject} as "
                        f"v{entry['version']} and filed it to the vendor "
                        "record. The vendor-specific values are in red — "
                        "verify them before it goes out for signature."})
    case.log("Vendor is on THEIR paper — document requested for review.")
    return JSONResponse({"ok": True, "source": choice, "upload": True,
                         "message": "Please upload their document (.docx or "
                                    ".pdf) and I'll compare it to our "
                                    "template."})


@app.post("/reviewer/case/{case_id}/paper-upload")
async def reviewer_paper_upload(case_id: str, file: UploadFile,
                                who: str = Form(""),
                                use_model: str = Form("1")) -> JSONResponse:
    """Read their paper, decide the instrument, compare it to our template,
    write the memo + tracked-changes redline, and file both onto the vendor
    record under the next sequential version."""
    case = STORE.cases.get(case_id)
    if not case:
        return JSONResponse({"ok": False, "error": "unknown case"},
                            status_code=404)
    if not CAPS["paper_redline"]:
        return JSONResponse(
            {"ok": False, "error": "vendor-paper redline ships with the "
                                   "Lightwork platform"}, status_code=501)
    filename = file.filename or "vendor-paper"
    mime = file.content_type or "application/octet-stream"
    if not paper_desk.accepted_upload(filename, mime):
        return JSONResponse(
            {"ok": False, "error": "only Word (.docx) and PDF documents can "
                                   "be reviewed"}, status_code=400)
    data, err = await _read_upload_bounded(file)
    if data is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    ok, msg = await asyncio.to_thread(
        _store_evidence, case, filename, data, mime, via="vendor paper")
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    text, extraction = await asyncio.to_thread(
        backend.document_text, data, mime)
    if not (text or "").strip():
        return JSONResponse(
            {"ok": False, "error": "no text could be read from that file — a "
                                   "scanned PDF needs OCR before review",
             "extraction": extraction}, status_code=400)

    reviewer = (who or case.assigned_to or "Privacy Reviewer").strip()
    review = await asyncio.to_thread(
        backend.review_vendor_paper, text, vendor=case.subject,
        document_name=filename, use_model=(use_model != "0"))
    redline = await asyncio.to_thread(
        backend.build_redline, review, original=data, mime=mime,
        date=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        title=f"{case.subject} — {review.to_dict()['instrument_label']} "
              "(Lightwork redline)")
    entry = await asyncio.to_thread(
        paper_desk.file_paper_review, case, review, redline,
        reviewer=reviewer)

    gid = getattr(case, "goal_id", None)
    _audit("TOOL_CALL", ANALYST, gid, tool="paper_review", case=case_id,
           vendor=case.subject, instrument=review.instrument,
           version=entry["version"], gaps=entry["gaps"])
    if gid:
        _post_event(gid, ANALYST, "observation",
                    f"Reviewed {case.subject}'s {entry['instrument_label']} "
                    f"(v{entry['version']}): {entry['gaps']} departure(s) from "
                    f"our template, {entry['high']} high severity.")
    return JSONResponse({"ok": True, "round": entry,
                         "review": review.to_dict(),
                         "extraction": extraction})


@app.get("/reviewer/case/{case_id}/round/{version}/report")
async def reviewer_round_report(case_id: str, version: int) -> Response:
    """The analysis memo as filed — same bytes that went to OneTrust."""
    case = STORE.cases.get(case_id)
    if not case:
        return PlainTextResponse("unknown case", status_code=404)
    for entry in case.paper_reviews:
        if entry["version"] == version:
            return PlainTextResponse(entry["report"])
    return PlainTextResponse("unknown version", status_code=404)


@app.get("/reviewer/case/{case_id}/round/{version}/redline")
async def reviewer_round_redline(case_id: str, version: int) -> Response:
    """The tracked-changes .docx as filed — the reviewer opens it in Word
    without a trip through the tenant."""
    case = STORE.cases.get(case_id)
    if not case:
        return PlainTextResponse("unknown case", status_code=404)
    for entry in case.paper_reviews:
        if entry["version"] == version:
            content = paper_desk.redline_bytes(
                case.id, entry["instrument"], version)
            if not content:
                return PlainTextResponse("no redline for this round",
                                         status_code=404)
            filename = entry.get("redline_filename") or "redline.docx"
            return Response(
                content, media_type=paper_desk.DOCX_MIME,
                headers={"Content-Disposition":
                         f'attachment; filename="{filename}"'})
    return PlainTextResponse("unknown version", status_code=404)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "cases": len(STORE.cases),
                        "tickets": len(STORE.tickets), "filed": len(STORE.onetrust)})


@app.get("/value.json")
async def value_json() -> JSONResponse:
    """The agent's counted value ledger — sums the per-case value recorded
    when each protocol run filed (never re-estimated after the fact). The
    partner fleet console rolls these up across deployments."""
    cases = [c for c in STORE.cases.values() if c.value]
    return JSONResponse({
        "agent": "pia-concierge", "version": AGENT_VERSION,
        "cases": len(cases),
        "hours": round(sum(c.value["hours"] for c in cases), 1),
        "dollars": round(sum(c.value["dollars"] for c in cases), 2),
        "privacy_hours": round(sum(c.value["privacy_hours"]
                                   for c in cases), 1),
        "business_hours": round(sum(c.value["business_hours"]
                                    for c in cases), 1),
    })
