"""Per-agent manager scorecard -- value, cost, and efficiency for each
specialist, from signals that already exist.

The workforce value report rolls economics up to the *department*; a manager
wants it per *specialist*. Every goal is stamped with the domain pack it ran
as (``goal.domain``), and every episode carries its ``cost_dollars`` and
outcome, so per-agent attribution needs no new capture -- only grouping.

For each agent this reports, over a window:
  * **runs / delivered** -- episodes, and how many reached a done outcome,
  * **cost** -- summed episode dollars (what the agent's runs actually spent),
  * **value / cost avoided** -- delivered x the operator's human-baseline cost
    per deliverable, minus cost (the same descriptive economics the workforce
    report uses; never used to defund anything),
  * **ROI multiple** and **cost per delivered**,
  * **success rate** -- the efficiency measure (delivered / runs),
  * **last active**.

Read-only and defensive: every signal degrades to empty rather than raising,
so a scorecard never blocks the page. Distinct from ``health_score`` (per-run
quality) -- this is the per-agent management rollup.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_DONE = {"done", "succeeded", "success", "completed", "fulfilled", "approved"}
# Fallback human cost of one comparable human deliverable (mirrors
# workforce_value.DEFAULT_HUMAN_COST_PER_DELIVERABLE; operator-set in practice).
DEFAULT_HUMAN_COST_PER_DELIVERABLE = 120.0


def _is_done(outcome: str | None) -> bool:
    return (outcome or "").strip().lower() in _DONE


@dataclass
class AgentScore:
    """One specialist's management scorecard over the window."""

    agent: str
    suite: str = ""
    runs: int = 0
    delivered: int = 0
    cost: float = 0.0
    tokens: int = 0
    last_active: float = 0.0
    human_cost_per_deliverable: float = DEFAULT_HUMAN_COST_PER_DELIVERABLE

    @property
    def value(self) -> float:
        """Human-baseline cost of the work this agent delivered."""
        return self.delivered * self.human_cost_per_deliverable

    @property
    def cost_avoided(self) -> float:
        return self.value - self.cost

    @property
    def roi_multiple(self) -> float | None:
        return round(self.value / self.cost, 1) if self.cost > 0 else None

    @property
    def cost_per_delivered(self) -> float | None:
        return round(self.cost / self.delivered, 4) if self.delivered else None

    @property
    def success_rate(self) -> float | None:
        return round(100.0 * self.delivered / self.runs, 1) if self.runs else None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "suite": self.suite,
            "runs": self.runs, "delivered": self.delivered,
            "cost": round(self.cost, 4), "tokens": self.tokens,
            "value": round(self.value, 2),
            "cost_avoided": round(self.cost_avoided, 2),
            "roi_multiple": self.roi_multiple,
            "cost_per_delivered": self.cost_per_delivered,
            "success_rate": self.success_rate,
            "last_active": self.last_active or None,
        }


def _domain_of(world: Any, goal_id: int, cache: dict[int, str]) -> str:
    if goal_id in cache:
        return cache[goal_id]
    dept = ""
    try:
        g = world.get_goal(goal_id)
        dept = (getattr(g, "domain", "") or "") if g else ""
    except Exception:  # pragma: no cover -- a read miss is just "(unattributed)"
        dept = ""
    cache[goal_id] = dept or "(unattributed)"
    return cache[goal_id]


def compute(
    world: Any, *, window_days: int = 90,
    human_cost: float | None = None, human_cost_for: Any = None,
    episode_limit: int = 5000, now: float | None = None,
) -> list[AgentScore]:
    """Per-agent scorecards over ``window_days``, richest-value first.

    ``human_cost`` overrides the flat baseline; ``human_cost_for(agent)`` is an
    optional per-agent override (e.g. a legal specialist's hour priced above
    support). Never raises."""
    ts_now = now if now is not None else time.time()
    cutoff = ts_now - window_days * 86400.0
    base_hc = (DEFAULT_HUMAN_COST_PER_DELIVERABLE if human_cost is None
               else float(human_cost))

    def _hc(agent: str) -> float:
        if human_cost_for is None:
            return base_hc
        try:
            return float(human_cost_for(agent))
        except Exception:  # pragma: no cover -- a bad override never breaks it
            return base_hc

    try:
        episodes = world.list_episodes(limit=episode_limit)
    except Exception as e:  # pragma: no cover -- report never blocks
        log.debug("agent_scorecard: episode read failed: %s", e)
        return []

    scores: dict[str, AgentScore] = {}
    cache: dict[int, str] = {}
    for ep in episodes:
        started = float(getattr(ep, "started_at", 0) or 0)
        if started < cutoff:
            continue
        agent = _domain_of(world, int(getattr(ep, "goal_id", 0) or 0), cache)
        sc = scores.get(agent)
        if sc is None:
            sc = scores[agent] = AgentScore(
                agent=agent, human_cost_per_deliverable=_hc(agent))
        sc.runs += 1
        sc.cost += float(getattr(ep, "cost_dollars", 0) or 0)
        sc.tokens += (int(getattr(ep, "input_tokens", 0) or 0)
                      + int(getattr(ep, "output_tokens", 0) or 0))
        if _is_done(getattr(ep, "outcome", None)):
            sc.delivered += 1
        sc.last_active = max(sc.last_active, started)

    # Stamp the suite so the manager view can group by department.
    try:
        from .domain import suite_for
        for agent, sc in scores.items():
            if agent != "(unattributed)":
                sc.suite = suite_for(agent) or ""
    except Exception:  # pragma: no cover -- suite lookup is cosmetic
        pass

    return sorted(scores.values(), key=lambda s: -s.cost_avoided)


def for_agent(world: Any, agent: str, **kwargs: Any) -> AgentScore:
    """One specialist's scorecard (empty if it hasn't run in the window)."""
    for sc in compute(world, **kwargs):
        if sc.agent == agent:
            return sc
    hc = kwargs.get("human_cost")
    return AgentScore(agent=agent,
                      human_cost_per_deliverable=(
                          DEFAULT_HUMAN_COST_PER_DELIVERABLE
                          if hc is None else float(hc)))


__all__ = ["AgentScore", "compute", "for_agent",
           "DEFAULT_HUMAN_COST_PER_DELIVERABLE"]
