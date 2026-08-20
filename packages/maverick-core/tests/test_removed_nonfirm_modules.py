"""The firm build must not regrow removed consumer or generic product surfaces."""
from __future__ import annotations

import importlib.util

import pytest

REMOVED_MODULES = (
    "maverick.achievements",
    "maverick.annotation_export",
    "maverick.audit.forwarder",
    "maverick.automation_events",
    "maverick.agent_autonomy",
    "maverick.browser_auth_vault",
    "maverick.browser_device",
    "maverick.connector_previews",
    "maverick.compaction.streaming",
    "maverick.computer_calibration",
    "maverick.controls",
    "maverick.conversational_supervisor",
    "maverick.coding_mode",
    "maverick.dom_diff",
    "maverick.energy_accounting",
    "maverick.earned_autonomy",
    "maverick.edit_format",
    "maverick.factory_learning",
    "maverick.fleet",
    "maverick.flywheel",
    "maverick.flow.analytics",
    "maverick.flow.approvals",
    "maverick.form_store",
    "maverick.governed_repl",
    "maverick.html_to_app",
    "maverick.hindsight",
    "maverick.harness_refine",
    "maverick.jd_hiring",
    "maverick.live_captions",
    "maverick.live_mic",
    "maverick.meeting_listener",
    "maverick.migration_calculator",
    "maverick.model_proxy",
    "maverick.multi_monitor",
    "maverick.negative_knowledge",
    "maverick.operating_record",
    "maverick.outcomes",
    "maverick.onboarding_v2",
    "maverick.perceptual_hash",
    "maverick.provider_cache_analytics",
    "maverick.procedural_memory",
    "maverick.replay.video",
    "maverick.replay.trace",
    "maverick.relay_reference",
    "maverick.rectification",
    "maverick.savings",
    "maverick.session_tree",
    "maverick.starter_templates",
    "maverick.skill.stats",
    "maverick.skill.synthesis",
    "maverick.tax_backtest",
    "maverick.tax_constants",
    "maverick.tax_onboarding",
    "maverick.tax_prep",
    "maverick.terminal_charts",
    "maverick.tutorial_export",
    "maverick.ux_retrospective",
    "maverick.ux_store",
    "maverick.vision_click",
    "maverick.voice_macros",
    "maverick.voice_models",
    "maverick.voice_only",
    "maverick.voice_unlock",
    "maverick.webhooks",
    "maverick.safety.voice_safety",
    "maverick.safety.action_evidence",
    "maverick.safety.action_gate",
    "maverick.safety_bulletins",
    "maverick.screenshot_seal",
    "maverick.tools.voice",
    "maverick.tools.voice_command_grammar",
    "maverick.worker_review",
    "maverick.workforce_value",
    "maverick.departments",
    "maverick.tools.image_content_classifier",
)


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_nonfirm_runtime_module_is_physically_absent(module: str):
    assert importlib.util.find_spec(module) is None


def test_security_and_local_document_primitives_remain_importable():
    # OIDC/invite sessions and isolated document parsing are law-firm
    # primitives; this prune must not erase those narrower seams.
    assert importlib.util.find_spec("maverick.web_session") is not None
    assert importlib.util.find_spec("maverick.parser_isolation") is not None


def test_no_bundled_goal_template_or_shell_catalog_is_shipped():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1]
    assert not (package / "maverick" / "starter_templates").exists()
    pyproject = (package / "pyproject.toml").read_text(encoding="utf-8")
    templates_source = (package / "maverick" / "templates.py").read_text(
        encoding="utf-8",
    )
    assert "starter_templates" not in pyproject
    assert "_BUNDLED_CANDIDATES" not in templates_source


def test_retired_trace_env_cannot_persist_blackboard_content(monkeypatch, tmp_path):
    from maverick.blackboard import Blackboard

    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("MAVERICK_TRACE_DIR", str(trace_dir))
    board = Blackboard()
    board.post("attorney-agent", "finding", "privileged client strategy")

    assert not hasattr(board, "attach_trace")
    assert not trace_dir.exists()
