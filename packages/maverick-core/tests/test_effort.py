"""Per-role reasoning effort (output_config.effort) — the cost/latency lever."""
from __future__ import annotations

import pytest
from maverick import effort
from maverick.effort import effort_for_model, effort_for_role, effort_supported

OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"
SONNET45 = "claude-sonnet-4-5"
EXACT_OPUS = f"anthropic:{OPUS}"
EXACT_SONNET = f"anthropic:{SONNET}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("MAVERICK_EFFORT"):
            monkeypatch.delenv(k, raising=False)
    # Default: no config, feature off.
    monkeypatch.setattr(effort, "_config_effort", dict)


# ---- model gating ----------------------------------------------------------

def test_effort_supported_matrix():
    assert effort_supported(OPUS) is True
    assert effort_supported(SONNET) is True
    assert effort_supported(HAIKU) is False     # haiku 4.5 rejects effort
    assert effort_supported(SONNET45) is False  # sonnet 4.5 rejects effort


def test_exact_anthropic_pin_has_bare_model_effort_parity():
    assert effort_supported(EXACT_OPUS) == effort_supported(OPUS)
    assert effort_supported(EXACT_SONNET) == effort_supported(SONNET)
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert effort_for_model(level, EXACT_OPUS) == effort_for_model(level, OPUS)
        assert effort_for_model(level, EXACT_SONNET) == effort_for_model(level, SONNET)


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic:claude-future-9",
        "openai:claude-opus-4-8",
        "anthropic:",
        "anthropic:claude-opus-4-8:extra",
    ],
)
def test_unknown_or_malformed_exact_model_fails_closed(model_id):
    assert effort_supported(model_id) is False
    assert effort_for_model("high", model_id) is None


def test_off_by_default():
    # Nothing configured -> None (omit effort, API default applies).
    assert effort_for_role("orchestrator", OPUS) is None
    assert effort_for_role("researcher", SONNET) is None


# ---- enabling via config ---------------------------------------------------

def test_enabled_applies_builtin_profile(monkeypatch):
    monkeypatch.setattr(effort, "_config_effort", lambda: {"enabled": True})
    # Critical roles stay high; bulk roles drop.
    assert effort_for_role("orchestrator", OPUS) == "high"
    assert effort_for_role("coder", SONNET) == "high"
    assert effort_for_role("researcher", SONNET) == "medium"
    assert effort_for_role("reflector", OPUS) == "low"
    # Unknown role -> no default -> None.
    assert effort_for_role("mystery", OPUS) is None


def test_enabled_but_unsupported_model_returns_none(monkeypatch):
    monkeypatch.setattr(effort, "_config_effort", lambda: {"enabled": True})
    assert effort_for_role("orchestrator", HAIKU) is None
    assert effort_for_role("researcher", SONNET45) is None


# ---- precedence ------------------------------------------------------------

def test_per_role_env_wins(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT_ORCHESTRATOR", "low")
    monkeypatch.setenv("MAVERICK_EFFORT", "max")
    assert effort_for_role("orchestrator", OPUS) == "low"


def test_global_env_applies_to_all_roles(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT", "medium")
    assert effort_for_role("orchestrator", OPUS) == "medium"
    assert effort_for_role("coder", SONNET) == "medium"


def test_config_per_role_and_default(monkeypatch):
    monkeypatch.setattr(effort, "_config_effort",
                        lambda: {"orchestrator": "max", "default": "low"})
    assert effort_for_role("orchestrator", OPUS) == "max"
    assert effort_for_role("coder", OPUS) == "low"  # falls to default


# ---- clamping (never 400) --------------------------------------------------

def test_xhigh_clamped_off_opus_78(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT", "xhigh")
    assert effort_for_role("coder", OPUS) == "xhigh"      # opus 4.8 supports it
    assert effort_for_role("coder", SONNET) == "high"     # sonnet -> clamp down


def test_max_clamped_off_opus_tier(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT", "max")
    assert effort_for_role("coder", OPUS) == "max"        # opus 4.8 supports max
    assert effort_for_role("coder", SONNET) == "high"     # sonnet -> clamp down


def test_invalid_level_is_ignored(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT", "turbo")
    assert effort_for_role("orchestrator", OPUS) is None


def test_preselected_effort_clamps_for_actual_model():
    # Dispatch must re-clamp a caller-provided effort against the exact run
    # model's own ceiling.
    assert effort_for_model("xhigh", OPUS) == "xhigh"
    assert effort_for_model("xhigh", SONNET) == "high"
    assert effort_for_model("max", SONNET) == "high"
    assert effort_for_model("medium", HAIKU) is None


# ---- provider request shaping ----------------------------------------------

def test_effort_lands_in_anthropic_output_config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from maverick.providers.anthropic_provider import AnthropicClient
    p = AnthropicClient()
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    # Supported model -> effort threads into output_config.
    kw = p._build_request("sys", msgs, None, 4096, None, OPUS, "medium")
    assert kw["output_config"]["effort"] == "medium"
    # No effort -> no output_config key (unchanged request).
    assert p._build_request("sys", msgs, None, 4096, None, OPUS, None).get("output_config") is None
    # Unsupported model -> effort is dropped defensively (never 400).
    assert p._build_request("sys", msgs, None, 4096, None, HAIKU, "medium").get("output_config") is None
    # A preselected effort is clamped for the actual pinned model.
    kw = p._build_request("sys", msgs, None, 4096, None, SONNET, "xhigh")
    assert kw["output_config"]["effort"] == "high"


# --- Pack-authored effort tier (DomainProfile.effort) -----------------------

_M = "claude-opus-4-8"


def test_pack_effort_ignored_when_feature_off(monkeypatch):
    monkeypatch.setattr(effort, "_config_effort", dict)
    assert effort_for_role("finance_gl_close", _M, pack_default="high") is None


def test_pack_effort_applies_when_enabled(monkeypatch):
    monkeypatch.setattr(effort, "_config_effort", lambda: {"enabled": True})
    assert effort_for_role("finance_gl_close", _M, pack_default="high") == "high"


def test_pack_effort_beats_global_default(monkeypatch):
    # A pack's tier is more specific than the deployment-wide default.
    monkeypatch.setattr(effort, "_config_effort",
                        lambda: {"enabled": True, "default": "low"})
    assert effort_for_role("finance_gl_close", _M, pack_default="high") == "high"
    # ...but a pack with no tier still gets the global default.
    assert effort_for_role("km_doc_quality", _M) == "low"


def test_per_role_override_still_beats_pack_effort(monkeypatch):
    monkeypatch.setenv("MAVERICK_EFFORT_FINANCE_GL_CLOSE", "low")
    monkeypatch.setattr(effort, "_config_effort", lambda: {"enabled": True})
    assert effort_for_role("finance_gl_close", _M, pack_default="high") == "low"
