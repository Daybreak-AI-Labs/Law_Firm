"""Pydantic inputs for the governed Model Risk assurance API."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., pattern="^(approved|rejected|revoked)$")
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)

def _validate_string_items(
    values: list[str],
    *,
    label: str,
    max_length: int,
) -> None:
    """Apply the core's per-item text boundary at the HTTP schema edge."""
    for index, value in enumerate(values):
        if not value.strip():
            raise ValueError(f"{label} {index} must not be empty")
        if len(value) > max_length:
            raise ValueError(
                f"{label} {index} exceeds the {max_length}-character limit"
            )


def _validate_assurance_json(
    value: object,
    *,
    label: str,
    max_bytes: int,
    max_depth: int,
    max_nodes: int,
    max_object_items: int | None = None,
    max_array_items: int | None = None,
    max_string_chars: int | None = None,
    max_key_chars: int | None = None,
) -> None:
    """Bound nested assurance values before they reach synchronous stores."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"{label} exceeds the structure limit")
        if depth > max_depth:
            raise ValueError(f"{label} exceeds the nesting limit")
        if isinstance(current, str):
            if max_string_chars is not None and len(current) > max_string_chars:
                raise ValueError(
                    f"{label} contains a string longer than "
                    f"{max_string_chars} characters"
                )
        elif isinstance(current, dict):
            if max_object_items is not None and len(current) > max_object_items:
                raise ValueError(f"{label} contains an oversized object")
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} object keys must be strings")
                if max_key_chars is not None and len(key) > max_key_chars:
                    raise ValueError(f"{label} contains an oversized object key")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if max_array_items is not None and len(current) > max_array_items:
                raise ValueError(f"{label} contains an oversized array")
            stack.extend((item, depth + 1) for item in current)


# Model Risk & AI Assurance Officer ----------------------------------------

_MODEL_RISK_DIGEST = "^(?:sha256:)?[0-9a-fA-F]{64}$"
_MODEL_RISK_ASSET_ID = "^MAA-[0-9a-f]{32}$"
_TRAINING_RECEIPT_ID = r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$"


class ModelRiskObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str = Field(..., pattern="^(model|agent|tool|dataset|provider)$")
    source: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=256)
    display_name: str = Field(..., min_length=1, max_length=256)
    version_digest: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    expected_revision: int = Field(0, ge=0)
    observed_at: float | None = Field(None, gt=0)
    metadata: dict = Field(default_factory=dict, max_length=128)
    dependencies: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def bound_observation(self):
        _validate_string_items(
            self.dependencies, label="model-risk dependency", max_length=256
        )
        _validate_assurance_json(
            self.metadata,
            label="model-risk metadata",
            max_bytes=64 * 1024,
            max_depth=8,
            max_nodes=2048,
            max_object_items=128,
            max_array_items=256,
            max_string_chars=4000,
            max_key_chars=128,
        )
        return self


class ModelRiskDeclarationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    owner: str = Field(..., min_length=1, max_length=256)
    purpose: str = Field(..., min_length=1, max_length=4000)
    intended_use: str = Field(..., min_length=1, max_length=4000)
    risk_tier: str = Field("medium", pattern="^(low|medium|high|critical)$")
    lifecycle: str = Field("proposed", pattern="^(proposed|rejected)$")
    risk_context: dict = Field(default_factory=dict, max_length=128)
    eu_ai_act: dict = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def bound_declaration(self):
        for value, label, fields in (
            (self.risk_context, "model-risk context", 128),
            (self.eu_ai_act, "EU AI Act assertion", 32),
        ):
            _validate_assurance_json(
                value,
                label=label,
                max_bytes=64 * 1024,
                max_depth=8,
                max_nodes=2048,
                max_object_items=fields,
                max_array_items=128,
                max_string_chars=4000,
                max_key_chars=128,
            )
        return self


class ModelRiskDeclarationUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=1)
    owner: str | None = Field(None, min_length=1, max_length=256)
    purpose: str | None = Field(None, min_length=1, max_length=4000)
    intended_use: str | None = Field(None, min_length=1, max_length=4000)
    risk_tier: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    risk_context: dict | None = Field(None, max_length=128)
    eu_ai_act: dict | None = Field(None, max_length=32)

    @model_validator(mode="after")
    def require_bounded_change(self):
        if all(
            value is None
            for value in (
                self.owner,
                self.purpose,
                self.intended_use,
                self.risk_tier,
                self.risk_context,
                self.eu_ai_act,
            )
        ):
            raise ValueError("at least one declaration field must be updated")
        for value, label, fields in (
            (self.risk_context, "model-risk context", 128),
            (self.eu_ai_act, "EU AI Act assertion", 32),
        ):
            if value is not None:
                _validate_assurance_json(
                    value,
                    label=label,
                    max_bytes=64 * 1024,
                    max_depth=8,
                    max_nodes=2048,
                    max_object_items=fields,
                    max_array_items=128,
                    max_string_chars=4000,
                    max_key_chars=128,
                )
        return self


class ModelRiskDeclarationReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., pattern="^(approved|rejected)$")
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class ModelRiskEvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    source_id: str = Field(..., min_length=1, max_length=256)
    evidence_kind: str = Field(
        ...,
        pattern=(
            "^(evaluation|red_team|drift_monitoring|model_card|data_assessment|"
            "vendor_assessment|incident_analysis|human_oversight_test)$"
        ),
    )
    result: str = Field(..., pattern="^(passed|failed|inconclusive)$")
    scope_digest: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    artifact_digest: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    evaluator_digest: str | None = Field(None, pattern=_MODEL_RISK_DIGEST)
    summary: str = Field("", max_length=4000)
    metrics: dict = Field(default_factory=dict, max_length=128)
    observed_at: float = Field(..., gt=0)
    valid_until: float = Field(..., gt=0)

    @model_validator(mode="after")
    def bound_evidence(self):
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be later than observed_at")
        _validate_assurance_json(
            self.metrics,
            label="model-risk evidence metrics",
            max_bytes=64 * 1024,
            max_depth=8,
            max_nodes=2048,
            max_object_items=128,
            max_array_items=256,
            max_string_chars=4000,
            max_key_chars=128,
        )
        return self


class ModelRiskTrainingReceiptEvidenceIn(BaseModel):
    """Register one tenant-private receipt using server-side trust policy."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    receipt_id: str = Field(..., pattern=_TRAINING_RECEIPT_ID)
    actor: str = Field(..., min_length=1, max_length=256)
    valid_until: float = Field(..., gt=0)

    @model_validator(mode="after")
    def validate_training_receipt_boundary(self):
        if self.actor != self.actor.strip():
            raise ValueError("actor must not have leading or trailing whitespace")
        return self


class ModelRiskIncidentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    source_id: str = Field(..., min_length=1, max_length=256)
    severity: str = Field(..., pattern="^(low|medium|high|critical)$")
    summary: str = Field(..., min_length=1, max_length=4000)
    occurred_at: float = Field(..., gt=0)
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def bound_evidence_ids(self):
        _validate_string_items(
            self.evidence_ids, label="model-risk evidence id", max_length=64
        )
        return self


class ModelRiskIncidentUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., pattern="^(open|contained|resolved|dismissed)$")
    resolution: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class ModelRiskAcceptanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_sha256: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    rationale: str = Field(..., min_length=1, max_length=4000)
    expires_at: float = Field(..., gt=0)
    expected_revision: int = Field(0, ge=0)


class ModelRiskPromotionAuthorizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., min_length=1, max_length=256)
    rung: str = Field(
        ..., pattern="^(config|prompt|tool|policy|evaluator|code|weights)$"
    )
    payload_sha256: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    artifact_revision: int = Field(..., ge=1)
    artifact_digest: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    declaration_revision: int = Field(..., ge=1)
    evaluator_asset_id: str = Field(..., pattern=_MODEL_RISK_ASSET_ID)
    evaluator_revision: int = Field(..., ge=1)
    evaluator_digest: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    evidence_ids: list[str] = Field(..., min_length=1, max_length=64)
    rationale: str = Field(..., min_length=1, max_length=4000)
    expires_at: float = Field(..., gt=0)
    expected_revision: int = Field(0, ge=0)

    @model_validator(mode="after")
    def bound_promotion_evidence(self):
        _validate_string_items(
            self.evidence_ids, label="promotion evidence id", max_length=64
        )
        return self


class ModelRiskPromotionRevocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., min_length=1, max_length=256)
    rung: str = Field(
        ..., pattern="^(config|prompt|tool|policy|evaluator|code|weights)$"
    )
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class ModelRiskDeploymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(..., min_length=1, max_length=256)
    candidate_id: str = Field(..., min_length=1, max_length=256)
    rung: str = Field(
        ..., pattern="^(config|prompt|tool|policy|evaluator|code|weights)$"
    )
    payload_sha256: str = Field(..., pattern=_MODEL_RISK_DIGEST)
    predecessor_digest: str | None = Field(None, pattern=_MODEL_RISK_DIGEST)
    expected_revision: int = Field(0, ge=0)


# AI Evidence-Ready Gateway -------------------------------------------------

_GATEWAY_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_GATEWAY_DIGEST = r"^(?:sha256:)?[0-9a-fA-F]{64}$"
_GATEWAY_OPTIONAL_DIGEST = (
    r"^(?:$|(?:sha256:)?[0-9a-fA-F]{64})$"
)


def _require_https(value: str, *, label: str) -> None:
    if not value.startswith("https://"):
        raise ValueError(f"{label} must use HTTPS")


class EvidenceGatewayCitationIn(BaseModel):
    """One immutable source reference attached to a regulatory impact."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_name: str = Field(..., min_length=1, max_length=256)
    retrieval_url: str = Field(..., min_length=9, max_length=2048)
    record_url: str = Field(..., min_length=9, max_length=2048)
    retrieved_at: str = Field(..., min_length=1, max_length=64)
    content_sha256: str = Field(..., pattern=_GATEWAY_DIGEST)
    feed_url: str | None = Field(None, min_length=9, max_length=2048)
    payload_sha256: str | None = Field(None, pattern=_GATEWAY_DIGEST)
    source_record_sha256: str | None = Field(
        None,
        pattern=_GATEWAY_DIGEST,
    )
    source_format: str | None = Field(None, min_length=1, max_length=32)
    parser_version: str | None = Field(None, min_length=1, max_length=64)
    acquisition: str | None = Field(None, min_length=1, max_length=64)
    acquired_by: str | None = Field(None, min_length=1, max_length=128)
    official_citation: str | None = Field(None, max_length=1024)

    @model_validator(mode="after")
    def require_https_sources(self):
        _require_https(self.retrieval_url, label="retrieval_url")
        _require_https(self.record_url, label="record_url")
        if self.feed_url is not None:
            _require_https(self.feed_url, label="feed_url")
        try:
            retrieved = datetime.fromisoformat(
                self.retrieved_at.replace("Z", "+00:00")
            )
            retrieved_timestamp = retrieved.timestamp()
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                "retrieved_at must be an ISO-8601 timestamp"
            ) from exc
        if (
            retrieved.tzinfo is None
            or retrieved_timestamp > time.time() + 300
        ):
            raise ValueError(
                "retrieved_at must be timezone-aware and not in the future"
            )
        return self


class EvidenceGatewayPolicyUpsertIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int | None = Field(None, ge=0)
    disclosure_text: str = Field(
        "AI disclosure: You are interacting with an AI system. Review important "
        "outputs before relying on them.",
        min_length=1,
        max_length=2000,
    )
    require_interaction_disclosure: bool = True
    require_machine_readable_marking: bool = True
    require_visible_marking: bool = False
    supported_modalities: list[str] = Field(
        default_factory=lambda: ["text"],
        min_length=1,
        max_length=16,
    )
    machine_marker: str = Field(
        "[maverick-ai-generated; evidence-receipt={receipt_id}]",
        min_length=1,
        max_length=1000,
    )
    visible_marker: str = Field(
        "Disclosure: This text was generated by an AI system.",
        min_length=1,
        max_length=1000,
    )
    model_sha256: str = Field("", pattern=_GATEWAY_OPTIONAL_DIGEST)
    context_sha256: str = Field("", pattern=_GATEWAY_OPTIONAL_DIGEST)
    metadata: dict = Field(default_factory=dict, max_length=128)

    @model_validator(mode="after")
    def bound_policy(self):
        if any(value != "text" for value in self.supported_modalities):
            raise ValueError("v1 supports only the text modality")
        if len(set(self.supported_modalities)) != len(self.supported_modalities):
            raise ValueError("supported_modalities must not contain duplicates")
        if self.machine_marker.count("{receipt_id}") != 1:
            raise ValueError(
                "machine_marker must contain one {receipt_id} placeholder"
            )
        marker_remainder = self.machine_marker.replace("{receipt_id}", "")
        if "{" in marker_remainder or "}" in marker_remainder:
            raise ValueError(
                "machine_marker contains an unsupported placeholder"
            )
        _validate_assurance_json(
            self.metadata,
            label="gateway policy metadata",
            max_bytes=64 * 1024,
            max_depth=8,
            max_nodes=2048,
            max_object_items=128,
            max_array_items=256,
            max_string_chars=4000,
            max_key_chars=128,
        )
        return self


class EvidenceGatewayDeliveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generated_text: str = Field(..., min_length=1, max_length=16 * 1024 * 1024)
    input_text: str = Field("", max_length=16 * 1024 * 1024)
    conversation_id: str = Field(..., min_length=1, max_length=1024)
    idempotency_key: str = Field(..., min_length=1, max_length=1024)
    policy_id: str = Field("default", pattern=_GATEWAY_ID)
    model_sha256: str = Field("", pattern=_GATEWAY_OPTIONAL_DIGEST)
    context_sha256: str = Field("", pattern=_GATEWAY_OPTIONAL_DIGEST)
    modality: str = Field("text", pattern="^text$")
    human_interaction: Literal[True] = True
    synthetic_content: Literal[True] = True
    deepfake: bool = False
    public_interest_text: bool = False


class EvidenceGatewayRegulatoryImpactIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    alert_id: str = Field(..., min_length=1, max_length=256)
    alert_revision: int = Field(..., ge=0)
    content_sha256: str = Field(..., pattern=_GATEWAY_DIGEST)
    citations: list[EvidenceGatewayCitationIn] = Field(
        ...,
        min_length=1,
        max_length=64,
    )
    affected_policy_ids: list[str] = Field(
        default_factory=lambda: ["default"],
        min_length=1,
        max_length=64,
    )
    affected_asset_ids: list[str] = Field(default_factory=list, max_length=128)
    control_ids: list[str] = Field(default_factory=list, max_length=128)
    match_reasons: list[str] = Field(default_factory=list, max_length=128)
    impact_id: str | None = Field(None, pattern=_GATEWAY_ID)

    @model_validator(mode="after")
    def bound_regulatory_impact(self):
        for values, label, maximum in (
            (self.affected_policy_ids, "affected policy id", 128),
            (self.affected_asset_ids, "affected asset id", 256),
            (self.control_ids, "control id", 256),
            (self.match_reasons, "match reason", 1000),
        ):
            _validate_string_items(values, label=label, max_length=maximum)
            if len(set(values)) != len(values):
                raise ValueError(f"{label}s must not contain duplicates")
        return self


class EvidenceGatewayImpactReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: str = Field(..., pattern="^(accepted|dismissed)$")
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=0)


class EvidenceGatewayPacketVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    packet: dict

    @model_validator(mode="after")
    def bound_packet(self):
        _validate_assurance_json(
            self.packet,
            label="assurance packet",
            max_bytes=2 * 1024 * 1024,
            max_depth=16,
            max_nodes=50_000,
            max_object_items=4096,
            max_array_items=10_000,
            max_string_chars=262_144,
            max_key_chars=256,
        )
        return self
