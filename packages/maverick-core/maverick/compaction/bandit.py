"""Small persistent bandit used only by the local compaction heuristic."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

log = logging.getLogger(__name__)

DEFAULT_EPSILON = 0.1
_MIN_PULLS = 2


@dataclass
class _Arm:
    pulls: int = 0
    total_reward: float = 0.0

    @property
    def mean(self) -> float:
        return self.total_reward / self.pulls if self.pulls else 0.0


@dataclass
class ContextualBandit:
    """Per-context running-mean reward with an epsilon-greedy policy.

    This is a local strategy-selection primitive.  It never chooses an LLM,
    provider, endpoint, or model.
    """

    epsilon: float = DEFAULT_EPSILON
    rng: Random = field(default_factory=lambda: Random(0))
    path: Path | None = None
    _table: dict[str, dict[str, _Arm]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self._load()

    def _cplock(self):
        if self.path is None:
            from contextlib import nullcontext

            return nullcontext()
        from ..file_lock import cross_process_lock

        return cross_process_lock(self.path)

    def _reload_locked(self) -> None:
        if self.path is None:
            return
        self._table = {}
        self._load()

    def record(self, context: str, arm: str, reward: float) -> None:
        with self._lock, self._cplock():
            self._reload_locked()
            item = self._table.setdefault(context, {}).setdefault(arm, _Arm())
            item.pulls += 1
            item.total_reward += float(reward)
            self._save()

    def record_outcome(
        self,
        context: str,
        arm: str,
        *,
        success: bool,
        dollars: float,
    ) -> None:
        reward = 0.0 if not success else 1.0 / max(dollars, 1e-4)
        self.record(context, arm, reward)

    def choose(self, context: str, arms: list[str]) -> str | None:
        if not arms:
            return None
        if len(arms) == 1:
            return arms[0]
        with self._lock:
            table = self._table.get(context, {})
            under = [
                arm
                for arm in arms
                if table.get(arm, _Arm()).pulls < _MIN_PULLS
            ]
        if under:
            return self.rng.choice(under)
        if self.rng.random() < self.epsilon:
            return self.rng.choice(arms)
        with self._lock:
            table = self._table.get(context, {})
            return max(arms, key=lambda arm: (table.get(arm, _Arm()).mean, arm))

    def stats(self, context: str) -> dict[str, dict[str, float]]:
        with self._lock:
            table = self._table.get(context, {})
            return {
                arm: {
                    "pulls": value.pulls,
                    "mean_reward": round(value.mean, 6),
                }
                for arm, value in sorted(table.items())
            }

    def _load(self) -> None:
        try:
            from ..file_lock import atomic_read_text, ensure_private_file

            ensure_private_file(Path(self.path))
            raw = json.loads(atomic_read_text(Path(self.path)))
        except (OSError, ValueError):
            return
        for context, arms in (raw or {}).items():
            self._table[context] = {
                arm: _Arm(
                    pulls=int(data.get("pulls", 0)),
                    total_reward=float(data.get("total_reward", 0.0)),
                )
                for arm, data in arms.items()
            }

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            data = {
                context: {
                    arm: {
                        "pulls": value.pulls,
                        "total_reward": value.total_reward,
                    }
                    for arm, value in arms.items()
                }
                for context, arms in self._table.items()
            }
            from ..file_lock import atomic_write_text

            atomic_write_text(Path(self.path), json.dumps(data, sort_keys=True))
        except Exception:  # pragma: no cover - compaction learning is best effort
            log.debug("compaction bandit save failed", exc_info=True)


__all__ = ["ContextualBandit", "DEFAULT_EPSILON"]
