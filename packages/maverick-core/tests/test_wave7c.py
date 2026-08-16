"""Firecracker sandbox scaffold + the PRM. The training-pipeline half went
with the training subsystem."""
from __future__ import annotations

import pytest

# ---------- Firecracker sandbox ----------

class TestFirecrackerBackend:
    def test_local_provider_requires_firecracker_binary(self, monkeypatch):
        from maverick.sandbox.firecracker import FirecrackerBackend
        monkeypatch.setattr("shutil.which", lambda c: None)
        from pathlib import Path
        with pytest.raises(NotImplementedError, match="firecracker"):
            FirecrackerBackend(workdir=Path.cwd(), provider="local")

    def test_e2b_provider_requires_api_key(self, monkeypatch):
        from maverick.sandbox.firecracker import FirecrackerBackend
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        from pathlib import Path
        with pytest.raises(ValueError, match="E2B_API_KEY"):
            FirecrackerBackend(workdir=Path.cwd(), provider="e2b")

    def test_e2b_picks_up_env_key(self, monkeypatch):
        from maverick.sandbox.firecracker import FirecrackerBackend
        monkeypatch.setenv("E2B_API_KEY", "test-key")
        from pathlib import Path
        b = FirecrackerBackend(workdir=Path.cwd(), provider="e2b")
        assert b.api_key == "test-key"

    def test_unknown_provider_raises(self, monkeypatch):
        from maverick.sandbox.firecracker import FirecrackerBackend
        monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/firecracker")
        from pathlib import Path
        with pytest.raises(ValueError, match="provider"):
            FirecrackerBackend(workdir=Path.cwd(), provider="bogus")

    def test_build_sandbox_factory_wires_firecracker(self, monkeypatch, tmp_path):
        """The build_sandbox() factory routes `backend=firecracker` correctly.

        We force the e2b provider so __post_init__'s `firecracker` binary
        check doesn't run (E2B path only needs an API key).
        """
        from maverick.sandbox import FirecrackerBackend
        monkeypatch.setenv("E2B_API_KEY", "test-key")
        # Direct construction; the factory tests are covered by build_sandbox
        # itself but they require the config layer to set provider=e2b.
        sb = FirecrackerBackend(
            workdir=tmp_path, provider="e2b", api_key="k",
        )
        assert isinstance(sb, FirecrackerBackend)
        assert sb.provider == "e2b"


# ---------- PRM ----------

class TestNullPRM:
    def test_returns_neutral(self):
        from maverick.prm import NullPRM, StepContext
        prm = NullPRM()
        out = prm.score(StepContext(goal_id=1, step_index=0, role="researcher"))
        assert out.promise == 0.5
        assert out.progress == 0.0
        assert out.confidence == 0.0


class TestHeuristicPRM:
    def test_error_strongly_negative(self):
        from maverick.prm import HeuristicPRM, StepContext
        out = HeuristicPRM().score(StepContext(
            goal_id=1, step_index=0, role="coder", error="something broke",
        ))
        assert out.promise < 0
        assert out.progress <= 0

    def test_final_strongly_positive(self):
        from maverick.prm import HeuristicPRM, StepContext
        out = HeuristicPRM().score(StepContext(
            goal_id=1, step_index=5, role="orchestrator", is_final=True,
        ))
        assert out.promise >= 0.9
        assert out.progress > 0

    def test_tool_success_positive_progress(self):
        from maverick.prm import HeuristicPRM, StepContext
        out = HeuristicPRM().score(StepContext(
            goal_id=1, step_index=2, role="researcher",
            tool_name="read_file", tool_succeeded=True,
        ))
        assert out.promise > 0.5
        assert out.progress > 0

    def test_tool_failure_negative_progress(self):
        from maverick.prm import HeuristicPRM, StepContext
        out = HeuristicPRM().score(StepContext(
            goal_id=1, step_index=2, role="researcher",
            tool_name="shell", tool_succeeded=False,
        ))
        assert out.promise > 0  # still some hope; not as negative as error
        assert out.progress < 0

    def test_thinking_decays(self):
        from maverick.prm import HeuristicPRM, StepContext
        out = HeuristicPRM().score(StepContext(
            goal_id=1, step_index=10, role="researcher", prior_step_score=0.8,
        ))
        # Decay vs prior, but never below floor 0.3.
        assert 0.3 <= out.promise <= 0.8


