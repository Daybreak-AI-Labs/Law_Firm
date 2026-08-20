from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest
from maverick.cli._practice_groups import _run_matter_rehearsal_goal
from maverick.matter_context import current_matter_context


class _World:
    def __init__(
        self,
        *,
        create_allowed: bool = True,
        revoke_after_create: bool = False,
        child_matter_id: int = 7,
    ) -> None:
        self.create_allowed = create_allowed
        self.revoke_after_create = revoke_after_create
        self.child_matter_id = child_matter_id
        self.role_reads = 0
        self.created: list[dict] = []
        self.goals: dict[int, SimpleNamespace] = {}

    def get_project(self, project_id: int):
        return {
            "id": project_id,
            "client_id": 3,
            "matter_number": "2026-0007",
            "jurisdiction": "US-TN",
            "egress_mode": "local_only",
        }

    def project_member_role(self, project_id: int, principal: str):
        self.role_reads += 1
        if self.revoke_after_create and self.role_reads > 1:
            return None
        return "attorney" if principal == "user:alice" else None

    def create_matter_goal(self, title: str, description: str, **authority):
        self.created.append({"title": title, "description": description, **authority})
        if not self.create_allowed:
            return None
        self.goals[41] = SimpleNamespace(
            project_id=self.child_matter_id,
            domain=authority["domain"],
            owner=authority["principal"],
        )
        return 41

    def get_goal(self, goal_id: int):
        return self.goals.get(goal_id)


@pytest.fixture(autouse=True)
def _legal_profile(monkeypatch):
    profile = SimpleNamespace(workflow=[SimpleNamespace(gate="approval")])
    monkeypatch.setattr(
        "maverick.domain.enabled_domains",
        lambda: {"legal_contract_review": profile},
    )
    monkeypatch.setattr("maverick.domain.suite_for", lambda domain: "legal")


@pytest.mark.asyncio
async def test_rehearsal_creates_exact_bound_goal_and_binds_fresh_context(monkeypatch):
    world = _World()

    async def fake_run_goal(**kwargs):
        context = current_matter_context()
        assert context is not None
        assert (context.matter_id, context.principal, context.domain) == (
            7,
            "user:alice",
            "legal_contract_review",
        )
        assert context.egress_mode == "local_only"
        assert kwargs["goal_id"] == 41
        return "DONE: rehearsed"

    orchestrator = ModuleType("maverick.orchestrator")
    orchestrator.run_goal = fake_run_goal
    monkeypatch.setitem(sys.modules, "maverick.orchestrator", orchestrator)
    result = await _run_matter_rehearsal_goal(
        world=world,
        llm=object(),
        sandbox=object(),
        prompt="review the limitation clause",
        matter_id=7,
        owner="user:alice",
        domain="legal_contract_review",
        budget_dollars=0.5,
    )
    assert result == "DONE: rehearsed"
    assert world.created[0]["project_id"] == 7
    assert world.created[0]["principal"] == "user:alice"
    assert world.created[0]["domain"] == "legal_contract_review"
    assert current_matter_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "world",
    [
        _World(create_allowed=False),
        _World(revoke_after_create=True),
        _World(child_matter_id=8),
    ],
)
async def test_rehearsal_refuses_revoked_or_cross_matter_child(world):
    result = await _run_matter_rehearsal_goal(
        world=world,
        llm=object(),
        sandbox=object(),
        prompt="review the limitation clause",
        matter_id=7,
        owner="user:alice",
        domain="legal_contract_review",
        budget_dollars=0.5,
    )
    assert result.startswith("BLOCKED:")
    assert current_matter_context() is None


@pytest.mark.asyncio
async def test_rehearsal_without_exact_owner_never_creates_goal():
    world = _World()
    result = await _run_matter_rehearsal_goal(
        world=world,
        llm=object(),
        sandbox=object(),
        prompt="review the limitation clause",
        matter_id=7,
        owner="",
        domain="legal_contract_review",
        budget_dollars=0.5,
    )
    assert result.startswith("BLOCKED:")
    assert world.created == []
