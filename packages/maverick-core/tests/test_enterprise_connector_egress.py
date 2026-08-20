"""The enterprise tool-egress lock covers retained REST connectors."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("MAVERICK_ENTERPRISE", "ACME_URL", "ACME_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})


def _acme_tool(monkeypatch, base):
    monkeypatch.setenv("ACME_URL", base)
    monkeypatch.setenv("ACME_TOKEN", "tok")
    from maverick.tools._rest_connector import make_rest_tool
    return make_rest_tool(name="acme", base_url_env="ACME_URL",
                          token_env="ACME_TOKEN", description="x")


def test_connector_egress_blocked_under_enterprise(monkeypatch):
    tool = _acme_tool(monkeypatch, "https://acme.invalid")
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    out = tool.fn({"op": "get", "path": "/tickets"})
    assert out.startswith("ERROR:") and "enterprise mode" in out
    assert "acme.invalid" in out


def test_connector_allowed_when_host_allow_listed(monkeypatch):
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {"enterprise": {"allowed_hosts": ["acme.invalid"]}},
    )
    tool = _acme_tool(monkeypatch, "https://acme.invalid")
    out = tool.fn({"op": "get", "path": "/x"})
    # Past the egress gate -> it attempts the request (and fails to connect), so
    # the error is the connection failure, NOT the enterprise denial.
    assert "enterprise mode" not in out


def test_connector_egress_noop_when_enterprise_off(monkeypatch):
    tool = _acme_tool(monkeypatch, "https://acme.invalid")
    out = tool.fn({"op": "get", "path": "/x"})       # enterprise off
    assert "enterprise mode" not in out               # not blocked
