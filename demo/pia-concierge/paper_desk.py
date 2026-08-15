"""The reviewer desk: a privacy reviewer's own queue, and the vendor-paper
round that produces a filed redline.

A reviewer signs in and is shown only the assessments assigned to them,
numbered, because "which of my things do I pick up next" is the first question
of the working day and every extra click is friction. Choosing a number opens
one case and asks the question that actually determines the next hour of work:
*is the vendor on our paper, or theirs?*

On our paper the round is done — our template already says what it says. On
theirs, the reviewer uploads the document and the desk performs the review a
human would otherwise do by hand:

1. read the file (``.docx`` or a digital PDF),
2. decide which instrument it is (DPA vs Addendum),
3. compare it clause by clause against our standard positions,
4. write two deliverables -- an analysis memo of the concerns, and the vendor's
   own document marked up with real Word tracked changes,
5. file both onto the vendor's record in OneTrust under the next sequential
   version for that vendor and instrument.

The version counter is per vendor+instrument, not per case: a paper gets
renegotiated across rounds, and the point of the numbering is that the
Documents tab reads as a legible history (``v1``, ``v2``, ``v3``) of one
negotiation rather than a pile of same-named files.

Judgement stays deterministic here (see :mod:`maverick.paper_review`); this
module only orchestrates and files.
"""
from __future__ import annotations

import time

import backend
from store import STORE, Case

# The instrument a reviewer is asked about first.
PAPER_SOURCES = ("ours", "theirs")

DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")
ACCEPTED_MIMES = (DOCX_MIME, "application/pdf")
ACCEPTED_SUFFIXES = (".docx", ".pdf")


def accepted_upload(filename: str, mime: str) -> bool:
    """Word and PDF only. Browsers are inconsistent about the .docx mime, so a
    correct suffix is accepted even when the mime is generic."""
    name = (filename or "").lower()
    if mime in ACCEPTED_MIMES:
        return True
    return name.endswith(ACCEPTED_SUFFIXES)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --- the queue ------------------------------------------------------------

def reviewers() -> list[str]:
    """Every reviewer who currently holds work, plus anyone seeded."""
    names = {c.assigned_to for c in STORE.cases.values() if c.assigned_to}
    return sorted(names)


def queue_for(reviewer: str) -> list[dict]:
    """One reviewer's open assessments, numbered from 1.

    Ordered oldest-first: the queue is a worklist, so the thing that has been
    waiting longest comes up first rather than the thing most recently filed."""
    name = (reviewer or "").strip()
    mine = [c for c in STORE.cases.values()
            if c.assigned_to == name and c.stage not in ("filed",)]
    mine.sort(key=lambda c: c.created_at)
    out = []
    for i, case in enumerate(mine, start=1):
        out.append({
            "n": i,
            "case_id": case.id,
            "ticket": case.ticket_number,
            "subject": case.subject,
            "requester": case.requester,
            "stage": case.stage,
            "paper_source": case.paper_source,
            "paper_rounds": len(case.paper_reviews),
            "age_days": round((time.time() - case.created_at) / 86400.0, 1),
        })
    return out


def case_by_number(reviewer: str, n: int) -> Case | None:
    """Resolve the number the reviewer typed against THEIR queue -- numbers are
    per-reviewer, so one reviewer's "2" can never open another's case."""
    for row in queue_for(reviewer):
        if row["n"] == n:
            return STORE.cases.get(row["case_id"])
    return None


def assign_unassigned(names: list[str]) -> int:
    """Round-robin any unassigned case across ``names`` so a fresh demo tenant
    opens with real queues. Returns how many were assigned."""
    if not names:
        return 0
    pending = sorted((c for c in STORE.cases.values() if not c.assigned_to),
                     key=lambda c: c.created_at)
    for i, case in enumerate(pending):
        case.assigned_to = names[i % len(names)]
    return len(pending)


# --- the analysis memo ----------------------------------------------------

