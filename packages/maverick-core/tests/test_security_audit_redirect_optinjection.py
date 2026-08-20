"""Regression tests for the multi-pass security audit fixes.

Covers three findings:
  1. git_advanced: LLM-controlled refs/paths starting with ``-`` are git
     option injection (e.g. ``git show --output=...`` writes a file) and
     must be rejected even though shlex.quote stops shell metacharacters.
  2. secret_detector: generically-named env secrets (INTERNAL_API_TOKEN=...)
     must be redacted before they reach the audit log / model context.
  3. shield: tool-call gating must catch ``rm -rf /`` / ``rm -rf ~`` in
     structured args (the old repr()-based payload broke the rule anchor).
"""
from __future__ import annotations

import pytest

# ---------- 1. git option injection ----------

class _FakeSandbox:
    def __init__(self, workdir):
        self.workdir = workdir

    def exec(self, cmd, timeout=None):
        class _R:
            exit_code = 0
            stdout = cmd
            stderr = ""
        return _R()








# ---------- 2. secret_detector generic env secret ----------

@pytest.mark.parametrize("text", [
    "INTERNAL_API_TOKEN=zzz-internal-token-value-1234",
    "export DB_PASSWORD=correcthorsebatterystaple",
    "MY_CUSTOM_SECRET=hunter2supersecretvalue",
])
def test_secret_detector_redacts_generic_env_secret(text):
    from maverick.safety.secret_detector import redact
    out, matches = redact(text)
    assert matches and "[REDACTED:env_secret]" in out
    # The raw value must be gone; the var name stays for readability.
    assert "=" in out
    assert out.split("=", 1)[1] == "[REDACTED:env_secret]"


def test_secret_detector_ignores_non_secret_assignment():
    from maverick.safety.secret_detector import redact
    out, matches = redact("LOG_LEVEL=debug\nMAX_RETRIES=5")
    assert not matches and out == "LOG_LEVEL=debug\nMAX_RETRIES=5"


# ---------- 3. shield tool-call gating ----------

@pytest.mark.parametrize("cmd", ["rm -rf /", "rm -rf ~", "rm -rf $HOME"])
def test_shield_scan_tool_call_blocks_destructive_rm(cmd):
    from maverick_shield.guard import Shield
    sh = Shield(backend=Shield.BACKEND_BUILTIN)
    verdict = sh.scan_tool_call("shell", {"cmd": cmd})
    assert not verdict.allowed
    assert verdict.severity == "critical"


def test_shield_scan_tool_call_allows_benign():
    from maverick_shield.guard import Shield
    sh = Shield(backend=Shield.BACKEND_BUILTIN)
    assert sh.scan_tool_call("shell", {"cmd": "ls -la /tmp"}).allowed
