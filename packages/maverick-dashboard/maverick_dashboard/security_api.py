"""Security/GRC and defensive-hunter REST surfaces.

Mounted below ``/api/v1`` by :mod:`maverick_dashboard.api`. Keeping this
department router separate prevents the already-large shared API module from
becoming the ownership boundary for every security record type.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import api_schemas as schemas
from .auth import require_global_permission, require_permission

log = logging.getLogger(__name__)

router = APIRouter(prefix="/security", tags=["security"])

from .model_risk_api import router as model_risk_router  # noqa: E402

router.include_router(model_risk_router)


def _actor(request: Request) -> str:
    # Import lazily: api.py mounts this router after defining the stricter
    # authenticated/local actor resolver, avoiding an import cycle.
    from .api import _request_actor

    return _request_actor(request)


def _document_principal(request: Request) -> str | None:
    """Reuse the dashboard's authenticated connector-credential boundary."""

    from .api import _document_source_principal

    return _document_source_principal(request)


def _ops():
    from maverick import security_ops

    if not security_ops.enabled():
        raise HTTPException(
            status_code=403,
            detail="security ops are disabled ([security_ops] enable)",
        )
    return security_ops


async def _ops_call(ops, fn: Callable, *args, **kwargs):
    """Run one synchronous store mutation with stable HTTP conflict semantics."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except ops.RecordConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ops.SecurityTransitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ops.SecurityAuditOutboxError, ops.SecurityStateError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _ops_record_call(ops, fn: Callable, *args, **kwargs) -> dict:
    """Call a record operation and map an unknown record to HTTP 404."""
    result = await _ops_call(ops, fn, *args, **kwargs)
    if result is None:
        raise HTTPException(status_code=404, detail="no such security record")
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _viewer_readiness(value: object) -> dict[str, Any]:
    """Return readiness aggregates without leaking nested control-owner text."""
    if not isinstance(value, dict):
        return {}
    safe = {
        key: _jsonable(value[key])
        for key in (
            "framework",
            "controls_total",
            "controls_applicable",
            "controls_implemented",
            "controls_partial",
            "coverage_percent",
            "approved_evidence",
            "review_gated_evidence",
            "note",
        )
        if key in value
    }
    gaps = value.get("top_control_gaps")
    if isinstance(gaps, list):
        # Viewer-tier summaries may expose aggregate status and stable control
        # references, but not the owner or descriptive title nested inside each
        # otherwise-innocent readiness object.
        safe["top_control_gaps"] = [
            {
                key: row[key]
                for key in ("id", "canonical_id", "status")
                if key in row
            }
            for row in gaps[:10]
            if isinstance(row, dict)
        ]
    return safe


# Security/GRC aggregate + configuration ------------------------------------

@router.get("/summary")
async def security_summary(request: Request) -> dict:
    """Viewer-safe aggregate: counts/readiness only, no evidence or case text."""
    require_permission(request, "view")
    ops = _ops()
    report = await _ops_call(ops, ops.program_report)
    keys = (
        "assessments", "controls", "evidence", "risks", "poam",
        "vendors", "policies", "incidents", "audits",
    )
    totals = {}
    for key in keys:
        value = report.get(key, {})
        totals[key] = value.get("total", 0) if isinstance(value, dict) else value
    return {
        "enabled": True,
        "generated_at": report.get("generated_at"),
        "totals": totals,
        "readiness": _viewer_readiness(report.get("readiness")),
    }


@router.get("/report")
async def security_report(request: Request, framework: str = "") -> dict:
    require_permission(request, "operate")
    ops = _ops()
    if framework.strip():
        return await _ops_call(
            ops, ops.readiness_report, framework=framework.strip()[:120]
        )
    return await _ops_call(ops, ops.program_report)


@router.get("/regulatory-clocks")
async def regulatory_clock_packs(request: Request) -> dict:
    require_permission(request, "view")
    ops = _ops()
    return {"clocks": await _ops_call(ops, ops.list_regulatory_clock_packs)}


@router.put("/regulatory-clocks")
async def upsert_regulatory_clock(
    request: Request, body: schemas.SecurityClockIn,
) -> dict:
    require_global_permission(request, "admin")
    ops = _ops()
    payload = body.model_dump(exclude_none=True)
    clock_id = payload.pop("clock_id", "")
    expected = payload.pop("expected_revision", None)
    return await _ops_record_call(
        ops,
        ops.upsert_regulatory_clock,
        payload,
        clock_id=clock_id,
        updated_by=_actor(request),
        expected_revision=expected,
    )


# Controls, crosswalks, and evidence ----------------------------------------

@router.get("/controls")
async def list_controls(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"controls": await _ops_call(ops, ops.list_controls)}


@router.post("/controls", status_code=201)
async def create_control(request: Request, body: schemas.SecurityControlIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.upsert_control,
        body.model_dump(),
        updated_by=_actor(request),
    )


@router.post("/controls/initialize", status_code=201)
async def initialize_controls(request: Request, owner: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    owner = owner.strip()
    if not owner:
        raise HTTPException(status_code=422, detail="control owner must not be blank")
    rows = await _ops_call(
        ops,
        ops.initialize_control_register,
        owner=owner[:200],
        created_by=_actor(request),
    )
    return {"controls": rows, "created": len(rows)}


@router.get("/controls/{control_id}")
async def get_control(request: Request, control_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_control, control_id[:160])
    if record is None:
        raise HTTPException(status_code=404, detail="no such control")
    return record


@router.patch("/controls/{control_id}")
async def update_control(
    request: Request,
    control_id: str,
    body: schemas.SecurityControlUpdateIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    payload = body.model_dump(exclude_none=True)
    expected = payload.pop("expected_revision")
    return await _ops_record_call(
        ops,
        ops.upsert_control,
        payload,
        control_id=control_id[:160],
        expected_revision=expected,
        updated_by=_actor(request),
    )


@router.get("/statement-of-applicability")
async def statement_of_applicability(request: Request, framework: str = "") -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {
        "controls": await _ops_call(
            ops, ops.statement_of_applicability, framework.strip()[:120]
        )
    }


@router.get("/crosswalk")
async def control_crosswalk(
    request: Request, control_id: str = "", framework: str = "",
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {
        "crosswalk": await _ops_call(
            ops,
            ops.control_crosswalk,
            control_id=control_id[:160],
            framework=framework[:120],
        )
    }


@router.get("/evidence")
async def list_evidence(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"evidence": await _ops_call(ops, ops.list_evidence)}


@router.post("/evidence", status_code=201)
async def map_evidence(request: Request, body: schemas.SecurityEvidenceIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.map_evidence,
        body.title,
        body.text,
        source=body.source,
        control_ids=body.control_ids,
        submitted_by=_actor(request),
    )


@router.get("/evidence-documents")
async def search_evidence_documents(request: Request, subject: str = "") -> dict:
    """Search configured sources for policies, logs, and assurance evidence."""

    require_permission(request, "operate")
    _ops()
    query = subject.strip()
    if not query:
        raise HTTPException(status_code=422, detail="evidence subject is required")
    from maverick import doc_discovery

    principal = _document_principal(request)
    allow_ambient = principal is None
    configured = await run_in_threadpool(
        doc_discovery.configured_sources,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    if not doc_discovery.enabled() or not configured:
        return {"enabled": False, "hits": []}
    hits = await run_in_threadpool(
        doc_discovery.discover,
        query[:300],
        keywords=(
            "security policy",
            "configuration",
            "audit evidence",
            "attestation",
            "access review",
            "security log",
        ),
        sources=configured,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    return {"enabled": True, "hits": [hit.to_dict() for hit in hits]}


@router.post("/evidence/from-document", status_code=201)
async def map_evidence_from_document(
    request: Request,
    body: schemas.SecurityEvidenceFromDocumentIn,
) -> dict:
    """Fetch bounded source bytes, extract text, and create untrusted evidence."""

    require_permission(request, "operate")
    ops = _ops()
    principal = _document_principal(request)
    try:
        return await run_in_threadpool(
            ops.map_evidence_from_document,
            body.title,
            body.source,
            body.doc_id,
            ref=body.ref,
            control_ids=body.control_ids,
            submitted_by=_actor(request),
            principal=principal,
            allow_ambient_credentials=principal is None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ops.SecurityAuditOutboxError, ops.SecurityStateError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - source failure details stay private
        raise HTTPException(
            status_code=502,
            detail="document source request failed",
        ) from exc


@router.get("/evidence/{evidence_id}")
async def get_evidence(request: Request, evidence_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_evidence, evidence_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such evidence")
    return record


@router.post("/evidence/{evidence_id}/decision")
async def decide_evidence(
    request: Request,
    evidence_id: str,
    body: schemas.SecurityEvidenceDecisionIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.decide_evidence,
        evidence_id[:80],
        body.decision,
        body.rationale,
        _actor(request),
        body.expected_revision,
    )


@router.post("/controls/{control_id}/evidence")
async def apply_control_evidence(
    request: Request,
    control_id: str,
    body: schemas.SecurityEvidenceApplyIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.apply_evidence_to_control,
        control_id[:160],
        body.evidence_id,
        body.implementation_status,
        _actor(request),
        body.expected_revision,
    )


# Risk, exceptions, and POA&M -----------------------------------------------

@router.get("/risks")
async def list_risks(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"risks": await _ops_call(ops, ops.list_risks)}


@router.post("/risks", status_code=201)
async def create_risk(request: Request, body: schemas.SecurityRiskIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.register_risk,
        body.title,
        body.likelihood,
        body.impact,
        body.owner,
        description=body.description,
        control_ids=body.control_ids,
        likelihood_rationale=body.likelihood_rationale,
        impact_rationale=body.impact_rationale,
        evidence_ids=body.evidence_ids,
        created_by=_actor(request),
    )


@router.get("/risks/{risk_id}")
async def get_risk(request: Request, risk_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_risk, risk_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such risk")
    return record


@router.post("/risks/{risk_id}/treatment")
async def set_risk_treatment(
    request: Request, risk_id: str, body: schemas.SecurityRiskTreatmentIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.set_risk_treatment,
        risk_id[:80],
        body.treatment,
        body.plan,
        body.residual_likelihood,
        body.residual_impact,
        body.owner,
        _actor(request),
        body.expected_revision,
        residual_likelihood_rationale=body.residual_likelihood_rationale,
        residual_impact_rationale=body.residual_impact_rationale,
        evidence_ids=body.evidence_ids,
    )


@router.post("/risks/{risk_id}/exception")
async def grant_risk_exception(
    request: Request, risk_id: str, body: schemas.SecurityRiskExceptionIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.grant_risk_exception,
        risk_id[:80],
        body.owner,
        body.rationale,
        body.expires_at,
        _actor(request),
        body.expected_revision,
    )


@router.post("/risks/{risk_id}/close")
async def close_risk(
    request: Request, risk_id: str, body: schemas.SecurityCloseIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.close_risk,
        risk_id[:80],
        body.rationale,
        _actor(request),
        body.expected_revision,
    )


@router.get("/poams")
async def list_poams(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"poams": await _ops_call(ops, ops.list_poams)}


@router.post("/poams", status_code=201)
async def create_poam(request: Request, body: schemas.SecurityPoamIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.create_poam,
        body.finding,
        body.owner,
        body.due_at,
        control_ids=body.control_ids,
        milestones=body.milestones,
        created_by=_actor(request),
    )


@router.get("/poams/{poam_id}")
async def get_poam(request: Request, poam_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_poam, poam_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such POA&M")
    return record


@router.patch("/poams/{poam_id}")
async def update_poam(
    request: Request, poam_id: str, body: schemas.SecurityPoamUpdateIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.update_poam,
        poam_id[:80],
        body.status,
        owner=body.owner,
        due_at=body.due_at,
        milestones=body.milestones,
        note=body.note,
        updated_by=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.post("/poams/{poam_id}/review")
async def trigger_poam_review(
    request: Request, poam_id: str, body: schemas.SecurityReviewTriggerIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.trigger_poam_review,
        poam_id[:80],
        body.reason,
        _actor(request),
        body.expected_revision,
    )


# Vendor, policy, and incident lifecycle ------------------------------------

@router.get("/vendors")
async def list_vendors(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"vendors": await _ops_call(ops, ops.list_vendor_assessments)}


@router.post("/vendors", status_code=201)
async def assess_vendor(request: Request, body: schemas.SecurityVendorIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.assess_vendor,
        body.name,
        body.posture,
        criticality=body.criticality,
        owner=body.owner,
        services=body.services,
        assessment_id=body.assessment_id,
        carry_forward_from=body.carry_forward_from,
        assessed_by=_actor(request),
    )


@router.get("/vendors/{vendor_id}")
async def get_vendor(request: Request, vendor_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_vendor_assessment, vendor_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such vendor assessment")
    return record


@router.post("/vendors/{vendor_id}/decision")
async def decide_vendor(
    request: Request, vendor_id: str, body: schemas.SecurityDecisionIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.decide_vendor_assessment,
        vendor_id[:80],
        body.decision,
        body.rationale,
        _actor(request),
        body.expected_revision,
    )


@router.get("/policies")
async def list_policies(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"policies": await _ops_call(ops, ops.list_policies)}


@router.post("/policies", status_code=201)
async def create_policy(request: Request, body: schemas.SecurityPolicyIn) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.create_policy,
        body.title,
        body.owner,
        content_ref=body.content_ref,
        review_cadence_days=body.review_cadence_days,
        created_by=_actor(request),
    )


@router.get("/policies/{policy_id}")
async def get_policy(request: Request, policy_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_policy, policy_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such policy")
    return record


@router.post("/policies/{policy_id}/transition")
async def transition_policy(
    request: Request, policy_id: str, body: schemas.SecurityPolicyTransitionIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.transition_policy,
        policy_id[:80],
        body.target_status,
        _actor(request),
        note=body.note,
        expected_revision=body.expected_revision,
    )


@router.post("/policies/{policy_id}/attest")
async def attest_policy(
    request: Request, policy_id: str, body: schemas.SecurityPolicyAttestIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.attest_policy,
        policy_id[:80],
        body.subject,
        body.statement,
        _actor(request),
        body.expected_revision,
    )


@router.post("/policies/{policy_id}/review")
async def trigger_policy_review(
    request: Request, policy_id: str, body: schemas.SecurityReviewTriggerIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.trigger_policy_review,
        policy_id[:80],
        body.reason,
        _actor(request),
        body.expected_revision,
    )


@router.get("/incidents")
async def list_security_incidents(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"incidents": await _ops_call(ops, ops.list_incidents)}


@router.post("/incidents", status_code=201)
async def open_security_incident(
    request: Request, body: schemas.SecurityIncidentIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.open_incident,
        body.title,
        severity=body.severity,
        description=body.description,
        mitre_techniques=body.mitre_techniques,
        clock_ids=body.clock_ids,
        custom_clocks=body.custom_clocks,
        reported_by=_actor(request),
        discovered_at=body.discovered_at,
    )


@router.get("/incidents/{incident_id}")
async def get_security_incident(request: Request, incident_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_incident, incident_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return record


@router.post("/incidents/{incident_id}/phase")
async def record_incident_phase(
    request: Request,
    incident_id: str,
    body: schemas.SecurityIncidentPhaseIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.record_incident_phase,
        incident_id[:80],
        body.phase,
        body.note,
        _actor(request),
        body.expected_revision,
        occurred_at=body.occurred_at,
    )


@router.post("/incidents/{incident_id}/clock")
async def start_incident_clock(
    request: Request,
    incident_id: str,
    body: schemas.SecurityIncidentClockIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.start_incident_clock,
        incident_id[:80],
        body.clock_id,
        body.anchor_at,
        _actor(request),
        body.expected_revision,
    )


@router.post("/incidents/{incident_id}/notification")
async def decide_incident_notification(
    request: Request,
    incident_id: str,
    body: schemas.SecurityIncidentNotifyIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.decide_incident_notification,
        incident_id[:80],
        body.clock_id,
        body.notifiable,
        body.rationale,
        _actor(request),
        body.expected_revision,
    )


@router.post("/incidents/{incident_id}/close")
async def close_security_incident(
    request: Request,
    incident_id: str,
    body: schemas.SecurityIncidentCloseIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.close_incident,
        incident_id[:80],
        body.summary,
        _actor(request),
        body.expected_revision,
    )


# Audit engagements ---------------------------------------------------------

@router.get("/audits")
async def list_audits(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return {"audits": await _ops_call(ops, ops.list_audit_engagements)}


@router.post("/audits", status_code=201)
async def create_audit(
    request: Request, body: schemas.SecurityEngagementIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.create_audit_engagement,
        body.name,
        body.framework,
        body.scope,
        body.owner,
        due_at=body.due_at,
        created_by=_actor(request),
    )


@router.get("/audits/{audit_id}")
async def get_audit(request: Request, audit_id: str) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    record = await _ops_call(ops, ops.get_audit_engagement, audit_id[:80])
    if record is None:
        raise HTTPException(status_code=404, detail="no such audit engagement")
    return record


@router.post("/audits/{audit_id}/evidence-requests")
async def add_evidence_request(
    request: Request,
    audit_id: str,
    body: schemas.SecurityEvidenceRequestIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.add_evidence_request,
        audit_id[:80],
        body.description,
        body.owner,
        body.due_at,
        control_ids=body.control_ids,
        requested_by=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.patch("/audits/{audit_id}/evidence-requests")
async def update_evidence_request(
    request: Request,
    audit_id: str,
    body: schemas.SecurityEvidenceRequestUpdateIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.update_evidence_request,
        audit_id[:80],
        body.request_id,
        body.status,
        evidence_ids=body.evidence_ids,
        updated_by=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.post("/audits/{audit_id}/control-tests")
async def record_control_test(
    request: Request,
    audit_id: str,
    body: schemas.SecurityControlTestIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.record_control_test,
        audit_id[:80],
        body.control_id,
        body.procedure,
        body.result,
        evidence_ids=body.evidence_ids,
        tested_by=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.post("/audits/{audit_id}/findings")
async def add_audit_finding(
    request: Request,
    audit_id: str,
    body: schemas.SecurityAuditFindingIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.add_audit_finding,
        audit_id[:80],
        body.title,
        body.severity,
        control_ids=body.control_ids,
        owner=body.owner,
        due_at=body.due_at,
        created_by=_actor(request),
        expected_revision=body.expected_revision,
    )


@router.patch("/audits/{audit_id}/findings")
async def update_audit_finding(
    request: Request,
    audit_id: str,
    body: schemas.SecurityAuditFindingUpdateIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.update_audit_finding,
        audit_id[:80],
        body.finding_id,
        body.status,
        _actor(request),
        body.expected_revision,
    )


@router.patch("/audits/{audit_id}/status")
async def update_audit_status(
    request: Request,
    audit_id: str,
    body: schemas.SecurityEngagementStatusIn,
) -> dict:
    require_permission(request, "operate")
    ops = _ops()
    return await _ops_record_call(
        ops,
        ops.update_audit_engagement_status,
        audit_id[:80],
        body.status,
        _actor(request),
        body.expected_revision,
    )


# Defensive-hunter helpers --------------------------------------------------

_MAX_ENVIRONMENT_BATCH_BYTES = 16 * 1024 * 1024
_PLATFORM_STATUSES = {
    "open", "triaged", "investigating", "resolved", "false_positive", "closed",
}
_INVESTIGATION_STATUSES = {
    "open", "triaged", "investigating", "contained", "resolved", "closed",
}


def _bounded_json(value: object, *, maximum: int, label: str) -> None:
    """Reject oversized already-decoded request structures before analysis."""
    try:
        size = len(json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{label} is not valid JSON") from exc
    if size > maximum:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds the {maximum // (1024 * 1024)} MiB limit",
        )


def _audit_media_snapshot(paths, audit_dir) -> tuple[tuple[str, bool, int, str], ...]:
    """Commit the exact audit media set consumed by verification and hunting."""
    from pathlib import Path

    directory = Path(audit_dir)
    candidates = {Path(path) for path in paths}
    if directory.exists() and directory.is_dir():
        candidates.update(directory.glob("*.ndjson"))
    candidates.update({directory / "anchors.ndjson", directory / ".anchors.required"})
    snapshot = []
    for path in sorted(candidates, key=lambda item: item.name):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            snapshot.append((path.name, False, 0, ""))
        except OSError:
            snapshot.append((path.name, True, -1, "unreadable"))
        else:
            snapshot.append((path.name, True, len(raw), hashlib.sha256(raw).hexdigest()))
    return tuple(snapshot)


def _capture_verified_audit_rows(
    paths, audit_dir,
) -> tuple[list[dict], tuple[tuple[str, bool, int, str], ...]]:
    """Authenticate and parse identity-bound bytes, returning their commitment.

    Closed segments are checked against the exact signed anchor snapshot. The
    live day is unanchored by design, but every captured row is still verified
    from the exact bytes read through the custody helper.  The returned media
    commitment is deliberately computed from those same bytes; callers bind it
    into their before/read/verify comparison so a same-path ABA rollback cannot
    make rows from one valid signed snapshot appear to belong to another.
    """
    from pathlib import Path

    from maverick.audit.worm import (
        _read_custodied_file,
        _verified_chain_records,
        _verified_source_snapshot,
    )

    directory = Path(audit_dir)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    normalized = tuple(sorted({Path(path) for path in paths}, key=str))
    closed = [path for path in normalized if path.stem < today]
    closed_snapshot = _verified_source_snapshot(directory, closed) if closed else {}
    rows: list[dict] = []
    captured_media: list[tuple[str, bool, int, str]] = []
    for path in normalized:
        raw = (
            closed_snapshot[path.name]
            if path.stem < today
            else _read_custodied_file(path, mode=0o600)
        )
        captured_media.append(
            (path.name, True, len(raw), hashlib.sha256(raw).hexdigest())
        )
        rows.extend(_verified_chain_records(raw))
    return rows, tuple(captured_media)


def _bind_captured_audit_snapshot(
    observed: tuple[tuple[str, bool, int, str], ...],
    captured: tuple[tuple[str, bool, int, str], ...],
) -> tuple[tuple[str, bool, int, str], ...]:
    """Replace selected-path observations with the exact bytes rows came from.

    ``observed`` also commits the anchor ledger, deletion marker, and audit files
    outside the selected window.  Only selected day-file entries are replaced;
    this retains that wider media-set check while binding verdict inputs to the
    identity-bound handles consumed by :func:`_capture_verified_audit_rows`.
    """
    captured_by_name = {entry[0]: entry for entry in captured}
    if len(captured_by_name) != len(captured):
        raise RuntimeError("captured audit media contains duplicate names")
    observed_names = {entry[0] for entry in observed}
    if not set(captured_by_name).issubset(observed_names):
        raise RuntimeError("captured audit media is absent from the observed set")
    return tuple(captured_by_name.get(entry[0], entry) for entry in observed)


def _audit_window(
    hours: int,
) -> tuple[list[dict], list, Any, float, float, tuple, tuple]:
    """Load a bounded tenant-local audit window and its signed media paths."""
    from maverick.audit.export import audit_event_paths
    from maverick.audit.reader import resolve_audit_dir
    from maverick.paths import current_tenant_id

    now = time.time()
    cutoff = now - hours * 3600.0
    baseline_cutoff = cutoff - 30 * 86400.0
    since_day = dt.datetime.fromtimestamp(
        baseline_cutoff, tz=dt.timezone.utc,
    ).date().isoformat()
    tenant = current_tenant_id()
    audit_dir = resolve_audit_dir(tenant)
    paths = audit_event_paths(since=since_day, tenant=tenant)
    snapshot_before = _audit_media_snapshot(paths, audit_dir)
    try:
        captured, captured_media = _capture_verified_audit_rows(paths, audit_dir)
        snapshot_after_read = _bind_captured_audit_snapshot(
            _audit_media_snapshot(paths, audit_dir), captured_media,
        )
    except Exception:  # failure-policy: fail_closed
        captured = []
        snapshot_after_read = (("audit-evidence", True, -1, "verification_error"),)
    rows = []
    for row in captured:
        try:
            observed = float(row.get("ts", 0.0))
        except (TypeError, ValueError):
            continue
        if baseline_cutoff <= observed <= now + 300:
            rows.append(row)
    # Preserve the newest bounded slice when a very busy deployment exceeds it.
    rows = sorted(rows, key=lambda item: float(item.get("ts", 0.0)))[-50_000:]
    return (
        rows,
        paths,
        audit_dir,
        cutoff,
        baseline_cutoff,
        snapshot_before,
        snapshot_after_read,
    )


def _approval_detail(kind: str, payload: dict) -> str:
    return json.dumps(
        {"kind": kind, **payload},
        sort_keys=True,
        separators=(",", ":"),
    )


def _environment_tenant_binding(tenant: str | None = None) -> str:
    """Return the exact canonical tenant binding for one trusted adapter.

    An empty string is the legacy single-tenant/root namespace.  Tenant-scoped
    requests never fall back to it: a credential-owning transport, enrichment
    provider, or mutating executor registered for one customer must not become
    ambient authority for another customer merely because both use the same
    connector name.
    """
    from maverick.paths import canonical_tenant_id, current_tenant_id_strict

    selected = current_tenant_id_strict() if tenant is None else tenant
    return canonical_tenant_id(selected) if selected else ""


def _environment_shield_posture(events, sigma_rules: list[str]) -> bool:
    """Screen hunter inputs when Shield exists; otherwise reduce autonomy.

    The kernel deliberately permits installations without ``maverick-shield``.
    Environment hunting therefore continues deterministic, local detection in
    that posture, but callers must suppress pivots, enrichment, automatic
    playbook proposals, and response execution.  When Shield is installed, any
    block or scanner error fails this batch toward the gate.
    """
    from maverick import shield_policy

    if not shield_policy.shield_available():
        log.warning(
            "environment hunter is running without Shield; reduced autonomy is active"
        )
        return False
    texts = [
        json.dumps(_jsonable(event), sort_keys=True, default=str)
        for event in events
    ]
    texts.extend(str(rule) for rule in sigma_rules)
    chunk = ""
    chunks: list[str] = []
    for text in texts:
        candidate = f"{chunk}\n{text}" if chunk else text
        if len(candidate.encode("utf-8")) <= 256 * 1024:
            chunk = candidate
            continue
        if chunk:
            chunks.append(chunk)
        chunk = text
    if chunk:
        chunks.append(chunk)
    for text in chunks:
        if shield_policy.scan_block(text):
            raise PermissionError("environment telemetry was rejected by Shield")
    return True


def _response_payload(proposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": proposal.digest,
        "action": proposal.action,
        "target": proposal.target,
        "reason": proposal.reason,
        "parameters": proposal.parameters,
        "executor": proposal.executor,
        "evidence_ids": [item.event_id for item in proposal.evidence],
    }


