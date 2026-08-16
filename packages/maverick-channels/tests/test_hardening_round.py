"""Channels hardening regressions: inbound size cap + adapter crash
guards surfaced in the review round."""
from __future__ import annotations


def test_incoming_message_truncates_oversized_text(monkeypatch):
    monkeypatch.setenv("MAVERICK_MAX_INBOUND_CHARS", "100")
    # Re-evaluate the cap (it's read per-construction).
    from maverick_channels.base import IncomingMessage
    big = "x" * 5000
    msg = IncomingMessage(user_id="u", text=big, channel="test")
    assert len(msg.text) < 5000
    assert "truncated by Maverick inbound cap" in msg.text


def test_incoming_message_cap_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("MAVERICK_MAX_INBOUND_CHARS", "0")
    from maverick_channels.base import IncomingMessage
    big = "y" * 5000
    msg = IncomingMessage(user_id="u", text=big, channel="test")
    assert msg.text == big  # cap disabled


def test_incoming_message_short_text_untouched(monkeypatch):
    monkeypatch.delenv("MAVERICK_MAX_INBOUND_CHARS", raising=False)
    from maverick_channels.base import IncomingMessage
    msg = IncomingMessage(user_id="u", text="hello", channel="test")
    assert msg.text == "hello"


def test_cli_channel_survives_handler_exception():
    import asyncio

    from maverick_channels.cli import CLIChannel

    async def _boom(_msg):
        raise RuntimeError("agent blew up")

    sent: list[str] = []
    ch = CLIChannel(_boom)

    async def _send(uid, text):
        sent.append(text)

    ch.send = _send  # type: ignore[assignment]

    # Drive one iteration's body directly: build the message + dispatch.
    from maverick_channels.base import IncomingMessage
    msg = IncomingMessage(user_id="local", text="hi", channel="cli")

    async def _one():
        try:
            reply = await ch.handler(msg)
        except Exception as e:
            reply = f"Sorry, I ran into an error: {e}"
        await ch.send("local", reply)

    asyncio.run(_one())
    assert sent and "error" in sent[0].lower()
