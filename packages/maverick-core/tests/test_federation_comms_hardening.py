"""Regression coverage for hardened inter-fleet federation semantics."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from maverick import agent_trust
from maverick import federation as fed
from maverick.federation import FederationError, FederationNode, FederationService, Peer


class _Goals:
    def __init__(self):
        self.started = 0

    def start_goal(self, title, description="", **kwargs):
        self.started += 1
        return self.started


def _service(goals: _Goals | None = None, *, state_path=None) -> FederationService:
    return FederationService(
        node="b",
        peers=[Peer("a", "a:1", "tok")],
        local_grant=None,
        goal_service=goals or _Goals(),
        record=lambda *args, **kwargs: None,
        state_path=state_path,
    )


def _payload(correlation_id: str = "corr-1", title: str = "do it") -> dict:
    return {
        "auth_token": "tok",
        "correlation_id": correlation_id,
        "goal_title": title,
        "goal_description": "",
        "requested_tools": [],
        "max_risk": "",
        "deadline_ms": 0,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    fed._seen_sigs.clear()
    fed._fed_rate_hits.clear()
    monkeypatch.setenv("MAVERICK_FEDERATION_RATE_LIMIT", "600")
    yield
    fed._seen_sigs.clear()
    fed._fed_rate_hits.clear()


def test_token_mode_correlation_retry_returns_original_goal(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    goals = _Goals()
    service = _service(goals)
    payload = _payload()

    first = service.delegate_goal(payload)
    retry = service.delegate_goal(dict(payload))

    assert first == retry == {"accepted": True, "goal_id": 1, "reason": ""}
    assert goals.started == 1
    assert len(fed._fed_rate_hits["peer:a"]) == 1


def test_correlation_result_survives_service_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    goals = _Goals()
    state_path = tmp_path / "federation.sqlite3"

    first = _service(goals, state_path=state_path).delegate_goal(_payload())
    restarted = _service(goals, state_path=state_path)
    retry = restarted.delegate_goal(dict(_payload()))
    changed = restarted.delegate_goal(_payload(title="changed"))

    assert retry == first == {"accepted": True, "goal_id": 1, "reason": ""}
    assert not changed["accepted"]
    assert "different delegation content" in changed["reason"]
    assert goals.started == 1


def test_corrupt_correlation_store_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    goals = _Goals()
    state_path = tmp_path / "federation.sqlite3"
    state_path.write_bytes(b"not a sqlite database")

    result = _service(goals, state_path=state_path).delegate_goal(_payload())

    assert not result["accepted"]
    assert "ledger is unavailable" in result["reason"]
    assert goals.started == 0


def test_sqlite_connection_cache_reopens_after_pid_change(monkeypatch, tmp_path):
    state = fed._FederationState(tmp_path / "federation.sqlite3")
    first = state._connect()
    real_pid = fed.os.getpid()
    monkeypatch.setattr(fed.os, "getpid", lambda: real_pid + 1)

    second = state._connect()

    assert second is not first
    with pytest.raises(fed.sqlite3.ProgrammingError):
        first.execute("SELECT 1")


def test_correlation_reuse_with_changed_content_is_refused(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    goals = _Goals()
    service = _service(goals)

    assert service.delegate_goal(_payload(title="first"))["accepted"] is True
    changed = service.delegate_goal(_payload(title="changed"))

    assert changed["accepted"] is False
    assert "different delegation content" in changed["reason"]
    assert goals.started == 1


def test_correlation_ids_are_strictly_bounded_on_both_halves(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    goals = _Goals()
    service = _service(goals)
    too_long = "c" * (fed._MAX_CORRELATION_ID_BYTES + 1)

    refused = service.delegate_goal(_payload(too_long))
    assert refused["accepted"] is False
    assert "too long" in refused["reason"]
    assert goals.started == 0 and service._correlations == {}

    node = FederationNode(
        node="a",
        peers=[Peer("b", "b:1", "tok")],
        transport_factory=lambda peer: service,
        record=lambda *args, **kwargs: None,
    )
    with pytest.raises(FederationError, match="too long"):
        node.delegate("b", "do it", correlation_id=too_long)


def test_concurrent_correlation_retries_start_exactly_one_goal(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    entered = threading.Event()
    release = threading.Event()

    class BlockingGoals(_Goals):
        def start_goal(self, title, description="", **kwargs):
            self.started += 1
            entered.set()
            release.wait(5)
            return self.started

    goals = BlockingGoals()
    service = _service(goals)
    results: list[dict] = []

    def call():
        results.append(service.delegate_goal(_payload()))

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    assert entered.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert goals.started == 1
    assert len(results) == 2
    assert results[0] == results[1] == {
        "accepted": True, "goal_id": 1, "reason": "",
    }


def test_signed_mode_new_signature_same_correlation_is_idempotent(monkeypatch):
    pytest.importorskip("cryptography")
    from maverick.agent_trust import TrustedAgent
    from maverick.audit import signing

    _private, public, _key_id = signing._load_or_create_keypair()
    registry = {
        "a": TrustedAgent(id="a", pubkey=public.hex(), allow_tools=frozenset()),
    }
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (True, registry))
    monkeypatch.setattr(fed, "require_signed", lambda: False)
    monkeypatch.setattr(FederationService, "_governance_block", lambda *args: None)
    monkeypatch.setattr(fed, "_shield_block", lambda text: None)
    goals = _Goals()
    service = _service(goals)

    def signed_payload() -> dict:
        payload = _payload("signed-corr")
        payload.update(fed._sign_delegation(
            "a", "b", "signed-corr", "do it", "", [], None, 0,
        ))
        return payload

    first_payload = signed_payload()
    second_payload = signed_payload()
    assert first_payload["sig"] != second_payload["sig"]

    first = service.delegate_goal(first_payload)
    retry = service.delegate_goal(second_payload)

    assert first == retry == {"accepted": True, "goal_id": 1, "reason": ""}
    assert goals.started == 1


def test_receiver_requires_signature_even_when_trust_plane_is_disengaged(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    monkeypatch.setattr(fed, "require_signed", lambda: True)
    goals = _Goals()

    result = _service(goals).delegate_goal(_payload("unsigned"))

    assert not result["accepted"]
    assert "signed delegation required" in result["reason"]
    assert goals.started == 0


def test_signature_policy_read_failure_requires_signatures(monkeypatch):
    import maverick.config as config

    monkeypatch.delenv("MAVERICK_FEDERATION_REQUIRE_SIGNED", raising=False)
    monkeypatch.setattr(
        config, "load_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config down")),
    )

    assert fed.require_signed() is True


def test_capability_policy_load_failure_is_deny_all(monkeypatch):
    import maverick.capability as capability

    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    monkeypatch.setattr(
        capability,
        "capability_from_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("config down")),
    )
    goals = _Goals()
    payload = _payload("policy-down")
    payload["requested_tools"] = ["read_file"]
    service = FederationService(
        node="b",
        peers=[Peer("a", "a:1", "tok")],
        goal_service=goals,
        record=lambda *args, **kwargs: None,
    )

    result = service.delegate_goal(payload)

    assert not result["accepted"]
    assert "required capabilities not granted" in result["reason"]
    assert goals.started == 0


@pytest.mark.parametrize("signed", [{}, {"sig": "partial-signature"}])
def test_signed_required_sender_does_not_downgrade(monkeypatch, signed):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    monkeypatch.setattr(fed, "require_signed", lambda: True)
    monkeypatch.setattr(
        fed, "_sign_delegation", lambda *args, **kwargs: dict(signed),
    )
    transport_calls: list[tuple[str, dict]] = []

    class Transport:
        def call(self, method, payload):
            transport_calls.append((method, payload))
            return {"accepted": True, "goal_id": 99, "reason": ""}

    node = FederationNode(
        node="a",
        peers=[Peer("b", "b:1", "tok")],
        transport_factory=lambda peer: Transport(),
        record=lambda *args, **kwargs: None,
    )
    result = node.delegate("b", "do it", correlation_id="must-sign")

    assert result.accepted is False
    assert "signing is unavailable" in result.reason
    assert transport_calls == []


def test_egress_redaction_failure_never_sends_plaintext(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (True, {}))
    monkeypatch.setattr(
        agent_trust,
        "decide_outbound",
        lambda *args, **kwargs: SimpleNamespace(denied=False, reason=""),
    )
    monkeypatch.setattr(agent_trust, "record_denied", lambda *args, **kwargs: None)
    monkeypatch.setattr(fed, "_shield_block", lambda text: None)
    monkeypatch.setattr(
        fed, "_redact", lambda text: (_ for _ in ()).throw(RuntimeError("down")),
    )
    transport_calls: list[dict] = []

    class Transport:
        def call(self, method, payload):
            transport_calls.append(payload)
            return {"accepted": True, "goal_id": 1, "reason": ""}

    node = FederationNode(
        node="a",
        peers=[Peer("b", "b:1", "tok")],
        transport_factory=lambda peer: Transport(),
        record=lambda *args, **kwargs: None,
    )
    result = node.delegate(
        "b", "secret title", "secret description", correlation_id="redact-fail",
    )

    assert result.accepted is False
    assert "redaction unavailable" in result.reason
    assert transport_calls == []


def test_governance_evaluation_exception_refuses_inbound(monkeypatch):
    import maverick.governance as governance

    decision = SimpleNamespace(denied=False, agent=None, capability=None)
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (True, {}))
    monkeypatch.setattr(
        agent_trust, "decide_inbound", lambda *args, **kwargs: decision,
    )
    monkeypatch.setattr(agent_trust, "record_denied", lambda *args, **kwargs: None)
    monkeypatch.setattr(fed, "require_signed", lambda: False)
    monkeypatch.setattr(
        governance,
        "evaluate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("PDP down")),
    )
    goals = _Goals()
    result = _service(goals).delegate_goal(_payload("governance-down"))

    assert result["accepted"] is False
    assert "governance policy unavailable" in result["reason"]
    assert goals.started == 0


def _fake_grpc_client(monkeypatch):
    from maverick import grpc_tls

    insecure_targets: list[str] = []

    class FakeGrpc:
        @staticmethod
        def insecure_channel(target):
            insecure_targets.append(target)
            return object()

    class FakePb2Grpc:
        class MaverickFederationStub:
            def __init__(self, channel):
                self.channel = channel

    monkeypatch.setattr(fed, "_require_grpc", lambda: FakeGrpc)
    monkeypatch.setattr(
        fed, "_load_stubs", lambda: (SimpleNamespace(), FakePb2Grpc),
    )
    monkeypatch.setattr(grpc_tls, "channel_credentials", lambda section: None)
    monkeypatch.setattr(grpc_tls, "tls_required", lambda section: False)
    return insecure_targets


def test_grpc_client_refuses_plaintext_nonloopback_by_default(monkeypatch):
    calls = _fake_grpc_client(monkeypatch)
    monkeypatch.delenv("MAVERICK_ALLOW_INSECURE_GRPC", raising=False)

    transport = fed._GrpcTransport(Peer("remote", "10.20.30.40:50061", "tok"))
    with pytest.raises(FederationError, match="non-loopback"):
        transport._bind()

    assert calls == []


@pytest.mark.parametrize(
    ("target", "allow_insecure"),
    [
        ("127.0.0.1:50061", False),
        ("localhost:50061", False),
        ("[::1]:50061", False),
        ("10.20.30.40:50061", True),
    ],
)
def test_grpc_client_plaintext_requires_loopback_or_explicit_override(
    monkeypatch, target, allow_insecure,
):
    calls = _fake_grpc_client(monkeypatch)
    if allow_insecure:
        monkeypatch.setenv("MAVERICK_ALLOW_INSECURE_GRPC", "1")
    else:
        monkeypatch.delenv("MAVERICK_ALLOW_INSECURE_GRPC", raising=False)

    transport = fed._GrpcTransport(Peer("peer", target, "tok"))
    stub, _pb2 = transport._bind()

    assert stub is not None
    assert calls == [target]


def test_correlation_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    monkeypatch.setattr(fed, "_CORRELATION_CACHE_MAX", 2)
    service = _service()

    first = service.delegate_goal(_payload("corr-0"))
    second = service.delegate_goal(_payload("corr-1"))
    refused = service.delegate_goal(_payload("corr-2"))

    assert first["accepted"] is True and second["accepted"] is True
    assert refused["accepted"] is False
    assert "ledger capacity" in refused["reason"]
    assert len(service._correlations) == 2
    # Capacity pressure must not evict a still-live idempotency record and
    # thereby reopen it to duplicate goal creation.
    assert service.delegate_goal(_payload("corr-0")) == first


def test_rate_limited_unique_id_does_not_consume_correlation_capacity(monkeypatch):
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))
    monkeypatch.setenv("MAVERICK_FEDERATION_RATE_LIMIT", "1")
    goals = _Goals()
    service = _service(goals)

    assert service.delegate_goal(_payload("allowed"))["accepted"]
    limited = service.delegate_goal(_payload("retry-later"))
    assert not limited["accepted"] and "rate limit" in limited["reason"]
    assert ("a", "retry-later") not in service._correlations

    # Raising the operator cap lets the same side-effect-free request claim and
    # run; the rate refusal did not leave a 24-hour correlation tombstone.
    monkeypatch.setenv("MAVERICK_FEDERATION_RATE_LIMIT", "600")
    assert service.delegate_goal(_payload("retry-later"))["accepted"]
    assert goals.started == 2
