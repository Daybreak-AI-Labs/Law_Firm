"""Retained sandbox timeout and provider-request regressions."""
from __future__ import annotations

import sys


class TestSandboxExecTimeout:
    def test_local_backend_accepts_timeout_kwarg(self, tmp_path):
        from maverick.sandbox import LocalBackend

        sb = LocalBackend(workdir=tmp_path, timeout=1.0)
        result = sb.exec("echo hello", timeout=10.0)
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_local_backend_timeout_kwarg_overrides_self_timeout(self, tmp_path):
        from maverick.sandbox import LocalBackend

        sb = LocalBackend(workdir=tmp_path, timeout=0.1)
        python = sys.executable.replace("\\", "/")
        result = sb.exec(
            f'"{python}" -c "import time; time.sleep(0.5); print(\'done\')"',
            timeout=5.0,
        )
        assert result.exit_code == 0
        assert "done" in result.stdout


class TestTemperatureWiredThrough:
    def test_build_request_includes_temperature_when_env_set(self, monkeypatch):
        from maverick.providers.anthropic_provider import AnthropicClient

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setenv("MAVERICK_TEMPERATURE", "0.85")
        client = AnthropicClient()
        kwargs = client._build_request(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=128,
            thinking_budget=None,
            model="claude-sonnet-4-6",
        )
        assert kwargs.get("temperature") == 0.85

    def test_build_request_no_temperature_when_unset(self, monkeypatch):
        from maverick.providers.anthropic_provider import AnthropicClient

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.delenv("MAVERICK_TEMPERATURE", raising=False)
        client = AnthropicClient()
        kwargs = client._build_request(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=128,
            thinking_budget=None,
            model="claude-sonnet-4-6",
        )
        assert "temperature" not in kwargs

    def test_build_request_no_temperature_when_thinking_enabled(self, monkeypatch):
        from maverick.providers.anthropic_provider import AnthropicClient

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setenv("MAVERICK_TEMPERATURE", "0.9")
        client = AnthropicClient()
        kwargs = client._build_request(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=128,
            thinking_budget=4000,
            model="claude-opus-4-7",
        )
        assert "temperature" not in kwargs
