"""Slack channel via Socket Mode (no public webhook needed).

Set up:
  1. Create a Slack app at https://api.slack.com/apps
  2. Enable Socket Mode; copy the App-Level Token to ${SLACK_APP_TOKEN}
  3. Install to your workspace; copy the Bot Token to ${SLACK_BOT_TOKEN}
  4. Subscribe to `message.im` events; add `chat:write`, `im:history` scopes
     (+ `files:read` if users will share files with the bot — shared files
     become goal attachments)

Requires::

    python -m pip install -e './packages/maverick-channels[slack]'
"""
from __future__ import annotations

import asyncio
import logging
import os

from .base import Channel, IncomingMessage, is_allowed, normalize_allowlist

log = logging.getLogger(__name__)

try:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient
    _HAVE_SLACK = True
except ImportError:
    _HAVE_SLACK = False
    SocketModeClient = AsyncWebClient = SocketModeResponse = None  # type: ignore


class SlackChannel(Channel):
    name = "slack"

    def __init__(
        self,
        handler,
        app_token: str | None = None,
        bot_token: str | None = None,
        allowed_user_ids=None,
        thread_replies: bool | None = None,
    ):
        super().__init__(handler)
        if not _HAVE_SLACK:
            raise ImportError(
                "slack_sdk not installed. From the reviewed checkout run: "
                "python -m pip install -e './packages/maverick-channels[slack]'"
            )
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN")
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        if not self.app_token or not self.bot_token:
            raise ValueError("SLACK_APP_TOKEN and SLACK_BOT_TOKEN must be set")
        # Without an allowlist, ANY workspace user who can message the bot
        # drives the agent (and burns the operator's budget). Require one,
        # matching discord/telegram. Default-deny via base.is_allowed.
        self.allowed_user_ids = normalize_allowlist(
            allowed_user_ids, "SLACK_ALLOWED_USER_IDS",
        )
        if not self.allowed_user_ids:
            raise ValueError("Set SLACK_ALLOWED_USER_IDS to restrict access")
        # Opt-in reply threading: answers post under the asking message's
        # thread (thread_ts) instead of interleaving into the channel.
        # [channels.slack] thread_replies / SLACK_THREAD_REPLIES.
        if thread_replies is None:
            thread_replies = os.environ.get(
                "SLACK_THREAD_REPLIES", "").strip().lower() in {"1", "true", "yes", "on"}
        self.thread_replies = bool(thread_replies)
        self._web = AsyncWebClient(token=self.bot_token)
        self._sm = SocketModeClient(app_token=self.app_token, web_client=self._web)
        self._sm.socket_mode_request_listeners.append(self._on_request)
        self._stop_event = asyncio.Event()

    async def _download_files(self, event: dict) -> list[dict]:
        """Download a message's shared files into IncomingMessage.attachments.

        Slack file objects carry ``url_private_download`` which requires the
        bot token as a bearer header (and the ``files:read`` scope). Each
        file becomes ``{"filename", "mime", "data"}``; the server stores them
        under the same size/mime/magic-byte rules as a dashboard upload.
        Best-effort: a missing scope or a failed download just means no
        attachment, never a dropped message.
        """
        files = event.get("files") or []
        if not files:
            return []
        try:
            import aiohttp  # slack_sdk's async client already requires it
        except ImportError:  # pragma: no cover
            return []
        out: list[dict] = []
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            for f in files[:10]:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                size = f.get("size")
                if size and int(size) > 100 * 1024 * 1024:
                    continue  # attachments.store would reject it anyway
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        data = await resp.content.read(100 * 1024 * 1024 + 1)
                except Exception:  # noqa: BLE001 -- never drop the message
                    log.warning("slack: file download failed", exc_info=True)
                    continue
                if not data or len(data) > 100 * 1024 * 1024:
                    continue
                out.append({
                    "filename": f.get("name") or "file.bin",
                    "mime": f.get("mimetype") or "application/octet-stream",
                    "data": data,
                })
        return out

    async def _on_request(self, client, req):
        # Ack IMMEDIATELY, before doing any work. Slack redelivers an event
        # (up to ~3x) if the Socket Mode ack doesn't arrive within ~3s, and the
        # handler below runs a full agent swarm that routinely takes far longer
        # -- so acking at the end caused Slack to redeliver and the agent to run
        # (and bill) 2-3x per message. Acking first trades at-least-once for
        # at-most-once delivery, which is the right call for an expensive,
        # side-effecting handler.
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
        if req.type == "events_api":
            event = req.payload.get("event", {})
            if event.get("type") == "message" and "bot_id" not in event:
                # Gate on the SENDER (event["user"]); user_id below is the
                # channel we reply to, not the author. Unlisted senders are
                # silently ignored.
                sender = event.get("user", "")
                if not is_allowed(sender, self.allowed_user_ids):
                    log.warning("unauthorized slack access: user=%s", sender)
                else:
                    # Authorized sender: pull any shared files so they become
                    # goal attachments (auth first — downloads for unlisted
                    # senders would be free resource burn).
                    attachments = await self._download_files(event)
                    text = event.get("text", "")
                    if not text.strip() and attachments:
                        # File-only share: give the swarm a working brief.
                        text = "Process the attached file(s)."
                    msg = IncomingMessage(
                        user_id=event.get("channel", ""),
                        text=text,
                        channel="slack",
                        raw=event,
                        sender_id=sender,
                        message_id=event.get("ts"),
                        attachments=attachments,
                    )
                    try:
                        reply = await self.dispatch_text(msg)
                    except Exception:  # pragma: no cover
                        log.exception("handler error")
                        reply = "⚠ An internal error occurred."
                    # An empty reply (action-only goal, or a Reply whose text
                    # dispatch dropped) must not be sent: chat_postMessage(text="")
                    # is rejected by Slack with `no_text`, raising out of the
                    # handler. Guard with `if reply:` like the other channels.
                    if reply and self.thread_replies and event.get("ts"):
                        await self.send_threaded(
                            event["channel"], reply, reply_to=event["ts"])
                    elif reply:
                        # Route through send() so the reply gets to_slack_mrkdwn
                        # + mrkdwn=True like every other outbound path; the raw
                        # chat_postMessage here left markdown unrendered whenever
                        # thread_replies was off.
                        await self.send(event["channel"], reply)

    async def start(self) -> None:
        await self._sm.connect()
        log.info("Slack channel connected")
        await self._stop_event.wait()

    async def send(self, user_id: str, text: str) -> None:
        from .formatting import to_slack_mrkdwn
        await self._web.chat_postMessage(
            channel=user_id, text=to_slack_mrkdwn(text), mrkdwn=True,
        )

    async def send_threaded(
        self, user_id: str, text: str, *, reply_to: str | None = None,
    ) -> None:
        from .formatting import to_slack_mrkdwn
        kwargs = {"channel": user_id, "text": to_slack_mrkdwn(text), "mrkdwn": True}
        if reply_to:
            kwargs["thread_ts"] = reply_to
        await self._web.chat_postMessage(**kwargs)

    async def stop(self) -> None:
        self._stop_event.set()
        await self._sm.disconnect()
