"""Firm-safe policy contract for retained local improvement loops."""
from __future__ import annotations

from maverick import self_learning


def test_local_learning_is_on_but_extra_provider_egress_is_off(monkeypatch):
    monkeypatch.delenv("MAVERICK_SELF_LEARNING", raising=False)
    monkeypatch.setattr(
        "maverick.config.get_self_learning",
        lambda: {
            "enable": True,
            "allow_provider_egress": False,
            "distill_local": True,
        },
    )
    assert self_learning.enabled() is True
    assert self_learning.provider_egress_enabled() is False


def test_environment_can_disable_local_learning(monkeypatch):
    monkeypatch.setenv("MAVERICK_SELF_LEARNING", "0")
    assert self_learning.enabled() is False


def test_policy_failure_disables_learning_and_egress(monkeypatch):
    def fail():
        raise RuntimeError("unreadable policy")

    monkeypatch.delenv("MAVERICK_SELF_LEARNING", raising=False)
    monkeypatch.setattr("maverick.config.get_self_learning", fail)
    assert self_learning.enabled() is False
    assert self_learning.settings() == {
        "enable": False,
        "allow_provider_egress": False,
        "distill_local": False,
    }
    assert self_learning.provider_egress_enabled() is False
