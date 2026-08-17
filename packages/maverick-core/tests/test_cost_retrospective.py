"""Cost retrospective: aggregation, failed-work + concentration, CLI."""
from __future__ import annotations

from maverick.cost import retrospective as cr
from maverick.world_model import WorldModel


def _seed(path, goals):
    """goals: list of (title, [(cost, outcome), ...])."""
    wm = WorldModel(path)
    for title, eps in goals:
        gid = wm.create_goal(title, "")
        for cost, outcome in eps:
            ep = wm.start_episode(gid)
            wm.end_episode(ep, summary="s", outcome=outcome, cost_dollars=cost)
    wm.close()


def test_totals_and_top_goals(tmp_path):
    _seed(tmp_path / "w.db", [
        ("cheap", [(0.10, "succeeded")]),
        ("expensive", [(5.0, "succeeded"), (1.0, "succeeded")]),
        ("free", [(0.0, "succeeded")]),
    ])
    rep = cr.retrospective(WorldModel(tmp_path / "w.db"))
    assert rep["total_spend"] == 6.10
    assert rep["priced_goals"] == 2          # the free goal is excluded
    assert rep["top_goals"][0]["title"] == "expensive"
    assert rep["top_goals"][0]["cost"] == 6.0


def test_failed_spend_is_attributed(tmp_path):
    _seed(tmp_path / "w.db", [
        ("ok", [(2.0, "succeeded")]),
        ("bad", [(3.0, "failed")]),
    ])
    rep = cr.retrospective(WorldModel(tmp_path / "w.db"))
    assert rep["failed_spend"] == 3.0
    assert rep["failed_share"] == 0.6
    assert any("failed" in o.lower() for o in rep["observations"])


def test_top_goal_dominance_observed(tmp_path):
    _seed(tmp_path / "w.db", [
        ("whale", [(10.0, "succeeded")]),
        ("a", [(0.5, "succeeded")]),
        ("b", [(0.5, "succeeded")]),
    ])
    rep = cr.retrospective(WorldModel(tmp_path / "w.db"))
    assert any("all spend" in o for o in rep["observations"])


def test_concentration_pure():
    # all spend in one of ten goals -> costliest 10% (=1 goal) holds it all
    costs = [10.0] + [0.0] * 9
    assert cr._concentration(costs) == 1.0
    # perfectly even -> top 10% holds ~10%
    assert cr._concentration([1.0] * 10) == 0.1


def test_empty_world(tmp_path):
    _seed(tmp_path / "w.db", [])
    rep = cr.retrospective(WorldModel(tmp_path / "w.db"))
    assert rep["total_spend"] == 0.0 and rep["priced_goals"] == 0
    assert "no priced goals" in rep["observations"][0]