_STATUS_LABEL = {
    "missing": "MISSING",
    "conflicting": "CONFLICTS WITH OUR POSITION",
    "unclear": "UNCLEAR / NEGATED",
    "present": "meets our position",
}

_RECOMMENDATION_LABEL = {
    "do_not_sign_without_changes":
        "DO NOT SIGN as drafted — high-severity gaps must be closed first.",
    "acceptable_with_changes":
        "Acceptable once the attached tracked changes are accepted.",
    "acceptable": "Acceptable — no departures from our standard position.",
}


def analysis_report(review, *, version: int, reviewer: str,
                    redline_filename: str, redline_result,
                    closed_from_previous: list | None = None,
                    previous_version: int = 0) -> str:
    """The reviewer-facing memo that goes on the vendor record beside the
    redline. Plain text on purpose: it has to be readable in OneTrust's
    document preview, and it must state what was NOT done as clearly as what
    was."""
    d = review.to_dict()
    lines = [
        f"VENDOR PAPER REVIEW — {review.vendor}  (v{version})",
        "=" * 68,
        f"Document reviewed : {review.document_name}",
        f"Instrument        : {d['instrument_label']} "
        f"({d['classification'].get('confidence', 'unknown')} confidence)",
        f"                    {d['classification'].get('reason', '')}",
        f"Reviewed by       : {reviewer}",
        f"Reviewed at       : {_iso(time.time())}",
        f"Clause coverage   : {d['clauses_present']} of {d['clauses_total']} "
        "meet our standard position",
        f"Departures        : {d['gaps']} "
        f"({d['high_severity_gaps']} high severity)",
        "",
        "RECOMMENDATION",
        "-" * 68,
        _RECOMMENDATION_LABEL.get(d["recommendation"], d["recommendation"]),
        "",
    ]
    if closed_from_previous is not None and previous_version:
        lines += ["NEGOTIATION PROGRESS", "-" * 68]
        if closed_from_previous:
            lines.append(
                f"This draft closes {len(closed_from_previous)} of the "
                f"change(s) we demanded in v{previous_version}:")
            lines += [f"   + {c}" for c in closed_from_previous]
        else:
            lines.append(
                f"None of the changes we demanded in v{previous_version} "
                "were accepted in this draft.")
        if d["gaps"]:
            lines.append(f"{d['gaps']} demand(s) remain open — see CONCERNS.")
        lines.append("")
    lines += [
        "CONCERNS",
        "-" * 68,
    ]
    gaps = review.gaps
    if not gaps:
        lines.append("None. Every checked clause meets our standard position.")
    for i, c in enumerate(gaps, start=1):
        lines += [
            f"{i}. [{c.severity.upper()}] {c.requirement}",
            f"   Basis          : {c.citation}",
            f"   Status         : {_STATUS_LABEL.get(c.status, c.status)}",
        ]
        if c.their_language:
            lines.append(f"   Their wording  : {c.their_language[:300]}")
        source = ("model-tailored to their drafting"
                  if c.drafted_by == "model" else "our standard template")
        lines += [
            f"   Required change: {c.proposed[:600]}",
            f"   Language source: {source}",
            "",
        ]
    lines += [
        "TRACKED-CHANGES DOCUMENT",
        "-" * 68,
        f"Attached as: {redline_filename}",
    ]
    if redline_result is not None:
        r = redline_result.to_dict()
        lines.append(
            f"{r['applied']} clause(s) marked up in place, "
            f"{r['inserted']} clause(s) proposed as additions.")
        if r["synthesized"]:
            lines.append(
                "The vendor supplied a PDF, so the redline is a reconstruction "
                "of the text we could read; their original file was not "
                "modified.")
        if r["unmatched"]:
            lines.append(
                f"{len(r['unmatched'])} required change(s) could NOT be "
                "anchored to a paragraph and must be applied by hand:")
            lines += [f"   - {u}" for u in r["unmatched"]]
    lines += [
        "",
        "HOW TO READ THIS",
        "-" * 68,
        "Gap findings are produced deterministically from our clause "
        "checklists — a model cannot create or clear a finding. Where a model "
        "was available it only re-drafted our required language to match the "
        "vendor's defined terms, and any draft that weakened our position was "
        "discarded in favour of the template wording.",
    ]
    return "\n".join(lines)


