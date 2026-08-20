"""Client-matter egress is the intersection of matter and deployment policy."""
from __future__ import annotations

import hashlib
import json

import pytest
from maverick.egress_guard import _check
from maverick.enterprise import (
    EgressBlocked,
    assert_provider_allowed,
    egress_permitted,
    enterprise_egress_denial,
)
from maverick.matter_context import (
    MatterContext,
    MatterContextError,
    matter_context_scope,
)


def _context(mode: str = "local_only") -> MatterContext:
    return MatterContext(
        matter_id=17,
        client_id=2,
        principal="user:attorney",
        membership_role="responsible_attorney",
        domain="legal",
        jurisdiction="Tennessee",
        purpose="goal-execution",
        source="test",
        egress_mode=mode,
    )


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch):
    monkeypatch.delenv("MAVERICK_ENTERPRISE", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: True)


def test_local_only_matter_blocks_cloud_provider_and_public_http():
    with matter_context_scope(_context()):
        with pytest.raises(EgressBlocked, match="matter egress policy") as exc:
            assert_provider_allowed("openai")
        assert exc.value.provider == "openai"
        assert egress_permitted("https://api.openai.com/v1/chat") is False
        denial = enterprise_egress_denial(
            "https://api.openai.com/v1/chat", tool="llm-adjacent"
        )
        assert denial is not None
        assert "matter 17" in denial

        assert_provider_allowed("ollama")
        assert egress_permitted("http://127.0.0.1:11434/api/chat") is True


def test_unparseable_egress_url_audit_never_records_path_or_query(monkeypatch):
    import maverick.audit as audit

    rows = []
    monkeypatch.setattr(
        audit,
        "record",
        lambda kind, **payload: rows.append((kind, payload)) or True,
    )
    url = "not-a-url/Client-Falcon/merger.docx?note=privileged-analysis"

    with matter_context_scope(_context()):
        with pytest.raises(EgressBlocked):
            _check(url)

    payload = next(
        row for kind, row in rows if kind == audit.EventKind.EGRESS_BLOCKED
    )
    assert payload["host"] == "invalid"
    assert payload["matter_id"] == 17
    assert payload["url_bytes"] == len(url.encode("utf-8"))
    assert payload["url_sha256"] == hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert url not in json.dumps(payload, ensure_ascii=False)
    assert "Client-Falcon" not in json.dumps(payload, ensure_ascii=False)
    assert "merger.docx" not in json.dumps(payload, ensure_ascii=False)


def test_approved_services_still_requires_exact_provider_and_host_allowlists(
    monkeypatch,
):
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {
                "approved_providers": ["openai"],
                "approved_hosts": ["api.openai.com"],
            }
        },
    )
    with matter_context_scope(_context("approved_services")):
        assert_provider_allowed("OPENAI")
        assert egress_permitted("https://api.openai.com/v1/chat") is True

        with pytest.raises(EgressBlocked):
            assert_provider_allowed("anthropic")
        assert egress_permitted("https://sub.api.openai.com/v1/chat") is False
        assert egress_permitted("https://example.com/") is False


def test_approved_services_with_missing_or_malformed_policy_fails_closed(
    monkeypatch,
):
    for config in (
        {"firm": {}},
        {"firm": {"approved_providers": "openai", "approved_hosts": "api.openai.com"}},
        {"firm": "invalid"},
    ):
        monkeypatch.setattr(
            "maverick.config.load_config",
            lambda *a, _config=config, **k: _config,
        )
        with matter_context_scope(_context("approved_services")):
            with pytest.raises(EgressBlocked):
                assert_provider_allowed("openai")
            assert egress_permitted("https://api.openai.com/v1/chat") is False


def test_enterprise_policy_is_a_stricter_intersection(monkeypatch):
    monkeypatch.setenv("MAVERICK_ENTERPRISE", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {
                "approved_providers": ["openai"],
                "approved_hosts": ["api.openai.com", "courtlistener.com"],
            },
            "enterprise": {"allowed_hosts": ["courtlistener.com"]},
        },
    )
    with matter_context_scope(_context("approved_services")):
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("openai")
        assert egress_permitted("https://api.openai.com/v1/chat") is False
        assert egress_permitted("https://courtlistener.com/api/rest/v3/") is True


