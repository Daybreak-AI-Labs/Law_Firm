"""POST /plugins/install — one-click install of an operator-allowlisted plugin
package, quad-gated (same-origin + MAVERICK_ALLOW_PLUGIN_INSTALL + admin +
allowlist). The pip work itself is covered in core test_plugin_install."""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


def test_install_blocked_without_opt_in(monkeypatch):
    monkeypatch.delenv("MAVERICK_ALLOW_PLUGIN_INSTALL", raising=False)
    called = []
    monkeypatch.setattr("maverick.plugins.install_plugin", lambda n: called.append(n))
    r = client.post("/plugins/install", data={"name": "approved-pkg"}, follow_redirects=False)
    assert r.status_code == 403
    assert called == []                       # never reached the installer




def test_install_surfaces_core_validation_error(monkeypatch):
    monkeypatch.setenv("MAVERICK_ALLOW_PLUGIN_INSTALL", "1")

    def boom(name):
        raise ValueError("'evil' is not on the [plugins] installable allowlist")

    monkeypatch.setattr("maverick.plugins.install_plugin", boom)
    r = client.post("/plugins/install", data={"name": "evil"}, follow_redirects=False)
    assert r.status_code == 400
    assert "allowlist" in r.json()["detail"]


