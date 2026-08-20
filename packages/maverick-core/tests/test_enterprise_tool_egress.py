"""Enterprise mode locks retained tool egress, not just LLM calls."""
from __future__ import annotations

import pytest
from maverick.enterprise import egress_permitted, enterprise_egress_denial


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MAVERICK_ENTERPRISE", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})


def _enterprise(monkeypatch, allowed=None):
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    if allowed is not None:
        monkeypatch.setattr(
            "maverick.config.load_config",
            lambda *a, **k: {"enterprise": {"allowed_hosts": allowed}},
        )


def test_egress_permitted_when_enterprise_off():
    assert egress_permitted("https://anywhere.example.com/x") is True
    assert enterprise_egress_denial("https://anywhere.example.com/x") is None


def test_public_host_denied_under_enterprise(monkeypatch):
    _enterprise(monkeypatch)
    assert egress_permitted("https://exfil.example.com/x") is False
    deny = enterprise_egress_denial("https://exfil.example.com/x", tool="web_search")
    assert deny and "exfil.example.com" in deny and "allowed_hosts" in deny


def test_local_endpoint_permitted_under_enterprise(monkeypatch):
    _enterprise(monkeypatch)
    assert egress_permitted("http://localhost:8080/x") is True
    assert egress_permitted("http://127.0.0.1/x") is True
    assert enterprise_egress_denial("http://localhost:8080/x") is None


def test_imds_is_not_local_and_is_denied_under_enterprise(monkeypatch):
    # Cloud IMDS is link-local but leaks instance credentials -- it must NOT be
    # treated as a local service that bypasses the egress lock.
    _enterprise(monkeypatch)
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://[fd00:ec2::254]/", "https://169.254.169.254/"):
        assert egress_permitted(url) is False, url
        assert enterprise_egress_denial(url) is not None, url
    # A non-IMDS link-local (APIPA) host is still treated as local (no regression).
    assert egress_permitted("http://169.254.1.5/x") is True


def test_allow_listed_host_permitted_under_enterprise(monkeypatch):
    _enterprise(monkeypatch, allowed=["api.tavily.com"])
    assert egress_permitted("https://api.tavily.com/search") is True
    assert egress_permitted("https://other.example.com/x") is False


def test_web_search_disabled_under_enterprise_without_allowlist(monkeypatch):
    _enterprise(monkeypatch)
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_SEARCH_BACKEND", "tavily")
    monkeypatch.setattr(
        "maverick.enterprise._audit_matter_egress_denial", lambda **kwargs: None
    )
    from maverick.tools.web_search import _run_search
    out = _run_search({"query": "sensitive patient data"})
    assert "enterprise mode" in out and "refusing" in out


def test_web_search_allows_an_allow_listed_backend(monkeypatch):
    # Allow-listing tavily lets it past the gate (it then fails on no API key, not on
    # the enterprise gate -- proving the gate permitted it rather than blocking all).
    _enterprise(monkeypatch, allowed=["api.tavily.com"])
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_SEARCH_BACKEND", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from maverick.tools.web_search import _run_search
    out = _run_search({"query": "x"})
    assert "disabled in enterprise mode" not in out