# --- filing ---------------------------------------------------------------

# Redline bytes by (case_id, instrument, version), so the desk can hand the
# reviewer the exact .docx that was filed. Kept OUT of the round entry itself:
# entries are serialized into the case page, and binary doesn't belong there.
_REDLINES: dict[tuple[str, str, int], bytes] = {}


def redline_bytes(case_id: str, instrument: str, version: int) -> bytes:
    return _REDLINES.get((case_id, instrument, version), b"")


def _safe(part: str) -> str:
    keep = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in (part or "")]
    return "".join(keep).strip("-")[:60] or "vendor"


def file_paper_review(case: Case, review, redline_result, *, reviewer: str,
                      client=None) -> dict:
    """Attach the memo and the tracked-changes document to the vendor's record
    under the next sequential version, and record the round on the case.

    Returns the round entry. Attachment failures are reported in the entry
    rather than raised: the review itself is still valuable, and the reviewer
    needs to know precisely which half did not land."""
    vendor_name = case.subject
    version = STORE.next_paper_version(vendor_name, review.instrument)
    stem = f"{_safe(vendor_name)}-{review.instrument}-v{version}"
    report_name = f"{stem}-analysis.txt"
    redline_name = f"{stem}-redline.docx"

    # The negotiation round-trip: when the vendor sends a renegotiated draft,
    # diff its gaps against the PREVIOUS round for this instrument so the
    # memo says which of our demanded changes they accepted. Requirement
    # labels come from the previous entry itself -- a closed gap has no
    # concern object in the current review.
    prev = next((e for e in reversed(case.paper_reviews)
                 if e.get("instrument") == review.instrument
                 and e.get("kind") != "our_paper"), None)
    closed: list | None = None
    prev_version = 0
    if prev is not None:
        prev_version = int(prev.get("version") or 0)
        prev_reqs = prev.get("gap_reqs") or {}
        now_keys = {c.clause_key for c in review.gaps}
        closed = sorted(str(prev_reqs[k]) for k in
                        set(prev_reqs) - now_keys)

    report = analysis_report(review, version=version, reviewer=reviewer,
                             redline_filename=redline_name,
                             redline_result=redline_result,
                             closed_from_previous=closed,
                             previous_version=prev_version)

    attachments: list[str] = []
    error = ""
    try:
        client = client or backend.onetrust_client()
        vendor = client.ensure_vendor(vendor_name)
        attachments.append(client.attach_to_record(
            vendor["id"], report_name, report.encode("utf-8"),
            mime="text/plain"))
        if redline_result is not None:
            attachments.append(client.attach_to_record(
                vendor["id"], redline_name, redline_result.content,
                mime=DOCX_MIME))
    except Exception as e:  # pragma: no cover -- tenant/network variance
        error = f"{type(e).__name__}: {e}"

    if redline_result is not None:
        _REDLINES[(case.id, review.instrument, version)] = \
            redline_result.content

    d = review.to_dict()
    entry = {
        "version": version,
        "instrument": review.instrument,
        "instrument_label": d["instrument_label"],
        "filename": review.document_name,
        "report_filename": report_name,
        "redline_filename": redline_name,
        "clauses_present": d["clauses_present"],
        "clauses_total": d["clauses_total"],
        "gaps": d["gaps"],
        "high": d["high_severity_gaps"],
        "recommendation": d["recommendation"],
        "drafted_with_model": d["drafted_with_model"],
        "reviewer": reviewer,
        "at": time.time(),
        "has_redline": redline_result is not None,
        "attachment_ids": attachments,
        "filed": bool(attachments) and not error,
        "error": error,
        "report": report,
        # Round-trip bookkeeping: what this round demands (for the NEXT round
        # to diff against) and what this round settled from the previous one.
        "gap_reqs": {c.clause_key: c.requirement for c in review.gaps},
        "closed_from_previous": closed or [],
        "previous_version": prev_version,
    }
    case.paper_reviews.append(entry)
    case.log(f"Vendor paper reviewed ({d['instrument_label']} v{version}): "
             f"{d['gaps']} departure(s), {d['high_severity_gaps']} high — "
             f"filed to the vendor record as {redline_name}.")
    return entry


