"""Regression tests for bug-hunt wave-3 fixes."""
from __future__ import annotations


class TestPersonaStyleCasing:
    def test_style_case_insensitive(self, monkeypatch):
        from maverick import persona
        # Pick a real style key, upper-cased, and confirm it still applies.
        key = next(iter(persona.STYLES))
        monkeypatch.setattr(
            persona, "load_persona",
            lambda: {"name": "", "style": key.upper(), "addendum": ""},
        )
        rendered = persona.render_persona_prompt()
        assert persona.STYLES[key] in rendered


class TestTemplateBudgetParse:
    def test_malformed_budget_does_not_crash(self):
        from maverick.templates import _parse_frontmatter
        meta = _parse_frontmatter("budget_dollars: 1.2.3\n")
        # The bad value must not raise; it is kept as a string, not float().
        assert meta.get("budget_dollars") == "1.2.3"

    def test_valid_budget_still_coerced(self):
        from maverick.templates import _parse_frontmatter
        meta = _parse_frontmatter("budget_dollars: 2.5\n")
        assert meta.get("budget_dollars") == 2.5




class TestReplayExportSanitize:
    def test_pii_and_secrets_redacted(self):
        from maverick.replay.export import _sanitize
        text = 'contact john@example.com key=sk-ant-abcdefghij0123456789XYZ'
        out = _sanitize(text)
        assert "john@example.com" not in out
        assert "sk-ant-abcdefghij0123456789XYZ" not in out
