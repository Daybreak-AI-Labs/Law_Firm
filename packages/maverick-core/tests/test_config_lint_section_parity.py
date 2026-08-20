"""config-lint and migrate must agree on which sections the runtime reads."""
from __future__ import annotations

from maverick.config_lint import KNOWN_SCHEMA, lint_config
from maverick.migrate import KNOWN_SECTIONS


def test_every_runtime_section_is_known_to_config_lint():
    missing = sorted(set(KNOWN_SECTIONS) - set(KNOWN_SCHEMA))
    assert not missing, f"config-lint would false-flag real sections: {missing}"


def test_registries_are_bidirectionally_consistent():
    # Neither tool may flag a section the other recognizes as real -- a2a
    # (read by a2a.py) and deployment (wizard-written) were known to
    # config-lint but flagged by migrate (round-6 finding).
    lint_only = sorted(set(KNOWN_SCHEMA) - set(KNOWN_SECTIONS))
    assert not lint_only, f"migrate would false-flag real sections: {lint_only}"


def test_sampling_of_governance_sections_lint_clean():
    cfg = {
        "enterprise": {"mode": "on"},
        "egress": {"deny": ["*"]},
        "governance": {"policy": "strict"},
        "encryption": {"at_rest": True},
        "telemetry": {"failure_modes": True},
    }
    unknown = [f for f in lint_config(cfg) if "unknown" in f.message.lower()]
    assert not unknown, [f.message for f in unknown]


def test_self_learning_lifecycle_sections_lint_clean():
    # The learning lifecycle the wizard offers -- all read by real load_config()
    # call sites -- must not be flagged "unknown". These shipped as unknown:
    # config-lint even suggested "self_harness -> did you mean self_learning?",
    # a *different* feature, which would silently disable self-harness.
    cfg = {s: {"enable": True} for s in (
        "self_harness", "self_improvement", "dreaming",
        "rehearsal", "memory_guard")}
    unknown = [f for f in lint_config(cfg) if "unknown" in f.message.lower()]
    assert not unknown, [f.message for f in unknown]


def test_genuinely_unknown_section_still_flagged():
    findings = lint_config({"totally_made_up_section": {"x": 1}})
    assert any("unknown" in f.message.lower() for f in findings)
