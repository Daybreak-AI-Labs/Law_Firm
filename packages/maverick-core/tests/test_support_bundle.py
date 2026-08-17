"""maverick support — a redacted diagnostics bundle. Secrets must never appear
in it; the core sections must always be present."""
from __future__ import annotations

import json

from maverick import support_bundle


def test_redacts_secret_named_keys():
    src = {
        "providers": {"anthropic": {"api_key": "sk-ant-SECRETVALUE"}},  # pragma: allowlist secret
        "federation": {"peers": [{"name": "vega", "token": "TOPSECRETTOKEN"}]},  # pragma: allowlist secret
        "models": {"orchestrator": "anthropic:claude-opus-4-8"},  # kept
    }
    out = support_bundle._redact(src)
    assert out["providers"]["anthropic"]["api_key"] == "[REDACTED]"
    assert out["federation"]["peers"][0]["token"] == "[REDACTED]"
    assert out["models"]["orchestrator"] == "anthropic:claude-opus-4-8"
    assert "SECRETVALUE" not in json.dumps(out)
    assert "TOPSECRETTOKEN" not in json.dumps(out)


def test_collect_has_core_sections_and_is_json():
    bundle = support_bundle.collect()
    for key in ("versions", "runtime", "readiness", "providers",
                "recent_failures", "config_redacted", "generated_at"):
        assert key in bundle
    # The whole bundle must serialize (no stray non-JSON objects).
    json.dumps(bundle, default=str)


def test_collect_config_is_redacted(monkeypatch):
    monkeypatch.setattr(
        "maverick.config.load_config",
        lambda: {"providers": {"openai": {"api_key": "sk-LEAKME"}}})  # pragma: allowlist secret
    bundle = support_bundle.collect()
    assert "sk-LEAKME" not in json.dumps(bundle["config_redacted"], default=str)




def test_collect_redacts_failed_job_errors(tmp_path, monkeypatch):
    from maverick import job_queue

    monkeypatch.setattr(job_queue, "DEFAULT_DB", tmp_path / "jobs.db")
    q = job_queue.JobQueue()
    job_id = q.enqueue("demo", {})
    claimed = q.claim()
    assert claimed and claimed.id == job_id
    q.fail(
        job_id,
        "RuntimeError: Authorization: Bearer sk-live-POCSECRET1234567890 "
        "and https://user:SuperSecretPass@example.test/cb?api_key=AKIAABCDEFGHIJKLMNOP",
        retry_after=None,
    )

    bundle = support_bundle.collect()
    text = json.dumps(bundle, default=str)
    assert "sk-live-POCSECRET1234567890" not in text
    assert "SuperSecretPass" not in text
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    last_error = bundle["recent_failures"]["failed_jobs"][0]["last_error"]
    assert "[REDACTED:openai_key]" in last_error
    assert "[REDACTED:url_credentials]" in last_error




