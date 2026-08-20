"""Mandatory, immutable execution context for client-matter work."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from maverick import domain as domain_mod
from maverick.matter_context import (
    MatterContext,
    MatterContextError,
    current_matter_context,
    matter_context_scope,
    require_matter_context,
    resolve_goal_matter_context,
)
from maverick.world_model import WorldModel

DOMAIN = "legal_context_test"
PRINCIPAL = "user:alice"


@pytest.fixture()
def governed_domain(monkeypatch):
    profile = domain_mod.DomainProfile(
        name=DOMAIN,
        workflow=[domain_mod.WorkflowStep(name="attorney review", gate="review")],
    )
    monkeypatch.setattr(domain_mod, "enabled_domains", lambda: {DOMAIN: profile})
    return profile


@pytest.fixture()
def matter_goal(tmp_path, governed_domain):
    world = WorldModel(tmp_path / "world.db")
    matter_id = world.create_client_matter(
        "Client v. Counterparty",
        principal=PRINCIPAL,
        domain=DOMAIN,
        matter_number="2026-001",
        jurisdiction="Tennessee",
        client_name="Example Client",
    )
    goal_id = world.create_matter_goal(
        "Prepare analysis",
        principal=PRINCIPAL,
        domain=DOMAIN,
        project_id=matter_id,
    )
    assert goal_id is not None
    try:
        yield world, matter_id, goal_id
    finally:
        world.close()


def test_context_is_frozen_exact_and_scope_restores_parent(matter_goal):
    world, matter_id, goal_id = matter_goal
    context = resolve_goal_matter_context(
        world,
        goal_id,
        principal=PRINCIPAL,
        source="runner",
    )

    assert context == MatterContext(
        matter_id=matter_id,
        client_id=world.get_project(matter_id)["client_id"],
        principal=PRINCIPAL,
        membership_role="responsible_attorney",
        domain=DOMAIN,
        jurisdiction="Tennessee",
        purpose="goal-execution",
        source="runner",
        egress_mode="local_only",
    )
    with pytest.raises(FrozenInstanceError):
        context.domain = "legal_other"  # type: ignore[misc]
    with pytest.raises(MatterContextError, match="not bound"):
        require_matter_context()

    with matter_context_scope(context):
        assert current_matter_context() is context
        assert require_matter_context() is context
    assert current_matter_context() is None


def test_context_resolves_current_durable_matter_egress_mode(matter_goal):
    world, matter_id, goal_id = matter_goal
    assert world.set_project_egress_mode(
        matter_id,
        "approved_services",
        principal=PRINCIPAL,
    ) is True

    context = resolve_goal_matter_context(
        world,
        goal_id,
        principal=PRINCIPAL,
        source="runner",
    )
    assert context.egress_mode == "approved_services"


@pytest.mark.parametrize(
    ("column", "value", "error"),
    [
        ("client_id", None, "client_id"),
        ("matter_number", "", "matter_number"),
        ("jurisdiction", "", "jurisdiction"),
    ],
)
def test_incomplete_legacy_matter_metadata_fails_closed(
    matter_goal, column, value, error,
):
    world, matter_id, goal_id = matter_goal
    with world._writing() as conn:
        conn.execute(f"UPDATE projects SET {column} = ? WHERE id = ?", (value, matter_id))

    with pytest.raises(MatterContextError, match=error):
        resolve_goal_matter_context(
            world,
            goal_id,
            principal=PRINCIPAL,
            source="runner",
        )


def test_static_shared_bearer_is_rejected_even_with_legacy_membership(matter_goal):
    world, matter_id, goal_id = matter_goal
    principal = "user:dashboard-static-bearer"
    with world._writing() as conn:
        conn.execute(
            "INSERT INTO matter_memberships("
            "project_id, principal, role, active, added_by, created_at) "
            "VALUES(?, ?, 'attorney', 1, 'legacy', 1)",
            (matter_id, principal),
        )

    with pytest.raises(MatterContextError, match="shared bearer"):
        resolve_goal_matter_context(
            world,
            goal_id,
            principal=principal,
            source="runner",
        )


@pytest.mark.parametrize(
    ("principal", "role", "error"),
    [
        ("user:admin", None, "not an active member"),
        ("user:viewer", "viewer", "viewer membership"),
    ],
)
def test_no_admin_bypass_and_viewers_cannot_execute(
    matter_goal, principal, role, error,
):
    world, matter_id, goal_id = matter_goal
    if role is not None:
        world.add_project_member(matter_id, principal, role, added_by=PRINCIPAL)

    with pytest.raises(MatterContextError, match=error):
        resolve_goal_matter_context(
            world,
            goal_id,
            principal=principal,
            source="runner",
        )


def test_matterless_goal_and_revoked_member_fail_closed(matter_goal):
    world, matter_id, goal_id = matter_goal
    loose = world.create_goal("Loose work", domain=DOMAIN, owner=PRINCIPAL)
    with pytest.raises(MatterContextError, match="not bound to a matter"):
        resolve_goal_matter_context(
            world,
            loose,
            principal=PRINCIPAL,
            source="runner",
        )

    world.add_project_member(
        matter_id,
        "user:backup",
        "responsible_attorney",
        added_by=PRINCIPAL,
    )
    assert world.deactivate_project_member(matter_id, PRINCIPAL) is True
    with pytest.raises(MatterContextError, match="not an active member"):
        resolve_goal_matter_context(
            world,
            goal_id,
            principal=PRINCIPAL,
            source="runner",
        )


def test_disabled_nonlegal_and_ungated_domains_fail_closed(
    matter_goal, monkeypatch,
):
    world, _matter_id, goal_id = matter_goal

    monkeypatch.setattr(domain_mod, "enabled_domains", dict)
    with pytest.raises(MatterContextError, match="unknown or disabled"):
        resolve_goal_matter_context(
            world, goal_id, principal=PRINCIPAL, source="runner",
        )

    nonlegal = domain_mod.DomainProfile(
        name="finance_context_test",
        workflow=[domain_mod.WorkflowStep(name="review", gate="review")],
    )
    world.set_goal_domain(goal_id, nonlegal.name)
    monkeypatch.setattr(
        domain_mod,
        "enabled_domains",
        lambda: {nonlegal.name: nonlegal},
    )
    with pytest.raises(MatterContextError, match="not in the legal suite"):
        resolve_goal_matter_context(
            world, goal_id, principal=PRINCIPAL, source="runner",
        )

    ungated = domain_mod.DomainProfile(
        name="legal_ungated_context_test",
        workflow=[domain_mod.WorkflowStep(name="draft")],
    )
    world.set_goal_domain(goal_id, ungated.name)
    monkeypatch.setattr(
        domain_mod,
        "enabled_domains",
        lambda: {ungated.name: ungated},
    )
    with pytest.raises(MatterContextError, match="review or approval"):
        resolve_goal_matter_context(
            world, goal_id, principal=PRINCIPAL, source="runner",
        )