def test_outside_matter_preserves_legacy_nonenterprise_behavior():
    assert_provider_allowed("openai")
    assert egress_permitted("https://api.openai.com/v1/chat") is True


def test_secure_public_dispatch_requires_live_authority_resolver(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {
                "approved_providers": ["openai"],
                "approved_hosts": ["api.openai.com"],
            }
        },
    )
    stale = _context("approved_services")
    with matter_context_scope(stale):
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("openai")
        assert egress_permitted("https://api.openai.com/v1/chat") is False


def test_membership_revocation_and_egress_mode_change_take_effect_at_dispatch(
    monkeypatch,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {
                "approved_providers": ["openai"],
                "approved_hosts": ["api.openai.com"],
            }
        },
    )
    stale = _context("approved_services")
    state = {"context": stale, "revoked": False}

    def resolve():
        if state["revoked"]:
            raise MatterContextError("membership revoked")
        return state["context"]

    with matter_context_scope(stale, authority_resolver=resolve):
        assert_provider_allowed("openai")
        assert egress_permitted("https://api.openai.com/v1/chat") is True

        state["context"] = _context("local_only")
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("openai")
        assert egress_permitted("https://api.openai.com/v1/chat") is False

        state["context"] = stale
        state["revoked"] = True
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("openai")
        assert egress_permitted("https://api.openai.com/v1/chat") is False


def test_local_provider_and_loopback_http_recheck_revocation(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    context = _context("local_only")
    revoked = False

    def resolve():
        if revoked:
            raise MatterContextError("membership revoked")
        return context

    with matter_context_scope(context, authority_resolver=resolve):
        assert_provider_allowed("ollama")
        assert egress_permitted("http://127.0.0.1:11434/api/chat") is True

        revoked = True
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("ollama")
        assert egress_permitted("http://127.0.0.1:11434/api/chat") is False
        with pytest.raises(EgressBlocked):
            _check("http://127.0.0.1:11434/api/chat")


def test_local_provider_and_loopback_http_reject_changed_matter(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    bound = _context("local_only")
    other = MatterContext(
        matter_id=bound.matter_id + 1,
        client_id=bound.client_id + 1,
        principal=bound.principal,
        membership_role=bound.membership_role,
        domain=bound.domain,
        jurisdiction=bound.jurisdiction,
        purpose=bound.purpose,
        source=bound.source,
        egress_mode=bound.egress_mode,
    )

    with matter_context_scope(bound, authority_resolver=lambda: other):
        with pytest.raises(EgressBlocked):
            assert_provider_allowed("ollama")
        assert egress_permitted("http://localhost:11434/api/chat") is False


@pytest.mark.parametrize(
    "url",
    [
        "https://10.42.0.8/service",
        "https://192.168.10.9/service",
        "https://169.254.20.30/service",
    ],
)
def test_firm_local_only_does_not_trust_the_lan_by_address_class(
    monkeypatch, url,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    context = _context("local_only")
    with matter_context_scope(context, authority_resolver=lambda: context):
        assert egress_permitted(url) is False


def test_exact_approved_on_prem_host_requires_https_and_live_membership(monkeypatch):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {"firm": {"approved_local_hosts": ["10.42.0.8"]}},
    )
    context = _context("local_only")
    revoked = False

    def resolve():
        if revoked:
            raise MatterContextError("membership revoked")
        return context

    with matter_context_scope(context, authority_resolver=resolve):
        assert egress_permitted("http://10.42.0.8/service") is False
        assert egress_permitted("https://10.42.0.8/service") is True
        assert egress_permitted("https://10.42.0.9/service") is False
        assert egress_permitted("https://169.254.169.254/latest/meta-data") is False
        revoked = True
        assert egress_permitted("https://10.42.0.8/service") is False


def test_approved_public_service_requires_https_and_certificate_verification(
    monkeypatch,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda *a, **k: {
            "firm": {"approved_hosts": ["api.openai.com"]},
        },
    )
    context = _context("approved_services")
    with matter_context_scope(context, authority_resolver=lambda: context):
        assert egress_permitted("http://api.openai.com/v1/chat") is False
        assert egress_permitted("https://api.openai.com/v1/chat") is True
        with pytest.raises(EgressBlocked, match="certificate verification disabled"):
            _check(
                "https://api.openai.com/v1/chat",
                verification_disabled=True,
            )
        _check(
            "https://api.openai.com/v1/chat",
            verification_disabled=False,
        )
