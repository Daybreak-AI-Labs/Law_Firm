"""Human-correction ingestion: "no, that's wrong" becomes a lesson."""
from __future__ import annotations

from types import SimpleNamespace

from maverick import corrections, reflexion
from maverick.matter_context import MatterContext, matter_context_scope


def _turn(role: str, content: str, ts: float = 0.0):
    return SimpleNamespace(role=role, content=content, ts=ts)


class TestDetectCorrection:
    def test_detects_explicit_correction_of_prior_answer(self):
        turns = [  # newest-first
            _turn("user", "That's wrong — the totals don't match Q3."),
            _turn("assistant", "The reconciled total is $1.2M."),
            _turn("user", "Reconcile the ledger."),
        ]
        hit = corrections.detect_correction(turns)
        assert hit is not None
        correction, prior = hit
        assert "wrong" in correction.lower()
        assert prior.startswith("The reconciled total")

    def test_plain_followup_is_not_a_correction(self):
        turns = [
            _turn("user", "Great, now do the same for Q4."),
            _turn("assistant", "Done."),
        ]
        assert corrections.detect_correction(turns) is None

    def test_correction_without_prior_answer_is_ignored(self):
        assert corrections.detect_correction(
            [_turn("user", "that's wrong")],
        ) is None

    def test_assistant_turn_newest_is_ignored(self):
        turns = [
            _turn("assistant", "that's wrong is a phrase"),
            _turn("user", "hello"),
        ]
        assert corrections.detect_correction(turns) is None


class _World:
    def __init__(self, turns):
        self._turns = turns  # chronological, like recent_turns

    def recent_turns(self, conversation_id, limit=6):
        return self._turns[-limit:]


class _FirmWorld(_World):
    def __init__(self, *, legacy_turns, matter_turns):
        super().__init__(legacy_turns)
        self._matter_turns = matter_turns
        self.matter_reads: list[tuple[int, int, str]] = []

    def recent_matter_turns(
        self, conversation_id, *, project_id, principal, limit=6,
    ):
        self.matter_reads.append((conversation_id, project_id, principal))
        return self._matter_turns[-limit:]


class TestMaybeRecordCorrection:
    def _goal(self):
        return SimpleNamespace(title="Reconcile the ledger", description="")

    def test_records_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAVERICK_REFLEXION", "1")
        captured: list[dict] = []
        monkeypatch.setattr(
            reflexion, "record", lambda **kw: captured.append(kw) or True,
        )
        world = _World([
            _turn("user", "Reconcile the ledger.", ts=1.0),
            _turn("assistant", "Total is $1.2M.", ts=2.0),
            _turn("user", "That's wrong, redo it against Q3.", ts=3.0),
        ])
        assert corrections.maybe_record_correction(
            world, 1, self._goal(), channel="slack", user_id="u1",
            domain="finance_gl_close",
        ) is True
        assert captured[0]["failure_class"] == "user_correction"
        assert captured[0]["domain"] == "finance_gl_close"
        assert captured[0]["channel"] == "slack"

    def test_noop_without_conversation(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_REFLEXION", "1")
        assert corrections.maybe_record_correction(
            _World([]), None, self._goal(),
        ) is False

    def test_noop_when_reflexion_disabled(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_REFLEXION", "0")
        world = _World([
            _turn("assistant", "Total is $1.2M.", ts=1.0),
            _turn("user", "that's wrong", ts=2.0),
        ])
        assert corrections.maybe_record_correction(world, 1, self._goal()) is False

    def test_firm_mode_uses_only_exact_matter_conversation(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
        monkeypatch.setenv("MAVERICK_REFLEXION", "1")
        captured: list[dict] = []
        monkeypatch.setattr(
            reflexion, "record", lambda **kw: captured.append(kw) or True,
        )
        world = _FirmWorld(
            legacy_turns=[
                _turn("assistant", "Other client's privileged answer", ts=1.0),
                _turn("user", "That's wrong", ts=2.0),
            ],
            matter_turns=[
                _turn("user", "Review the filing", ts=1.0),
                _turn("assistant", "The filing date is June 3", ts=2.0),
                _turn("user", "That's wrong, re-check the order", ts=3.0),
            ],
        )
        context = MatterContext(
            matter_id=41,
            client_id=9,
            principal="user:counsel",
            membership_role="attorney",
            domain="legal",
            jurisdiction="Federal-VA",
            purpose="goal-execution",
            source="correction-test",
            egress_mode="local_only",
        )
        goal = SimpleNamespace(
            title="Review the filing",
            description="",
            project_id=41,
            owner="user:counsel",
        )
        with matter_context_scope(context, authority_resolver=lambda: context):
            assert corrections.maybe_record_correction(
                world,
                7,
                goal,
                channel="dashboard",
                user_id="user:counsel",
                domain="legal",
            ) is True
        assert world.matter_reads == [(7, 41, "user:counsel")]
        assert captured[0]["matter_id"] == 41
        assert captured[0]["owner"] == "user:counsel"
        assert "Other client" not in captured[0]["reflection"]

    def test_firm_mode_rejects_goal_scope_mismatch(self, monkeypatch):
        monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
        monkeypatch.setenv("MAVERICK_REFLEXION", "1")
        world = _FirmWorld(legacy_turns=[], matter_turns=[])
        context = MatterContext(
            matter_id=41,
            client_id=9,
            principal="user:counsel",
            membership_role="attorney",
            domain="legal",
            jurisdiction="Federal-VA",
            purpose="goal-execution",
            source="correction-test",
            egress_mode="local_only",
        )
        wrong_goal = SimpleNamespace(
            title="Other matter",
            description="",
            project_id=42,
            owner="user:counsel",
        )
        with matter_context_scope(context, authority_resolver=lambda: context):
            assert corrections.maybe_record_correction(
                world, 7, wrong_goal, domain="legal",
            ) is False
        assert world.matter_reads == []
