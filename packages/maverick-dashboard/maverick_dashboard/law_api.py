"""Firm-only REST endpoints for client matters and ethical walls."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._shared import _world
from .auth import (
    caller_principal,
    list_accessible_goals,
    list_accessible_projects,
    require_qualified_attorney,
)

router = APIRouter(prefix="/api/v1", tags=["law-firm"])

_OPAQUE_CONFLICT = "intake could not be cleared; conflicts-counsel review required"
_ATTORNEY_ROLES = frozenset({"responsible_attorney", "attorney"})


class MatterIntakeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    client_name: str | None = Field(default=None, max_length=500)
    client_id: int | None = Field(default=None, gt=0)
    matter_number: str = Field(min_length=1, max_length=200)
    jurisdiction: str = Field(min_length=1, max_length=500)
    domain: str = Field(min_length=1, max_length=128)
    adverse_parties: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def one_client_source(self):
        if bool(self.client_name) == bool(self.client_id):
            raise ValueError("provide exactly one of client_name or client_id")
        if any(not party.strip() or len(party) > 500 for party in self.adverse_parties):
            raise ValueError("each adverse party must be between 1 and 500 characters")
        return self


class MatterPartyIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=500)
    role: Literal["client", "adverse"]


class MatterMemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    principal: str = Field(min_length=6, max_length=255, pattern=r"^user:[^\x00-\x1f]+$")
    role: Literal["responsible_attorney", "attorney", "staff", "viewer"]


class MatterEgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["local_only", "approved_services"]


def _named_attorney(request: Request) -> str:
    return require_qualified_attorney(request)


def _audit_intent(
    actor: str,
    operation: str,
    *,
    candidate_count: int,
    matter_id: int | None = None,
    client_id: int | None = None,
) -> None:
    from maverick.audit import EventKind, audit_event

    fields: dict[str, object] = {
        "actor": actor,
        "operation": operation,
        "candidate_count": candidate_count,
    }
    if matter_id is not None:
        fields["matter_id"] = matter_id
    if client_id is not None:
        fields["client_id"] = client_id
    try:
        written = audit_event(EventKind.MATTER_INTAKE, agent=actor, **fields)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="matter operation audit is temporarily unavailable",
        ) from exc
    if not written:
        raise HTTPException(
            status_code=503,
            detail="matter operation audit is temporarily unavailable",
        )


def _audit_access_intent(
    actor: str,
    operation: str,
    *,
    matter_id: int,
    target_principal: str | None = None,
    target_role: str | None = None,
    requested_mode: str | None = None,
) -> None:
    from maverick.audit import EventKind, audit_event

    fields: dict[str, object] = {
        "actor": actor,
        "matter_id": matter_id,
        "action": operation,
    }
    if target_principal is not None:
        fields["target_principal"] = target_principal
    if target_role is not None:
        fields["target_role"] = target_role
    if requested_mode is not None:
        fields["requested_mode"] = requested_mode
    try:
        written = audit_event(EventKind.ACCESS_GRANT_CHANGED, agent=actor, **fields)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="matter access audit is temporarily unavailable",
        ) from exc
    if not written:
        raise HTTPException(
            status_code=503,
            detail="matter access audit is temporarily unavailable",
        )


def _legal_domains() -> set[str]:
    from maverick.domain import enabled_domains, suite_for

    return {
        name
        for name, profile in enabled_domains().items()
        if suite_for(name) == "legal"
        and profile.workflow
        and profile.workflow[-1].gate in {"review", "approval"}
    }


def _manager(request: Request, matter_id: int) -> tuple[object, str]:
    world = _world()
    principal = caller_principal(request)
    if not principal or world.get_project(matter_id, principal=principal) is None:
        raise HTTPException(status_code=404, detail="no such matter")
    if world.project_member_role(matter_id, principal) != "responsible_attorney":
        raise HTTPException(status_code=404, detail="no such matter")
    return world, principal


def _matter_out(project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "domain": project.domain,
        "client_id": project.client_id,
        "client_name": project.client_name,
        "matter_number": project.matter_number,
        "jurisdiction": project.jurisdiction,
        "egress_mode": project.egress_mode,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


@router.get("/matters")
async def list_matters(request: Request) -> dict:
    world = _world()
    return {"matters": [_matter_out(item) for item in list_accessible_projects(request, world)]}


@router.post("/matters", status_code=201)
async def open_matter(request: Request, payload: MatterIntakeIn) -> dict:
    actor = _named_attorney(request)
    if payload.domain not in _legal_domains():
        raise HTTPException(
            status_code=422,
            detail="an enabled gated legal domain is required",
        )
    _audit_intent(
        actor,
        "open_matter",
        candidate_count=len(payload.adverse_parties) + bool(payload.client_name),
        client_id=payload.client_id,
    )
    from maverick.world_model import PotentialConflict

    try:
        matter_id = _world().create_client_matter(
            payload.name,
            principal=actor,
            domain=payload.domain,
            matter_number=payload.matter_number,
            jurisdiction=payload.jurisdiction,
            description=payload.description,
            client_name=payload.client_name,
            client_id=payload.client_id,
            adverse_parties=payload.adverse_parties,
        )
    except PotentialConflict as exc:
        raise HTTPException(status_code=409, detail=_OPAQUE_CONFLICT) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project = _world().get_project(matter_id, principal=actor)
    if project is None:
        raise HTTPException(status_code=503, detail="matter intake could not be reconciled")
    return _matter_out(project)


@router.get("/matters/{matter_id}")
async def get_matter(request: Request, matter_id: int) -> dict:
    world = _world()
    principal = caller_principal(request)
    project = world.get_project(matter_id, principal=principal)
    if project is None:
        raise HTTPException(status_code=404, detail="no such matter")
    return {
        **_matter_out(project),
        "members": world.list_project_members(matter_id, principal=principal),
        "parties": world.list_matter_parties(matter_id, principal=principal),
        "goals": [
            {"id": goal.id, "title": goal.title, "status": goal.status}
            for goal in list_accessible_goals(request, world, project_id=matter_id, order="desc")
        ],
    }


@router.post("/matters/{matter_id}/parties", status_code=201)
async def add_matter_party(
    request: Request,
    matter_id: int,
    payload: MatterPartyIn,
) -> dict:
    world, actor = _manager(request, matter_id)
    _audit_intent(actor, "add_party", candidate_count=1, matter_id=matter_id)
    from maverick.world_model import PotentialConflict

    try:
        party_id = world.add_matter_party(
            matter_id,
            payload.name,
            payload.role,
            principal=actor,
        )
    except PotentialConflict as exc:
        raise HTTPException(status_code=409, detail=_OPAQUE_CONFLICT) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if party_id is None:
        raise HTTPException(status_code=404, detail="no such matter")
    return {"id": party_id}


@router.post("/matters/{matter_id}/members", status_code=204)
async def add_matter_member(
    request: Request,
    matter_id: int,
    payload: MatterMemberIn,
) -> None:
    world, actor = _manager(request, matter_id)
    if payload.principal == "user:dashboard-static-bearer":
        raise HTTPException(status_code=422, detail="a named user principal is required")
    _audit_access_intent(
        actor,
        "matter_member_add_requested",
        matter_id=matter_id,
        target_principal=payload.principal,
        target_role=payload.role,
    )
    try:
        world.add_project_member(
            matter_id,
            payload.principal,
            payload.role,
            added_by=actor,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/matters/{matter_id}/members/{principal}", status_code=204)
async def revoke_matter_member(request: Request, matter_id: int, principal: str) -> None:
    world, actor = _manager(request, matter_id)
    _audit_access_intent(
        actor,
        "matter_member_revoke_requested",
        matter_id=matter_id,
        target_principal=principal,
    )
    try:
        changed = world.deactivate_project_member(matter_id, principal)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="no such membership")


@router.patch("/matters/{matter_id}/egress", status_code=204)
async def set_matter_egress(
    request: Request,
    matter_id: int,
    payload: MatterEgressIn,
) -> None:
    world, actor = _manager(request, matter_id)
    _audit_access_intent(
        actor,
        "matter_egress_mode_requested",
        matter_id=matter_id,
        requested_mode=payload.mode,
    )
    if not world.set_project_egress_mode(matter_id, payload.mode, principal=actor):
        raise HTTPException(status_code=404, detail="no such matter")


__all__ = ["router"]
