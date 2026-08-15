"""Ekko dashboard lifecycle, discovery, and preview-only authoring handoff."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(tmp_path, monkeypatch, *, enabled: bool = True):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "\n".join([
            "[ekko]",
            f"enable = {'true' if enabled else 'false'}",
            'capture_level = "application_metadata"',
            'allowed_apps = ["excel", "powerpoint"]',
            (
                'blocked_apps = ["email", "outlook", "gmail", "chat", "teams", '
                '"slack", "crm", "salesforce", "erp", "sap", "database"]'
            ),
            "retention_days = 14",
            "min_occurrences = 2",
            "min_distinct_days = 2",
            "poll_interval_seconds = 5",
            "provider_egress = false",
            "",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    for name in ("MAVERICK_EKKO", "MAVERICK_CONFIG_OVERLAY", "MAVERICK_TENANT"):
        monkeypatch.delenv(name, raising=False)
    from maverick.config import reset_config_cache

    reset_config_cache()
    return cfg


def _enroll(owner: str | None = None, device: str = "desk"):
    from maverick.config import get_ekko_policy
    from maverick.work_discovery_identity import local_os_principal
    from maverick.work_discovery_store import WorkDiscoveryStore

    store = WorkDiscoveryStore(owner or local_os_principal(), device)
    store.enroll(get_ekko_policy())
    return store


def _seed_recurring_evidence(store) -> None:
    from maverick.work_discovery import SessionState, WorkEvent

    policy = store.get_policy()
    assert policy is not None
    first_day = datetime.now(timezone.utc).date() - timedelta(days=2)
    base = datetime.combine(first_day, time(hour=12), tzinfo=timezone.utc).timestamp()
    for day in range(2):
        session = store.create_session(
            policy=policy,
            session_id=f"repeat-{day}",
            started_at=base + day * 86_400,
        )
        collector_id = f"collector-seed-{day}"
        store.claim_collector(
            session.session_id,
            collector_id,
            policy=policy,
            ttl_seconds=30,
        )
        store.append_event(WorkEvent(
            event_id=f"event-{day}-1",
            session_id=session.session_id,
            sequence=1,
            occurred_at=base + day * 86_400 + 10,
            app="excel",
            action="switch",
            object_type="none",
            duration_seconds=120,
        ), policy=policy, collector_id=collector_id)
        store.append_event(WorkEvent(
            event_id=f"event-{day}-2",
            session_id=session.session_id,
            sequence=2,
            occurred_at=base + day * 86_400 + 140,
            app="powerpoint",
            action="switch",
            object_type="none",
            duration_seconds=480,
        ), policy=policy, collector_id=collector_id)
        store.transition_session(
            session.session_id,
            SessionState.STOPPED,
            at=base + day * 86_400 + 700,
        )


def test_ekko_page_and_agent_factory_entry_explain_the_safe_boundary(
    tmp_path, monkeypatch,
):
    _isolate(tmp_path, monkeypatch, enabled=False)

    page = client.get("/ekko")
    assert page.status_code == 200
    for phrase in (
        "EKKO",
        "Work Discovery",
        "Ekko is off by default",
        "Never captures",
        "not employee productivity",
        "lightwork.authoring-handoff",
    ):
        assert phrase in page.text
    assert "collector online" not in page.text.lower()
    assert "collector heartbeat stale" in page.text.lower()

    factory = client.get("/agents")
    assert factory.status_code == 200
    assert 'id="ekko-work-discovery-card"' in factory.text
    assert 'href="/ekko"' in factory.text


def test_status_is_default_off_and_payload_cannot_inject_owner_or_tenant(
    tmp_path, monkeypatch,
):
    _isolate(tmp_path, monkeypatch, enabled=False)
    status = client.get("/api/v1/ekko/status?device_id=desk")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["enrollment"] is None

    injected = client.post(
        "/api/v1/ekko/sessions",
        json={
            "device_id": "desk",
            "acknowledge_capture_scope": True,
            "owner": "user:mallory",
            "tenant": "other-client",
        },
    )
    assert injected.status_code == 422


def test_owner_bound_session_start_pause_resume_stop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _enroll(device="desk")

    missing_ack = client.post(
        "/api/v1/ekko/sessions",
        json={"device_id": "desk", "acknowledge_capture_scope": False},
    )
    assert missing_ack.status_code == 400

    started = client.post(
        "/api/v1/ekko/sessions",
        json={"device_id": "desk", "acknowledge_capture_scope": True},
    )
    assert started.status_code == 201
    session_id = started.json()["session"]["session_id"]
    assert started.json()["session"]["state"] == "running"
    assert started.json()["unsaved"] is True

    for action, expected in (
        ("pause", "paused"),
        ("resume", "running"),
        ("stop", "stopped"),
    ):
        response = client.post(
            f"/api/v1/ekko/sessions/{session_id}/{action}",
            json={"device_id": "desk"},
        )
        assert response.status_code == 200
        assert response.json()["session"]["state"] == expected

    # A different device selector remains inside the same owner but cannot see
    # or mutate the enrolled device's session.
    hidden = client.get("/api/v1/ekko/status?device_id=other-device").json()
    assert hidden["session"] is None and hidden["event_count"] == 0
    denied = client.post(
        f"/api/v1/ekko/sessions/{session_id}/pause",
        json={"device_id": "other-device"},
    )
    assert denied.status_code == 404


def test_mining_and_handoff_are_evidence_backed_preview_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = _enroll(device="desk")
    _seed_recurring_evidence(store)

    mined = client.post(
        "/api/v1/ekko/candidates/mine",
        json={"device_id": "desk"},
    )
    assert mined.status_code == 200
    assert mined.json()["count"] >= 1
    candidate = mined.json()["candidates"][0]
    assert candidate["occurrences"] >= 2
    assert candidate["distinct_days"] == 2
    assert candidate["evidence"]

    handoff = client.post(
        f"/api/v1/ekko/candidates/{candidate['opportunity_id']}/handoff",
        json={"device_id": "desk", "kind": "flow"},
    )
    assert handoff.status_code == 200
    body = handoff.json()
    assert body["unsaved"] is True
    assert body["requires_human_approval"] is True
    assert body["redirect"] == "/flows/designer"
    assert "approved, content-free application metadata" in body["brief"]
    assert "do not" in body["brief"].lower()

    erased = client.delete("/api/v1/ekko/data?device_id=desk")
    assert erased.status_code == 200
    assert erased.json()["erased"]["sessions"] == 2
    assert erased.json()["erased"]["events"] == 4
    assert client.get(
        "/api/v1/ekko/candidates?device_id=desk"
    ).json()["candidates"] == []


def test_forget_device_revokes_enrollment_and_erases_data(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = _enroll(device="desk")
    session = store.create_session(policy=store.get_policy())

    forgotten = client.delete("/api/v1/ekko/device?device_id=desk")

    assert forgotten.status_code == 200
    assert forgotten.json()["forgotten"]["enrollments"] == 1
    status = client.get("/api/v1/ekko/status?device_id=desk").json()
    assert status["enrollment"] is None
    assert status["session"] is None
    assert store.get_session(session.session_id) is None
