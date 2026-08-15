"""Verifier role: parsing, verdict shape, integration into the agent loop."""
from __future__ import annotations

import pytest
from maverick.verifier import (
    _parse,
    verify_proposal,
)


class TestParse:
    def test_clean_json_accepts(self):
        text = '{"confidence": 0.9, "accepts": true, "critique": "looks good", "issues": []}'
        v = _parse(text)
        assert v.confidence == 0.9
        assert v.accepts is True
        assert v.critique == "looks good"
        assert v.issues == []

    def test_clean_json_rejects(self):
        text = '{"confidence": 0.3, "accepts": false, "critique": "wrong", "issues": ["bad math"]}'
        v = _parse(text)
        assert v.accepts is False
        assert v.issues == ["bad math"]

    def test_extracts_json_from_prose(self):
        """Model wraps the JSON in prose despite system prompt."""
        text = (
            'Here is the verdict:\n\n'
            '{"confidence": 0.8, "accepts": true, "critique": "ok", "issues": []}\n\n'
            'Hope that helps!'
        )
        v = _parse(text)
        assert v.confidence == 0.8
        assert v.accepts is True

    def test_extracts_json_from_markdown_fence(self):
        text = (
            '```json\n'
            '{"confidence": 0.5, "accepts": false, "critique": "iffy", "issues": []}\n'
            '```'
        )
        v = _parse(text)
        assert v.confidence == 0.5
        assert v.accepts is False

    def test_empty_response_rejects(self):
        v = _parse("")
        assert v.accepts is False
        assert "empty" in v.critique.lower()

    def test_unparseable_rejects(self):
        v = _parse("not json at all")
        assert v.accepts is False

    def test_confidence_clamped(self):
        v = _parse('{"confidence": 5.0, "accepts": true, "critique": ""}')
        assert v.confidence == 1.0
        v = _parse('{"confidence": -1.0, "accepts": false, "critique": ""}')
        assert v.confidence == 0.0

    def test_string_accepts_value(self):
        """Some models emit string booleans."""
        v = _parse('{"confidence": 0.8, "accepts": "true", "critique": "ok"}')
        assert v.accepts is True

    def test_low_confidence_accept_is_downgraded(self):
        # The confidence floor is enforced in code: a verifier must not wave
        # output through with accepts=true below the threshold (fail-open the
        # last gate before FINAL). The verdict is forced to reject.
        v = _parse('{"confidence": 0.1, "accepts": true, "critique": "", '
                   '"issues": ["the answer is wrong"]}')
        assert v.accepts is False
        assert v.critique  # a revision brief is populated for the proposer

    def test_high_confidence_accept_is_honored(self):
        v = _parse('{"confidence": 0.95, "accepts": true, "critique": "ok", '
                   '"issues": []}')
        assert v.accepts is True


class TestVerifyProposal:
    @pytest.mark.asyncio
    async def test_empty_proposal_rejected_without_llm_call(self):
        from maverick.budget import Budget

        class _ShouldNotBeCalled:
            async def complete_async(self, **kw):
                raise AssertionError("LLM was called for empty proposal")

        v = await verify_proposal("brief", "", _ShouldNotBeCalled(), Budget())
        assert v.accepts is False

    @pytest.mark.asyncio
    async def test_calls_llm_with_verifier_role(self, fake_llm, make_llm_response):
        from maverick.budget import Budget
        fake_llm.scripted = [make_llm_response(
            text='{"confidence": 0.9, "accepts": true, "critique": "ok", "issues": []}',
        )]
        v = await verify_proposal(
            "brief: plan a trip", "Visit Lisbon.", fake_llm, Budget(),
        )
        assert v.accepts is True
        # The LLM call recorded the verifier system prompt.
        assert len(fake_llm.calls) == 1
        assert "verifier" in fake_llm.calls[0]["system"].lower()


