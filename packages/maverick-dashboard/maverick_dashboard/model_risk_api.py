"""Operator API for governed Model Risk & AI Assurance."""
from __future__ import annotations

import importlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from . import model_risk_schemas as schemas
from .auth import require_global_permission, require_permission

router = APIRouter(prefix="/assurance", tags=["model-risk-assurance"])
_GATEWAY_ACTION_LIMITS = {"delivery": 120, "packet_issuance": 12}
_GATEWAY_ACTION_BUCKET_CAP = 10_000
_gateway_action_times: dict[tuple[str, str, str], deque[float]] = {}
_gateway_action_lock = threading.Lock()


def _actor(request: Request) -> str:
    from .api import _request_actor

    return _request_actor(request)


def _limit_gateway_action(request: Request, action: str) -> None:
    from maverick.paths import current_tenant_id_strict

    actor = _actor(request)
    # The gateway operation itself enforces an explicit tenant.  Keep the
    # limiter safe for disabled/fake modules and return the domain's actionable
    # tenant error instead of pre-empting it here.
    tenant = current_tenant_id_strict() or "<unbound>"
    now = time.monotonic()
    limit = _GATEWAY_ACTION_LIMITS[action]
    key = (tenant, actor, action)
    with _gateway_action_lock:
        if (
            key not in _gateway_action_times
            and len(_gateway_action_times) >= _GATEWAY_ACTION_BUCKET_CAP
        ):
            for candidate, candidate_window in tuple(
                _gateway_action_times.items()
            ):
                while candidate_window and now - candidate_window[0] >= 60:
                    candidate_window.popleft()
                if not candidate_window:
                    _gateway_action_times.pop(candidate, None)
            if len(_gateway_action_times) >= _GATEWAY_ACTION_BUCKET_CAP:
                raise HTTPException(
                    status_code=429,
                    detail="AI evidence gateway rate-limit capacity reached",
                    headers={"Retry-After": "60"},
                )
        window = _gateway_action_times.setdefault(key, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"AI evidence gateway {action} rate limit reached",
                headers={"Retry-After": "60"},
            )
        window.append(now)


def _module(name: str, knob: str):
    module = importlib.import_module(f"maverick.{name}")
    if not module.enabled():
        raise HTTPException(
            status_code=403,
            detail=f"{name.replace('_', ' ')} is disabled ([{knob}] enable)",
        )
    return module


