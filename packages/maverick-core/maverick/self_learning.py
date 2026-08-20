"""Policy gates shared by Maverick's governed local improvement loops.

The law-firm runtime does not acquire remote skills, plugins, tools, API specs,
or subprocess servers. Retained learning is local: reflexion, rehearsal,
distillation, and evidence-gated improvement. Extra model calls remain a
separate operator-controlled egress decision.
"""
from __future__ import annotations

from .config import governed_learning_env_flag


def enabled() -> bool:
    """Whether governed local learning is active. On by default."""
    override = governed_learning_env_flag("MAVERICK_SELF_LEARNING")
    if override is not None:
        return override
    try:
        from .config import get_self_learning

        return bool(get_self_learning()["enable"])
    except Exception:  # pragma: no cover - config failure disables learning
        return False


def settings() -> dict[str, bool]:
    """Return retained local-learning settings, failing closed."""
    try:
        from .config import get_self_learning

        return get_self_learning()
    except Exception:  # pragma: no cover - config failure disables learning
        return {
            "enable": False,
            "allow_provider_egress": False,
            "distill_local": False,
        }


def provider_egress_enabled() -> bool:
    """Whether learning may make extra task/result-bearing model calls."""
    return settings().get("allow_provider_egress") is True


__all__ = ["enabled", "settings", "provider_egress_enabled"]
