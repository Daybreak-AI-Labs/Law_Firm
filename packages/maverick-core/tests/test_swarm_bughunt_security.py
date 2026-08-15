"""Regression tests for security-detector bugs found during bug-hunt.

Covered:
  - secrets.scrub leaked the tail of a quoted secret value with spaces.
  - secret_detector.redact only redacted a prefix of sk-proj- keys
    (body class excluded the `_`/`-` they actually contain).
  - unicode_filter ignored the implicit bidi marks LRM/RLM/ALM that are
    part of the Trojan Source surface it claims to cover.
"""
from __future__ import annotations


class TestSecretsScrubQuotedValues:
    def test_double_quoted_value_with_spaces_fully_redacted(self):
        from maverick.secrets import scrub
        out = scrub('API_SECRET="my secret value"')
        assert "secret value" not in out
        assert "my" not in out.split("=", 1)[1]
        assert "[REDACTED:env_secret]" in out

    def test_single_quoted_value_with_spaces_fully_redacted(self):
        from maverick.secrets import scrub
        out = scrub("export DB_PASSWORD='a b c d'")
        assert "a b c d" not in out
        assert "[REDACTED:env_secret]" in out

    def test_bare_value_still_redacted(self):
        from maverick.secrets import scrub
        out = scrub("FOO_TOKEN=abc123def456")
        assert "abc123def456" not in out


class TestSecretDetectorProjKey:
    def test_sk_proj_key_with_separators_fully_detected(self):
        from maverick.safety.secret_detector import redact
        key = "sk-proj-AbC_def-GhiJklMno_pqrStuv0123"
        text = f"OPENAI_API_KEY={key}"
        red, matches = redact(text)
        # The whole key must be gone, not just the prefix before the `_`.
        assert key not in red
        assert "def-GhiJklMno" not in red
        assert any(m.name == "openai_api_key" for m in matches)


class TestUnicodeImplicitBidiMarks:
    def test_implicit_marks_flagged(self):
        from maverick.safety.unicode_filter import has_dangerous_unicode
        for cp in (0x200E, 0x200F, 0x061C):
            assert has_dangerous_unicode("ok" + chr(cp) + "text"), hex(cp)

    def test_implicit_marks_stripped(self):
        from maverick.safety.unicode_filter import normalize
        res = normalize("a" + chr(0x200E) + "b" + chr(0x061C))
        assert res.cleaned == "ab"
        assert 0x200E in res.removed_codepoints
        assert 0x061C in res.removed_codepoints
