"""The Consequence Engine -- reality as the reward.

Every learning signal in production AI is a proxy: a human saying "looks good"
(RLHF), an LLM judge (RLAIF), or a hardcoded checker (RLVR). None is the *actual
consequence* of the action in the world -- because no one else operates a
governed workforce that acts on real business systems and can observe the
result. Maverick can: weeks later, the invoice gets paid or it doesn't, the
contract renews or it doesn't, the ticket stays closed or it reopens. That
downstream fact is **ground truth** -- the one signal that can't be gamed,
because a policy that fools an LLM judge still fails reality.

This module is the grounded-outcome **join**: a system of record (CRM / ERP /
ticketing) reports a real outcome for a past episode via :func:`record_outcome`,
keyed by the ``(goal_id, episode_id)`` the agent acted under; :func:`resolve`
returns it if it has landed. :func:`grounded_outcome` is the helper the data
engine / causal credit use to **prefer reality over the proxy** wherever reality
has reported back -- so triage and promotion learn from what actually happened,
not from a model's opinion of it.

The per-customer connectors that CALL ``record_outcome`` (map an invoice/contract
id back to the episode that touched it) are integration seams; the grounded join,
the store, and the prefer-reality rule are here. Grounding is ON by default.
Direct external outcome submissions remain durable evidence even when the join is
disabled; the data engine only consumes them while ``[consequence]`` is enabled.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import governed_learning_env_flag

log = logging.getLogger(__name__)

_MAX_ROWS = 100_000


def enabled() -> bool:
    """Whether the default-on data-engine join should prefer real outcomes."""
    _v = governed_learning_env_flag("MAVERICK_CONSEQUENCE")
    if _v is not None:
        return _v
    try:
        from .config import get_consequence

        return bool(get_consequence().get("enable", False))
    except Exception:  # pragma: no cover -- config never blocks a run
        return False


def _clamp(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ConsequenceStore:
    """Append-only store of real-world outcome events, keyed (goal_id, episode_id).

    The latest event for a key wins (a contract can renew, then churn -- the most
    recent ground truth is the reward). Atomic-append, 0600, bounded.
    """

    path: Path | None = None
    max_rows: int = _MAX_ROWS
    _latest: dict = None  # type: ignore[assignment]
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._lock is None:
            self._lock = threading.Lock()
        if self._latest is None:
            self._latest = {}
        if self.path is not None:
            self._load()

    def record(self, goal_id: int, episode_id: int, value: float, *, kind: str = "",
               ts: float | None = None) -> bool:
        """Append a real outcome for a past episode. Never raises."""
        key = (int(goal_id), int(episode_id))
        row = {"ts": ts if ts is not None else time.time(), "goal_id": key[0],
               "episode_id": key[1], "value": _clamp(value), "kind": str(kind)[:64]}
        with self._lock:
            self._latest[key] = (row["ts"], row["value"])
            if self.path is None:
                return True
            try:
                p = Path(self.path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                return True
            except Exception:  # pragma: no cover -- best-effort
                log.debug("consequence record failed", exc_info=True)
                return False

    def resolve(self, goal_id: int, episode_id: int) -> float | None:
        """The latest real outcome for the episode, or None if none has landed."""
        with self._lock:
            hit = self._latest.get((int(goal_id), int(episode_id)))
            return hit[1] if hit is not None else None

    def count(self) -> int:
        """How many distinct episodes have a real outcome recorded -- the size of
        the grounded-learning signal accumulated so far."""
        with self._lock:
            return len(self._latest)

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return
        for raw in lines[-self.max_rows:]:
            try:
                d = json.loads(raw)
                key = (int(d["goal_id"]), int(d["episode_id"]))
                ts = float(d.get("ts", 0.0))
                prev = self._latest.get(key)
                if prev is None or ts >= prev[0]:
                    self._latest[key] = (ts, _clamp(d["value"]))
            except (KeyError, ValueError, TypeError):
                continue


_shared: dict = {}
_shared_lock = threading.Lock()


def shared() -> ConsequenceStore:
    from .paths import data_dir

    path = data_dir("consequences.ndjson")
    with _shared_lock:
        store = _shared.get(path)
        if store is None:
            store = ConsequenceStore(path=path)
            _shared[path] = store
        return store


def reset_shared() -> None:
    with _shared_lock:
        _shared.clear()
    with _shared_corr_lock:
        _shared_corr.clear()


def record_outcome(goal_id: int, episode_id: int, value: float, *, kind: str = "",
                   store: ConsequenceStore | None = None) -> bool:
    """Record a real downstream outcome for a past episode (the grounded reward).

    Called by a system-of-record connector once reality reports back. ``value`` is
    the real result in [0, 1] (paid=1.0 / unpaid=0.0, renewed=1.0, reopened=0.0,
    or a graded result). Just stores; the data-engine join decides whether to use
    it (gated by ``[consequence]``)."""
    from .learning_guard import learning_write_allowed
    if not learning_write_allowed("consequence"):
        return False
    return (store or shared()).record(goal_id, episode_id, value, kind=kind)


def resolve(goal_id: int, episode_id: int, *, store: ConsequenceStore | None = None) -> float | None:
    """The real outcome for an episode if it has landed, else None."""
    return (store or shared()).resolve(goal_id, episode_id)


def count(*, store: ConsequenceStore | None = None) -> int:
    """Number of episodes with a grounded outcome recorded (accumulated signal)."""
    return (store or shared()).count()


def grounded_outcome(goal_id: int, episode_id: int, proxy: float | None, *,
                     store: ConsequenceStore | None = None) -> float | None:
    """Prefer the real outcome over the proxy (verifier confidence) when present.

    The one-line rule that grounds learning in reality: if a real consequence has
    landed for this episode, that's the reward; otherwise fall back to the proxy.
    A no-op (returns ``proxy``) unless ``[consequence]`` is enabled.
    """
    if not enabled():
        return proxy
    real = resolve(goal_id, episode_id, store=store)
    return real if real is not None else proxy


def record_self_outcome(world, goal_id: int, value: float, *, kind: str,
                        store: ConsequenceStore | None = None) -> bool:
    """Ground a **first-party** outcome against a goal's latest episode.

    The single helper for the outcomes Maverick observes directly -- a human's
    certify/reject, a thumbs-up/down, or the run's own terminal failure -- as
    opposed to :func:`record_outcome`, which a system-of-record connector calls
    with an episode id it already knows. Resolves the episode from ``world``
    (duck-typed: needs ``list_episodes(goal_id=, limit=)``). A no-op returning
    ``False`` when ``[consequence]`` is explicitly disabled, and never raises --
    grounding is best-effort and must not fail the
    action that produced it."""
    try:
        if not enabled():
            return False
        episodes = world.list_episodes(goal_id=goal_id, limit=1)
        if not episodes:
            return False
        return record_outcome(goal_id, episodes[0].id, float(value), kind=kind, store=store)
    except Exception:  # pragma: no cover -- grounding is best-effort
        log.debug("self-outcome not recorded for goal %s", goal_id, exc_info=True)
        return False


# ---- outcome correlation: external business key -> the episode that acted ----
#
# The Consequence Engine keys reward on ``(goal_id, episode_id)``, but a system
# of record reports back with only the business key it owns -- ``invoice INV-42
# was paid``, ``ticket 91 reopened``. This store is the join that lets a webhook
# ground an outcome with just that key: the run that acted on the entity links
# the key to its episode, and the outcome ingest resolves the key back. This is
# what makes the marquee "weeks later reality reports back" signal reachable from
# an integration that has never heard of an episode id.

_MAX_LINKS = 100_000


@dataclass
class CorrelationStore:
    """Append-only map ``external key -> (goal_id, episode_id)`` that acted on it.

    The latest link for a key wins (the most recent run to touch the entity owns
    the outcome). Atomic-append, 0600, bounded -- the ``ConsequenceStore`` shape.
    """

    path: Path | None = None
    max_rows: int = _MAX_LINKS
    _latest: dict = None  # type: ignore[assignment]
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._lock is None:
            self._lock = threading.Lock()
        if self._latest is None:
            self._latest = {}
        if self.path is not None:
            self._load()

    def link(self, key: str, goal_id: int, episode_id: int, *,
             ts: float | None = None) -> bool:
        """Link an external business key to the episode that acted. Never raises."""
        k = str(key).strip()
        if not k:
            return False
        row = {"ts": ts if ts is not None else time.time(), "key": k[:256],
               "goal_id": int(goal_id), "episode_id": int(episode_id)}
        with self._lock:
            self._latest[row["key"]] = (row["ts"], row["goal_id"], row["episode_id"])
            if self.path is None:
                return True
            try:
                p = Path(self.path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600),
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                return True
            except Exception:  # pragma: no cover -- best-effort
                log.debug("outcome-link record failed", exc_info=True)
                return False

    def resolve(self, key: str) -> tuple[int, int] | None:
        """The (goal_id, episode_id) most recently linked to ``key``, or None."""
        with self._lock:
            hit = self._latest.get(str(key).strip())
            return (hit[1], hit[2]) if hit is not None else None

    def count(self) -> int:
        with self._lock:
            return len(self._latest)

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return
        for raw in lines[-self.max_rows:]:
            try:
                d = json.loads(raw)
                key = str(d["key"])
                ts = float(d.get("ts", 0.0))
                prev = self._latest.get(key)
                if prev is None or ts >= prev[0]:
                    self._latest[key] = (ts, int(d["goal_id"]), int(d["episode_id"]))
            except (KeyError, ValueError, TypeError):
                continue


_shared_corr: dict = {}
_shared_corr_lock = threading.Lock()


def shared_correlation() -> CorrelationStore:
    from .paths import data_dir

    path = data_dir("outcome_links.ndjson")
    with _shared_corr_lock:
        store = _shared_corr.get(path)
        if store is None:
            store = CorrelationStore(path=path)
            _shared_corr[path] = store
        return store


def link_outcome_key(key: str, goal_id: int, episode_id: int, *,
                     store: CorrelationStore | None = None) -> bool:
    """Record that ``key`` (an external business id) was acted on by this episode,
    so a later outcome reported against ``key`` can be grounded. Always harmless."""
    from .learning_guard import learning_write_allowed
    if not learning_write_allowed("consequence", "link_outcome_key"):
        return False
    return (store or shared_correlation()).link(key, goal_id, episode_id)


def record_outcome_for_key(key: str, value: float, *, kind: str = "",
                           store: ConsequenceStore | None = None,
                           corr: CorrelationStore | None = None,
                           authorize=None) -> bool:
    """Ground a downstream outcome using only the external business ``key``.

    Resolves ``key`` to the episode that acted (via the correlation store) and
    records the grounded reward there. ``authorize(goal_id, episode_id) -> bool``,
    when given, must approve the resolved target first (the caller's access
    check). Returns ``True`` when a correlation existed, was authorized, and the
    outcome was recorded; ``False`` when no run has linked that key yet, or the
    caller may not ground that target."""
    hit = (corr or shared_correlation()).resolve(key)
    if hit is None:
        return False
    goal_id, episode_id = hit
    if authorize is not None and not authorize(goal_id, episode_id):
        return False
    return record_outcome(goal_id, episode_id, value, kind=kind, store=store)


__all__ = [
    "ConsequenceStore", "enabled", "shared", "reset_shared",
    "record_outcome", "resolve", "grounded_outcome", "count",
    "record_self_outcome",
    "CorrelationStore", "shared_correlation", "link_outcome_key",
    "record_outcome_for_key",
]
