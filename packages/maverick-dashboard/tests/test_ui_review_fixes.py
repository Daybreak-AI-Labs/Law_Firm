"""User-reported UI fixes: the pop-out→approval bridge, the audit quick
filter, capability/feature descriptions, and the departments dialog."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _fresh_world(tmp_path, monkeypatch):
    from maverick import config, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    config.reset_config_cache()
    import maverick_dashboard.api as api
    api._world_cache.clear()
    yield
    config.reset_config_cache()
    api._world_cache.clear()


def test_popout_decision_bridges_to_the_approval_row():
    html = client.get("/approvals").text
    # The pop-out announces its decision...
    assert "mv-assessment-decided" in html
    # ...and the queue page listens and clicks the matching row's own
    # (audited) approve/deny path.
    assert "tr.dataset.assessment" in html
    assert "addEventListener('mv-assessment-decided'" in html


def test_audit_page_has_instant_filter():
    from maverick.audit import record
    record("TOOL_CALL", agent="pia-analyst", goal_id=1, tool="email")
    html = client.get("/audit").text
    assert 'id="audit-quick"' in html
    assert 'id="audit-quick-count"' in html
    assert 'id="audit-table"' in html


def test_settings_capabilities_carry_descriptions():
    html = client.get("/settings").text
    # Every toggle explains itself before it is selected.
    assert "highest-impact capability" in html          # computer_use
    assert "governed" in html and "sandbox.exec" in html  # code_exec
    assert "skill library built by the learning loop" in html  # skills
    from maverick_dashboard.settings_store import (
        CAPABILITY_DEFAULTS,
        CAPABILITY_INFO,
        FEATURE_DEFAULTS,
        FEATURE_INFO,
    )
    assert set(CAPABILITY_INFO) == set(CAPABILITY_DEFAULTS)
    assert set(FEATURE_INFO) == set(FEATURE_DEFAULTS)


def test_agents_page_has_departments_dialog():
    html = client.get("/agents").text
    assert 'id="dept-dialog"' in html
    assert "dep-details" in html
    assert 'id="dept-dlg-members"' in html
    # The old direct jump is replaced by the dialog (Workforce stays one
    # deliberate link inside it).
    assert "Open Workforce" in html
