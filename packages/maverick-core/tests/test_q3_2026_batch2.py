"""Provider-health dispatch accounting and local sandbox isolation."""
from __future__ import annotations

import sys
import time

# ---------- provider health ----------

def test_provider_health_records_calls():
    from maverick.provider_health import ProviderHealth
    ph = ProviderHealth()
    ph.record("openai", "gpt-5", latency_ms=120, dollars=0.001)
    ph.record("openai", "gpt-5", latency_ms=200, dollars=0.002)
    ph.record("openai", "gpt-5", latency_ms=180, dollars=0.001, error=True)

    snap = ph.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-5"
    assert row["calls"] == 3
    assert row["errors"] == 1
    assert abs(row["error_rate"] - (1 / 3)) < 1e-6
    assert row["p50_ms"] == 180.0  # median of [120, 200, 180]
    assert row["total_dollars"] > 0
    assert row["last_seen"] > 0


def test_provider_health_snapshot_sorts_by_calls():
    from maverick.provider_health import ProviderHealth
    ph = ProviderHealth()
    for _ in range(2):
        ph.record("a", "m1", latency_ms=10)
    for _ in range(5):
        ph.record("b", "m2", latency_ms=20)
    snap = ph.snapshot()
    assert [r["provider"] for r in snap] == ["b", "a"]


def test_provider_health_reset_clears():
    from maverick.provider_health import ProviderHealth
    ph = ProviderHealth()
    ph.record("x", "y", latency_ms=1)
    assert ph.snapshot()
    ph.reset()
    assert ph.snapshot() == []


def test_provider_health_singleton_is_shared():
    from maverick.provider_health import get
    a = get()
    b = get()
    assert a is b


# ---------- LLM call records provider health ----------

def test_llm_complete_records_provider_health(monkeypatch):
    from maverick.budget import Budget
    from maverick.llm import LLM
    from maverick.provider_health import get as _h

    _h().reset()

    class _FakeResp:
        text = "ok"
        thinking = None
        tool_calls = []
        stop_reason = "end_turn"
        cache_creation_tokens = 0
        cache_read_tokens = 0
        raw = None
        thinking_blocks = []
        thinking_signature = None

    class _FakeClient:
        def complete(self, **kwargs):
            time.sleep(0.005)
            return _FakeResp()

    llm = LLM(model="anthropic:claude-haiku-4-5-20251001",
              api_key="dummy")
    monkeypatch.setattr(llm, "_get_client", lambda provider: _FakeClient())

    budget = Budget(max_dollars=1.0)
    llm.complete(system="s", messages=[{"role": "user", "content": "hi"}],
                 budget=budget)
    snap = _h().snapshot()
    assert len(snap) == 1
    assert snap[0]["provider"] == "anthropic"
    assert snap[0]["calls"] == 1
    assert snap[0]["errors"] == 0
    assert snap[0]["p50_ms"] is not None


def test_llm_complete_records_error(monkeypatch):
    from maverick.llm import LLM
    from maverick.provider_health import get as _h

    _h().reset()

    class _Boom:
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    llm = LLM(model="anthropic:claude-haiku-4-5-20251001",
              api_key="dummy")
    monkeypatch.setattr(llm, "_get_client", lambda provider: _Boom())

    try:
        llm.complete(system="s", messages=[{"role": "user", "content": "x"}])
    except RuntimeError:
        pass
    snap = _h().snapshot()
    assert len(snap) == 1
    assert snap[0]["errors"] == 1


def test_local_backend_strips_gitlab_token(monkeypatch, tmp_path):
    from maverick.sandbox.local import LocalBackend
    monkeypatch.setenv("GITLAB_TOKEN", "glpat_test_secret")
    sb = LocalBackend(workdir=tmp_path)
    out = sb.exec(
        f'"{sys.executable}" -c "import os; '
        "print(os.environ.get('GITLAB_TOKEN', 'missing'))\""
    )
    assert out.exit_code == 0
    assert out.stdout.strip() == "missing"
