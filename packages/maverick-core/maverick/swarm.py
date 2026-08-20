"""Swarm context: shared state for all agents in a single run.

Every agent in a swarm shares:
  - one LLM client (with its own connection pool)
  - one WorldModel (persistent state)
  - one Budget (global cost/time/token cap)
  - one Blackboard (shared workspace for the run)
  - one Sandbox (execution backend)
  - one Shield (input/tool-call/output scans; may be None if disabled)

Children inherit the parent's context but get their own brief, role, and depth.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .blackboard import Blackboard
from .budget import Budget
from .llm import LLM
from .world_model import WorldModel


def _default_use_skills() -> bool:
    """Whether to inject skills into agent prompts.

    Precedence: the MAVERICK_USE_SKILLS env var wins when set (the runbook
    tells operators to set MAVERICK_USE_SKILLS=0 for SWE-bench Pro runs --
    skill memorization is a contamination risk, see reproducibility-audit +
    Karpathy-review findings). When the env var is unset, fall back to the
    [features] skills config toggle (default on). Fail-soft to on so an
    unreadable config never silently disables skills.
    """
    env = os.environ.get("MAVERICK_USE_SKILLS")
    if env is not None:
        return env.lower() not in ("0", "false", "no")
    try:
        from .config import get_features
        return bool(get_features()["skills"])
    except Exception:
        return True


def _default_max_total_spawns() -> int:
    try:
        return max(1, int(os.environ.get("MAVERICK_MAX_TOTAL_SPAWNS", "64")))
    except ValueError:
        return 64


def _new_bus_namespace() -> str:
    """Opaque routing capability for one swarm's in-process message bus."""
    return secrets.token_hex(16)


