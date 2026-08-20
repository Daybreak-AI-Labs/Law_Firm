"""Retained regression tests for contested audit findings."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from maverick.blackboard import Blackboard
from maverick.budget import Budget
from maverick.domain import enabled_domains
from maverick.quarantine import QuarantineRegistry
from maverick.tools.spawn import spawn_specialist_tool


def test_scrub_env_strips_passphrase_netrc_cookie_auth():
    from maverick.sandbox.local import scrub_env
    src = {
        "ANSIBLE_VAULT_PASSPHRASE": "p",
        "NETRC": "/root/.netrc",
        "SESSION_COOKIE": "abc",
        "GITHUB_AUTH": "tok",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
    }
    out = scrub_env(src)
    for leaked in ("ANSIBLE_VAULT_PASSPHRASE", "NETRC", "SESSION_COOKIE", "GITHUB_AUTH"):
        assert leaked not in out, leaked
    assert out["PATH"] == "/usr/bin" and out["HOME"] == "/home/u"


def _fake_parent(depth=0):
    ctx = SimpleNamespace(
        budget=Budget(max_dollars=10.0), blackboard=Blackboard(), max_depth=3,
        goal_id=1, quarantine=None,
        try_reserve_spawns=lambda n: True, release_spawns=lambda n: None,
    )
    return SimpleNamespace(depth=depth, name="orchestrator", role="orchestrator",
                           ctx=ctx, max_steps=17, capability=None)


@pytest.mark.asyncio
async def test_sealed_mid_run_child_final_is_withheld(monkeypatch):
    dom = sorted(enabled_domains())[0]
    quarantine = QuarantineRegistry()
    parent = _fake_parent()
    parent.ctx.quarantine = quarantine

    def fake_agent_from_profile(profile, ctx, task, *, parent=None, depth=0, principal=None):
        name = f"agent:{profile.name}-{depth}"

        async def _run():
            quarantine.seal(name, "compromised mid-run")  # sealed AFTER spawn check
            return SimpleNamespace(final="SHOULD NOT LEAK", blocked_on_user=False, error=None)
        return SimpleNamespace(role=profile.name, name=name,
                               domain=profile.compartment, max_steps=None, run=_run)

    monkeypatch.setattr("maverick.domain.agent_from_profile", fake_agent_from_profile)

    out = await spawn_specialist_tool(parent).fn({"domain": dom, "task": "do it"})
    assert "SHOULD NOT LEAK" not in out
    assert "withheld" in out
