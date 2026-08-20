"""Characterization tests for the first extracted PromptBuilder seam.

``agent.select_base_template`` was lifted out of ``Agent._build_system`` as the
first side-effect-free collaborator in the god-module decomposition. These pin
its branch behavior so the extraction remains explicit for orchestrators and
workers after the interactive coding-agent surface was retired.
"""
from __future__ import annotations

from maverick.agent import (
    ORCHESTRATOR_SYSTEM_TEMPLATE,
    WORKER_SYSTEM_TEMPLATE,
    apply_global_overlays,
    apply_role_overlays,
    apply_skill_overlays,
    select_base_template,
)


class _FakeSkill:
    def __init__(self, name: str) -> None:
        self.name = name


def test_orchestrator_template():
    out = select_base_template(role="orchestrator", depth=0, max_depth=5)
    assert out == ORCHESTRATOR_SYSTEM_TEMPLATE.format(max_depth=5)


def test_worker_template_for_other_roles():
    out = select_base_template(role="researcher", depth=2, max_depth=5)
    assert out == WORKER_SYSTEM_TEMPLATE.format(
        role="researcher", depth=2, max_depth=5)

# ---- apply_global_overlays (second PromptBuilder collaborator) ----

def test_overlays_append_persona_and_style_in_order(monkeypatch):
    monkeypatch.setattr("maverick.persona.render_persona_prompt", lambda: "P")
    monkeypatch.setattr("maverick.styles.render_active_style_prompt", lambda: "S")
    # Global procedural memory is intentionally absent: only operator-curated
    # persona and style overlays may enter every matter prompt.
    assert apply_global_overlays("BASE") == "BASEPS"


def test_overlays_skip_empty_and_disabled(monkeypatch):
    monkeypatch.setattr("maverick.persona.render_persona_prompt", lambda: "")
    monkeypatch.setattr("maverick.styles.render_active_style_prompt", lambda: "")
    assert apply_global_overlays("BASE") == "BASE"


def test_overlays_fail_open_on_error(monkeypatch):
    def boom():
        raise RuntimeError("overlay source down")
    monkeypatch.setattr("maverick.persona.render_persona_prompt", boom)
    monkeypatch.setattr("maverick.styles.render_active_style_prompt", lambda: "S")
    # Persona raises -> that overlay is skipped; style still applies; no raise.
    assert apply_global_overlays("BASE") == "BASES"


# ---- apply_role_overlays (third PromptBuilder collaborator) ----

def test_role_overlays_append_addendum_then_domain(monkeypatch):
    monkeypatch.setattr("maverick.role_edit.role_addendum", lambda role: "ADD")
    out = apply_role_overlays("BASE", role="coder", domain_persona="DOM")
    # role addendum first, then domain persona, each blank-line separated.
    assert out == "BASE\n\nADD\n\nDOM"


def test_role_overlays_no_addendum_no_domain(monkeypatch):
    monkeypatch.setattr("maverick.role_edit.role_addendum", lambda role: "")
    assert apply_role_overlays("BASE", role="coder", domain_persona=None) == "BASE"


def test_role_overlays_addendum_fails_open(monkeypatch):
    def boom(role):
        raise RuntimeError("role editor down")
    monkeypatch.setattr("maverick.role_edit.role_addendum", boom)
    # Addendum raises -> skipped; domain persona still applies; no raise.
    out = apply_role_overlays("BASE", role="coder", domain_persona="DOM")
    assert out == "BASE\n\nDOM"


def test_role_overlays_addendum_keyed_on_role(monkeypatch):
    seen = {}
    def _addendum(role):
        seen["role"] = role
        return ""
    monkeypatch.setattr("maverick.role_edit.role_addendum", _addendum)
    apply_role_overlays("BASE", role="analyst", domain_persona=None)
    assert seen["role"] == "analyst"


# ---- apply_skill_overlays (fourth PromptBuilder collaborator) ----

def test_skill_overlays_appends_and_returns_skills(monkeypatch):
    sk = [_FakeSkill("a"), _FakeSkill("b")]
    monkeypatch.setattr("maverick.skills.available_skills", lambda: ["all"])
    monkeypatch.setattr("maverick.skills.relevant_skills", lambda brief, avail, max_n=3: sk)
    monkeypatch.setattr("maverick.skills.render_for_prompt", lambda skills: "RENDERED")
    base, skills = apply_skill_overlays("BASE", brief="do x", use_skills=True)
    assert base == "BASE\n\nRENDERED"
    assert skills == sk  # caller uses these to record stats + skills_used


def test_skill_overlays_disabled_is_noop(monkeypatch):
    # Skills off -> the store is never consulted, returns (base, []).
    monkeypatch.setattr(
        "maverick.skills.relevant_skills",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    assert apply_skill_overlays("BASE", brief="x", use_skills=False) == ("BASE", [])


def test_skill_overlays_no_relevant_skills(monkeypatch):
    monkeypatch.setattr("maverick.skills.available_skills", list)
    monkeypatch.setattr("maverick.skills.relevant_skills", lambda brief, avail, max_n=3: [])
    assert apply_skill_overlays("BASE", brief="x", use_skills=True) == ("BASE", [])


def test_skill_overlays_fail_open_on_missing_store(monkeypatch):
    def boom():
        raise FileNotFoundError("no skills dir")
    monkeypatch.setattr("maverick.skills.available_skills", boom)
    # FileNotFoundError/ImportError/ValueError are swallowed -> no-op.
    assert apply_skill_overlays("BASE", brief="x", use_skills=True) == ("BASE", [])
