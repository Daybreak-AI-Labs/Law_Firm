"""Voice channel (Vapi) construction smoke tests.

We can't actually run the FastAPI server in a unit test without the
fastapi extra installed, and we can't make real Vapi calls from CI.
These tests verify the channel constructs correctly + rejects bad
config, similar to test_sms_signature.
"""
from __future__ import annotations

import pytest


def _have_deps() -> bool:
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_voice_channel_requires_api_key():
    from maverick_channels.voice import VoiceChannel

    async def _noop(_):
        return ""

    with pytest.raises(ValueError, match="VAPI_API_KEY"):
        VoiceChannel(handler=_noop, api_key=None)


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_voice_channel_constructs_with_api_key():
    from maverick_channels.voice import VoiceChannel

    async def _noop(_):
        return ""

    chan = VoiceChannel(
        handler=_noop,
        api_key="vapi-test-key",
        phone_number="+14155551234",
        port=8770,
        assistant_id="asst_xyz",
    )
    # FastAPI route is wired.
    routes = [r.path for r in chan._app.routes if hasattr(r, "path")]
    assert "/webhook/voice" in routes
    assert chan.provider == "vapi"


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_voice_channel_picks_up_env(monkeypatch):
    from maverick_channels.voice import VoiceChannel

    async def _noop(_):
        return ""

    monkeypatch.setenv("VAPI_API_KEY", "from-env")
    chan = VoiceChannel(handler=_noop)
    assert chan.api_key == "from-env"


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_voice_webhook_requires_bearer_token(monkeypatch):
    from fastapi.testclient import TestClient
    from maverick_channels.voice import VoiceChannel

    async def _noop(_):
        return "ok"

    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN", "voice-secret")
    chan = VoiceChannel(handler=_noop, api_key="vapi-test-key")
    client = TestClient(chan._app)
    payload = {"message": {"type": "transcript", "role": "user", "transcript": "hi"}}
    resp = client.post("/webhook/voice", json=payload)
    assert resp.status_code == 401


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_voice_webhook_accepts_valid_bearer_token(monkeypatch):
    from fastapi.testclient import TestClient
    from maverick_channels.voice import VoiceChannel

    async def _noop(_):
        return "ok"

    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN", "voice-secret")
    chan = VoiceChannel(handler=_noop, api_key="vapi-test-key")
    client = TestClient(chan._app)
    payload = {"message": {"type": "transcript", "role": "user", "transcript": "hi"}}
    resp = client.post(
        "/webhook/voice",
        json=payload,
        headers={"Authorization": "Bearer voice-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"response": "ok"}


# ---- multi-provider outbound send() ----------------------------------

async def _noop(_):
    return ""


def _chan(provider, **kw):
    from maverick_channels.voice import VoiceChannel
    kw.setdefault("api_key", "k")
    kw.setdefault("assistant_id", "asst_xyz")
    kw.setdefault("phone_number", "+14155551234")
    return VoiceChannel(handler=_noop, provider=provider, **kw)


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
@pytest.mark.parametrize("provider,host,path", [
    ("vapi", "api.vapi.ai", "/call"),
    ("retell", "api.retellai.com", "/v2/create-phone-call"),
    ("bland", "api.bland.ai", "/v1/calls"),
])
def test_outbound_request_per_provider(provider, host, path):
    req = _chan(provider)._outbound_request("+14150000000", "hello there")
    assert host in req["url"] and req["url"].endswith(path)
    # the spoken text + destination number must be in the body somewhere
    body = str(req["json"])
    assert "hello there" in body
    assert "+14150000000" in body
    assert req["headers"]  # auth header present


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_provider_key_env_resolution(monkeypatch):
    from maverick_channels.voice import VoiceChannel
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    monkeypatch.setenv("RETELL_API_KEY", "retell-secret")
    chan = VoiceChannel(handler=_noop, provider="retell")
    assert chan.api_key == "retell-secret"
    # wrong provider's key missing -> clear error naming the right env var
    monkeypatch.delenv("BLAND_API_KEY", raising=False)
    with pytest.raises(ValueError, match="BLAND_API_KEY"):
        VoiceChannel(handler=_noop, provider="bland")


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_unsupported_provider_send_is_soft_noop():
    chan = _chan("nope")
    # _outbound_request rejects it; send() must swallow that, not raise.
    with pytest.raises(ValueError, match="unsupported voice provider"):
        chan._outbound_request("+1", "hi")
    import asyncio
    asyncio.run(chan.send("+14150000000", "hi"))  # no exception = pass


@pytest.mark.skipif(not _have_deps(), reason="fastapi+httpx not installed")
def test_send_posts_to_provider_endpoint(monkeypatch):
    import asyncio

    import maverick_channels.voice as voice

    captured = {}

    class _Resp:
        status_code = 200
        text = ""

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

    monkeypatch.setattr(voice.httpx, "AsyncClient", _FakeClient)
    chan = _chan("retell")
    asyncio.run(chan.send("+14150000000", "ping"))
    assert captured["url"].endswith("/v2/create-phone-call")
    assert "+14150000000" in str(captured["json"])