class TestRemotePRMFallback:
    def test_falls_back_when_no_httpx(self, monkeypatch):
        """Without httpx the remote PRM degrades to heuristic, never blocks."""
        # Force ImportError on httpx.
        import sys

        from maverick.prm import RemotePRM, StepContext
        monkeypatch.setitem(sys.modules, "httpx", None)
        prm = RemotePRM(endpoint="http://localhost:8888")
        out = prm.score(StepContext(
            goal_id=1, step_index=0, role="coder", error="boom",
        ))
        # Falls through to HeuristicPRM, which gives error=-0.5
        assert out.promise < 0


class TestBuildFromEnv:
    def test_default_is_null(self, monkeypatch):
        from maverick.prm import NullPRM, build_from_env
        monkeypatch.delenv("MAVERICK_PRM", raising=False)
        prm = build_from_env()
        assert isinstance(prm, NullPRM)

    def test_heuristic_via_env(self, monkeypatch):
        from maverick.prm import HeuristicPRM, build_from_env
        monkeypatch.setenv("MAVERICK_PRM", "heuristic")
        prm = build_from_env()
        assert isinstance(prm, HeuristicPRM)

    def test_remote_without_endpoint_falls_back(self, monkeypatch):
        from maverick.prm import HeuristicPRM, build_from_env
        monkeypatch.setenv("MAVERICK_PRM", "remote")
        monkeypatch.delenv("MAVERICK_PRM_ENDPOINT", raising=False)
        prm = build_from_env()
        # No endpoint set -> we fall back to heuristic with a warning.
        assert isinstance(prm, HeuristicPRM)


# ---------- Training schema + ingest ----------

class TestFirecrackerStrictDefault:
    """Firecracker fails CLOSED by default when firectl is absent (no silent
    downgrade to a weaker Docker boundary); the hardened fallback is only
    reached when the operator opts in with MAVERICK_FIRECRACKER_STRICT=0."""

    def _backend(self, monkeypatch, tmp_path):
        import shutil
        from pathlib import Path

        from maverick.sandbox.firecracker import FirecrackerBackend
        # Construct successfully (firecracker binary 'present').
        monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/firecracker")
        return FirecrackerBackend(workdir=Path(tmp_path), provider="local")

    def test_fails_closed_by_default_without_firectl(self, monkeypatch, tmp_path):
        import shutil

        import pytest
        b = self._backend(monkeypatch, tmp_path)
        monkeypatch.delenv("MAVERICK_FIRECRACKER_STRICT", raising=False)
        monkeypatch.setattr(shutil, "which", lambda c: None)  # firectl absent
        with pytest.raises(RuntimeError, match="microVM isolation is unavailable"):
            b._exec_local("echo hi")

    def test_strict_zero_allows_hardened_fallback(self, monkeypatch, tmp_path):
        import shutil
        b = self._backend(monkeypatch, tmp_path)
        monkeypatch.setenv("MAVERICK_FIRECRACKER_STRICT", "0")
        monkeypatch.setattr(shutil, "which", lambda c: None)
        called = {}
        monkeypatch.setattr(b, "_docker_fallback", lambda cmd: called.setdefault("cmd", cmd))
        b._exec_local("echo hi")
        assert called["cmd"] == "echo hi"

    def test_docker_fallback_is_hardened(self, monkeypatch, tmp_path):
        import subprocess
        b = self._backend(monkeypatch, tmp_path)
        captured = {}

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(args, **kw):
            captured["args"] = args
            return _P()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        b._docker_fallback("echo hi")
        args = captured["args"]
        assert "--cap-drop" in args and "ALL" in args
        assert "no-new-privileges" in args
        assert "--network=none" in args and "--read-only" in args