@dataclass
class SwarmContext:
    llm: LLM
    world: WorldModel
    budget: Budget
    blackboard: Blackboard
    sandbox: Any
    goal_id: int
    # Agent-bus isolation: every run gets an unguessable namespace and a
    # run-scoped roster. Agent tools must be both namespaced and roster-bound,
    # so learning another run's display id cannot route a message into it.
    bus_namespace: str = field(default_factory=_new_bus_namespace, init=False)
    _bus_agents: set[str] = field(default_factory=set, init=False, repr=False)
    _bus_agents_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )
    max_depth: int = 3
    use_skills: bool = field(default_factory=_default_use_skills)
    shield: Any | None = None
    # Agent compartments (Rung 1 containment): a run-scoped QuarantineRegistry
    # of agents sealed off after a confirmed threat. None == disabled. Typed
    # Any to avoid importing the quarantine module here.
    quarantine: Any | None = None
    # Per-domain document knowledge (vector RAG). None == disabled. Set when
    # [knowledge] is enabled; the knowledge_search tool binds to it per agent.
    knowledge: Any | None = None
    channel: str | None = None
    user_id: str | None = None
    # P0 identity layer: the root principal's capability grant for this run.
    # None == unrestricted (the default). Set when capability enforcement is
    # enabled; children inherit an *attenuated* copy so a sub-agent can never
    # exceed it. Typed Any to avoid importing the capability module here.
    capability: Any | None = None
    # Department-suite grant for this run. None == unrestricted (auth off,
    # dashboard admin, or no explicit/default grant). Non-None means specialist
    # discovery/spawn tools may only expose packs whose suite is in the grant;
    # generic packs with no suite remain unscoped.
    allowed_suites: frozenset[str] | None = None
    # Verified peer-to-peer handoffs (bus_handoff): the run's signed-delegation
    # trust domain -- an ephemeral issuer key + a process-wide replay nonce cache.
    # Lazily installed by ``bus_handoff.authority_for`` when capability
    # enforcement is on; None == plain, unverified bus messages. Typed Any to
    # avoid importing bus_handoff here.
    handoff_authority: Any | None = None
    # Durable execution: the episode this run belongs to. Discriminates
    # best-of-N attempts (same goal_id, distinct episodes) so a resumed
    # attempt doesn't pick up a sibling's checkpoint. Defaults to 0 for
    # callers that don't checkpoint.
    episode_id: int = 0
    # Exact client-matter boundary for every run-scoped memory/knowledge path.
    # ``None`` is legacy/unfiled and must fail closed for client-derived recall.
    matter_id: int | None = None
    max_total_spawns: int = field(default_factory=_default_max_total_spawns)
    # Names of skills recalled into any agent's prompt during this run. Retained
    # only as ephemeral governed-action provenance; no tenant-global use or
    # outcome statistics are written. Shared across the swarm and mutated only
    # on the single event loop, so a plain set is safe.
    skills_used: set[str] = field(default_factory=set)
    # Models whose learned self-harness guidance was recalled into any agent's
    # prompt during this run (stamped by Agent._with_harness_addendum). The
    # orchestrator attributes the run's final outcome to each at finalize
    # (self_harness.note_outcome) so a WORKER model's guidance accumulates
    # outcome counters too, not just the orchestrator's. Same sharing/mutation
    # pattern as ``skills_used``.
    harness_models: set[str] = field(default_factory=set)
    # Live trust signals consumed by the autonomy gate (maverick.autonomy) and
    # the trajectory-donation selector. ``last_disagreement`` is the normalized
    # answer entropy of the most recent swarm fan-out (0 == consensus); it is
    # stamped by ``spawn_swarm``. ``last_verifier_confidence`` is the most
    # recent verifier verdict's confidence (1.0 == not yet verified, i.e. no
    # tightening).
    last_disagreement: float = 0.0
    last_verifier_confidence: float = 1.0
    # Counterfactual swarm credit (maverick.credit): agent name -> marginal
    # credit from the most recent fan-out, when CSCA is enabled. Read by the
    # donation selector / routing; empty when not computed.
    last_credit: dict = field(default_factory=dict)
    # Per-sub-agent trajectories from the most recent fan-out (role, name,
    # tool-name actions, credit, learn-weight) -- the credit-weighted units the
    # data engine learns from. Empty when CSCA didn't run.
    last_subtrajectories: list = field(default_factory=list)
    _spawns_used: int = 0
    _workdir_lock: asyncio.Lock | None = field(default=None, repr=False)

    def register_bus_agent(self, agent_id: str) -> None:
        """Add an instantiated agent to this run's communication roster."""
        with self._bus_agents_lock:
            self._bus_agents.add(agent_id)

    def knows_bus_agent(self, agent_id: str) -> bool:
        """Whether ``agent_id`` belongs to this exact swarm/run."""
        with self._bus_agents_lock:
            return agent_id in self._bus_agents

    @property
    def bus_agents(self) -> frozenset[str]:
        """Immutable roster snapshot for diagnostics and tests."""
        with self._bus_agents_lock:
            return frozenset(self._bus_agents)

    @property
    def workdir_lock(self) -> asyncio.Lock:
        """Serialize the coding-mode apply/test/reset critical section.

        ``spawn_swarm`` runs coder children concurrently via
        ``asyncio.gather``; they all share one ``sandbox.workdir`` and
        mutate its git tree (apply patch -> run tests -> reset). Without
        a lock, two children stomp each other's working tree. Created
        lazily: a raw ``asyncio.Lock`` as a dataclass default would bind
        to whatever loop exists at construction (often none), so we make
        it on first access, which always happens inside a running loop.
        """
        if self._workdir_lock is None:
            self._workdir_lock = asyncio.Lock()
        return self._workdir_lock

    def try_reserve_spawns(self, n: int) -> bool:
        """Reserve ``n`` child-agent slots for this goal.

        ``max_depth`` + per-call fan-out alone allow an exponential herd
        (8 + 64 + 512 + ... agents) that a hijacked/confused orchestrator
        can use to burn the whole budget on attacker work before refusal.
        This bounds the TOTAL agents a single goal may create. Synchronous
        (no await between check and bump), so atomic on the event loop.
        """
        if self._spawns_used + max(0, n) > self.max_total_spawns:
            return False
        self._spawns_used += max(0, n)
        return True

    def release_spawns(self, n: int) -> None:
        """Return ``n`` reserved spawn slots after a child genuinely FAILED.

        ``try_reserve_spawns`` bumps ``_spawns_used`` at reservation time, but
        nothing ever gave the slot back: a long run with transient child
        errors would burn through ``max_total_spawns`` and hit the per-goal
        cap prematurely (#612). Callers release only on a child that RAISED
        (a real failure) -- a successful child legitimately consumed its slot.
        Synchronous, so atomic on the event loop; clamped at 0.
        """
        self._spawns_used = max(0, self._spawns_used - max(0, n))
