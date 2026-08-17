"""Compliance assessment engine: templates, scoring, persistence, CLI."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Keep saved assessments off the real ~/.maverick.
    from maverick import paths

    monkeypatch.setattr(paths, "maverick_home", lambda: tmp_path / "home")


def test_templates_registered_and_looked_up():
    from maverick.assessment import get_template, list_templates

    types = {t.type for t in list_templates()}
    # Core compliance templates + the finance suite (finance-agent-suite §6).
    assert types == {
        "pia",
        "dpia",
        "lia",
        "ccpa",
        "aira",
        "vendor_risk",
        "tia",
        "hipaa",
        "soc2",
        "iso27001",
        "nist_csf",
        "nist_800_53",
        "cis_v8",
        "pci_dss",
        "cmmc_l2",
        "fedramp_moderate",
        "sox_control",
        "fraud_risk",
        "itgc",
        "credit_risk",
        "close_readiness",
    }
    assert get_template("VENDOR_RISK").title == "Vendor Risk Assessment"  # case-insensitive
    assert get_template("nope") is None


def test_fedramp_catalog_tracks_the_2026_transition_source():
    from maverick.assessment import FRAMEWORK_SOURCES, get_template

    source = FRAMEWORK_SOURCES["fedramp_moderate"]
    template = get_template("fedramp_moderate")

    assert source["source_url"] == "https://www.fedramp.gov/2026/"
    assert "Class C" in source["version"]
    assert template is not None
    assert "Class C" in template.framework
    assert "screen" in template.description.lower()


def test_privacy_templates_dpia_lia_ccpa_score_and_route():
    from maverick.assessment import (
        AssessmentSession,
        get_template,
        template_department,
    )

    expected = {"dpia": 10, "lia": 10, "ccpa": 11}
    for type_, n_questions in expected.items():
        tpl = get_template(type_)
        assert tpl is not None and len(tpl.questions) == n_questions
        # New privacy frameworks belong to the privacy workspace, not finance.
        assert template_department(type_) == "privacy"
        # The safe answer to every question yields a clean, minimal result...
        clean = AssessmentSession(type=type_, subject="Clean")
        for q in tpl.questions:
            clean.record(q.id, "no" if q.risk_answer == "yes" else "yes")
        r = clean.evaluate()
        assert r.risk_rating == "minimal" and r.findings == []
        # ...and the risky answer to every question raises findings that roll up.
        risky = AssessmentSession(type=type_, subject="Risky")
        for q in tpl.questions:
            risky.record(q.id, q.risk_answer)
        rr = risky.evaluate()
        assert rr.risk_rating in ("high", "medium")
        assert len(rr.findings) == n_questions
    # The DPIA is a distinct, deeper instrument than the lightweight PIA.
    assert get_template("dpia").title != get_template("pia").title
    assert "Art. 35" in get_template("dpia").framework


def test_clean_answers_are_minimal_risk():
    from maverick.assessment import AssessmentSession, get_template

    tpl = get_template("pia")
    s = AssessmentSession(type="pia", subject="Marketing emails")
    for q in tpl.questions:
        s.record(q.id, "no" if q.risk_answer == "yes" else "yes")  # the safe answer
    r = s.evaluate()
    assert r.risk_rating == "minimal"
    assert r.findings == []
    assert r.answered == r.total


def test_risky_answers_score_findings_and_roll_up():
    from maverick.assessment import AssessmentSession

    s = AssessmentSession(type="vendor_risk", subject="Acme Corp")
    s.record("vr_dpa", "no")  # high (risk answer)
    s.record("vr_soc2", "yes")  # safe -> no finding
    s.record("vr_breach_history", "yes")  # medium (risk answer is "yes")
    s.record("vr_business_continuity", "unknown")  # low, unverified
    r = s.evaluate()

    assert r.risk_rating == "high"  # max severity among findings
    kinds = {f.question_id: f.kind for f in r.findings}
    assert kinds["vr_dpa"] == "risk"
    assert kinds["vr_breach_history"] == "risk"
    assert kinds["vr_business_continuity"] == "unverified"
    assert "vr_soc2" not in kinds
    assert r.answered == 3  # yes/no/na count; unknown does not


def test_inherent_vs_residual_risk_pair():
    """Inherent = every applicable risk area (before crediting controls);
    residual = what the findings leave open. A control in place lowers
    residual but never lowers inherent; 'na' removes the area from both."""
    from maverick.assessment import AssessmentSession

    s = AssessmentSession(type="vendor_risk", subject="Acme Corp")
    s.record("vr_dpa", "yes")  # high area, CONTROLLED
    s.record("vr_encryption", "yes")  # high area, CONTROLLED
    s.record("vr_breach_history", "no")  # medium area, safe answer
    s.record("vr_deletion", "no")  # medium area, RISK answer
    s.record("vr_business_continuity", "na")  # out of scope entirely
    r = s.evaluate()

    assert r.inherent_risk == "high"  # high areas apply to this vendor
    assert r.residual_risk == "medium"  # only the deletion gap is open
    assert r.risk_rating == r.residual_risk  # back-compat alias
    assert r.risks_in_scope == 4  # the four non-na answers
    assert r.controls_in_place == 3  # dpa, encryption, breach history
    # Fully clean subject: both ratings collapse to minimal.
    s2 = AssessmentSession(type="vendor_risk", subject="Clean Corp")
    s2.record("vr_dpa", "na")
    r2 = s2.evaluate()
    assert (r2.inherent_risk, r2.residual_risk) == ("minimal", "minimal")
    assert r2.risks_in_scope == 0


def test_saved_summaries_carry_the_risk_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    from maverick.assessment import AssessmentSession, list_saved, save_session

    s = AssessmentSession(type="pia", subject="Acme CRM")
    s.record("pia_transfers", "yes")  # high risk answer -> open finding
    s.record("pia_security", "yes")  # safe answer -> controlled
    save_session(s)
    row = next(r for r in list_saved() if r["id"] == s.id)
    assert row["inherent_risk"] == "high"
    assert row["residual_risk"] == "high"


def test_record_rejects_bad_answer_and_unknown_question():
    from maverick.assessment import AssessmentSession

    s = AssessmentSession(type="aira", subject="Resume screener")
    with pytest.raises(ValueError):
        s.record("aira_purpose", "maybe")
    with pytest.raises(KeyError):
        s.record("does_not_exist", "yes")


def test_assessment_session_ids_are_unique_for_rapid_creation():
    from maverick.assessment import AssessmentSession

    ids = {AssessmentSession(type="pia", subject=f"Subject {i}").id for i in range(1000)}

    assert len(ids) == 1000


def test_persistence_round_trip():
    from maverick.assessment import (
        AssessmentSession,
        list_saved,
        load_saved,
        save_session,
    )

    s = AssessmentSession(type="vendor_risk", subject="Acme Corp")
    s.record("vr_dpa", "no")
    path = save_session(s)
    assert path.exists()

    rows = list_saved()
    assert len(rows) == 1
    assert rows[0]["subject"] == "Acme Corp"
    assert rows[0]["risk_rating"] == "high"

    data = load_saved(s.id)
    assert data["type"] == "vendor_risk"
    assert data["result"]["findings"][0]["question_id"] == "vr_dpa"




def test_load_saved_rejects_path_traversal():
    from maverick.assessment import load_saved

    # `assess show ../../etc/passwd` must not read outside the assessments dir.
    assert load_saved("../../etc/passwd") is None
    assert load_saved("../secret") is None
    assert load_saved("does-not-exist") is None


def test_save_session_is_written_private_0600():
    from maverick.assessment import AssessmentSession, save_session
    from maverick.file_lock import private_path_is_restricted

    path = save_session(AssessmentSession(type="pia", subject="Acme"))
    assert private_path_is_restricted(path, 0o600)