class TestAgentVerifierIntegration:
    @pytest.mark.asyncio
    async def test_orchestrator_revises_on_rejection(
        self, tmp_path, fake_llm, make_llm_response,
    ):
        """When the verifier rejects, the proposer gets a revision brief
        and a second chance. The second answer is accepted regardless."""
        from maverick.agent import Agent
        from maverick.blackboard import Blackboard
        from maverick.budget import Budget
        from maverick.sandbox import LocalBackend
        from maverick.swarm import SwarmContext
        from maverick.world_model import WorldModel

        # Scripted LLM responses:
        #   1. orchestrator FINAL: "first answer"
        #   2. verifier rejects (low confidence)
        #   3. orchestrator (revision) FINAL: "second answer"
        #   4. verifier accepts (May 26 fix: revised FINALs DO get
        #      re-verified now; the old "skip re-verify" behavior
        #      let bogus revisions through with verifier_confidence=1.0)
        fake_llm.scripted = [
            make_llm_response(text="FINAL: first answer"),
            make_llm_response(
                text='{"confidence": 0.3, "accepts": false, '
                     '"critique": "first attempt was wrong", '
                     '"issues": ["missing X"]}',
            ),
            make_llm_response(text="FINAL: second answer"),
            make_llm_response(
                text='{"confidence": 0.95, "accepts": true, '
                     '"critique": "second attempt addresses the issues", '
                     '"issues": []}',
            ),
        ]
        world = WorldModel(tmp_path / "w.db")
        gid = world.create_goal("test", "")
        ctx = SwarmContext(
            llm=fake_llm, world=world, budget=Budget(max_dollars=1.0),
            blackboard=Blackboard(),
            sandbox=LocalBackend(workdir=tmp_path),
            goal_id=gid, max_depth=1,
        )
        agent = Agent(ctx=ctx, role="orchestrator", brief="test", depth=0)
        result = await agent.run()
        assert result.final == "second answer"
        # 4 LLM calls (May 26 fix): propose -> verify -> revise -> re-verify.
        # Earlier behavior skipped the re-verify, letting bogus revisions
        # through with verifier_confidence=1.0 fallback.
        assert len(fake_llm.calls) == 4


class TestStructuredVerify:
    """The rubric judge (reasoning_reward) wired into verify_final, on by default."""

    @pytest.mark.asyncio
    async def test_rubric_reply_drives_verdict(self, fake_llm, make_llm_response):
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        fake_llm.scripted = [make_llm_response(text=(
            '{"reasoning": "checked", "dimensions": ['
            '{"name": "correctness", "score": 0.9, "critique": "ok"},'
            '{"name": "completeness", "score": 0.9, "critique": ""},'
            '{"name": "grounding", "score": 0.9, "critique": ""},'
            '{"name": "safety", "score": 0.9, "critique": ""}],'
            '"critique": "solid", "score": 0.9, "confidence": 0.8}'))]
        v = await verify_proposal_structured("brief", "a real answer", fake_llm, Budget())
        assert v.accepts is True
        assert v.confidence == 0.9
        assert v.reward_audit is not None and v.reward_audit["dimensions"]

    @pytest.mark.asyncio
    async def test_safety_veto_rejects_high_holistic(self, fake_llm, make_llm_response):
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        # High holistic score but safety below its veto floor -> reject.
        fake_llm.scripted = [make_llm_response(text=(
            '{"dimensions": ['
            '{"name": "correctness", "score": 1.0},'
            '{"name": "completeness", "score": 1.0},'
            '{"name": "grounding", "score": 1.0},'
            '{"name": "safety", "score": 0.1, "critique": "unsafe"}],'
            '"critique": "unsafe", "score": 0.95, "confidence": 0.9}'))]
        v = await verify_proposal_structured("brief", "answer", fake_llm, Budget())
        assert v.accepts is False
        assert any("safety" in i for i in v.issues)

    @pytest.mark.asyncio
    async def test_non_rubric_reply_falls_back_to_scalar(self, fake_llm, make_llm_response):
        # A scalar verdict reply (no dimensions) must behave exactly like the
        # scalar verifier -- this is what keeps on-by-default backward-compatible.
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        fake_llm.scripted = [make_llm_response(
            text='{"confidence": 0.95, "accepts": true, "critique": "ok", "issues": []}')]
        v = await verify_proposal_structured("brief", "answer", fake_llm, Budget())
        assert v.accepts is True
        assert v.confidence == 0.95
        assert v.reward_audit is None  # scalar path, no rubric attached

    @pytest.mark.asyncio
    async def test_incomplete_rubric_rejects_instead_of_falling_back(
        self, fake_llm, make_llm_response,
    ):
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        fake_llm.scripted = [make_llm_response(text=(
            '{"dimensions": [{"name": "correctness", "score": 1.0}], '
            '"score": 1.0, "confidence": 1.0, "accepts": true}'))]
        v = await verify_proposal_structured("brief", "answer", fake_llm, Budget())
        assert v.accepts is False
        assert v.reward_audit is None
        assert "incomplete" in v.critique

    @pytest.mark.asyncio
    async def test_reward_signed_into_audit_when_enabled(
        self, fake_llm, make_llm_response, monkeypatch,
    ):
        import maverick.audit as audit
        from maverick import reasoning_reward
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        monkeypatch.setattr(reasoning_reward, "audit_rewards_enabled", lambda: True)
        recorded = []
        monkeypatch.setattr(audit, "record",
                            lambda kind, **kw: recorded.append((kind, kw)) or True)
        fake_llm.scripted = [make_llm_response(text=(
            '{"reasoning": "long trace", "dimensions": '
            '[{"name": "correctness", "score": 1.0},'
            '{"name": "completeness", "score": 1.0},'
            '{"name": "grounding", "score": 1.0},'
            '{"name": "safety", "score": 0.1, "critique": "unsafe"}], '
            '"score": 0.9, "confidence": 0.8}'))]
        await verify_proposal_structured("b", "a", fake_llm, Budget())
        assert recorded, "structured reward was not signed into the audit chain"
        kind, payload = recorded[0]
        assert kind == audit.EventKind.VERIFICATION_REWARD
        assert payload["vetoed"] is True and "dimensions" in payload
        assert "reasoning" not in payload  # compact summary, not the full trace

    @pytest.mark.asyncio
    async def test_reward_not_audited_when_disabled(
        self, fake_llm, make_llm_response, monkeypatch,
    ):
        import maverick.audit as audit
        from maverick import reasoning_reward
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        monkeypatch.setattr(reasoning_reward, "audit_rewards_enabled", lambda: False)
        recorded = []
        monkeypatch.setattr(audit, "record", lambda kind, **kw: recorded.append(kind) or True)
        fake_llm.scripted = [make_llm_response(text=(
            '{"dimensions": [{"name": "correctness", "score": 0.9},'
            '{"name": "completeness", "score": 0.9},'
            '{"name": "grounding", "score": 0.9},'
            '{"name": "safety", "score": 0.9}], "score": 0.9}'))]
        await verify_proposal_structured("b", "a", fake_llm, Budget())
        assert recorded == []

    @pytest.mark.asyncio
    async def test_audit_failure_never_breaks_verification(
        self, fake_llm, make_llm_response, monkeypatch,
    ):
        import maverick.audit as audit
        from maverick import reasoning_reward
        from maverick.budget import Budget
        from maverick.verifier import verify_proposal_structured
        monkeypatch.setattr(reasoning_reward, "audit_rewards_enabled", lambda: True)

        def _boom(*a, **k):
            raise RuntimeError("audit chain down")

        monkeypatch.setattr(audit, "record", _boom)
        fake_llm.scripted = [make_llm_response(text=(
            '{"dimensions": [{"name": "correctness", "score": 0.9},'
            '{"name": "completeness", "score": 0.9},'
            '{"name": "grounding", "score": 0.9},'
            '{"name": "safety", "score": 0.9}], "score": 0.9}'))]
        v = await verify_proposal_structured("b", "a", fake_llm, Budget())
        assert v.accepts is True  # audit blew up; the verification still returned

    @pytest.mark.asyncio
    async def test_verify_final_routes_to_structured_when_enabled(
        self, fake_llm, make_llm_response, monkeypatch,
    ):
        from maverick import reasoning_reward
        from maverick.budget import Budget
        from maverick.verifier import verify_final
        monkeypatch.setattr(reasoning_reward, "enabled", lambda: True)
        fake_llm.scripted = [make_llm_response(text=(
            '{"dimensions": [{"name": "correctness", "score": 1.0},'
            '{"name": "completeness", "score": 1.0},'
            '{"name": "grounding", "score": 1.0},'
            '{"name": "safety", "score": 0.1, "critique": "unsafe"}], '
            '"score": 0.9, "confidence": 0.8}'))]
        v = await verify_final("brief", "answer", fake_llm, Budget())
        # Routed to the rubric judge: the safety veto rejected despite score 0.9.
        assert v.accepts is False
        assert v.reward_audit is not None


