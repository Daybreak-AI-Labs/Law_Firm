"""Shared in-memory state for the PIA Concierge demo.

Holds the demo's cases, the mock ServiceNow ticket table, and the mock OneTrust
assessment table. Deliberately in-memory: a demo restart is a clean slate, and
there is no data worth persisting. Real platform state (the assessment scoring,
the signed audit chain) lives in the actual maverick modules, not here.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

_LOCK = threading.RLock()


def _now() -> float:
    return time.time()


@dataclass
class Ticket:
    """A mock ServiceNow record: PIA/privacy-assessment intake."""
    number: str
    short_description: str
    requester: str
    requester_email: str
    system_name: str
    data_types: str
    state: str = "New"          # New -> In Progress -> Resolved
    work_notes: list[dict] = field(default_factory=list)
    case_id: str = ""
    created_at: float = field(default_factory=_now)

    def note(self, text: str, by: str = "PIA Concierge (agent)") -> None:
        self.work_notes.append({"at": _now(), "by": by, "text": text})


@dataclass
class OneTrustAssessment:
    """A mock OneTrust assessment record.

    Filed by the agent as "Under Review" the moment analysis completes; the
    human decision (Approve / Send back) happens INSIDE this tenant and is
    mirrored onto the Lightwork approval row + signed audit chain."""
    assessment_id: str
    name: str
    template: str
    subject: str
    status: str                 # Under Review -> Completed | Sent back
    risk_level: str
    result: dict[str, Any]
    case_id: str = ""
    controls: list[dict] = field(default_factory=list)
    # Documents appended AFTER the assessment existed ("we're just adding
    # stuff"): [{at, filename, summary, gaps, re_review, dpa_review_id}].
    addenda: list[dict] = field(default_factory=list)
    needs_re_review: bool = False
    # Wire-protocol state (the mock tenant enforces the verified shapes):
    # export-shaped sections/questions, per-question response entries, and
    # the launch metadata a real tenant requires.
    questions: list[dict] = field(default_factory=list)
    responses: dict[str, list[dict]] = field(default_factory=dict)
    respondent: str = ""
    org_group: str = ""
    submitted_on: str = ""
    decided_by: str = ""
    decided_at: float = 0.0
    filed_at: float = field(default_factory=_now)


@dataclass
class Case:
    """The concierge's view of one PIA in flight."""
    id: str
    ticket_number: str
    subject: str                # the system under assessment
    requester: str
    requester_email: str
    data_types: str
    stage: str = "intake_sent"  # intake_sent -> interview_done -> pending_review
                                # -> approved / rejected -> filed
    answers: dict[str, dict] = field(default_factory=dict)
    uploads: list[str] = field(default_factory=list)
    # Delegated question sets: token -> {qids, to_name, to_email, note, answered}.
    delegations: dict[str, dict] = field(default_factory=dict)
    # The saved maverick.assessment record id (set at submit) — the dashboard
    # review pop-out and the follow-up loop key on it.
    assessment_id: str = ""
    result: dict[str, Any] | None = None
    controls: list[dict] = field(default_factory=list)
    approval_id: str = ""
    decided_by: str = ""
    onetrust_id: str = ""
    # The privacy reviewer this case sits with. The reviewer desk lists a
    # person's own queue off this field; empty means unassigned.
    assigned_to: str = ""
    # Whose contract is on the table: "" (not asked) | "ours" | "theirs".
    paper_source: str = ""
    # Filed vendor-paper redlines, newest last:
    # [{version, instrument, filename, redline_filename, gaps, high, at,
    #   recommendation, attachment_ids}].
    paper_reviews: list[dict] = field(default_factory=list)
    # The counted value ledger for this case, set when the protocol run
    # files it (value_ledger.case_value output) — /value.json sums these.
    value: dict[str, Any] | None = None
    created_at: float = field(default_factory=_now)
    submitted_at: float = 0.0
    events: list[dict] = field(default_factory=list)

    def log(self, text: str) -> None:
        self.events.append({"at": _now(), "text": text})


class Store:
    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self.cases: dict[str, Case] = {}
        self.onetrust: dict[str, OneTrustAssessment] = {}
        self._seq = {"ticket": 1000, "case": 1, "ot": 1}
        # Sequential document versions per "<vendor>|<instrument>". A vendor's
        # paper is renegotiated in rounds, so every filed redline gets the next
        # version for that pairing and the history stays readable in OneTrust.
        self._paper_seq: dict[str, int] = {}

    def next_paper_version(self, vendor: str, instrument: str) -> int:
        key = f"{(vendor or '').strip().lower()}|{instrument}"
        with _LOCK:
            n = self._paper_seq.get(key, 0) + 1
            self._paper_seq[key] = n
            return n

    def paper_version_count(self, vendor: str, instrument: str) -> int:
        return self._paper_seq.get(
            f"{(vendor or '').strip().lower()}|{instrument}", 0)

    def next_doc_version(self, vendor: str, kind: str) -> int:
        """Sequential versions for NON-paper deliverables (risk analysis,
        notice cross-check) per vendor+kind, so the Documents tab reads as a
        history of legible names, never a pile of case-id files. Namespaced
        into the same counter map as the paper stream."""
        return self.next_paper_version(vendor, f"doc:{kind}")

    def next_ticket_number(self) -> str:
        with _LOCK:
            self._seq["ticket"] += 1
            return f"PRV{self._seq['ticket']:07d}"

    def next_case_id(self) -> str:
        with _LOCK:
            n = self._seq["case"]
            self._seq["case"] += 1
            return f"LW-PIA-{n:04d}"

    def next_onetrust_id(self) -> str:
        with _LOCK:
            n = self._seq["ot"]
            self._seq["ot"] += 1
            return f"OT-ASMT-{n:04d}"


STORE = Store()
