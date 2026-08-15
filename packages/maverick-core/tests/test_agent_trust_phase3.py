"""Phase 3 — gate the remaining external surfaces and add per-caller A2A
identity. Covers A2A per-caller bearer -> agent identity + admission + ceiling,
governance gating on federation delegation (DENY + fail-closed REQUIRE_HUMAN),
and channel/marketplace federation gating by registered origin."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick import agent_trust
from maverick.agent_trust import TrustedAgent

# ---- per-caller A2A token resolution --------------------------------------


# ---- A2A auth + principal + admission + ceiling ---------------------------


def _patch(monkeypatch, registry, *, enforced=True):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (enforced, registry))
    monkeypatch.setattr(agent_trust, "agent_trust_enforced", lambda cfg=None: enforced)
    monkeypatch.setattr(agent_trust, "load_registry", lambda cfg=None: registry)


def test_external_agent_allowlist_does_not_implicitly_gain_coordination():
    from maverick.capability import COORDINATION_TOOLS

    entry = TrustedAgent(
        id="reader",
        allow_tools=frozenset({"read_file", "send_to_agent"}),
    )
    cap = entry.capability()

    assert cap.permits("read_file")
    assert cap.permits("send_to_agent")
    assert all(
        not cap.permits(tool)
        for tool in COORDINATION_TOOLS - {"send_to_agent"}
    )


# ---- governance gating on federation delegation ---------------------------


class _Goals:
    def start_goal(self, title, description="", **kw):
        return 1

    def status(self, goal_id):
        return SimpleNamespace(status="done", result="ok")


def _fed(monkeypatch, registry):
    from maverick import federation
    from maverick.federation import FederationService, Peer
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (True, registry))
    monkeypatch.setattr(agent_trust, "agent_trust_enforced", lambda cfg=None: True)
    monkeypatch.setattr(agent_trust, "load_registry", lambda cfg=None: registry)
    return federation, FederationService(
        node="b", peers=[Peer("a", "a:1", "tok")], local_grant=None,
        goal_service=_Goals(), record=lambda *a, **k: None,
    )


def _delegate(svc, **over):
    payload = {"auth_token": "tok", "correlation_id": "c1", "goal_title": "x",
               "requested_tools": ["read_file"]}
    payload.update(over)
    return svc.delegate_goal(payload)


# ---- channel / marketplace gating by registered origin --------------------


# ---- per-surface token isolation ------------------------------------------


def test_token_surface_isolation():
    # A token configured for one surface must not authenticate another.
    reg = {"vega": TrustedAgent(id="vega", grpc_token="g", mcp_token="m",
                                rest_token="r")}
    assert agent_trust.agent_for_token("g", "grpc", registry=reg).id == "vega"
    assert agent_trust.agent_for_token("g", "mcp", registry=reg) is None
    assert agent_trust.agent_for_token("g", "rest", registry=reg) is None
    assert agent_trust.agent_for_token("m", "mcp", registry=reg).id == "vega"
    assert agent_trust.agent_for_token("r", "rest", registry=reg).id == "vega"


def test_a_retired_surface_authenticates_nothing():
    # The a2a surface is gone. A bearer presented for it must not resolve --
    # and an unknown surface name must not fall back to some other surface's
    # token, which is how a retired surface turns into a bypass.
    reg = {"vega": TrustedAgent(id="vega", grpc_token="g", mcp_token="m")}
    assert agent_trust.agent_for_token("g", "a2a", registry=reg) is None
    assert agent_trust.agent_for_token("m", "a2a", registry=reg) is None
    assert "a2a" not in agent_trust._TOKEN_ATTRS


def test_duplicate_active_surface_token_is_not_an_identity():
    reg = {
        "vega": TrustedAgent(id="vega", mcp_token="duplicate"),
        "sirius": TrustedAgent(id="sirius", mcp_token="duplicate"),
    }

    assert agent_trust.agent_for_token("duplicate", "mcp", registry=reg) is None


def test_duplicate_token_ignores_inactive_entries():
    reg = {
        "vega": TrustedAgent(id="vega", grpc_token="duplicate"),
        "revoked": TrustedAgent(
            id="revoked", grpc_token="duplicate", revoked=True,
        ),
    }

    assert agent_trust.agent_for_token(
        "duplicate", "grpc", registry=reg,
    ).id == "vega"


# ---- gRPC goal API gating -------------------------------------------------


class _Ctx:
    def abort(self, code, details):
        raise PermissionError(details)


def _without_grpc_runtime(monkeypatch, server):
    codes = SimpleNamespace(
        UNAVAILABLE="UNAVAILABLE",
        PERMISSION_DENIED="PERMISSION_DENIED",
    )
    monkeypatch.setattr(server, "_grpc_code", lambda: codes)


def test_grpc_trust_capability_denies_unregistered(monkeypatch):
    pytest.importorskip("grpc")
    from maverick.grpc_api import server
    _patch(monkeypatch, {})  # engaged, no surface-wide "grpc" entry
    with pytest.raises(PermissionError):
        server._trust_capability(_Ctx(), None)


def test_grpc_trust_capability_allows_registered(monkeypatch):
    pytest.importorskip("grpc")
    from maverick.grpc_api import server
    reg = {"grpc": TrustedAgent(id="grpc", allow_tools=frozenset({"read_file"}))}
    _patch(monkeypatch, reg)
    cap = server._trust_capability(_Ctx(), None)
    assert cap is not None and cap.permits("read_file") and not cap.permits("shell")


def test_grpc_trust_capability_noop_when_disengaged(monkeypatch):
    pytest.importorskip("grpc")
    from maverick.grpc_api import server
    _patch(monkeypatch, {}, enforced=False)
    assert server._trust_capability(_Ctx(), None) is None


def test_grpc_trust_capability_load_error_fails_closed(monkeypatch):
    from maverick import agent_trust
    from maverick.grpc_api import server
    _without_grpc_runtime(monkeypatch, server)

    def _broken_state():
        raise OSError("trust registry unreadable")

    monkeypatch.setattr(agent_trust, "load_trust_state", _broken_state)
    with pytest.raises(PermissionError, match="policy unavailable"):
        server._trust_capability(_Ctx(), None)


@pytest.mark.parametrize(
    "state",
    [
        (None, {}),
        (False, []),
        ("false", {}),
    ],
)
def test_grpc_trust_capability_malformed_state_fails_closed(monkeypatch, state):
    from maverick.grpc_api import server
    _without_grpc_runtime(monkeypatch, server)

    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: state)
    with pytest.raises(PermissionError, match="policy unavailable"):
        server._trust_capability(_Ctx(), None)


def test_grpc_trust_capability_decision_error_fails_closed(monkeypatch):
    from maverick.grpc_api import server
    _without_grpc_runtime(monkeypatch, server)

    _patch(monkeypatch, {})

    def _broken_decision(*args, **kwargs):
        raise RuntimeError("decision engine unavailable")

    monkeypatch.setattr(agent_trust, "decide_inbound", _broken_decision)
    with pytest.raises(PermissionError, match="policy unavailable"):
        server._trust_capability(_Ctx(), None)


def test_grpc_denial_survives_audit_sink_failure(monkeypatch):
    from maverick.grpc_api import server
    _without_grpc_runtime(monkeypatch, server)

    _patch(monkeypatch, {})

    def _broken_audit(*args, **kwargs):
        raise OSError("audit sink unavailable")

    monkeypatch.setattr(agent_trust, "record_denied", _broken_audit)
    with pytest.raises(PermissionError, match="not in the trust registry"):
        server._trust_capability(_Ctx(), None)


# ---- MCP gating -----------------------------------------------------------


def test_mcp_admits_registered_shared_token(monkeypatch):
    from maverick_mcp import http_transport as ht
    monkeypatch.setenv("MAVERICK_MCP_TOKEN", "shared")
    _patch(monkeypatch, {"mcp": TrustedAgent(id="mcp", direction="both")})
    # Shared bearer -> authed with NO per-caller identity (empty string).
    assert ht._check_bearer("Bearer shared") == (True, "")


def test_mcp_denies_when_engaged_no_entry(monkeypatch):
    from maverick_mcp import http_transport as ht
    monkeypatch.setenv("MAVERICK_MCP_TOKEN", "shared")
    _patch(monkeypatch, {})  # engaged, no "mcp" surface entry
    assert ht._check_bearer("Bearer shared") == (False, "")


def test_mcp_per_caller_token(monkeypatch):
    from maverick_mcp import http_transport as ht
    monkeypatch.delenv("MAVERICK_MCP_TOKEN", raising=False)
    reg = {"vega": TrustedAgent(id="vega", mcp_token="v-tok", direction="both")}
    _patch(monkeypatch, reg)
    # Per-caller token -> authed AND carries the resolved agent identity.
    assert ht._check_bearer("Bearer v-tok") == (True, "vega")
    assert ht._check_bearer("Bearer nope") == (False, "")


def test_mcp_disengaged_auth_only(monkeypatch):
    from maverick_mcp import http_transport as ht
    monkeypatch.setenv("MAVERICK_MCP_TOKEN", "shared")
    _patch(monkeypatch, {}, enforced=False)
    assert ht._check_bearer("Bearer shared") == (True, "")
    assert ht._check_bearer("Bearer wrong") == (False, "")


def test_mcp_trust_state_load_error_fails_closed(monkeypatch):
    from maverick import agent_trust
    from maverick_mcp import http_transport as ht

    monkeypatch.setenv("MAVERICK_MCP_TOKEN", "shared")

    def _broken_state():
        raise OSError("trust registry unreadable")

    monkeypatch.setattr(agent_trust, "load_trust_state", _broken_state)
    assert ht._check_bearer("Bearer shared") == (False, "")