def file_our_paper(case: Case, *, reviewer: str, client=None) -> dict | None:
    """The vendor signs OUR paper: draft our template instrument with this
    vendor's values filled **in red**, and file it onto the vendor's record
    under the next sequential version.

    Deterministic end to end -- the clause language is the operator's
    playbook; the only things inserted are the party names and date, and
    every insertion is listed in the cover note and rendered red in the
    document so counsel verifies exactly what the machine filled in.
    Returns None off-platform (standalone has no Word engine)."""
    draft = backend.draft_our_paper(case.subject, instrument="dpa")
    if draft is None:
        return None
    vendor_name = case.subject
    version = STORE.next_paper_version(vendor_name, draft.instrument)
    stem = f"{_safe(vendor_name)}-{draft.instrument}-v{version}"
    draft_name = f"{stem}-our-paper.docx"
    note_name = f"{stem}-our-paper-note.txt"
    d = draft.to_dict()
    note = "\n".join([
        f"OUR PAPER DRAFT — {vendor_name}  (v{version})",
        "=" * 68,
        f"Instrument     : {d['instrument_label']}",
        "Drafted by     : Lightwork (deterministic; no model)",
        f"Drafted at     : {_iso(time.time())}",
        f"Prepared for   : {reviewer}",
        f"Clauses        : {d['clause_count']}, from the operator's clause "
        "playbook",
        "",
        "AUTO-FILLED VALUES (rendered in red in the document)",
        "-" * 68,
        *[f"   - {f}" for f in d["filled"]],
        "",
        "Verify every red value before signature. All non-red language is "
        "our standard position; changing it re-opens clause review.",
    ])

    attachments: list[str] = []
    error = ""
    try:
        client = client or backend.onetrust_client()
        vendor = client.ensure_vendor(vendor_name)
        attachments.append(client.attach_to_record(
            vendor["id"], note_name, note.encode("utf-8"),
            mime="text/plain"))
        attachments.append(client.attach_to_record(
            vendor["id"], draft_name, draft.content, mime=DOCX_MIME))
    except Exception as e:  # pragma: no cover -- tenant/network variance
        error = f"{type(e).__name__}: {e}"

    _REDLINES[(case.id, draft.instrument, version)] = draft.content
    entry = {
        "kind": "our_paper",
        "version": version,
        "instrument": draft.instrument,
        "instrument_label": d["instrument_label"],
        "filename": "our template",
        "report_filename": note_name,
        "redline_filename": draft_name,
        "clauses_present": d["clause_count"],
        "clauses_total": d["clause_count"],
        "gaps": 0,
        "high": 0,
        "recommendation": "our_paper_governs",
        "drafted_with_model": False,
        "reviewer": reviewer,
        "at": time.time(),
        "has_redline": True,
        "attachment_ids": attachments,
        "filed": bool(attachments) and not error,
        "error": error,
        "report": note,
        "filled": d["filled"],
    }
    case.paper_reviews.append(entry)
    case.log(f"Our-paper draft filed ({d['instrument_label']} v{version}): "
             f"{len(d['filled'])} value(s) auto-filled in red — "
             f"{draft_name} on the vendor record.")
    return entry


__all__ = ["PAPER_SOURCES", "DOCX_MIME", "ACCEPTED_MIMES", "accepted_upload",
           "reviewers", "queue_for", "case_by_number", "assign_unassigned",
           "analysis_report", "file_paper_review", "file_our_paper",
           "redline_bytes"]
