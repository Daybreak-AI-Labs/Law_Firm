"""Privacy and draft-safety invariants for Ekko work discovery."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from maverick.work_discovery import (
    CapturePolicy,
    ObservedActivity,
    WorkEvent,
    build_draft_bundle,
    discover_candidates,
)


def _timestamp(day: int, hour: int = 12) -> float:
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc).timestamp()


def _event(
    event_id: str,
    session_id: str,
    sequence: int,
    day: int,
    app: str,
    action: str,
    object_type: str,
    duration: float = 60.0,
) -> WorkEvent:
    return WorkEvent(
        event_id=event_id,
        session_id=session_id,
        sequence=sequence,
        occurred_at=_timestamp(day),
        app=app,
        action=action,
        object_type=object_type,
        duration_seconds=duration,
    )


def test_observation_schema_rejects_content_bearing_extension_fields():
    secret = "Quarterly Board Deck - confidential"  # pragma: allowlist secret

    with pytest.raises(ValueError, match="forbidden fields") as exc:
        ObservedActivity.from_mapping({
            "app": "powerpoint",
            "action": "edit",
            "window_title": secret,
        })

    assert secret not in str(exc.value)


def test_persisted_policy_parser_restores_sensitive_block_floor():
    original = CapturePolicy(
        enabled=True,
        capture_level="guided",
        allowed_apps=frozenset({"excel"}),
        blocked_apps=frozenset(),
        allowed_actions=frozenset({"open"}),
        allowed_object_types=frozenset({"report"}),
        retention_days=14,
        min_occurrences=2,
        min_distinct_days=2,
        poll_interval_seconds=5,
        provider_egress=False,
    )

    policy = CapturePolicy.from_dict(original.to_dict())

    assert "email" in policy.blocked_apps
    assert "slack" in policy.blocked_apps
    assert policy.fingerprint() == original.fingerprint()


def test_persisted_policy_parser_uses_sensitive_floor_when_field_missing():
    policy = CapturePolicy.from_dict({
        "enabled": True,
        "capture_level": "guided",
        "allowed_apps": ["excel", "email"],
        "allowed_actions": ["open"],
        "allowed_object_types": ["report"],
        "retention_days": 14,
        "min_occurrences": 2,
        "min_distinct_days": 2,
        "poll_interval_seconds": 5,
        "provider_egress": False,
    })

    assert policy.allows(ObservedActivity("excel", "open", "report"))
    assert not policy.allows(ObservedActivity("email", "open", "report"))


def test_application_metadata_policy_cannot_smuggle_guided_labels():
    policy = CapturePolicy(
        enabled=True,
        capture_level="application_metadata",
        allowed_apps=frozenset({"chrome"}),
        allowed_actions=frozenset({"switch", "download"}),
        allowed_object_types=frozenset({"none", "report"}),
    )

    with pytest.raises(ValueError, match="application_metadata"):
        policy.require_valid(require_enabled=True)


def test_guided_policy_rejects_sensitive_object_metadata():
    policy = CapturePolicy(
        enabled=True,
        capture_level="guided",
        allowed_apps=frozenset({"chrome"}),
        allowed_actions=frozenset({"open"}),
        allowed_object_types=frozenset({"report", "email"}),
    )

    with pytest.raises(ValueError, match="sensitive object kinds"):
        policy.require_valid(require_enabled=True)
    assert not policy.allows(ObservedActivity("chrome", "open", "email"))


def test_discovery_requires_recurrence_across_distinct_days():
    same_day = [
        _event("one", "s1", 1, 1, "chrome", "download", "report"),
        _event("two", "s1", 2, 1, "powerpoint", "create", "presentation"),
        _event("three", "s1", 3, 1, "chrome", "download", "report"),
        _event("four", "s1", 4, 1, "powerpoint", "create", "presentation"),
    ]

    assert discover_candidates(
        same_day, min_occurrences=2, min_distinct_days=2,
    ) == []


def test_report_to_presentation_becomes_an_evidence_backed_unsaved_draft():
    events = []
    for day in (1, 2, 3):
        session = f"session-{day}"
        events.extend([
            _event(f"download-{day}", session, 1, day, "chrome", "download", "report", 90),
            _event(f"build-{day}", session, 2, day, "powerpoint", "create", "presentation", 510),
        ])

    opportunities = discover_candidates(
        events, min_occurrences=3, min_distinct_days=2,
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.title == "Automate recurring report-to-presentation workflow"
    assert opportunity.occurrences == 3
    assert opportunity.distinct_days == 3
    assert opportunity.risk == "low"
    assert len(opportunity.evidence) == 3

    bundle = build_draft_bundle(opportunity, owner="alice")
    assert bundle.unsaved is True
    assert bundle.requires_human_approval is True
    assert bundle.flow.owner == "alice"
    assert bundle.flow.schedule == ""
    assert bundle.flow.nodes["human-review"].kind == "approval"
    assert bundle.flow.validate() == []
    assert bundle.demonstration.source == "ekko-semantic-observation"


def test_discovery_deduplicates_event_ids_instead_of_double_counting():
    event = _event("duplicate", "s1", 1, 1, "excel", "open", "report")

    assert discover_candidates([event, event], min_occurrences=2) == []
