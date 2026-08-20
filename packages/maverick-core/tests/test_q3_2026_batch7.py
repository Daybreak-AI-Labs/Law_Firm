"""Q3 2026 tests for the vLLM provider and persistent job queue."""
from __future__ import annotations

import time

import pytest


def _openai_available() -> bool:
    try:
        import openai  # noqa: F401
        return True
    except ImportError:
        return False


_needs_openai = pytest.mark.skipif(
    not _openai_available(),
    reason="openai SDK extra not installed in this env",
)


# ---------- vLLM provider ----------

@_needs_openai
def test_vllm_provider_default_url(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    from maverick.providers.vllm_provider import VLLMClient
    c = VLLMClient()
    assert c.DEFAULT_MODEL == "vllm"
    assert c.base_url.endswith("/v1")


@_needs_openai
def test_vllm_provider_env_url(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu-box:8000")
    monkeypatch.setenv("VLLM_API_KEY", "sekret")
    from maverick.providers.vllm_provider import VLLMClient
    c = VLLMClient()
    assert c.base_url == "http://gpu-box:8000/v1"


def test_vllm_registered_in_provider_registry():
    """Registry membership check works without instantiating."""
    from maverick.providers import KNOWN_PROVIDERS
    assert "vllm" in KNOWN_PROVIDERS


@_needs_openai
def test_vllm_provider_instantiates():
    from maverick.providers import get_provider_client
    c = get_provider_client("vllm")
    assert c.__class__.__name__ == "VLLMClient"


# ---------- Job queue ----------

def test_job_queue_enqueue_claim_complete(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    jid = q.enqueue("run_goal", {"goal_id": 7})
    assert jid > 0

    job = q.claim()
    assert job is not None
    assert job.kind == "run_goal"
    assert job.payload == {"goal_id": 7}
    assert job.status == "running"
    assert job.attempts == 1

    assert q.complete(jid) is True
    fetched = q.get(jid)
    assert fetched is not None
    assert fetched.status == "done"


def test_job_queue_claim_returns_none_when_empty(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    assert q.claim() is None


def test_job_queue_respects_run_at(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    future = time.time() + 60
    jid = q.enqueue("later", {}, run_at=future)
    # Nothing ready yet.
    assert q.claim() is None
    # Simulate clock moving forward.
    job = q.claim(now=future + 1)
    assert job is not None and job.id == jid


def test_job_queue_fail_reschedules(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    jid = q.enqueue("flaky", {})
    job = q.claim()
    assert job is not None

    q.fail(jid, "transient", retry_after=0, max_attempts=3)
    j2 = q.get(jid)
    assert j2.status == "pending"
    assert j2.attempts == 1
    assert "transient" in j2.last_error

    # Re-claim works after the reschedule.
    re = q.claim()
    assert re is not None and re.id == jid
    assert re.attempts == 2


def test_job_queue_fail_terminal_after_max(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    jid = q.enqueue("doomed", {})
    # Cycle: claim, fail with retry, claim, fail with retry, ... until max.
    for _ in range(3):
        q.claim()
        q.fail(jid, "boom", retry_after=0, max_attempts=3)
    # Next fail (now 4th attempt) should mark failed.
    q.claim()
    q.fail(jid, "boom-final", retry_after=0, max_attempts=3)
    final = q.get(jid)
    assert final.status == "failed"
    assert "boom-final" in final.last_error


def test_job_queue_list_filters_status(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    a = q.enqueue("a", {})
    b = q.enqueue("b", {})
    q.claim()  # makes one running
    pending = q.list(status="pending")
    pending_ids = {j.id for j in pending}
    # exactly one of {a,b} is still pending
    assert len(pending_ids & {a, b}) == 1


def test_job_queue_purge_removes_done(tmp_path):
    from maverick.job_queue import JobQueue
    q = JobQueue(db_path=tmp_path / "jobs.db")
    jid = q.enqueue("ephemeral", {})
    q.claim()
    q.complete(jid)
    # Backdate the updated_at so purge can see it.
    import sqlite3
    with sqlite3.connect(str(q.db_path)) as c:
        c.execute(
            "UPDATE jobs SET updated_at=? WHERE id=?",
            (time.time() - 365 * 86400, jid),
        )
    deleted = q.purge(older_than_days=30)
    assert deleted == 1
    assert q.get(jid) is None
