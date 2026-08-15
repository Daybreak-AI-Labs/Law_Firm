"""Reverse-proxy SSO: honor a forwarded identity header only from a trusted peer.

The security-critical bit is :func:`proxy_trusts` -- a forwarded header is
spoofable by a direct client, so it must be accepted only when the request's
network peer is the trusted upstream.
"""
from __future__ import annotations

import logging

import pytest
from maverick.proxy_auth import (
    principal_from_proxy,
    proxy_auth_enabled,
    proxy_header_name,
    proxy_trusts,
    warn_if_untrusted_proxy_config,
)


def _cfg(monkeypatch, cfg):
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: cfg)


def test_disabled_by_default(monkeypatch):
    _cfg(monkeypatch, {})
    monkeypatch.delenv("MAVERICK_PROXY_AUTH", raising=False)
    assert proxy_auth_enabled() is False


def test_enabled_via_config(monkeypatch):
    monkeypatch.delenv("MAVERICK_PROXY_AUTH", raising=False)
    _cfg(monkeypatch, {"auth": {"proxy": {"enabled": True}}})
    assert proxy_auth_enabled() is True


def test_enabled_via_env(monkeypatch):
    _cfg(monkeypatch, {})
    monkeypatch.setenv("MAVERICK_PROXY_AUTH", "1")
    assert proxy_auth_enabled() is True


def test_header_default_and_override(monkeypatch):
    monkeypatch.delenv("MAVERICK_PROXY_AUTH_HEADER", raising=False)
    _cfg(monkeypatch, {})
    assert proxy_header_name() == "X-Forwarded-User"
    _cfg(monkeypatch, {"auth": {"proxy": {"header": "X-Auth-Request-Email"}}})
    assert proxy_header_name() == "X-Auth-Request-Email"


def test_unpinned_peer_is_untrusted_by_default(monkeypatch):
    # #11: with no trusted_proxies pin and trust_loopback unset, NO peer is
    # trusted -- a co-located loopback process must not be able to spoof
    # X-Forwarded-User just by living on 127.0.0.1. Explicit opt-in required.
    _cfg(monkeypatch, {})
    assert proxy_trusts("127.0.0.1") is False
    assert proxy_trusts("::1") is False
    assert proxy_trusts("10.0.0.9") is False   # a remote peer is not trusted
    assert proxy_trusts("") is False           # unknown peer fails closed
    assert proxy_trusts(None) is False


def test_trusts_loopback_only_with_explicit_opt_in(monkeypatch):
    # trust_loopback = true is the explicit opt-in for the same-host proxy.
    _cfg(monkeypatch, {"auth": {"proxy": {"trust_loopback": True}}})
    assert proxy_trusts("127.0.0.1") is True
    assert proxy_trusts("::1") is True
    assert proxy_trusts("10.0.0.9") is False   # opt-in is loopback-only
    # An explicit false is honored the same way an unset value is (no trust).
    _cfg(monkeypatch, {"auth": {"proxy": {"trust_loopback": False}}})
    assert proxy_trusts("127.0.0.1") is False


def test_trusts_configured_proxies_replace_default(monkeypatch):
    _cfg(monkeypatch, {"auth": {"proxy": {"trusted_proxies": ["10.0.0.5"]}}})
    assert proxy_trusts("10.0.0.5") is True
    # An explicit list is exact: it replaces the loopback default so a stray
    # local process can't assert identity unless you listed loopback too.
    assert proxy_trusts("127.0.0.1") is False


def test_warn_if_untrusted_proxy_config(monkeypatch, caplog):
    monkeypatch.delenv("MAVERICK_PROXY_AUTH", raising=False)

    # Off by default -> silent.
    _cfg(monkeypatch, {})
    with caplog.at_level(logging.WARNING, logger="maverick.proxy_auth"):
        warn_if_untrusted_proxy_config()
    assert caplog.records == []

    # Enabled but neither pinned nor trust_loopback set -> loud warning.
    _cfg(monkeypatch, {"auth": {"proxy": {"enabled": True}}})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="maverick.proxy_auth"):
        warn_if_untrusted_proxy_config()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "trusted_proxies" in caplog.text

    # Pinned -> silent (a peer is configured).
    _cfg(monkeypatch, {"auth": {"proxy": {"enabled": True, "trusted_proxies": ["10.0.0.5"]}}})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="maverick.proxy_auth"):
        warn_if_untrusted_proxy_config()
    assert caplog.records == []

    # Explicit trust_loopback -> silent (operator opted in deliberately).
    _cfg(monkeypatch, {"auth": {"proxy": {"enabled": True, "trust_loopback": True}}})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="maverick.proxy_auth"):
        warn_if_untrusted_proxy_config()
    assert caplog.records == []


def test_principal_from_proxy_maps_to_user():
    p = principal_from_proxy("alice@example.com")
    assert p.principal == "user:alice@example.com"
    assert p.claims.get("via") == "proxy"


@pytest.mark.parametrize("subject", [" alice", "alice ", "alice\n", "x" * 252])
def test_principal_from_proxy_rejects_invalid_subject_domain(subject):
    with pytest.raises(ValueError, match="authenticated subject"):
        principal_from_proxy(subject)


def test_principal_from_proxy_accepts_full_principal_length_boundary():
    subject = "x" * 251
    assert len(principal_from_proxy(subject).principal) == 256
