"""Per-agent scorecard: value/cost/efficiency grouped by the pack a goal ran
as, from existing episode signals."""
from __future__ import annotations

from types import SimpleNamespace

from maverick import agent_scorecard as sc


class _FakeWorld:
    """Minimal world stub: episodes carry goal_id + cost + outcome; goals
    carry the domain (the specialist pack that ran)."""

    def __init__(self, episodes, goal_domains):
        self._episodes = episodes
        self._domains = goal_domains

    def list_episodes(self, limit=5000):
        return self._episodes

    def get_goal(self, goal_id):
        return SimpleNamespace(domain=self._domains.get(goal_id, ""))


def _ep(goal_id, cost, outcome, started):
    return SimpleNamespace(goal_id=goal_id, cost_dollars=cost, outcome=outcome,
                           started_at=started, input_tokens=1000,
                           output_tokens=2000)


def test_groups_by_pack_and_computes_value_cost_roi(monkeypatch):
    import time
    now = time.time()
    world = _FakeWorld(
        episodes=[
            _ep(1, 0.50, "done", now - 100),
            _ep(2, 0.30, "done", now - 200),
            _ep(3, 0.40, "failed", now - 300),   # finance_ap, not delivered
            _ep(4, 0.20, "done", now - 400),      # hr_i9_compliance
        ],
        goal_domains={1: "finance_ap", 2: "finance_ap",
                      3: "finance_ap", 4: "hr_i9_compliance"})
    scores = {s.agent: s for s in sc.compute(world, human_cost=120.0, now=now)}
    ap = scores["finance_ap"]
    assert ap.runs == 3 and ap.delivered == 2
    assert ap.cost == 0.50 + 0.30 + 0.40
    assert ap.value == 2 * 120.0                  # two delivered
    assert ap.roi_multiple == round(240.0 / 1.20, 1)
    assert ap.success_rate == round(100.0 * 2 / 3, 1)
    d = ap.to_dict()
    assert d["cost_avoided"] == round(240.0 - 1.20, 2)
    # hr_i9_compliance delivered its one run.
    assert scores["hr_i9_compliance"].success_rate == 100.0


def test_window_excludes_old_episodes():
    import time
    now = time.time()
    world = _FakeWorld(
        episodes=[_ep(1, 1.0, "done", now - 10 * 86400),
                  _ep(2, 1.0, "done", now - 200 * 86400)],
        goal_domains={1: "finance_ap", 2: "finance_ap"})
    ap = sc.for_agent(world, "finance_ap", window_days=90, now=now)
    assert ap.runs == 1        # the 200-day-old run is outside the window


def test_empty_and_defensive():
    assert sc.compute(None) == []           # no world -> empty, no raise
    empty = sc.for_agent(None, "finance_ap")
    assert empty.agent == "finance_ap" and empty.runs == 0
    assert empty.roi_multiple is None and empty.success_rate is None
