"""Legacy durable workers also enforce the law-firm execution boundary."""
from __future__ import annotations

import pytest
from maverick import domain as domain_mod

DOMAIN = "legal_worker_context_test"
PRINCIPAL = "user:alice"


@pytest.fixture()
def worker_world(tmp_path, monkeypatch):
    from maverick import world_model

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    profile = domain_mod.DomainProfile(
        name=DOMAIN,
        workflow=[domain_mod.WorkflowStep(name="attorney review", gate="review")],
    )
    monkeypatch.setattr(domain_mod, "enabled_domains", lambda: {DOMAIN: profile})
    world = world_model.WorldModel(db)
    matter_id = world.create_client_matter(
        "Client matter",
        principal=PRINCIPAL,
        domain=DOMAIN,
        matter_number="2026-WORK-1",
        jurisdiction="Tennessee",
        client_name="Worker Client",
    )
    try:
        yield world, matter_id
    finally:
        world.close()


def _payload(goal_id, matter_id, **overrides):
    payload = {
        "goal_id": goal_id,
        "matter_id": matter_id,
        "domain": DOMAIN,
        "concurrency_principal": PRINCIPAL,
    }
    payload.update(overrides)
    return payload


def test_worker_rechecks_exact_context_then_dispatches(
    worker_world, tmp_path, monkeypatch,
):
    from maverick import runner
    from maverick.job_queue import JobQueue
    from maverick.worker import Worker

    world, matter_id = worker_world
    goal_id = world.create_matter_goal(
        "Analyze issue",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    seen = {}
    monkeypatch.setattr(
        runner,
        "run_goal_in_thread",
        lambda goal_id, **kwargs: seen.update(goal_id=goal_id, **kwargs) or "done",
    )
    queue = JobQueue(db_path=tmp_path / "jobs.db")
    job_id = queue.enqueue("run_goal", _payload(goal_id, matter_id))

    assert Worker(queue=queue, idle_sleep=0).run_once() is True
    assert queue.get(job_id).status == "done"
    assert seen["goal_id"] == goal_id
    assert seen["concurrency_principal"] == PRINCIPAL


@pytest.mark.parametrize("mutation", ["revoked", "moved", "domain"])
def test_worker_rejects_revoked_moved_or_changed_goal_before_dispatch(
    worker_world, tmp_path, monkeypatch, mutation,
):
    from maverick import runner
    from maverick.job_queue import JobQueue
    from maverick.worker import Worker

    world, matter_id = worker_world
    goal_id = world.create_matter_goal(
        "Analyze issue",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    payload = _payload(goal_id, matter_id)
    if mutation == "revoked":
        world.add_project_member(
            matter_id,
            "user:backup",
            "responsible_attorney",
            added_by=PRINCIPAL,
        )
        assert world.deactivate_project_member(matter_id, PRINCIPAL) is True
    elif mutation == "moved":
        other = world.create_client_matter(
            "Other client",
            principal=PRINCIPAL,
            domain=DOMAIN,
            matter_number="2026-WORK-2",
            jurisdiction="Tennessee",
            client_name="Other Worker Client",
        )
        assert world.set_goal_project(goal_id, other, principal=PRINCIPAL) is True
    else:
        changed = domain_mod.DomainProfile(
            name="legal_changed_worker_context_test",
            workflow=[domain_mod.WorkflowStep(name="review", gate="review")],
        )
        monkeypatch.setattr(
            domain_mod,
            "enabled_domains",
            lambda: {DOMAIN: domain_mod.DomainProfile(
                name=DOMAIN,
                workflow=[domain_mod.WorkflowStep(name="review", gate="review")],
            ), changed.name: changed},
        )
        world.set_goal_domain(goal_id, changed.name)
    monkeypatch.setattr(
        runner,
        "run_goal_in_thread",
        lambda *_args, **_kwargs: pytest.fail("invalid matter job dispatched"),
    )
    queue = JobQueue(db_path=tmp_path / f"jobs-{mutation}.db")
    job_id = queue.enqueue("run_goal", payload)

    assert Worker(
        queue=queue,
        idle_sleep=0,
        retry_after=0,
        max_attempts=1,
    ).run_once() is True
    assert queue.get(job_id).status == "failed"


@pytest.mark.parametrize(
    "payload",
    [
        {"goal_id": 1},
        {
            "goal_id": 1,
            "matter_id": 1,
            "domain": DOMAIN,
            "concurrency_principal": "",
        },
    ],
)
def test_worker_rejects_missing_execution_context_before_dispatch(
    worker_world, tmp_path, monkeypatch, payload,
):
    from maverick import runner
    from maverick.job_queue import JobQueue
    from maverick.worker import Worker

    world, matter_id = worker_world
    goal_id = world.create_matter_goal(
        "Analyze issue",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    payload = {**payload, "goal_id": goal_id}
    monkeypatch.setattr(
        runner,
        "run_goal_in_thread",
        lambda *_args, **_kwargs: pytest.fail("contextless job dispatched"),
    )
    queue = JobQueue(db_path=tmp_path / "jobs-missing.db")
    job_id = queue.enqueue("run_goal", payload)

    assert Worker(
        queue=queue,
        idle_sleep=0,
        retry_after=0,
        max_attempts=1,
    ).run_once() is True
    assert queue.get(job_id).status == "failed"


def test_scheduled_worker_creates_goal_atomically_inside_matter(
    worker_world, tmp_path, monkeypatch,
):
    from maverick import runner
    from maverick.job_queue import JobQueue
    from maverick.worker import Worker

    world, matter_id = worker_world
    seen = {}

    def fake_run(goal_id, **kwargs):
        goal = world.get_goal(goal_id)
        seen.update(goal=goal, kwargs=kwargs)
        return "done"

    monkeypatch.setattr(runner, "run_goal_in_thread", fake_run)
    queue = JobQueue(db_path=tmp_path / "jobs-scheduled.db")
    job_id = queue.enqueue(
        "start_goal",
        {
            "text": "Prepare weekly matter update",
            "title": "Weekly update",
            "matter_id": matter_id,
            "domain": DOMAIN,
            "concurrency_principal": PRINCIPAL,
        },
    )

    assert Worker(queue=queue, idle_sleep=0).run_once() is True
    assert queue.get(job_id).status == "done"
    goal = seen["goal"]
    assert goal.project_id == matter_id
    assert goal.domain == DOMAIN
    assert goal.owner == PRINCIPAL
    assert seen["kwargs"]["concurrency_principal"] == PRINCIPAL
