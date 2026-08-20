"""Physical and mounted-surface contract for the firm-only dashboard."""
from __future__ import annotations

import importlib.util

from maverick_dashboard.app import app


def test_daily_law_firm_rest_surface_remains_mounted():
    paths = set(app.openapi()["paths"])
    required = {
        "/api/v1/conflicts/preflight",
        "/api/v1/matters",
        "/api/v1/matters/{matter_id}",
        "/api/v1/matters/{matter_id}/members",
        "/api/v1/matters/{matter_id}/parties",
        "/api/v1/goals",
        "/api/v1/goals/{goal_id}",
        "/api/v1/goals/{goal_id}/attachments",
        "/api/v1/goals/{goal_id}/attachments/{attachment_id}/download",
        "/api/v1/goals/{goal_id}/feedback",
        "/api/v1/goals/{goal_id}/signoff",
        "/api/v1/goals/{goal_id}/deliverable.csv",
        "/api/v1/goals/{goal_id}/share",
        "/api/v1/audit/tail",
        "/api/v1/halt",
        "/healthz",
    }
    assert required <= paths


def test_retired_product_routes_are_not_mounted():
    paths = set(app.openapi()["paths"])
    banned_fragments = (
        "/saml", "/Users", "/Groups", "/ResourceTypes",
        "/ServiceProviderConfig", "/docs/discover", "/from-source",
        "/ws/", "/flows", "/schedules", "/triggers", "/automations",
        "/agents", "/fleets", "/marketplace", "/plugins", "/mcp",
        "/tools", "/connections", "/oauth/connect", "/privacy",
        "/assess", "/billing", "/tenants", "/voice", "/workforce",
        "/workflows", "/replay", "/simulate", "/tutorial",
    )
    assert not {
        path for path in paths if any(fragment in path for fragment in banned_fragments)
    }
    assert all(
        getattr(route, "path", None) != "/ws/v1/runs/{goal_id}/events"
        for route in app.routes
    )


def test_retired_dashboard_modules_are_physically_absent():
    removed = (
        "maverick_dashboard.saml",
        "maverick_dashboard.saml_replay",
        "maverick_dashboard.scim",
        "maverick_dashboard.scim_groups",
        "maverick_dashboard.subject_directory",
        "maverick_dashboard.automation_queue",
        "maverick_dashboard.event_poll",
        "maverick_dashboard.control_plane",
        "maverick_dashboard.partner_store",
        "maverick_dashboard.suite_grants",
        "maverick_dashboard.triggers_store",
        "maverick_dashboard.goal_feedback_store",
        "maverick_dashboard.workflow_ai",
    )
    assert {name for name in removed if importlib.util.find_spec(name) is not None} == set()
