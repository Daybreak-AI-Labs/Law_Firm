"""Hand-edited config must apply (case/whitespace) and tolerate bad values.

profile/backend/provider etc. come from hand-edited TOML (or CLI flags) and
were compared against lowercase literals, so "Docker" / "Anthropic:" silently
misapplied; a non-numeric [sandbox] timeout crashed the kernel outright. These
tests pin normalization + defensive coercion at the highest-impact
chokepoints:

  - build_sandbox(): a mis-cased backend must NOT silently degrade to the
    unsandboxed local backend (a security downgrade), and an unrecognized
    backend must warn loudly instead of failing quiet.
  - llm._parse_spec(): a mis-cased / aliased provider must canonicalize so the
    case-sensitive API-key lookup resolves (not just client creation).
"""
from __future__ import annotations

import logging

import maverick.sandbox as sandbox_mod
import pytest
from maverick.llm import _parse_spec
from maverick.sandbox import LocalBackend, SandboxPolicyError, build_sandbox


class TestSandboxBackendCasing:
    # Patch Docker to a sentinel: DockerBackend verifies its runtime in
    # __init__ (and raises when absent), so we test the
    # selection logic in isolation -- and a sentinel return sharply proves the
    # branch taken (vs a silent fall-through to the real LocalBackend).
    def test_mixed_case_docker_resolves_to_docker_not_local(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod, "DockerBackend", lambda **kw: "DOCKER")
        assert build_sandbox(backend="Docker") == "DOCKER"

    def test_surrounding_whitespace_and_case_tolerated(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod, "DockerBackend", lambda **kw: "DOCKER")
        assert build_sandbox(backend="  DOCKER  ") == "DOCKER"

    def test_unknown_backend_fails_closed(self):
        with pytest.raises(SandboxPolicyError, match="dokcer"):
            build_sandbox(backend="dokcer")

    def test_explicit_local_does_not_warn_unrecognized(self, monkeypatch, caplog):
        monkeypatch.setenv("MAVERICK_SUPPRESS_SANDBOX_WARNING", "1")
        with caplog.at_level(logging.WARNING, logger="maverick.sandbox"):
            sb = build_sandbox(backend="Local")
        assert isinstance(sb, LocalBackend)
        assert "unsupported sandbox backend" not in caplog.text


class TestParseSpecProviderCasing:
    def test_mixed_case_provider_canonicalizes(self):
        assert _parse_spec("Anthropic:claude-opus-4-7") == ("anthropic", "claude-opus-4-7")

    def test_advertised_alias_resolves(self):
        # 'claude' and 'kimi' are documented aliases (providers._PROVIDER_ALIASES).
        assert _parse_spec("claude:some-model") == ("anthropic", "some-model")
        assert _parse_spec("kimi:k2") == ("moonshot", "k2")

    def test_bare_model_id_defaults_to_anthropic(self):
        assert _parse_spec("claude-opus-4-7") == ("anthropic", "claude-opus-4-7")

    def test_only_first_colon_splits_provider(self):
        # Model ids can contain colons; split(":", 1) must preserve them.
        assert _parse_spec("openai:org/model:v2") == ("openai", "org/model:v2")


class TestDiagnosticsSandboxCasing:
    # The retained health readout reads the same user-typed sandbox backend;
    # it must normalize like build_sandbox or a valid "Docker" config
    # misreports (skips the docker probe / unsupported row).
    def test_health_routes_mixed_case_docker_to_docker_probe(self, monkeypatch):
        from maverick import health
        rows: list[str] = []
        monkeypatch.setattr(health, "_row", lambda color, name, msg, **k: rows.append(msg))
        monkeypatch.setattr("shutil.which", lambda name: None)  # docker "absent"
        health._check_sandbox({"sandbox": {"backend": "Docker"}})
        text = " ".join(rows)
        assert "docker not on PATH" in text          # took the docker branch
        assert "supported in v0.1" not in text        # not the unsupported catch-all

class TestSandboxTimeoutCoercion:
    # [sandbox] timeout is hand-editable; a bad value must fall back to the
    # default, not crash build_sandbox() (and with it the whole agent startup).
    def test_non_numeric_timeout_falls_back_to_default(self, monkeypatch):
        from maverick import config
        monkeypatch.setattr(config, "get_sandbox", lambda: {"backend": "local", "timeout": "fast"})
        sb = build_sandbox()  # must not raise
        assert isinstance(sb, LocalBackend)
        assert sb.timeout == 60.0

    def test_non_positive_timeout_falls_back_to_default(self, monkeypatch):
        from maverick import config
        for bad in (-5, 0):
            monkeypatch.setattr(config, "get_sandbox", lambda bad=bad: {"backend": "local", "timeout": bad})
            assert build_sandbox().timeout == 60.0

    def test_valid_timeout_is_honored(self, monkeypatch):
        from maverick import config
        monkeypatch.setattr(config, "get_sandbox", lambda: {"backend": "local", "timeout": 30})
        assert build_sandbox().timeout == 30.0
