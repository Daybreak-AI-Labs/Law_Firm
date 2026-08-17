"""The /start Get Started page: a live setup checklist that self-completes as
the workspace gets configured (provider -> built -> run/automated)."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    import maverick.templates as tpl
    monkeypatch.setattr(tpl, "USER_TEMPLATES", tmp_path / ".maverick" / "templates")
    import maverick.domain_edit as de
    monkeypatch.setattr(de, "list_agents", list)   # no overridden agents in a fresh ws


def test_fresh_workspace_nothing_done(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import maverick.config as config
    monkeypatch.setattr(config, "any_provider_configured", lambda: False)
    t = _client().get("/start").text
    assert "Connect a model provider" in t and "Build a workflow or agent" in t
    assert "0 of 3 done" in t and 'role="progressbar"' in t
    assert "Offline install preflight" in t
    # The `maverick preflight` CLI was removed in the CLI reduction; the page
    # must not advertise it.
    assert "maverick preflight" not in t
    # The Assurance Cockpit synthetic demo went with the GRC cluster.
    assert "SYNTHETIC DEMO DATA" not in t
    assert "/security/assurance" not in t


def test_checklist_reflects_progress(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import maverick.config as config
    monkeypatch.setattr(config, "any_provider_configured", lambda: True)  # step 1 done
    t = _client().get("/start").text
    assert "1 of 3 done" in t and "gs__step--done" in t


def test_get_started_reachable(monkeypatch, tmp_path):
    # Get started is folded out of the sidebar in the moderate declutter (it's
    # onboarding, linked from the empty states), but still renders at /start.
    _isolate(monkeypatch, tmp_path)
    import maverick.config as config
    monkeypatch.setattr(config, "any_provider_configured", lambda: False)
    r = _client().get("/start")
    assert r.status_code == 200
    assert 'page-title">Get started' in r.text


def test_runtime_completion_is_gated_separately_from_assurance(monkeypatch):
    from maverick import config, operator_preflight
    from maverick_dashboard import onboarding_state

    class Report:
        def __init__(self, ready):
            self.ready = ready

        def to_dict(self):
            return {
                "ready": self.ready,
                "checks": [],
                "next_action": "none",
            }

    class World:
        @staticmethod
        def list_goals(limit=1):
            return [{"id": 1}][:limit]

    monkeypatch.setattr(config, "any_provider_configured", lambda: True)
    monkeypatch.setattr(onboarding_state, "_has_user_template", lambda: True)
    monkeypatch.setattr(onboarding_state, "_has_user_agent", lambda: False)
    monkeypatch.setattr(onboarding_state, "_has_automation", lambda: False)
    monkeypatch.setattr(
        operator_preflight,
        "collect",
        lambda profile: Report(profile == "run"),
    )

    state = onboarding_state.build(World())
    assert state["runtime_ready"] is True
    assert state["assurance_ready"] is False
    assert state["runtime_all_set"] is True

    monkeypatch.setattr(
        operator_preflight,
        "collect",
        lambda _profile: Report(False),
    )
    blocked = onboarding_state.build(World())
    assert blocked["done_count"] == blocked["total"]
    assert blocked["runtime_all_set"] is False
