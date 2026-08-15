"""The seam between the PIA Concierge agent and the Lightwork platform.

Every capability the app needs is resolved here to either the real
``maverick`` implementation (platform build) or a self-contained standalone
implementation (sold-on-its-own build). The app imports from this module and
never branches on the mode itself.

Standalone loses, by design: the governed engine and control catalog, the
signed audit chain, world-model governance, learning, connectors, and the
LLM assist. What survives is the agent: scoring, OneTrust filing + review,
document append, and vendor memory.
"""
from __future__ import annotations

import os
from typing import Any

from capabilities import CAPS, STANDALONE

# --------------------------------------------------------------------------- #
# Engine: the scorer + record store.
# --------------------------------------------------------------------------- #
if STANDALONE:
    from pia_engine import (  # noqa: F401
        AssessmentSession,
        add_followups,
        answer_followup,
        decide_assessment,
        get_template,
        list_saved,
        list_templates,
        load_saved,
        save_session,
        template_department,
    )
    DEFAULT_DB = None
else:
    from maverick.assessment import (  # noqa: F401
        AssessmentSession,
        add_followups,
        answer_followup,
        decide_assessment,
        get_template,
        list_saved,
        list_templates,
        load_saved,
        save_session,
        template_department,
    )
    from maverick.world_model import DEFAULT_DB  # noqa: F401


# --------------------------------------------------------------------------- #
# World-model governance (goals + approval queue) — platform only.
# --------------------------------------------------------------------------- #
def world():
    """The shared world model, or None when there is no platform to govern
    against. Callers must treat None as 'no governed goal/approval here'."""
    if STANDALONE:
        return None
    from maverick.world_model import DEFAULT_DB as _db
    from maverick.world_model import WorldModel
    return WorldModel(_db)


# --------------------------------------------------------------------------- #
# Signed audit chain — platform only (no audit trail standalone, by choice).
# --------------------------------------------------------------------------- #
def audit_record(kind: str, *, agent: str = "", goal_id: int | None = None,
                 **payload: Any) -> None:
    if not CAPS["signed_audit"]:
        return
    try:
        from maverick.audit import record
        record(kind, agent=agent, goal_id=goal_id, **payload)
    except Exception as exc:  # pragma: no cover - demo resilience
        print(f"[audit] {kind} failed: {exc}")


# --------------------------------------------------------------------------- #
# Control mapping with framework citations — platform only.
# --------------------------------------------------------------------------- #
def find_controls(text: str, limit: int = 2) -> list:
    if not CAPS["control_catalog"]:
        return []
    from maverick.controls import find_controls as _fc
    return _fc(text, limit=limit)