@pytest.mark.asyncio
async def test_ensemble_reraises_budget_exceeded_not_swallowed():
    # A BudgetExceeded from one panel member must PROPAGATE out of the ensemble
    # (so the budget stops the run), not be collected by gather(return_exceptions)
    # and folded into a combined reject verdict. The other member is still awaited
    # rather than orphaned. Regression for the gather() that used to propagate
    # mid-flight and leave siblings running.
    from types import SimpleNamespace

    from maverick.budget import Budget, BudgetExceeded
    from maverick.verifier import verify_proposal_ensemble

    class _PanelLLM:
        def __init__(self):
            self.calls = []

        async def complete_async(self, **kw):
            self.calls.append(kw.get("model"))
            if kw.get("model") == "m-budget":
                raise BudgetExceeded("verifier out of budget")
            return SimpleNamespace(
                text='{"confidence": 0.9, "accepts": true, "critique": "ok", "issues": []}')

    llm = _PanelLLM()
    with pytest.raises(BudgetExceeded):
        await verify_proposal_ensemble(
            "brief", "a genuine proposal to verify", llm, Budget(),
            panel=["m-ok", "m-budget"], weighted=True,
        )
    # Both members were awaited (the panel ran), not just the failing one.
    assert set(llm.calls) == {"m-ok", "m-budget"}
