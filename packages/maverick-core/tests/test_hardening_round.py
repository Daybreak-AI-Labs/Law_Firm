"""Hardening regressions (review round): workflow loop-safety, circuit
breaker reconfig, scheduler impossible-schedule bound, llm_cache cap,
and crash-on-success format fixes across the SaaS tools."""
from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock

import pytest


def _fake_httpx(monkeypatch, **methods):
    mod = types.ModuleType("httpx")
    for n, v in methods.items():
        setattr(mod, n, v)
    monkeypatch.setitem(sys.modules, "httpx", mod)
    return mod


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.text = str(body)
    return r


# ---------- crash-on-success: null fields in valid 2xx bodies ----------













# ---------- workflow: callable from inside a running event loop ----------

def test_workflow_runs_inside_running_loop():
    import asyncio

    from maverick.tools import Tool, ToolRegistry
    from maverick.workflow import Step, Workflow

    reg = ToolRegistry()
    reg.register(Tool(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        fn=lambda args: "ok",
    ))
    wf = Workflow(steps=[Step("a", "echo", {})])

    async def _driver():
        # Calling the SYNC wf.run() from inside a running loop must not
        # raise "asyncio.run() cannot be called from a running event loop".
        return wf.run(reg)

    result = asyncio.run(_driver())
    assert not result.failed
    assert result.steps[0].output == "ok"


def test_workflow_still_runs_without_loop():
    from maverick.tools import Tool, ToolRegistry
    from maverick.workflow import Step, Workflow
    reg = ToolRegistry()
    reg.register(Tool(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        fn=lambda args: "sync-ok",
    ))
    res = Workflow(steps=[Step("a", "echo", {})]).run(reg)
    assert res.steps[0].output == "sync-ok"


# ---------- circuit breaker: honor explicit reconfig on existing key ----

def test_circuit_breaker_reconfigures_existing_key():
    from maverick.circuit_breaker import get, reset_all
    reset_all()
    first = get("svc")  # defaults: threshold 5, cooldown 30
    assert first.failure_threshold == 5
    again = get("svc", failure_threshold=2, cooldown_seconds=120)
    assert again is first  # same instance
    assert again.failure_threshold == 2  # override applied, not ignored
    assert again.cooldown_seconds == 120
    reset_all()


def test_circuit_breaker_default_get_does_not_clobber():
    from maverick.circuit_breaker import get, reset_all
    reset_all()
    get("svc2", failure_threshold=2)
    # A later default get() must NOT reset the custom threshold back to 5.
    again = get("svc2")
    assert again.failure_threshold == 2
    reset_all()


# ---------- scheduler: impossible schedule is bounded + fast ----------

def test_scheduler_impossible_schedule_raises_fast():
    from maverick.scheduler import CronError, next_run
    # Feb 30 never exists; must raise CronError quickly (day-skip walk),
    # not hang on ~2M minute iterations.
    t0 = time.time()
    raised = False
    try:
        next_run("0 0 30 2 *")
    except CronError:
        raised = True
    elapsed = time.time() - t0
    assert raised
    assert elapsed < 2.0, f"took {elapsed:.2f}s — should day-skip, not minute-walk"


def test_scheduler_leap_day_still_resolves():
    import datetime as _dt

    from maverick.scheduler import next_run
    # Feb 29 IS valid on leap years; must resolve to 2028-02-29.
    base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc).timestamp()
    ts = next_run("0 0 29 2 *", after=base)
    got = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    assert got.month == 2 and got.day == 29


# ---------- llm_cache: row cap eviction ----------

