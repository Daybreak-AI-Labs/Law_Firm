"""Firm deployments cannot disable the security floor or use shared identity."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard import app as app_mod


def _required_legal_profile():
    return SimpleNamespace(
        allow_tools=["knowledge_search"],
        knowledge_sources=["legal"],
    )


def test_named_auth_refuses_disabled_secure_defaults(monkeypatch):
    monkeypatch.setattr(app_mod, "non_static_auth_configured", lambda: True)
    monkeypatch.setattr(app_mod, "_require_auth_enabled", lambda: False)
    monkeypatch.setattr(app_mod, "_secure_default_policy_valid", lambda: True)
    monkeypatch.setattr(
        app_mod,
        "qualified_attorney_policy",
        lambda: (True, frozenset({"user:counsel"})),
    )
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.public_origin_policy",
        lambda: (True, "https://firm.example", frozenset({"firm.example"})),
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    with pytest.raises(RuntimeError, match="requires secure defaults"):
        app_mod._assert_firm_security_posture()


def test_remote_refuses_malformed_secure_default_policy(monkeypatch):
    monkeypatch.setattr(app_mod, "non_static_auth_configured", lambda: False)
    monkeypatch.setattr(app_mod, "_require_auth_enabled", lambda: False)
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "certainly")
    with pytest.raises(RuntimeError, match="requires secure defaults"):
        app_mod._assert_firm_security_posture(remotely_reachable=True)


@pytest.mark.parametrize(
    ("at_rest", "strict"),
    [("0", "1"), ("1", "0")],
)
def test_firm_requires_encryption_and_strict_reads(
    monkeypatch, at_rest: str, strict: str,
):
    monkeypatch.setattr(app_mod, "non_static_auth_configured", lambda: True)
    monkeypatch.setattr(app_mod, "_require_auth_enabled", lambda: False)
    monkeypatch.setattr(app_mod, "_secure_default_policy_valid", lambda: True)
    monkeypatch.setattr(
        app_mod,
        "qualified_attorney_policy",
        lambda: (True, frozenset({"user:counsel"})),
    )
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.public_origin_policy",
        lambda: (True, "https://firm.example", frozenset({"firm.example"})),
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", at_rest)
    monkeypatch.setenv("MAVERICK_ENCRYPT_STRICT", strict)
    with pytest.raises(RuntimeError, match="encryption migrate"):
        app_mod._assert_firm_security_posture()


def test_local_auth_off_development_may_disable_security_floor(monkeypatch):
    monkeypatch.setattr(app_mod, "non_static_auth_configured", lambda: False)
    monkeypatch.setattr(app_mod, "_require_auth_enabled", lambda: False)
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPT_STRICT", "0")
    app_mod._assert_firm_security_posture(remotely_reachable=False)


@pytest.mark.parametrize(
    "knowledge",
    [
        {
            "enable": False,
            "embedder": "local",
            "model": "C:/models/legal",
            "model_digest": "sha256:" + "0" * 64,
        },
        {
            "enable": True,
            "embedder": "deterministic",
            "model": "",
            "model_digest": "",
        },
        {
            "enable": True,
            "embedder": "local",
            "model": "relative/model",
            "model_digest": "sha256:" + "0" * 64,
        },
    ],
)
def test_required_legal_knowledge_rejects_stub_disabled_or_unpinned(
    monkeypatch, knowledge,
):
    monkeypatch.setattr(
        "maverick.domain.enabled_domains",
        lambda: {"legal": _required_legal_profile()},
    )
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: knowledge)
    monkeypatch.delenv("MAVERICK_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("MAVERICK_EMBED_MODEL", raising=False)
    monkeypatch.delenv("MAVERICK_EMBED_MODEL_DIGEST", raising=False)

    assert app_mod._required_legal_knowledge_config_valid() is False


def test_required_legal_knowledge_admits_exact_local_model_tree(monkeypatch, tmp_path):
    from maverick_knowledge.local_embed import model_tree_digest

    model = tmp_path / "legal-embedder"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"pinned test weights")
    digest = model_tree_digest(model)
    monkeypatch.setattr(
        "maverick.domain.enabled_domains",
        lambda: {"legal": _required_legal_profile()},
    )
    monkeypatch.setattr(
        "maverick.config.get_knowledge",
        lambda: {
            "enable": True,
            "embedder": "local",
            "model": str(model),
            "model_digest": digest,
        },
    )
    monkeypatch.delenv("MAVERICK_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("MAVERICK_EMBED_MODEL", raising=False)
    monkeypatch.delenv("MAVERICK_EMBED_MODEL_DIGEST", raising=False)

    assert app_mod._required_legal_knowledge_config_valid() is True


def test_firm_startup_rejects_invalid_required_knowledge(monkeypatch):
    monkeypatch.setattr(app_mod, "non_static_auth_configured", lambda: True)
    monkeypatch.setattr(app_mod, "_require_auth_enabled", lambda: False)
    monkeypatch.setattr(app_mod, "_secure_default_policy_valid", lambda: True)
    monkeypatch.setattr(
        app_mod,
        "qualified_attorney_policy",
        lambda: (True, frozenset({"user:counsel"})),
    )
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.public_origin_policy",
        lambda: (True, "https://firm.example", frozenset({"firm.example"})),
    )
    monkeypatch.setattr(
        app_mod, "_required_legal_knowledge_config_valid", lambda: False,
    )
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_STRICT", "1")

    with pytest.raises(RuntimeError, match="local semantic embedder"):
        app_mod._assert_firm_security_posture()


def test_static_bearer_is_rejected_in_secure_firm_mode(monkeypatch):
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    client = TestClient(app_mod.app)
    response = client.get(
        "/api/v1/halt",
        headers={"Authorization": "Bearer shared-secret"},
    )
    assert response.status_code == 401


def test_static_bearer_legacy_is_local_insecure_dev_only(monkeypatch):
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    local = TestClient(app_mod.app)
    assert local.get(
        "/api/v1/halt",
        headers={"Authorization": "Bearer shared-secret"},
    ).status_code == 200

    remote = TestClient(app_mod.app, client=("203.0.113.7", 50000))
    assert remote.get(
        "/api/v1/halt",
        headers={"Authorization": "Bearer shared-secret"},
    ).status_code == 401
