"""Security-bearing URLs use one HTTPS origin and reject forged Host values."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_http_public_origin_is_invalid(monkeypatch):
    import importlib

    from maverick_dashboard import public_origin

    monkeypatch.setattr("maverick.config.config_source_errors", lambda **_kwargs: [])
    monkeypatch.setattr(
        "maverick.config.load_global_config",
        lambda: {
            "dashboard": {
                "public_base_url": "http://firm.example",
                "trusted_hosts": ["firm.example"],
            }
        },
    )

    # conftest replaces the public seam with the synthetic TestClient origin;
    # reload here to exercise the production parser itself.
    public_origin = importlib.reload(public_origin)
    valid, base, hosts = public_origin.public_origin_policy()
    assert (valid, base, hosts) == (False, "", frozenset())


def test_forged_host_is_rejected_before_invite_or_share_routing(monkeypatch):
    from maverick_dashboard import app as app_module

    monkeypatch.setattr(app_module, "non_static_auth_configured", lambda: True)
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.public_origin_policy",
        lambda: (True, "https://firm.example", frozenset({"firm.example"})),
    )
    client = TestClient(app_module.app)
    headers = {"Host": "attacker.example", "Origin": "https://attacker.example"}

    invite = client.post(
        "/users/invite",
        data={"email": "victim@example.com", "role": "viewer"},
        headers=headers,
    )
    share = client.post("/api/v1/goals/1/share", headers=headers)

    assert invite.status_code == 400
    assert share.status_code == 400
    assert invite.json() == {"detail": "untrusted host"}
    assert share.json() == {"detail": "untrusted host"}
