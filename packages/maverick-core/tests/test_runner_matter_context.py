"""The production runner binds matter authority before paid/sandbox work."""
from __future__ import annotations

from maverick import domain as domain_mod
from maverick.matter_context import current_matter_context
from maverick.world_model import WorldModel

DOMAIN = "legal_runner_context_test"
PRINCIPAL = "user:alice"


def _install_runner_stubs(monkeypatch, world, seen):
    from maverick import budget as budget_mod
    from maverick import llm as llm_mod
    from maverick import orchestrator, world_model
    from maverick import sandbox as sandbox_mod

    profile = domain_mod.DomainProfile(
        name=DOMAIN,
        workflow=[domain_mod.WorkflowStep(name="release", gate="approval")],
    )
    monkeypatch.setattr(domain_mod, "enabled_domains", lambda: {DOMAIN: profile})
    monkeypatch.setattr(world_model, "open_world", lambda: world)
    monkeypatch.setattr(world_model, "close_world_if_owned", lambda _world: None)

    def llm():
        seen["llm_context"] = current_matter_context()
        return object()

    class Sandbox:
        def close(self):
            seen["close_context"] = current_matter_context()

    def build_sandbox():
        seen["sandbox_context"] = current_matter_context()
        return Sandbox()

    def run_goal_sync(_llm, run_world, _budget, goal_id, **_kwargs):
        seen["run_context"] = current_matter_context()
        run_world.set_goal_status(goal_id, "done", result="complete")

    monkeypatch.setattr(llm_mod, "LLM", llm)
    monkeypatch.setattr(sandbox_mod, "build_sandbox", build_sandbox)
    monkeypatch.setattr(budget_mod, "budget_from_config", lambda **_kwargs: object())
    monkeypatch.setattr(orchestrator, "run_goal_sync", run_goal_sync)


def test_runner_binds_exact_context_before_llm_and_sandbox(
    tmp_path, monkeypatch,
):
    from maverick import runner

    world = WorldModel(tmp_path / "world.db")
    matter_id = world.create_client_matter(
        "Client matter",
        principal=PRINCIPAL,
        domain=DOMAIN,
        matter_number="2026-RUN-1",
        jurisdiction="Tennessee",
        client_name="Runner Client",
    )
    goal_id = world.create_matter_goal(
        "Analyze issue",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    seen = {}
    _install_runner_stubs(monkeypatch, world, seen)
    try:
        assert runner.run_goal_in_thread(
            goal_id,
            user_id="alice",
            concurrency_principal=PRINCIPAL,
        ) == "done"
        for stage in ("llm_context", "sandbox_context", "run_context", "close_context"):
            context = seen[stage]
            assert context.matter_id == matter_id
            assert context.client_id == world.get_project(matter_id)["client_id"]
            assert context.principal == PRINCIPAL
            assert context.membership_role == "responsible_attorney"
            assert context.domain == DOMAIN
            assert context.jurisdiction == "Tennessee"
            assert context.purpose == "goal-execution"
            assert context.source == "runner"
        assert current_matter_context() is None
    finally:
        world.close()


def test_runner_rejects_missing_principal_before_opening_world(monkeypatch):
    from maverick import runner, world_model

    monkeypatch.setattr(
        world_model,
        "open_world",
        lambda: (_ for _ in ()).throw(AssertionError("world opened")),
    )
    assert runner.run_goal_in_thread(1, user_id="alice") is None


def test_runner_rechecks_revocation_before_any_llm_or_sandbox(
    tmp_path, monkeypatch,
):
    from maverick import llm as llm_mod
    from maverick import runner, world_model
    from maverick import sandbox as sandbox_mod

    world = WorldModel(tmp_path / "world.db")
    matter_id = world.create_client_matter(
        "Client matter",
        principal=PRINCIPAL,
        domain=DOMAIN,
        matter_number="2026-RUN-2",
        jurisdiction="Tennessee",
        client_name="Revocation Client",
    )
    world.add_project_member(
        matter_id,
        "user:backup",
        "responsible_attorney",
        added_by=PRINCIPAL,
    )
    goal_id = world.create_matter_goal(
        "Analyze issue",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    assert world.deactivate_project_member(matter_id, PRINCIPAL) is True
    profile = domain_mod.DomainProfile(
        name=DOMAIN,
        workflow=[domain_mod.WorkflowStep(name="release", gate="review")],
    )
    monkeypatch.setattr(domain_mod, "enabled_domains", lambda: {DOMAIN: profile})
    monkeypatch.setattr(world_model, "open_world", lambda: world)
    monkeypatch.setattr(world_model, "close_world_if_owned", lambda _world: None)
    monkeypatch.setattr(
        llm_mod,
        "LLM",
        lambda: (_ for _ in ()).throw(AssertionError("LLM constructed")),
    )
    monkeypatch.setattr(
        sandbox_mod,
        "build_sandbox",
        lambda: (_ for _ in ()).throw(AssertionError("sandbox constructed")),
    )
    try:
        assert runner.run_goal_in_thread(
            goal_id,
            concurrency_principal=PRINCIPAL,
        ) is None
    finally:
        world.close()
