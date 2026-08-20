"""Compact named-identity contract for the firm-only dashboard."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick.oidc import VerifiedPrincipal
from maverick_dashboard import auth
from starlette.datastructures import Headers


def _request(*, headers=None, peer="127.0.0.1", host="testserver"):
    return SimpleNamespace(
        headers=Headers(headers or {"Host": host}),
        cookies={},
        url=SimpleNamespace(path="/api/v1/goals", hostname=host.split(":", 1)[0]),
        client=SimpleNamespace(host=peer),
        state=SimpleNamespace(),
    )


def test_valid_oidc_bearer_is_named_and_invalid_bearer_cannot_borrow_cookie(
    monkeypatch,
):
    ambient = VerifiedPrincipal(
        sub="ambient", issuer="browser", audience="dashboard", claims={"via": "session"}
    )
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(auth, "_session_principal", lambda _request: ambient)
    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda token: VerifiedPrincipal(
            sub=token, issuer="https://issuer.example", audience="dashboard", claims={}
        ),
    )
    request = _request(headers={"Host": "testserver", "Authorization": "Bearer alice"})
    assert auth.require_principal(request).principal == "user:alice"

    monkeypatch.setattr(
        auth,
        "verify_oidc_token",
        lambda _token: (_ for _ in ()).throw(auth.OIDCError("bad")),
    )
    rejected = _request(headers={"Host": "testserver", "Authorization": "Bearer bad"})
    with pytest.raises(auth.HTTPException) as exc:
        auth.require_principal(rejected)
    assert exc.value.status_code == 401


def test_static_bearer_is_denied_in_secure_or_named_firm_mode(monkeypatch):
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    monkeypatch.setattr("maverick.security_defaults.secure_by_default", lambda: True)
    monkeypatch.setattr(auth, "non_static_auth_configured", lambda: True)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    request = _request(
        headers={"Host": "testserver", "Authorization": "Bearer shared-secret"}
    )

    with pytest.raises(auth.HTTPException) as exc:
        auth.require_principal(request)

    assert exc.value.status_code == 401
    assert not hasattr(request.state, "principal")


def test_static_bearer_legacy_escape_is_local_auth_off_only(monkeypatch):
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    monkeypatch.setattr("maverick.security_defaults.secure_by_default", lambda: False)
    monkeypatch.setattr(auth, "non_static_auth_configured", lambda: False)
    request = _request(
        headers={"Host": "testserver", "Authorization": "Bearer shared-secret"}
    )
    assert auth.require_principal(request).principal == "user:dashboard-static-bearer"

    remote = _request(
        headers={"Host": "firm.example", "Authorization": "Bearer shared-secret"},
        peer="10.0.0.9",
        host="firm.example",
    )
    with pytest.raises(auth.HTTPException):
        auth.require_principal(remote)


def test_auth_off_ambient_operator_is_direct_loopback_only(monkeypatch):
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(auth, "auth_genuinely_off", lambda: True)
    monkeypatch.setattr("maverick.client.client_binding_enforced", lambda: False)
    remote = _request(peer="203.0.113.9", host="firm.example")

    with pytest.raises(auth.HTTPException) as exc:
        auth._authorization_principal(remote)

    assert exc.value.status_code == 401


def test_dashboard_has_no_websocket_auth_surface():
    assert not hasattr(auth, "websocket_authorized")
    assert not hasattr(auth, "require_websocket_principal_in_context")


def test_dashboard_has_no_proxy_identity_or_tenant_role_surface():
    from maverick_dashboard import rbac

    assert not hasattr(auth, "_proxy_principal")
    assert not hasattr(auth, "_pin_tenant_from_principal")
    assert not hasattr(rbac, "set_tenant_role")
