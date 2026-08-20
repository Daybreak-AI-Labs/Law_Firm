"""Q3 2026 batch 14 — retained Azure/Bedrock providers and scheduler."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


def _fake_httpx(monkeypatch, **methods):
    mod = types.ModuleType("httpx")
    for n, v in methods.items():
        setattr(mod, n, v)
    monkeypatch.setitem(sys.modules, "httpx", mod)
    return mod


def _resp(status, body, *, text=None):
    r = MagicMock()
    r.status_code = status
    if isinstance(body, (dict, list)):
        r.json = MagicMock(return_value=body)
        r.text = text if text is not None else str(body)
    else:
        r.json = MagicMock(side_effect=ValueError("not json"))
        r.text = text if text is not None else str(body)
    r.content = (body if isinstance(body, bytes) else str(body).encode())
    return r


def _openai_available() -> bool:
    try:
        import openai  # noqa: F401
        return True
    except ImportError:
        return False


_needs_openai = pytest.mark.skipif(
    not _openai_available(), reason="openai extra not installed")


# ---------- Providers ----------

def test_providers_registered():
    from maverick.providers import KNOWN_PROVIDERS
    assert "azure" in KNOWN_PROVIDERS
    assert "bedrock" in KNOWN_PROVIDERS


def test_azure_extra_installs_bounded_identity_dependency():
    metadata = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = [
        Requirement(item)
        for item in metadata["project"]["optional-dependencies"]["azure"]
    ]
    by_name = {requirement.name: requirement for requirement in requirements}

    assert "openai" in by_name
    assert Version("1.25.3") in by_name["azure-identity"].specifier
    assert Version("2.0.0") not in by_name["azure-identity"].specifier


@_needs_openai
def test_azure_requires_config(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    from maverick.providers.azure_openai_provider import AzureOpenAIClient
    try:
        AzureOpenAIClient()
    except RuntimeError as e:
        assert "AZURE_OPENAI_ENDPOINT" in str(e)
        return
    raise AssertionError("expected RuntimeError")


@_needs_openai
def test_azure_uses_dedicated_client(monkeypatch):
    """The provider must build the SDK's AzureOpenAI client (which sends
    the api-key header + api-version query), NOT a plain OpenAI client
    with the api-version baked into base_url (the SDK drops it)."""
    from openai import AzureOpenAI
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_AUTH", raising=False)
    from maverick.providers.azure_openai_provider import AzureOpenAIClient
    c = AzureOpenAIClient()
    assert isinstance(c._sync, AzureOpenAI)
    assert c.deployment == "gpt5"
    assert c.api_version == "2024-10-21"
    assert c.endpoint == "https://res.openai.azure.com"
    assert c.DEFAULT_MODEL == "gpt5"
    assert c.auth_mode == "api_key"


def _clear_azure_auth(monkeypatch):
    for name in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_AD_TOKEN",
        "AZURE_OPENAI_AUTH",
        "AZURE_OPENAI_TOKEN_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_azure_auth_never_substitutes_fake_key(monkeypatch):
    from maverick.providers.azure_openai_provider import _azure_auth

    _clear_azure_auth(monkeypatch)
    with pytest.raises(RuntimeError, match="requires authentication"):
        _azure_auth(None, None)


def test_azure_auth_rejects_ambiguous_credentials(monkeypatch):
    from maverick.providers.azure_openai_provider import _azure_auth

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "real-key")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "real-token")
    with pytest.raises(RuntimeError, match="exactly one"):
        _azure_auth(None, None)


def test_azure_auth_supports_static_entra_token(monkeypatch):
    from maverick.providers.azure_openai_provider import _azure_auth

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", " entra-token ")
    kwargs, owner, mode = _azure_auth(None, None)
    assert kwargs == {"azure_ad_token": "entra-token"}
    assert owner is None
    assert mode == "entra_id"


def test_azure_entra_token_is_not_forwarded_as_an_api_key(monkeypatch):
    import maverick.llm as llm

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "entra-token")
    monkeypatch.setattr(llm, "_configured_provider_api_key", lambda _provider: None)

    assert llm._provider_api_key("azure", None) is None


def test_azure_auth_uses_documented_default_credential_path(monkeypatch):
    from maverick.providers.azure_openai_provider import AzureOpenAIClient

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    monkeypatch.setenv("AZURE_OPENAI_AUTH", "entra_id")
    monkeypatch.setenv("AZURE_OPENAI_TOKEN_SCOPE", "scope://custom/.default")

    calls = []

    class _FakeAzureClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.AzureOpenAI = _FakeAzureClient
    fake_openai.AsyncAzureOpenAI = _FakeAzureClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    credential = object()
    scopes = []

    def provider():
        return "entra-token"

    class _DefaultAzureCredential:
        def __new__(cls):
            return credential

    def _get_bearer_token_provider(owner, scope):
        scopes.append((owner, scope))
        return provider

    azure = types.ModuleType("azure")
    azure.__path__ = []
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = _DefaultAzureCredential
    identity.get_bearer_token_provider = _get_bearer_token_provider
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)

    client = AzureOpenAIClient()

    assert scopes == [(credential, "scope://custom/.default")]
    assert len(calls) == 2
    assert all(call["azure_ad_token_provider"] is provider for call in calls)
    assert all("api_key" not in call for call in calls)
    assert client.auth_mode == "entra_id"
    assert client._azure_credential is credential


def test_azure_identity_dependency_is_preflighted(monkeypatch):
    import importlib.util

    from maverick import providers

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_AUTH", "entra_id")

    def _find_spec(name):
        return None if name == "azure.identity" else object()

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    messages = providers.missing_sdks(["azure:gpt5"])

    assert any("maverick-core[azure]" in message for message in messages)


def test_static_azure_ad_token_does_not_require_identity_sdk(monkeypatch):
    import importlib.util

    from maverick import providers

    _clear_azure_auth(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "static-token")

    def _find_spec(name):
        return None if name == "azure.identity" else object()

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    assert providers.missing_sdks(["azure:gpt5"]) == []


@_needs_openai
def test_bedrock_requires_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    from maverick.providers.bedrock_provider import BedrockClient
    try:
        BedrockClient()
    except RuntimeError as e:
        assert "AWS_REGION" in str(e)
        return
    raise AssertionError("expected RuntimeError")


@_needs_openai
def test_bedrock_builds_url(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_API_KEY", "k")
    from maverick.providers.bedrock_provider import BedrockClient
    c = BedrockClient()
    assert "bedrock-runtime.us-east-1.amazonaws.com" in c.base_url

































































# ---------- Scheduler ----------

def test_scheduler_parse_basic():
    from maverick.scheduler import parse_cron
    minute, hour, dom, mon, dow = parse_cron("0 9 * * 1-5")
    assert minute == {0}
    assert hour == {9}
    assert dow == {1, 2, 3, 4, 5}


def test_scheduler_rejects_bad_field_count():
    from maverick.scheduler import CronError, parse_cron
    try:
        parse_cron("0 9 * *")
    except CronError as e:
        assert "5 fields" in str(e)
        return
    raise AssertionError("expected CronError")


def test_scheduler_step_syntax():
    from maverick.scheduler import parse_cron
    minute, *_ = parse_cron("*/15 * * * *")
    assert minute == {0, 15, 30, 45}


def test_scheduler_single_value_step_expands():
    """'5/15' means 5,20,35,50 — not just {5}."""
    from maverick.scheduler import parse_cron
    minute, *_ = parse_cron("5/15 * * * *")
    assert minute == {5, 20, 35, 50}


def test_scheduler_sunday_as_7_accepted():
    """'7' and ranges containing 7 are Sunday, folded to 0 — not rejected."""
    from maverick.scheduler import parse_cron
    *_, dow = parse_cron("0 9 * * 7")
    assert dow == {0}
    *_, dow2 = parse_cron("0 9 * * 5-7")
    assert dow2 == {5, 6, 0}


def test_scheduler_next_run_daily_9am_utc():
    import datetime as _dt

    from maverick.scheduler import next_run
    # Monday 2026-06-01 08:00 UTC -> next "0 9 * * *" is same day 09:00 UTC.
    base = _dt.datetime(2026, 6, 1, 8, 0, tzinfo=_dt.timezone.utc).timestamp()
    ts = next_run("0 9 * * *", after=base)
    got = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    assert got.hour == 9 and got.minute == 0
    assert got.date() == _dt.date(2026, 6, 1)


def test_scheduler_next_run_weekday_only_utc():
    import datetime as _dt

    from maverick.scheduler import next_run
    # Friday 2026-06-05 10:00 UTC, "0 9 * * 1-5" -> Monday 2026-06-08 09:00.
    base = _dt.datetime(2026, 6, 5, 10, 0, tzinfo=_dt.timezone.utc).timestamp()
    ts = next_run("0 9 * * 1-5", after=base)
    got = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    assert got.weekday() == 0  # Monday
    assert got.date() == _dt.date(2026, 6, 8)


def test_scheduler_next_run_is_utc():
    """Fields match UTC regardless of host TZ — '0 0 * * *' is 00:00 UTC."""
    import datetime as _dt

    from maverick.scheduler import next_run
    base = _dt.datetime(2026, 6, 1, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
    ts = next_run("0 0 * * *", after=base)
    got = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    assert got.hour == 0 and got.minute == 0
    assert got.date() == _dt.date(2026, 6, 2)


def test_scheduler_schedule_cron_enqueues(tmp_path):
    import datetime as _dt

    from maverick.job_queue import JobQueue
    from maverick.scheduler import schedule_cron
    q = JobQueue(db_path=tmp_path / "jobs.db")
    base = _dt.datetime(2026, 6, 1, 8, 0, 0).timestamp()
    job_id, run_at = schedule_cron(q, "0 9 * * *", "run_goal",
                                    {"goal_id": 1}, after=base)
    assert job_id > 0
    job = q.get(job_id)
    assert job is not None
    assert job.kind == "run_goal"
    assert abs(job.run_at - run_at) < 1.0
    # Not claimable before run_at.
    assert q.claim(now=base) is None
    # Claimable at/after run_at.
    assert q.claim(now=run_at + 1) is not None
