"""The seeded bring-your-own-agent beat: an Agentforce quoting agent enrolled
across the trust plane + sidecar, two weeks of TERMINAL backdated runs owned
by ``agent:sf-quotebot`` on the Operating Record (orphan-reclaim-proof, same
rule as the Operating Record seed), exactly one approval parked in the queue
by the pre-action screen, and a no-restack guard on second boot."""
from __future__ import annotations

import random
import time

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from maverick import agent_trust, config, external_agents
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MAVERICK_EXTERNAL_AGENTS", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.setattr(agent_trust, "managed_path",
                        lambda: tmp_path / "agent_trust.json")
    monkeypatch.setattr(external_agents, "registry_path",
                        lambda: tmp_path / "external_agents.json")
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def test_seeds_enrollment_terminal_runs_and_one_parked_approval():
    import seed_workspace as sw
    sw._seed_external_agent(random.Random(11))

    from maverick import external_agents
    (row,) = [r for r in external_agents.roster() if r["id"] == "sf-quotebot"]
    assert row["platform"] == "agentforce"
    assert row["platform_label"] == "Salesforce Agentforce"
    assert row["department"] == "sales"
    assert row["owner"] == "jordan@company.com"
    assert row["max_dollars"] == 250.0
    assert row["period"] == "monthly"
    assert row["tool_risks"] == {"crm_update": "low", "send_contract": "high"}
    assert row["runs"] == 6
    # 6 runs at $1-4 each: real money on the meter, nowhere near the cap.
    assert 6.0 <= row["spent_dollars"] <= 24.0
    assert not row["over_budget"] and not row["contained"]

    from maverick.world_model import WorldModel
    w = WorldModel()
    rows = w.conn.execute(
        "SELECT status, created_at FROM goals "
        "WHERE owner = 'agent:sf-quotebot'").fetchall()
    assert len(rows) == 6
    statuses = [r[0] for r in rows]
    # Terminal only — a seeded active/pending row would be flipped to blocked
    # by the platform's orphan reclaim ~60s after boot and read as a crash.
    assert set(statuses) <= {"done", "blocked"}, statuses
    assert statuses.count("blocked") == 1   # the one honest failure
    # Backdated history, not stamped "now": even the newest run is >3d old.
    assert max(r[1] for r in rows) < time.time() - 3 * 86400
    # Episode windows were rewritten consistently (ended after started).
    bad = w.conn.execute(
        "SELECT COUNT(*) FROM episodes e JOIN goals g ON g.id = e.goal_id "
        "WHERE g.owner = 'agent:sf-quotebot' AND e.ended_at <= e.started_at"
    ).fetchone()[0]
    assert bad == 0
    # Exactly one approval parked by screen(), carrying the trusted
    # provenance the /approvals queue labels "external agent · BYOA gateway".
    pending = w.conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE status = 'pending' "
        "AND provenance = 'external_agents'").fetchone()[0]
    assert pending == 1


def test_second_seed_is_a_no_op():
    import seed_workspace as sw
    sw._seed_external_agent(random.Random(11))
    sw._seed_external_agent(random.Random(99))   # must skip: already enrolled

    from maverick import external_agents
    (row,) = [r for r in external_agents.roster() if r["id"] == "sf-quotebot"]
    assert row["runs"] == 6
    from maverick.world_model import WorldModel
    w = WorldModel()
    assert w.conn.execute(
        "SELECT COUNT(*) FROM goals WHERE owner = 'agent:sf-quotebot'"
    ).fetchone()[0] == 6
    assert w.conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE provenance = 'external_agents'"
    ).fetchone()[0] == 1
