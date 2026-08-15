"""Self-contained scoring engine for the STANDALONE PIA Concierge product.

This is the agent's own brain when it ships WITHOUT the Lightwork platform.
It deliberately imports nothing from ``maverick`` — a client who buys the
standalone agent gets exactly this: a deterministic yes/no/risk scorer over a
small built-in questionnaire set, persisted to a plain local JSON store.

What you DON'T get here (these are Lightwork platform benefits, gated on
buying the platform): the full governed questionnaire catalog with immutable
content-addressed releases, the control-catalog citations, the Ed25519 signed
audit chain, cross-run assessment memory / learning, and the governance
workspace. The richer engine lives in ``maverick.assessment``; this vendored
copy is intentionally minimal and may drift from it — it is a separate SKU.

The result shape mirrors ``maverick.assessment.AssessmentResult`` so the same
templates and app code render it unchanged.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ANSWERS = ("yes", "no", "na", "unknown")
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _rollup(severities: list[str]) -> str:
    if not severities:
        return "minimal"
    top = max(_SEVERITY_RANK.get(s, 1) for s in severities)
    return {3: "high", 2: "medium", 1: "low"}[top]


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    text: str
    risk_answer: str
    severity: str
    guidance: str = ""


@dataclass(frozen=True)
class Template:
    type: str
    title: str
    framework: str
    description: str
    questions: tuple[Question, ...]

    def question(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.id == qid), None)


def _q(qid, section, text, risk_answer, severity, guidance=""):
    return Question(qid, section, text, risk_answer, severity, guidance)


@dataclass(frozen=True)
class Finding:
    # Field names mirror maverick.assessment.Finding so app.py reads either
    # engine's result the same way (f.question_id, f.question, f.recommendation).
    question_id: str
    section: str
    question: str
    severity: str
    answer: str
    kind: str
    recommendation: str


@dataclass
class AssessmentResult:
    type: str
    subject: str
    risk_rating: str
    findings: list
    answered: int
    total: int
    inherent_risk: str = "minimal"
    residual_risk: str = "minimal"
    risks_in_scope: int = 0
    controls_in_place: int = 0


# --- The standalone questionnaire set --------------------------------------
# Two frameworks ship with the standalone agent: a Privacy Impact Assessment
# and a Vendor Risk Assessment. The full catalog (DPIA, LIA, CCPA/CPRA, TIA,
# AIRA, HIPAA, SOC 2, PCI DSS, the finance suite) comes with Lightwork.
_PIA = Template(
    type="pia",
    title="Privacy Impact Assessment",
    framework="ISO 29134 / GDPR Art. 35",
    description="Assess the privacy risk of a processing activity.",
    questions=(
        _q("pia_necessity", "Necessity",
           "Is the personal data collected strictly necessary for the stated purpose?",
           "no", "high", "Minimize collection to what the purpose requires (Art. 5(1)(c))."),
        _q("pia_lawful_basis", "Lawful basis",
           "Is there a documented lawful basis for the processing (Art. 6)?",
           "no", "high", "Identify and record a lawful basis before processing."),
        _q("pia_special_category", "Lawful basis",
           "Does it process special-category data (health, biometrics, etc.) without an Art. 9 condition?",
           "yes", "high", "Establish an Art. 9 condition or stop processing special-category data."),
        _q("pia_transparency", "Transparency",
           "Are data subjects informed of the processing (privacy notice, Art. 13/14)?",
           "no", "medium", "Provide a clear privacy notice at or before collection."),
        _q("pia_rights", "Data-subject rights",
           "Can data subjects exercise access / erasure / portability rights?",
           "no", "medium", "Wire up DSAR handling (access, erasure, portability)."),
        _q("pia_retention", "Storage limitation",
           "Is there a defined retention period after which the data is deleted?",
           "no", "medium", "Set and enforce a retention schedule (Art. 5(1)(e))."),
        _q("pia_security", "Security",
           "Is the personal data encrypted in transit and at rest?",
           "no", "high", "Encrypt in transit (TLS) and at rest (Art. 32)."),
        _q("pia_transfers", "International transfers",
           "Is personal data transferred outside the EU/EEA without a Chapter V safeguard?",
           "yes", "high", "Put an adequacy decision or SCCs in place before transferring."),
        _q("pia_processors", "Processors",
           "Is every processor bound by a data-processing agreement (Art. 28)?",
           "no", "medium", "Execute an Art. 28 DPA with each processor."),
        _q("pia_automated", "Automated decisions",
           "Does it make solely-automated decisions with legal/significant effects (Art. 22)?",
           "yes", "medium", "Add human review or an Art. 22 exception/safeguard."),
    ),
)

_VENDOR_RISK = Template(
    type="vendor_risk",
    title="Vendor Risk Assessment",
    framework="Third-party risk management (TPRM)",
    description="Assess the security and privacy risk of a third-party vendor.",
    questions=(
        _q("vr_dpa", "Contracts",
           "Is a data-processing agreement (Art. 28) signed with the vendor?",
           "no", "high", "Execute a DPA before sharing personal data."),
        _q("vr_soc2", "Assurance",
           "Does the vendor hold a current SOC 2 Type II (or ISO 27001) attestation?",
           "no", "medium", "Obtain and review the vendor's security attestation."),
        _q("vr_subprocessors", "Supply chain",
           "Is the vendor's sub-processor list disclosed and contractually controlled?",
           "no", "medium", "Require sub-processor disclosure and flow-down terms."),
        _q("vr_encryption", "Security",
           "Does the vendor encrypt personal data in transit and at rest?",
           "no", "high", "Confirm TLS in transit and encryption at rest."),
        _q("vr_breach_history", "History",
           "Has the vendor had a reported data breach in the last 24 months?",
           "yes", "medium", "Assess the breach, remediation, and residual exposure."),
        _q("vr_transfers", "International transfers",
           "Does the vendor process data outside the EU/EEA without a Chapter V safeguard?",
           "yes", "high", "Put SCCs or an adequacy basis in place before transfer."),
        _q("vr_business_continuity", "Resilience",
           "Does the vendor have a tested business-continuity / DR plan?",
           "no", "low", "Request BC/DR evidence and RTO/RPO commitments."),
        _q("vr_data_return", "Exit",
           "Does the contract require return or deletion of data at termination?",
           "no", "medium", "Add return/deletion-on-exit obligations."),
    ),
)

TEMPLATES: dict[str, Template] = {t.type: t for t in (_PIA, _VENDOR_RISK)}


def get_template(assessment_type: str) -> Template | None:
    return TEMPLATES.get((assessment_type or "").strip().lower())


def list_templates() -> list[Template]:
    return list(TEMPLATES.values())


def template_department(assessment_type: str) -> str:
    # The standalone agent is a privacy tool; everything routes to privacy.
    return "privacy"


# --- Sessions + scoring ----------------------------------------------------
@dataclass
class Session:
    type: str
    subject: str
    id: str = field(default_factory=lambda: f"{int(time.time())}-{uuid.uuid4().hex[:8]}")
    created_at: float = field(default_factory=time.time)
    answers: dict = field(default_factory=dict)

    def template(self) -> Template:
        tpl = get_template(self.type)
        if tpl is None:
            raise KeyError(f"no standalone template {self.type!r}")
        return tpl

    def record(self, question_id: str, answer: str, note: str = "") -> None:
        answer = (answer or "").strip().lower()
        if answer not in ANSWERS:
            raise ValueError(f"answer must be one of {ANSWERS}, got {answer!r}")
        if self.template().question(question_id) is None:
            raise KeyError(f"no question {question_id!r} in {self.type!r}")
        self.answers[question_id] = {"answer": answer, "note": note}

    def evaluate(self) -> AssessmentResult:
        tpl = self.template()
        findings: list[Finding] = []
        answered = 0
        in_scope: list[str] = []
        controls_in_place = 0
        for q in tpl.questions:
            rec = self.answers.get(q.id)
            if not rec:
                continue
            ans = rec["answer"]
            if ans in {"yes", "no", "na"}:
                answered += 1
            if ans != "na":
                in_scope.append(q.severity)
            if ans == q.risk_answer:
                findings.append(Finding(q.id, q.section, q.text, q.severity,
                                        ans, "risk", q.guidance))
            elif ans == "unknown":
                findings.append(Finding(q.id, q.section, q.text, q.severity,
                                        ans, "unverified", q.guidance))
            elif ans != "na":
                controls_in_place += 1
        residual = _rollup([f.severity for f in findings])
        return AssessmentResult(
            type=self.type, subject=self.subject, risk_rating=residual,
            findings=findings, answered=answered, total=len(tpl.questions),
            inherent_risk=_rollup(in_scope), residual_risk=residual,
            risks_in_scope=len(in_scope), controls_in_place=controls_in_place)


# Alias so app code that says AssessmentSession(...) works unchanged.
AssessmentSession = Session


# --- Local JSON store (no signing, no maverick) ----------------------------
def _data_dir() -> Path:
    root = os.environ.get("PIA_DATA_DIR") or os.path.join(
        os.environ.get("PIA_HOME", str(Path.cwd())), ".pia-standalone")
    d = Path(root) / "assessments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(assessment_id: str) -> Path | None:
    aid = str(assessment_id or "")
    # Same id charset guard the platform store uses; blocks path traversal.
    if not aid or "/" in aid or "\\" in aid or ".." in aid or len(aid) > 64:
        return None
    return _data_dir() / f"{aid}.json"


def save_session(session: Session) -> Path:
    result = session.evaluate()
    record = {
        "id": session.id, "type": session.type, "subject": session.subject,
        "created_at": session.created_at, "status": "pending_review",
        "revision": 1, "answers": session.answers, "result": asdict(result),
    }
    path = _path(session.id)
    if path is None:
        raise ValueError("invalid assessment id")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def load_saved(assessment_id: str) -> dict | None:
    path = _path(assessment_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(record: dict) -> dict:
    path = _path(record["id"])
    if path is None:
        raise ValueError("invalid assessment id")
    record["revision"] = int(record.get("revision", 1)) + 1
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def list_saved() -> list[dict]:
    out: list[dict] = []
    now = time.time()
    for p in _data_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        res = data.get("result", {})
        fups = data.get("followups") or []
        out.append({
            "id": data.get("id", p.stem), "type": data.get("type", "?"),
            "subject": data.get("subject", "?"),
            "risk_rating": res.get("risk_rating", "?"),
            "inherent_risk": res.get("inherent_risk", res.get("risk_rating", "?")),
            "residual_risk": res.get("residual_risk", res.get("risk_rating", "?")),
            "findings": len(res.get("findings", [])),
            "created_at": data.get("created_at", 0),
            "revision": int(data.get("revision", 1)),
            "status": data.get("status", "pending_review"),
            "open_followups": sum(1 for f in fups if not f.get("answer")),
            "decided_at": data.get("decided_at"),
            "next_review_at": data.get("next_review_at"),
            "review_due": (data.get("status") == "approved"
                           and bool(data.get("next_review_at"))
                           and now >= float(data.get("next_review_at") or 0)),
        })
    return sorted(out, key=lambda r: r["created_at"], reverse=True)


DECISIONS = ("approved", "rejected")


def decide_assessment(assessment_id: str, decision: str, *,
                      decided_by: str = "", note: str = "",
                      cadence_days: int | None = 365,
                      expected_revision: int | None = None) -> dict | None:
    decision = (decision or "").strip().lower()
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    record = load_saved(assessment_id)
    if record is None:
        return None
    record["status"] = decision
    record["decided_at"] = time.time()
    record["decided_by"] = decided_by
    record["decision_note"] = (note or "").strip()[:4000]
    if decision == "approved" and cadence_days:
        record["cadence_days"] = int(cadence_days)
        record["next_review_at"] = record["decided_at"] + int(cadence_days) * 86400
    else:
        record["cadence_days"] = None
        record["next_review_at"] = None
    return _write(record)


def add_followups(assessment_id: str, questions: list[str], asked_by: str = "",
                  *, expected_revision: int | None = None) -> dict | None:
    record = load_saved(assessment_id)
    if record is None or not questions:
        return None
    fups = record.get("followups") or []
    for q in questions:
        q = str(q).strip()
        if q:
            fups.append({"id": uuid.uuid4().hex[:8], "question": q,
                         "asked_by": asked_by, "answer": ""})
    record["followups"] = fups
    record["status"] = "needs_more"
    return _write(record)


def answer_followup(assessment_id: str, followup_id: str, answer: str,
                    answered_by: str = "") -> dict | None:
    record = load_saved(assessment_id)
    if record is None:
        return None
    hit = False
    for f in record.get("followups") or []:
        if f.get("id") == followup_id and not f.get("answer"):
            f["answer"] = str(answer).strip()
            f["answered_by"] = answered_by
            hit = True
            break
    if not hit:
        return None
    return _write(record)
