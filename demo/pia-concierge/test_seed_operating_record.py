"""The seeded Operating Record behind the Overview / Spend / Workforce
dashboards: terminal statuses only (the platform's orphan-reclaim would flip
seeded 'active'/'pending' rows to blocked on the next boot), a costed run
ledger, backdated history, and a no-restack guard."""
from __future__ import annotations

import random
import time


def _fresh_home(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")


def test_operating_record_seeds_terminal_history(monkeypatch, tmp_path):
    _fresh_home(monkeypatch, tmp_path)
    import seed_workspace as sw
    sw._seed_operating_record(random.Random(1))

    from maverick.world_model import WorldModel
    w = WorldModel()
    statuses = {r[0] for r in w.conn.execute(
        "SELECT DISTINCT status FROM goals").fetchall()}
    assert statuses <= {"done", "blocked"}, statuses
    n_goals = w.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    assert n_goals == 52

    spend = w.total_spend()
    assert spend["runs"] > 30 and spend["dollars"] > 5
    # Opening any seeded goal shows real work, never "Worked across 0 steps":
    # every done goal carries a step trail and a result summary, and most are
    # signed off so the deliverables queue is a handful, not a backlog.
    done = [r[0] for r in w.conn.execute(
        "SELECT id FROM goals WHERE status = 'done'").fetchall()]
    with_events = {r[0] for r in w.conn.execute(
        "SELECT DISTINCT goal_id FROM goal_events").fetchall()}
    assert set(done) <= with_events
    assert w.conn.execute(
        "SELECT COUNT(*) FROM goals WHERE result IS NULL OR result = ''"
    ).fetchone()[0] == 0
    signed = w.conn.execute("SELECT COUNT(*) FROM signoffs").fetchone()[0]
    awaiting = len(done) - signed
    assert 0 < awaiting <= 8, (len(done), signed)
    # History is backdated months into the past, not stamped "now".
    oldest = w.conn.execute("SELECT MIN(created_at) FROM goals").fetchone()[0]
    assert oldest < time.time() - 60 * 86400
    # Episode windows were backdated consistently (ended after started).
    bad = w.conn.execute(
        "SELECT COUNT(*) FROM episodes "
        "WHERE ended_at IS NOT NULL AND ended_at <= started_at").fetchone()[0]
    assert bad == 0


def test_operating_record_never_stacks_a_second_batch(monkeypatch, tmp_path):
    _fresh_home(monkeypatch, tmp_path)
    import seed_workspace as sw
    sw._seed_operating_record(random.Random(1))
    sw._seed_operating_record(random.Random(2))   # must no-op: world not empty

    from maverick.world_model import WorldModel
    assert WorldModel().conn.execute(
        "SELECT COUNT(*) FROM goals").fetchone()[0] == 52
