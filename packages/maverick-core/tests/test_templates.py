"""Template tests."""
from __future__ import annotations

import pytest
from maverick.templates import (
    Template,
    _substitute,
    list_templates,
    load_template,
)

TEMPLATE_BODY = """---
title: Research {{ topic }}
budget_dollars: 2.5
budget_wall_seconds: 1200
params:
  - topic
  - depth
---
Research {{ topic }} across {{ depth }} dimensions. Write to report.md.
"""


def test_parse_with_frontmatter():
    t = Template.parse(TEMPLATE_BODY, "research")
    assert t.title == "Research {{ topic }}"
    assert t.budget_dollars == 2.5
    assert t.budget_wall_seconds == 1200
    assert "topic" in t.params
    assert "depth" in t.params


def test_parse_without_frontmatter():
    t = Template.parse("just a body", "plain")
    assert t.title == "plain"
    assert t.body == "just a body"
    assert t.budget_dollars == 5.0


def test_parse_non_numeric_budget_raises_clear_error():
    # A user-authored template with a non-numeric budget must raise a clear,
    # catchable message -- not a raw float() ValueError traceback.
    import pytest
    with pytest.raises(ValueError, match=r"budget_dollars.*must be a number.*not-a-number"):
        Template.parse("---\nbudget_dollars: not-a-number\n---\nbody", "bad")


def test_render_substitutes_variables():
    t = Template.parse(TEMPLATE_BODY, "research")
    title, body = t.render(topic="AI agents", depth="4")
    assert title == "Research AI agents"
    assert "AI agents across 4 dimensions" in body


def test_render_missing_required_param():
    t = Template.parse(TEMPLATE_BODY, "research")
    with pytest.raises(ValueError, match="missing required params"):
        t.render(topic="x")  # forgot 'depth'


def test_render_extra_params_ignored():
    t = Template.parse(TEMPLATE_BODY, "research")
    title, body = t.render(topic="x", depth="y", unused="z")
    assert title == "Research x"


def test_substitute_leaves_unknown_vars_alone():
    out = _substitute("hello {{ name }}, {{ missing }}", {"name": "world"})
    assert "hello world" in out
    assert "{{ missing }}" in out


def test_load_template_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid template name"):
        load_template("../secret")


def test_load_template_rejects_absolute_path():
    with pytest.raises(ValueError, match="invalid template name"):
        load_template("/tmp/secret")


def test_no_bundled_template_catalog(tmp_path, monkeypatch):
    import maverick.templates as tpl_mod

    monkeypatch.setattr(tpl_mod, "USER_TEMPLATES", tmp_path)
    assert list_templates() == []


def test_only_operator_authored_local_templates_are_loaded(tmp_path, monkeypatch):
    import maverick.templates as tpl_mod

    monkeypatch.setattr(tpl_mod, "USER_TEMPLATES", tmp_path)
    (tmp_path / "nda-review.md").write_text(
        "---\ntitle: NDA review\n---\nDraft a deviation memo for attorney review.",
        encoding="utf-8",
    )
    assert list_templates() == ["nda-review"]
    template = load_template("nda-review")
    assert template.body == "Draft a deviation memo for attorney review."


def test_user_template_path_resolves_current_tenant_at_call_time(tmp_path, monkeypatch):
    """One long-lived process must not pin tenant A's template dir at import."""
    import maverick.templates as tpl_mod
    from maverick.paths import reset_tenant, set_tenant

    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    name = "tenant-isolation-probe-7391"

    token = set_tenant("acme")
    try:
        tpl_mod.save_user_template(name, title="Acme", body="acme-only")
        assert tpl_mod.load_template(name).body == "acme-only"
    finally:
        reset_tenant(token)

    token = set_tenant("globex")
    try:
        with pytest.raises(FileNotFoundError):
            tpl_mod.load_template(name)
        tpl_mod.save_user_template(name, title="Globex", body="globex-only")
        assert tpl_mod.load_template(name).body == "globex-only"
    finally:
        reset_tenant(token)

    token = set_tenant("acme")
    try:
        assert tpl_mod.load_template(name).body == "acme-only"
    finally:
        reset_tenant(token)