def test_llm_cache_evicts_beyond_max_rows(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db", max_rows=3)
    # Insert 5 distinct keys; cap is 3.
    for i in range(5):
        cache.store(f"k{i}", provider="p", model="m", text=f"v{i}")
    s = cache.stats()
    assert s["entries"] <= 3, f"cap not enforced: {s['entries']} rows"


def test_llm_cache_eviction_is_lru_keeps_newest(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db", max_rows=2)
    cache.store("hot", provider="p", model="m", text="x")
    # hit_count is intentionally NOT the eviction key. A naive LFU policy
    # (ORDER BY hit_count DESC) evicted the just-inserted hit_count=0 row and
    # thrashed to ~0% hit rate under a stream of unique prompts, so eviction
    # is recency-based: keep the newest rows by created_at (see llm_cache.py).
    for _ in range(5):
        cache.lookup("hot")
    cache.store("a", provider="p", model="m", text="x")
    cache.store("b", provider="p", model="m", text="x")
    cache.store("c", provider="p", model="m", text="x")
    # Cap is 2; the two most-recently-stored keys survive, the oldest ("hot")
    # is evicted regardless of its hit_count.
    assert cache.lookup("c") is not None
    assert cache.lookup("b") is not None
    assert cache.lookup("hot") is None
    assert cache.stats()["entries"] == 2


def test_llm_cache_unbounded_when_max_rows_zero(tmp_path):
    from maverick.cache.llm import LLMCache
    cache = LLMCache(db_path=tmp_path / "c.db", max_rows=0)
    for i in range(20):
        cache.store(f"k{i}", provider="p", model="m", text="x")
    assert cache.stats()["entries"] == 20


# ---------- cost_router: tolerates partial health snapshot ----------
def test_record_metric_unlabeled_gauge_no_crash(monkeypatch):
    import maverick.observability as obs

    calls = {"set": None}

    class _Gauge:
        def inc(self, _v):
            raise AssertionError("gauge must not use inc() for absolute values")

        def set(self, v):
            calls["set"] = v

        def labels(self, **kw):
            raise AssertionError("must not call .labels() with no labels")

    monkeypatch.setattr(obs, "_metrics", {"budget_dollars": _Gauge()})
    monkeypatch.setattr(obs, "_initialize", lambda: None)
    obs.record_metric("budget_dollars", 1.23)  # no labels
    assert calls["set"] == 1.23


def test_record_metric_labeled_counter(monkeypatch):
    import maverick.observability as obs

    seen = {"labels": None, "inc": None}

    class _Child:
        def inc(self, v):
            seen["inc"] = v

    class _Counter:
        def labels(self, **kw):
            seen["labels"] = kw
            return _Child()

    monkeypatch.setattr(obs, "_metrics", {"llm_calls": _Counter()})
    monkeypatch.setattr(obs, "_initialize", lambda: None)
    obs.record_metric("llm_calls", 1.0,
                      labels={"provider": "anthropic", "model": "m"})
    assert seen["labels"] == {"provider": "anthropic", "model": "m"}
    assert seen["inc"] == 1.0


def test_record_metric_bounds_dynamic_label_series(monkeypatch):
    import maverick.observability as obs

    seen = []

    class _Child:
        @staticmethod
        def inc(_value):
            return None

    class _Counter:
        @staticmethod
        def labels(**labels):
            seen.append(labels)
            return _Child()

    monkeypatch.setattr(obs, "_metrics", {"llm_calls": _Counter()})
    monkeypatch.setattr(obs, "_initialize", lambda: None)
    monkeypatch.setattr(obs, "_MAX_LABEL_SERIES_PER_METRIC", 3)
    obs._metric_label_series.clear()
    try:
        for model in ("one", "two", "three", "four"):
            obs.record_metric(
                "llm_calls",
                labels={"provider": "custom", "model": model},
            )
    finally:
        obs._metric_label_series.clear()

    assert seen[:2] == [
        {"provider": "custom", "model": "one"},
        {"provider": "custom", "model": "two"},
    ]
    assert seen[2:] == [
        {"provider": obs._OVERFLOW_LABEL, "model": obs._OVERFLOW_LABEL},
        {"provider": obs._OVERFLOW_LABEL, "model": obs._OVERFLOW_LABEL},
    ]


def test_record_metric_hashes_control_character_labels(monkeypatch):
    import maverick.observability as obs

    captured = {}

    class _Child:
        @staticmethod
        def inc(_value):
            return None

    class _Counter:
        @staticmethod
        def labels(**labels):
            captured.update(labels)
            return _Child()

    monkeypatch.setattr(obs, "_metrics", {"tool_calls": _Counter()})
    monkeypatch.setattr(obs, "_initialize", lambda: None)
    obs._metric_label_series.clear()
    try:
        obs.record_metric(
            "tool_calls",
            labels={"tool": "unsafe\nlabel", "status": "ok"},
        )
    finally:
        obs._metric_label_series.clear()

    assert captured["tool"].startswith("redacted-")
    assert "\n" not in captured["tool"]
    assert captured["status"] == "ok"


def test_provider_latency_collector_reads_ms_snapshot_fields(monkeypatch):
    import maverick.observability as obs
    from maverick import provider_health

    captured = []

    class _Family:
        def __init__(self, name, _help, labels=None):
            self.name = name
            self.labels = labels or []
            self.metrics = []

        def add_metric(self, labels, value):
            self.metrics.append((labels, value))

    fake_client = types.ModuleType("prometheus_client")
    fake_client.REGISTRY = types.SimpleNamespace(
        register=lambda collector: captured.append(collector)
    )
    fake_core = types.ModuleType("prometheus_client.core")
    fake_core.GaugeMetricFamily = _Family
    monkeypatch.setitem(sys.modules, "prometheus_client", fake_client)
    monkeypatch.setitem(sys.modules, "prometheus_client.core", fake_core)
    monkeypatch.setattr(
        provider_health,
        "get",
        lambda: types.SimpleNamespace(
            snapshot=lambda: [{
                "provider": "anthropic",
                "model": "model",
                "error_rate": 0.25,
                "p50_ms": 75.0,
                "p95_ms": 100.0,
            }]
        ),
    )

    obs._register_computed_collector()
    families = {family.name: family for family in captured[0].collect()}

    assert families["maverick_provider_latency_ms"].metrics == [
        (["anthropic", "model", "0.5"], 75.0),
        (["anthropic", "model", "0.95"], 100.0),
    ]


def test_prometheus_exporter_defaults_to_loopback_bind(monkeypatch):
    import importlib
    import sys
    import types

    monkeypatch.setenv("MAVERICK_PROMETHEUS_PORT", "9999")
    monkeypatch.delenv("MAVERICK_PROMETHEUS_ADDR", raising=False)
    monkeypatch.delenv("MAVERICK_OTEL_EXPORTER", raising=False)

    calls = {}

    def _start_http_server(port, *args, **kwargs):
        calls["port"] = port
        calls["args"] = args
        calls["kwargs"] = kwargs

    fake_prom = types.SimpleNamespace(
        Counter=lambda *a, **k: object(),
        Gauge=lambda *a, **k: object(),
        Histogram=lambda *a, **k: object(),
        start_http_server=_start_http_server,
    )
    monkeypatch.setitem(sys.modules, "prometheus_client", fake_prom)

    import maverick.observability as obs
    obs = importlib.reload(obs)
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs, "_metrics", {})
    obs._initialize()

    assert calls["port"] == 9999
    assert calls["kwargs"].get("addr") == "127.0.0.1"


# ---------- chaos: concurrent roll() doesn't tear the RNG ----------

def test_chaos_roll_is_thread_safe_smoke():
    import threading

    from maverick.chaos import ChaosController, ChaosInjected, maybe_fail
    c = ChaosController()
    c.set(active=True, seed=1, sandbox_exec_fail_pct=50)
    errors: list[str] = []

    def _hammer():
        for _ in range(200):
            try:
                maybe_fail("sandbox_exec")
            except ChaosInjected:
                pass
            except Exception as e:  # a torn RNG read would land here
                errors.append(repr(e))

    threads = [threading.Thread(target=_hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.disable()
    assert not errors, f"concurrent roll() raised: {errors[:3]}"


# ---------- audit signing: tampered rows flagged, not crashed ----------

def _crypto_available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401
        return True
    except BaseException:
        return False


def test_audit_verify_flags_nonhex_sig_instead_of_crashing(tmp_path, monkeypatch):
    if not _crypto_available():
        return
    import json as _json

    from maverick.audit import signing
    monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
    path = tmp_path / "audit.ndjson"
    s = signing.AuditSigner(path)
    s.write({"event": "a"})
    s.write({"event": "b"})
    # Corrupt line 1's sig to a non-hex string.
    lines = path.read_text().splitlines()
    row = _json.loads(lines[0])
    row["sig"] = "zzzz-not-hex"
    lines[0] = _json.dumps(row)
    path.write_text("\n".join(lines) + "\n")
    # Must return a ChainBreak (not raise), and still check later rows.
    breaks = signing.verify_chain(path)
    assert breaks  # did not crash
    assert any(b.reason == "bad_signature" for b in breaks)


@pytest.mark.usefixtures("local_audit_key_custody")
def test_audit_verify_rejects_lone_pubkey(tmp_path, monkeypatch):
    """A .pub with no sibling .key (attacker-dropped) is not trusted."""
    if not _crypto_available():
        return
    import json as _json

    from maverick.audit import signing
    keydir = tmp_path / "keys"
    monkeypatch.setattr(signing, "KEY_DIR", keydir)
    path = tmp_path / "audit.ndjson"
    s = signing.AuditSigner(path)
    s.write({"event": "a"})
    # Remove the private key, leaving only the .pub (simulating a
    # verifier host that only has a dropped pubkey).
    for keyfile in keydir.glob("*.key"):
        keyfile.unlink()
    breaks = signing.verify_chain(path)
    assert any(b.reason == "no_pubkey" for b in breaks)
    _ = _json  # silence



def test_audit_verify_rejects_path_traversal_key_id(tmp_path, monkeypatch):
    if not _crypto_available():
        return
    import json as _json

    from maverick.audit import signing
    monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
    path = tmp_path / "audit.ndjson"
    s = signing.AuditSigner(path)
    s.write({"event": "a"})

    lines = path.read_text().splitlines()
    row = _json.loads(lines[0])
    row["key_id"] = "../../tmp/evil"
    lines[0] = _json.dumps(row)
    path.write_text("\n".join(lines) + "\n")

    breaks = signing.verify_chain(path)
    assert any(b.reason == "no_pubkey" for b in breaks)


# ---------- hackernews: null points on comment hits ----------



# ---------- calendar find_slot: latest_hour=23 must not crash ----------



# ---------- compute fallback: power-tower CPU/memory DoS ----------



# ---------- replay_export: non-numeric goal_id skips, not crashes ----

def test_replay_export_skips_bad_goal_id(tmp_path, monkeypatch):
    import json as _json

    import maverick.replay.export as rex
    audit = tmp_path / "audit"
    audit.mkdir()
    f = audit / "2026-05-28.ndjson"
    rows = [
        {"goal_id": 7, "kind": "goal_start"},
        {"goal_id": "not-a-number", "kind": "junk"},  # must not abort
        {"goal_id": 7, "kind": "goal_end"},
    ]
    f.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(rex, "_AUDIT_DIR", audit)
    out_file = tmp_path / "r.json"
    n = rex.export_json(7, out_file)
    assert n == 2  # both goal-7 rows survived; the bad row was skipped


# ---------- retention: only the known tables are purgeable ----------

def test_retention_rejects_unknown_table(tmp_path):
    import sqlite3

    from maverick.audit.retention import _purge_table_by_time
    db = tmp_path / "w.db"
    sqlite3.connect(str(db)).close()
    try:
        _purge_table_by_time(db, "goals; DROP TABLE goals", "x", 0.0, dry_run=True)
    except ValueError as e:
        assert "unknown table/column" in str(e)
        return
    raise AssertionError("expected ValueError on non-whitelisted table")


def test_retention_allows_known_table(tmp_path):
    import sqlite3

    from maverick.audit.retention import _purge_table_by_time
    db = tmp_path / "w.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE episodes (id INTEGER, ended_at REAL)")
    c.execute("INSERT INTO episodes VALUES (1, 100.0)")
    c.commit()
    c.close()
    removed = _purge_table_by_time(db, "episodes", "ended_at", 200.0, dry_run=True)
    assert removed == 1  # dry run counts the old row, doesn't delete


# ---------- verifier: fail CLOSED on LLM error ----------

def test_verifier_fails_closed_on_llm_error():
    import asyncio

    from maverick.budget import Budget
    from maverick.verifier import verify_proposal

    class _Boom:
        async def complete_async(self, **kw):
            raise RuntimeError("provider down")

    v = asyncio.run(verify_proposal("brief", "some proposal", _Boom(), Budget()))
    # Contract: any failure -> reject (NOT the old accepts=True fail-open).
    assert v.accepts is False


def test_verifier_propagates_budget_exceeded():
    import asyncio

    from maverick.budget import Budget, BudgetExceeded
    from maverick.verifier import verify_proposal

    class _OverBudget:
        async def complete_async(self, **kw):
            raise BudgetExceeded("$5 > $5")

    try:
        asyncio.run(verify_proposal("brief", "p", _OverBudget(), Budget()))
    except BudgetExceeded:
        return  # budget is a control signal, must propagate
    raise AssertionError("expected BudgetExceeded to propagate")