async def _call(fn: Callable, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such assurance record") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        from maverick.privacy_ops import RecordConflict

        if isinstance(exc, RecordConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(
            status_code=503,
            detail=f"assurance operation failed safely ({type(exc).__name__})",
        ) from exc


async def _record_call(fn: Callable, *args, **kwargs) -> dict[str, Any]:
    record = await _call(fn, *args, **kwargs)
    if record is None:
        raise HTTPException(status_code=404, detail="no such assurance record")
    return record



@router.get("/model-risk/inventory")
async def model_risk_inventory(request: Request) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {"assets": await _call(officer.list_inventory)}


@router.post("/model-risk/observations")
async def observe_model_risk_asset(
    request: Request,
    body: schemas.ModelRiskObservationIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _call(
        officer.observe_asset,
        **body.model_dump(),
        actor=_actor(request),
    )


@router.get("/model-risk/declarations")
async def model_risk_declarations(request: Request) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {"declarations": await _call(officer.list_declarations)}


@router.post("/model-risk/declarations")
async def declare_model_risk_asset(
    request: Request,
    body: schemas.ModelRiskDeclarationIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    payload = body.model_dump()
    asset_id = payload.pop("asset_id")
    return await _call(
        officer.declare_asset,
        asset_id,
        **payload,
        actor=_actor(request),
    )


@router.patch("/model-risk/declarations/{asset_id}")
async def update_model_risk_declaration(
    request: Request,
    asset_id: str,
    body: schemas.ModelRiskDeclarationUpdateIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    payload = body.model_dump(exclude_none=True)
    expected_revision = payload.pop("expected_revision")
    return await _record_call(
        officer.update_declaration,
        asset_id,
        expected_revision=expected_revision,
        actor=_actor(request),
        **payload,
    )


@router.post("/model-risk/declarations/{asset_id}/review")
async def review_model_risk_declaration(
    request: Request,
    asset_id: str,
    body: schemas.ModelRiskDeclarationReviewIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _record_call(
        officer.review_declaration,
        asset_id,
        decision=body.decision,
        rationale=body.rationale,
        reviewer=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.get("/model-risk/evidence")
async def model_risk_evidence(
    request: Request,
    asset_id: str = "",
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {
        "evidence": await _call(
            officer.list_evidence,
            asset_id=asset_id or None,
        )
    }


@router.post("/model-risk/evidence")
async def record_model_risk_evidence(
    request: Request,
    body: schemas.ModelRiskEvidenceIn,
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    payload = body.model_dump()
    asset_id = payload.pop("asset_id")
    return await _call(
        officer.record_evidence,
        asset_id,
        **payload,
        actor=_actor(request),
    )


@router.post("/model-risk/evidence/training-receipts")
async def record_verified_training_receipt_evidence(
    request: Request,
    body: schemas.ModelRiskTrainingReceiptEvidenceIn,
) -> dict[str, Any]:
    """Register only the commitment from a verified tenant-private receipt."""
    require_permission(request, "operate")
    request_actor = _actor(request)
    if body.actor != request_actor:
        raise HTTPException(
            status_code=403,
            detail="actor must match the authenticated caller",
        )
    officer = _module("model_risk_assurance", "model_risk_assurance")
    payload = body.model_dump()
    asset_id = payload.pop("asset_id")
    return await _call(
        officer.record_verified_training_receipt_evidence,
        asset_id,
        **payload,
    )


@router.post("/model-risk/evidence/{evidence_id}/review")
async def review_model_risk_evidence(
    request: Request,
    evidence_id: str,
    body: schemas.EvidenceReviewIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _record_call(
        officer.review_evidence,
        evidence_id,
        decision=body.decision,
        rationale=body.rationale,
        reviewer=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.get("/model-risk/findings")
async def model_risk_findings(request: Request) -> dict[str, Any]:
    require_permission(request, "view")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {"findings": await _call(officer.findings)}


@router.post("/model-risk/findings/{finding_id}/risk-acceptance")
async def accept_model_risk(
    request: Request,
    finding_id: str,
    body: schemas.ModelRiskAcceptanceIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _record_call(
        officer.accept_risk,
        finding_id,
        finding_sha256=body.finding_sha256,
        rationale=body.rationale,
        reviewer=_actor(request),
        expires_at=body.expires_at,
        expected_revision=body.expected_revision,
    )


@router.get("/model-risk/incidents")
async def model_risk_incidents(
    request: Request,
    asset_id: str = "",
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {
        "incidents": await _call(
            officer.list_incidents,
            asset_id=asset_id or None,
        )
    }


@router.post("/model-risk/incidents")
async def record_model_risk_incident(
    request: Request,
    body: schemas.ModelRiskIncidentIn,
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    payload = body.model_dump()
    asset_id = payload.pop("asset_id")
    return await _call(
        officer.record_incident,
        asset_id,
        **payload,
        actor=_actor(request),
    )


@router.patch("/model-risk/incidents/{incident_id}")
async def update_model_risk_incident(
    request: Request,
    incident_id: str,
    body: schemas.ModelRiskIncidentUpdateIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _record_call(
        officer.update_incident,
        incident_id,
        **body.model_dump(),
        actor=_actor(request),
    )


@router.post("/model-risk/promotion-authorizations")
async def authorize_model_risk_promotion(
    request: Request,
    body: schemas.ModelRiskPromotionAuthorizationIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _call(
        officer.authorize_promotion_candidate,
        **body.model_dump(),
        reviewer=_actor(request),
    )


@router.post("/model-risk/promotion-authorizations/revoke")
async def revoke_model_risk_promotion(
    request: Request,
    body: schemas.ModelRiskPromotionRevocationIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _call(
        officer.revoke_promotion_candidate,
        **body.model_dump(),
        reviewer=_actor(request),
    )


@router.get("/model-risk/deployments")
async def model_risk_deployments(
    request: Request,
    asset_id: str = "",
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return {
        "deployments": await _call(
            officer.list_deployments,
            asset_id=asset_id or None,
        )
    }


@router.post("/model-risk/deployments")
async def record_model_risk_deployment(
    request: Request,
    body: schemas.ModelRiskDeploymentIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _call(
        officer.record_deployment,
        **body.model_dump(),
        actor=_actor(request),
    )


@router.get("/model-risk/evidence-pack")
async def model_risk_evidence_pack(
    request: Request,
    asset_id: str = "",
) -> dict[str, Any]:
    require_permission(request, "operate")
    officer = _module("model_risk_assurance", "model_risk_assurance")
    return await _call(
        officer.render_assurance_pack,
        actor=_actor(request),
        asset_id=asset_id or None,
    )


# AI Evidence-Ready Gateway -------------------------------------------------


@router.get("/gateway/summary")
async def evidence_gateway_summary(request: Request) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _call(gateway.summary)


@router.get("/gateway/policies")
async def evidence_gateway_policies(request: Request) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return {"policies": await _call(gateway.list_policies)}


@router.get("/gateway/policies/{policy_id}")
async def evidence_gateway_policy(
    request: Request,
    policy_id: str,
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _record_call(gateway.get_policy, policy_id)


@router.put("/gateway/policies/{policy_id}")
async def upsert_evidence_gateway_policy(
    request: Request,
    policy_id: str,
    body: schemas.EvidenceGatewayPolicyUpsertIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    current = await _call(gateway.get_policy, policy_id)
    payload = body.model_dump(
        exclude_unset=isinstance(current, dict),
    )
    if isinstance(current, dict):
        settings = current.get("settings")
        if not isinstance(settings, dict):
            raise HTTPException(
                status_code=503,
                detail="current evidence policy is malformed",
            )
        for field in (
            "disclosure_text",
            "require_interaction_disclosure",
            "require_machine_readable_marking",
            "require_visible_marking",
            "supported_modalities",
            "machine_marker",
            "visible_marker",
        ):
            payload.setdefault(field, settings[field])
        for field in ("model_sha256", "context_sha256", "metadata"):
            payload.setdefault(field, current[field])
    return await _call(
        gateway.upsert_policy,
        policy_id,
        **payload,
        actor=_actor(request),
    )


@router.post("/gateway/deliver")
async def deliver_evidence_ready_text(
    request: Request,
    body: schemas.EvidenceGatewayDeliveryIn,
) -> dict[str, Any]:
    require_permission(request, "operate")
    _limit_gateway_action(request, "delivery")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    payload = body.model_dump()
    generated_text = payload.pop("generated_text")
    return await _call(
        gateway.deliver_text,
        generated_text,
        **payload,
        actor=_actor(request),
    )


@router.get("/gateway/receipts")
async def evidence_gateway_receipts(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, min_length=16, max_length=2048),
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    page = await _call(
        gateway.list_interaction_receipts_page,
        limit=limit,
        cursor=cursor,
    )
    return {
        "receipts": page["items"],
        "next_cursor": page["next_cursor"],
        "snapshot_total": page["snapshot_total"],
        "count": page["count"],
        "missing_governed_count": page["missing_governed_count"],
    }


@router.get("/gateway/receipts/{receipt_id}")
async def evidence_gateway_receipt(
    request: Request,
    receipt_id: str,
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _record_call(gateway.get_interaction_receipt, receipt_id)


@router.post("/gateway/receipts/{receipt_id}/verify")
async def verify_evidence_gateway_receipt(
    request: Request,
    receipt_id: str,
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    receipt = await _record_call(gateway.get_interaction_receipt, receipt_id)
    valid = await _call(gateway.verify_interaction_receipt, receipt)
    return {"receipt_id": receipt_id, "valid": bool(valid)}


@router.get("/gateway/regulatory-impacts")
async def evidence_gateway_regulatory_impacts(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, max_length=2_048),
    status: str | None = Query(
        None,
        pattern="^(pending_review|accepted_refreshing|accepted|dismissed)$",
    ),
    pending_first: bool = Query(True),
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    page = await _call(
        gateway.list_regulatory_impacts_page,
        limit=limit,
        cursor=cursor,
        status=status,
        pending_first=pending_first,
    )
    return {
        "regulatory_impacts": page["items"],
        "next_cursor": page["next_cursor"],
        "snapshot_total": page["snapshot_total"],
        "count": page["count"],
        "status": page["status"],
        "pending_first": page["pending_first"],
    }


@router.get("/gateway/regulatory-impacts/{impact_id}")
async def evidence_gateway_regulatory_impact(
    request: Request,
    impact_id: str,
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _record_call(gateway.get_regulatory_impact, impact_id)


@router.post("/gateway/regulatory-impacts")
async def record_evidence_gateway_regulatory_impact(
    request: Request,
    body: schemas.EvidenceGatewayRegulatoryImpactIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    payload = body.model_dump()
    alert_id = payload.pop("alert_id")
    return await _call(
        gateway.record_regulatory_impact,
        alert_id,
        **payload,
        actor=_actor(request),
    )


@router.post("/gateway/regulatory-impacts/{impact_id}/review")
async def review_evidence_gateway_regulatory_impact(
    request: Request,
    impact_id: str,
    body: schemas.EvidenceGatewayImpactReviewIn,
) -> dict[str, Any]:
    require_global_permission(request, "admin")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _record_call(
        gateway.review_regulatory_impact,
        impact_id,
        decision=body.decision,
        reviewer=_actor(request),
        rationale=body.rationale,
        expected_revision=body.expected_revision,
    )


@router.post("/gateway/assurance-packet")
async def download_evidence_gateway_assurance_packet(
    request: Request,
    profile: str = Query(
        "combined",
        pattern="^(article50_gpai|nist_ai_rmf|federal_vendor|combined)$",
    ),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=1_024,
    ),
) -> Response:
    # Rendering appends a signed attestation to the packet ledger, so this is
    # an explicit privileged mutation rather than a cacheable/read-only GET.
    require_global_permission(request, "admin")
    _limit_gateway_action(request, "packet_issuance")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    issued = await _call(
        gateway.issue_assurance_packet,
        profile=profile,
        actor=_actor(request),
        idempotency_key=idempotency_key,
    )
    packet = issued["packet"]
    try:
        payload = json.dumps(
            packet,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="assurance packet was not valid JSON",
        ) from exc
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Idempotent-Replay": (
                "true" if issued["idempotent_replay"] else "false"
            ),
            "Content-Disposition": (
                'attachment; filename="maverick-ai-assurance-packet.json"'
            ),
        },
    )


@router.post("/gateway/assurance-packet/verify")
async def verify_evidence_gateway_assurance_packet(
    request: Request,
    body: schemas.EvidenceGatewayPacketVerifyIn,
) -> dict[str, Any]:
    require_permission(request, "operate")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    valid = await _call(gateway.verify_assurance_packet, body.packet)
    return {"valid": bool(valid)}


@router.post("/gateway/demo-seed")
async def seed_evidence_gateway_demo(request: Request) -> dict[str, Any]:
    require_global_permission(request, "admin")
    gateway = _module("ai_evidence_gateway", "evidence_gateway")
    return await _call(gateway.seed_demo, actor=_actor(request))