# --------------------------------------------------------------------------- #
# Email — real SMTP tool on the platform; in-process delivery standalone.
# --------------------------------------------------------------------------- #
def send_email(to: str, subject: str, body: str) -> str:
    """Synchronous send, branded: the agent's plain text rides verbatim as
    the first MIME part with a styled text/html alternative
    (``mail_style.email_html``) beside it. Standalone delivers straight into
    the in-process capture inbox; the platform build composes the multipart
    message here and speaks the same SMTP(+STARTTLS) the governed email tool
    does — the tool's own composer is text/plain-only — reusing its config
    resolution and mirroring its guards and result strings."""
    to = to or "requester@example.com"
    # The composer is resolved by name so it stays OPTIONAL: the standalone
    # container ships a fixed module list (Dockerfile.standalone) that does
    # not include mail_style.py — without it, mail degrades to plain text
    # rather than failing the send (and the deployment-kit import scan
    # rightly keeps treating it as not-required).
    try:
        from importlib import import_module
        html_alt = import_module("mail_style").email_html(subject, body)
    except Exception:
        html_alt = ""
    if STANDALONE:
        import mailsink
        sender = os.environ.get("EMAIL_USER", "privacy-office@company.com")
        mailsink.INBOX.insert(0, mailsink.Captured(
            to=to, sender=sender, subject=subject, body=body, html=html_alt))
        return "ok (standalone: delivered to inbox)"
    if os.environ.get("MAVERICK_EMAIL_DISABLE") == "1":
        return "ERROR: email send disabled by MAVERICK_EMAIL_DISABLE=1"
    import smtplib
    from email.message import EmailMessage

    from maverick.tools.email_tool import _cfg
    user = _cfg("user", "EMAIL_USER")
    pw = _cfg("app_password", "EMAIL_APP_PASSWORD")
    host = _cfg("smtp_host", "EMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(_cfg("smtp_port", "EMAIL_SMTP_PORT", "465"))
    if not user or not pw:
        return ("ERROR: email requires EMAIL_USER + EMAIL_APP_PASSWORD "
                "(use an app password, NOT your account password).")
    if not subject:
        return "ERROR: send requires `to` and `subject`"
    for name, value in (("to", to), ("subject", subject)):
        if "\r" in value or "\n" in value:
            return f"ERROR: newline in email `{name}` (header injection blocked)"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)                  # plain text stays the first part
    if html_alt:
        msg.add_alternative(html_alt, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
    except Exception as exc:
        return f"ERROR: smtp send failed: {type(exc).__name__}: {exc}"
    return f"sent to {to} (subject: {subject[:60]})"


# --------------------------------------------------------------------------- #
# OneTrust — the verified wire client (platform and standalone alike; the
# protocol was proven against a live tenant, see ONETRUST-INTEGRATION.md).
# Point ONETRUST_HOSTNAME at a real tenant and the same calls file there.
# --------------------------------------------------------------------------- #
def onetrust_client():
    """A configured client for the tenant (real or the in-process mock).
    Tests patch this to bind the client to the mock over ASGI."""
    from onetrust_client import OneTrustClient
    return OneTrustClient()


# --------------------------------------------------------------------------- #
# Attachments — the upload size cap. The governed store (mime allowlist, dedup,
# executable deny) is applied inside app._store_evidence only when a goal
# exists, i.e. on the platform; standalone just records the upload metadata.
# --------------------------------------------------------------------------- #
if STANDALONE:
    MAX_FILE_BYTES = 100 * 1024 * 1024
else:
    from maverick.attachments import MAX_FILE_BYTES  # noqa: F401


# --------------------------------------------------------------------------- #
# Document discovery (connected sources) — platform + connector only.
# --------------------------------------------------------------------------- #
def discovery_available() -> bool:
    return bool(CAPS["doc_discovery"])


# --------------------------------------------------------------------------- #
# Art. 28 DPA clause review on appended documents — platform only.
# --------------------------------------------------------------------------- #
def dpa_clause_review(vendor: str, text: str, *, filename: str, mime: str,
                      data: bytes) -> dict | None:
    """Full clause-by-clause Art. 28 review (returns the saved record), or
    None when the platform engine isn't present."""
    if not CAPS["dpa_clause_review"]:
        return None
    from maverick import privacy_ops
    extraction: dict = {}
    try:
        text = privacy_ops._document_text(data, mime, _metadata=extraction) or text
    except Exception:
        pass
    return privacy_ops.review_dpa(
        vendor, text, document_name=filename, reviewed_by="pia-concierge",
        _document_evidence=privacy_ops._build_document_evidence(
            data=data, text=text, mime=mime, source="upload",
            doc_id=filename, ref=None, extraction=extraction))


# --------------------------------------------------------------------------- #
# Vendor-paper redline — the reviewer desk's engine. Platform only: the
# comparison checklists and the OOXML writer live in maverick-core.
# --------------------------------------------------------------------------- #
def document_text(data: bytes, mime: str) -> tuple[str, dict]:
    """Extracted text plus extraction metadata (method / confidence /
    review_required). Handles .docx and digital PDFs on the platform; returns
    ("", {}) when the engine isn't present."""
    if not CAPS["paper_redline"]:
        return "", {}
    from maverick import privacy_ops
    meta: dict = {}
    try:
        return (privacy_ops._document_text(data, mime, _metadata=meta) or "",
                meta)
    except Exception:  # pragma: no cover -- a bad upload is not a crash
        return "", meta


def review_vendor_paper(text: str, *, vendor: str, document_name: str,
                        instrument: str = "", use_model: bool = True):
    """Classify the instrument and compare it against our standard positions.
    Returns a ``maverick.paper_review.PaperReview`` or None off-platform."""
    if not CAPS["paper_redline"]:
        return None
    from maverick import paper_review
    return paper_review.review_paper(
        text, vendor=vendor, instrument=instrument,
        document_name=document_name, use_model=use_model)


def build_redline(review, *, original: bytes | None, mime: str, date: str,
                  title: str = ""):
    """The tracked-changes .docx: edit their file in place when they sent a
    .docx, otherwise rebuild the text we could read from their PDF. Returns a
    ``RedlineResult`` or None off-platform."""
    if not CAPS["paper_redline"]:
        return None
    from maverick import docx_redline
    edits = review.edits()
    if original and mime == docx_redline.DOCX_MIME:
        try:
            return docx_redline.redline_docx(original, edits, date=date)
        except docx_redline.RedlineError:
            pass   # fall through to a reconstruction rather than failing
    paragraphs = [p for p in (review_text_paragraphs(review)) if p]
    return docx_redline.build_redlined_docx(
        paragraphs, edits, date=date, title=title)


def review_text_paragraphs(review) -> list[str]:
    """The vendor's paragraphs as we read them, for the reconstructed redline
    path. Kept on the review so the redline and the report agree."""
    return list(getattr(review, "source_paragraphs", []) or [])


def draft_our_paper(vendor: str, *, instrument: str = "dpa"):
    """OUR template instrument filled for this vendor, values in red for
    counsel to verify. Returns a ``paper_review.OurPaperDraft`` or None
    off-platform. Deterministic -- clause language is the operator's
    playbook; no model is involved."""
    if not CAPS["paper_redline"]:
        return None
    from maverick import paper_review
    return paper_review.draft_our_paper(vendor, instrument=instrument)


def report_docx(text: str, *, title: str = "",
                subtitle: str = "") -> bytes | None:
    """A plain-text report rendered as a designed Word document, or None
    off-platform (the standalone agent files the honest ``.txt`` instead)."""
    if not CAPS["paper_redline"]:
        return None
    from maverick import docx_redline
    return docx_redline.text_to_docx(text, title=title, subtitle=subtitle)


def previous_dpa_gaps(vendor: str) -> set[str]:
    if not CAPS["dpa_clause_review"]:
        return set()
    from maverick import privacy_ops
    gaps: set[str] = set()
    try:
        for summary in privacy_ops.list_dpa_reviews():
            if ((summary.get("vendor") or "").strip().lower()
                    == vendor.strip().lower()):
                full = privacy_ops.get_dpa_review(summary["id"]) or {}
                gaps = {c["requirement"] for c in full.get("clauses", [])
                        if c.get("status") == "missing"}
    except Exception:  # pragma: no cover - demo resilience
        pass
    return gaps


# --------------------------------------------------------------------------- #
# Document text extraction (for standalone addendum summary).
# --------------------------------------------------------------------------- #
def extract_text(data: bytes, mime: str) -> str:
    if not STANDALONE:
        from maverick import privacy_ops
        try:
            return privacy_ops._document_text(data, mime) or ""
        except Exception:
            return ""
    # Standalone: only decode text-like payloads; PDFs/docx need the platform.
    m = (mime or "").split(";")[0].strip().lower()
    if m.startswith("text/") or m in ("application/json", "application/xml"):
        return data.decode("utf-8", errors="ignore")
    return ""


# --------------------------------------------------------------------------- #
# LLM-assisted chat interpretation — platform only.
# --------------------------------------------------------------------------- #
def llm_interpret(question: str, text: str) -> str | None:
    if not CAPS["llm_assist"] or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from maverick.budget import Budget
        from maverick.llm import LLM, model_for_role
        llm = LLM(model_for_role("summarizer"))
        resp = llm.complete(
            system=("You classify a respondent's reply to one yes/no privacy "
                    "questionnaire question. Answer with exactly one word: "
                    "yes, no, na, or unknown."),
            messages=[{"role": "user",
                       "content": f"Question: {question}\nReply: {text[:1000]}"}],
            budget=Budget(max_dollars=0.25, max_output_tokens=1000),
            max_tokens=10)
        word = (resp.text or "").strip().lower().split()
        return word[0] if word and word[0] in ("yes", "no", "na", "unknown") else None
    except Exception as exc:  # pragma: no cover - network/key variance
        print(f"[interpret-llm] degraded to scripted clarification: {exc}")
        return None
