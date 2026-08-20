"""Assessment memory: the flow learns from the org's own past assessments."""
from __future__ import annotations

import pytest
from maverick import assessment_memory
from maverick.assessment import AssessmentSession, save_session
from maverick.config import get_assessments, reset_config_cache


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("MAVERICK_ASSESS_LEARN", raising=False)
    reset_config_cache()
    yield
    reset_config_cache()


def _saved_pia(subject: str, answers: dict[str, str]) -> AssessmentSession:
    s = AssessmentSession(type="pia", subject=subject)
    for qid, ans in answers.items():
        s.record(qid, ans)
    save_session(s)
    return s


class TestSimilar:
    def test_ranks_by_subject_overlap(self):
        _saved_pia("Acme CRM", {"pia_necessity": "yes"})
        _saved_pia("Globex payroll", {"pia_necessity": "no"})
        rows = assessment_memory.similar("Acme CRM rollout",
                                         assessment_type="pia")
        assert rows
        assert rows[0]["subject"] == "Acme CRM"
        assert rows[0]["similarity"] > 0.5
        assert all("risk_rating" in r for r in rows)

    def test_no_history_is_empty(self):
        assert assessment_memory.similar("anything") == []

    def test_exclude_id_skips_self(self):
        s = _saved_pia("Acme CRM", {"pia_necessity": "yes"})
        assert assessment_memory.similar("Acme CRM", exclude_id=s.id) == []

    def test_learn_kill_switch(self, monkeypatch):
        _saved_pia("Acme CRM", {"pia_necessity": "yes"})
        monkeypatch.setenv("MAVERICK_ASSESS_LEARN", "0")
        reset_config_cache()
        assert assessment_memory.similar("Acme CRM") == []


class TestSuggestAnswers:
    def test_majority_vote_with_confidence_and_provenance(self):
        a = _saved_pia("Acme CRM east", {"pia_necessity": "yes",
                                         "pia_retention": "no"})
        b = _saved_pia("Acme CRM west", {"pia_necessity": "yes",
                                         "pia_retention": "yes"})
        sugg = assessment_memory.suggest_answers("pia", "Acme CRM", k=2)
        assert sugg["pia_necessity"]["answer"] == "yes"
        assert sugg["pia_necessity"]["confidence"] == 1.0
        assert set(sugg["pia_necessity"]["based_on"]) == {a.id, b.id}
        # A 50/50 split fails the default min_confidence and is dropped...
        assert "pia_retention" not in sugg
        # ...but survives a permissive threshold.
        loose = assessment_memory.suggest_answers("pia", "Acme CRM", k=2,
                                                  min_confidence=0.0)
        assert "pia_retention" in loose

    def test_other_templates_never_leak_in(self):
        s = AssessmentSession(type="vendor_risk", subject="Acme CRM")
        s.record("vr_dpa", "yes")
        save_session(s)
        assert assessment_memory.suggest_answers("pia", "Acme CRM") == {}


class TestRecordSession:
    def test_fail_open_without_knowledge_plane(self):
        # Knowledge RAG is off in a bare env: recording is a no-op, not a crash.
        assert assessment_memory.record_session(
            {"id": "x", "subject": "s", "result": {}}) is False

    def test_save_session_hook_never_breaks_save(self, monkeypatch):
        import maverick.assessment_memory as mem

        def boom(_payload):
            raise RuntimeError("knowledge exploded")

        monkeypatch.setattr(mem, "record_session", boom)
        s = _saved_pia("Acme CRM", {"pia_necessity": "yes"})  # must not raise
        assert s.id

    def test_distill_mentions_rating_and_findings(self):
        text = assessment_memory._distill({
            "id": "A1", "type": "pia", "subject": "Acme CRM",
            "result": {"risk_rating": "high", "answered": 9, "total": 10,
                       "findings": [{"severity": "high", "section": "Transfers",
                                     "question": "SCCs in place?",
                                     "answer": "no"}]},
        })
        assert "risk high" in text
        assert "Transfers" in text


class TestConfig:
    def test_defaults(self):
        cfg = get_assessments()
        assert cfg == {"learn": True}

    def test_section_overrides(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[assessments]\nlearn = false\n',
            encoding="utf-8")
        monkeypatch.setenv("MAVERICK_CONFIG", str(cfg_file))
        reset_config_cache()
        cfg = get_assessments()
        assert cfg == {"learn": False}
