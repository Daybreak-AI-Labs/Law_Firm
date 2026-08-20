"""Branded 404 / 500 error pages — council polish pass."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app)


def test_404_html_for_browser(monkeypatch, tmp_path):
    """Browser navigation to a missing path gets the branded 404 page."""
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    client = _client()
    resp = client.get("/this/does/not/exist", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert "404" in resp.text
    assert "/this/does/not/exist" in resp.text
    assert "Bjerken and Day" in resp.text
    assert 'href="/projects"' in resp.text


def test_404_json_for_api(monkeypatch, tmp_path):
    """API path keeps JSON so SDKs / curl don't get an HTML surprise."""
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    client = _client()
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_unhandled_exception_renders_500(monkeypatch, tmp_path):
    """A route blowing up renders the branded 500 page for browsers."""
    from maverick_dashboard import app as dash_app

    @dash_app.app.get("/__boom")
    async def boom():
        raise RuntimeError("test-only failure")

    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    # TestClient re-raises unhandled exceptions by default; disable so
    # our exception handler can produce the branded page.
    from fastapi.testclient import TestClient
    client = TestClient(dash_app.app, raise_server_exceptions=False)
    resp = client.get("/__boom", headers={"Accept": "text/html"})
    assert resp.status_code == 500
    assert "Something went wrong" in resp.text
    assert "RuntimeError" not in resp.text
    assert "test-only failure" not in resp.text


def test_validation_error_page_does_not_echo_detail(monkeypatch, tmp_path):
    """A 422 routed to the branded page must not echo arbitrary request
    detail. validation_exception_handler renders 500.html, so the page
    must not template {{ detail }} — otherwise crafted input could leak
    into the HTML response (reflected-XSS / info-leak surface)."""
    from maverick import world_model
    from maverick_dashboard import app as dash_app

    @dash_app.app.get("/__needs_param")
    async def needs_param(marker_xyzzy: int):  # required query param
        return {"ok": marker_xyzzy}

    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    client = _client()
    # Missing/non-int required param -> RequestValidationError -> branded page.
    resp = client.get(
        "/__needs_param?marker_xyzzy=PWNED_REFLECTED",
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 400  # handler downgrades 422 -> 400 for browsers
    assert "Something went wrong" in resp.text
    # The crafted value and validation internals must not appear in the HTML.
    assert "PWNED_REFLECTED" not in resp.text
    assert "marker_xyzzy" not in resp.text


def test_runtime_policy_error_is_actionable_and_path_safe(monkeypatch):
    from maverick.runtime_overrides import RuntimeOverridesSecurityError
    from maverick_dashboard import app as dash_app

    @dash_app.app.get("/__policy_unavailable")
    async def policy_unavailable():
        raise RuntimeOverridesSecurityError(
            "private path C:/Users/example/runtime-overrides.toml"
        )

    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    client = TestClient(dash_app.app, raise_server_exceptions=False)

    html = client.get(
        "/__policy_unavailable",
        headers={"Accept": "text/html"},
    )
    assert html.status_code == 503
    assert "Operator policy unavailable" in html.text
    assert "maverick doctor" in html.text
    assert "C:/Users/example" not in html.text

    api = client.get(
        "/__policy_unavailable",
        headers={"Accept": "application/json"},
    )
    assert api.status_code == 503
    assert api.json()["code"] == "operator_policy_unavailable"
    assert "C:/Users/example" not in api.text
