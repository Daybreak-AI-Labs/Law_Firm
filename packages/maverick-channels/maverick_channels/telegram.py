"""Telegram bot channel.

The simplest path to phone-companion mode. Set ``[channels.telegram]
enabled = true`` in ``~/.maverick/config.toml`` and provide a bot token,
and any message you send to your bot reaches the orchestrator.

Requires::

    python -m pip install -e './packages/maverick-channels[telegram]'
"""
from __future__ import annotations

import logging
import os

from .base import Channel, IncomingMessage, normalize_allowlist

log = logging.getLogger(__name__)

# Telegram's Bot API hard-rejects text messages over 4096 chars with a 400
# "message is too long", which (like the empty-reply case below) raises out of
# the handler and the user gets NOTHING despite the completed run. Chunk long
# outbound text on line boundaries instead, like the discord/whatsapp adapters.
TELEGRAM_LIMIT = 4096

try:
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters
    _HAVE_TELEGRAM = True
except ImportError:
    _HAVE_TELEGRAM = False
    Update = ContextTypes = Application = MessageHandler = filters = None  # type: ignore


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(
        self,
        handler,
        token: str | None = None,
        allowed_user_ids: set[str] | None = None,
        allowed_chat_ids: set[str] | None = None,
    ):
        super().__init__(handler)
        if not _HAVE_TELEGRAM:
            raise ImportError(
                "python-telegram-bot not installed. Install with: "
                "From the reviewed checkout run: python -m pip install -e "
                "'./packages/maverick-channels[telegram]'"
            )
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")
        self._app: Application | None = None
        self.allowed_user_ids = normalize_allowlist(
            allowed_user_ids, "TELEGRAM_ALLOWED_USER_IDS",
        )
        self.allowed_chat_ids = normalize_allowlist(
            allowed_chat_ids, "TELEGRAM_ALLOWED_CHAT_IDS",
        )
        if not self.allowed_user_ids and not self.allowed_chat_ids:
            raise ValueError(
                "Set TELEGRAM_ALLOWED_USER_IDS or TELEGRAM_ALLOWED_CHAT_IDS to restrict access"
            )

    def _is_authorized(self, update: Update) -> bool:
        user_id = str(update.effective_user.id) if update.effective_user else ""
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        chat_type = getattr(update.effective_chat, "type", None)

        # An allowlisted sender is always authorized, in any chat.
        if self.allowed_user_ids and user_id in self.allowed_user_ids:
            return True

        # A chat allowlist must NOT authorize every member of a group: any
        # participant of an allowlisted group could otherwise drive the agent.
        # A private (1:1) chat has a single sender, so an allowlisted private
        # chat is equivalent to an allowlisted user -- honour it there only.
        # In groups, require the sender to be on TELEGRAM_ALLOWED_USER_IDS.
        if (
            chat_type == "private"
            and self.allowed_chat_ids
            and chat_id in self.allowed_chat_ids
        ):
            return True
        return False

    @staticmethod
    async def _collect_attachments(message) -> list[dict]:
        """Download a message's files into IncomingMessage.attachments dicts.

        Documents, photos (largest size), voice notes, audio, and video all
        map to ``{"filename", "mime", "data"}``; the server stores them under
        the same rules as a dashboard upload (and voice notes auto-transcribe
        into the goal context via the companion pipeline). Entirely
        best-effort: any download error just means no attachment.
        """
        candidates: list[tuple[object, str, str]] = []
        if getattr(message, "document", None):
            d = message.document
            candidates.append((d, d.file_name or "document.bin",
                               d.mime_type or "application/octet-stream"))
        if getattr(message, "photo", None):
            candidates.append((message.photo[-1], "photo.jpg", "image/jpeg"))
        if getattr(message, "voice", None):
            candidates.append((message.voice, "voice-note.ogg", "audio/ogg"))
        if getattr(message, "audio", None):
            a = message.audio
            candidates.append((a, a.file_name or "audio.mp3",
                               a.mime_type or "audio/mpeg"))
        if getattr(message, "video", None):
            v = message.video
            candidates.append((v, v.file_name or "video.mp4",
                               v.mime_type or "video/mp4"))
        out: list[dict] = []
        for obj, filename, mime in candidates[:10]:
            size = getattr(obj, "file_size", None)
            if size and size > 100 * 1024 * 1024:
                continue  # attachments.store would reject it anyway
            try:
                f = await obj.get_file()
                data = bytes(await f.download_as_bytearray())
            except Exception:  # noqa: BLE001 -- a broken file never blocks
                log.warning("telegram: attachment download failed", exc_info=True)
                continue
            if data:
                out.append({"filename": filename, "mime": mime, "data": data})
        return out

    async def _on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        if not self._is_authorized(update):
            log.warning("unauthorized telegram access: user_id=%s chat_id=%s",
                        getattr(update.effective_user, "id", None),
                        getattr(update.effective_chat, "id", None))
            return
        # Authorize FIRST, download after — file downloads on behalf of an
        # unauthorized sender would be free resource burn.
        attachments = await self._collect_attachments(update.message)
        text = update.message.text or update.message.caption or ""
        if not text and attachments:
            # Attachment-only message: give the swarm a working brief.
            text = "Process the attached file(s)."
        if not text:
            return
        # Per the IncomingMessage contract, user_id is the REPLY/SEND TARGET and
        # sender_id is the human identity. For a room-based adapter that means
        # the CHAT id is the target (so a proactive channel.send(msg.user_id, ...)
        # reaches the group, not the sender's private chat -- which the bot often
        # can't even open), and the user id is the sender. Matches the Slack
        # adapter. In a 1:1 chat the two ids coincide, so phone-companion mode is
        # unchanged, and principal_id (sender_id or user_id) stays the human in
        # both cases -- so auth/history/tenant keying is identical to before.
        # effective_user is None for channel posts / anonymous admins;
        # _is_authorized denies those, but guard here too rather than
        # AttributeError.
        msg = IncomingMessage(
            user_id=str(update.effective_chat.id) if update.effective_chat else "",
            text=text,
            channel="telegram",
            raw=update,
            sender_id=str(update.effective_user.id) if update.effective_user else None,
            message_id=str(update.message.message_id),
            attachments=attachments,
        )
        try:
            reply = await self.dispatch_text(msg)
        except Exception:  # pragma: no cover
            # Don't reflect the raw exception text to the remote user -- it can
            # carry a credential/internal path. The detail is logged above;
            # the user gets a generic message (matches slack/signal).
            log.exception("handler error")
            reply = "⚠ An internal error occurred."
        # An empty reply (action-only goal, or a Reply whose text dispatch
        # dropped) must not be sent: Telegram rejects reply_text("") with a 400
        # "message text is empty", which raises out of the handler. Guard with
        # `if reply:`, matching bluesky/whatsapp/mastodon and the other channels.
        if reply:
            from .formatting import split_for_discord
            for chunk in split_for_discord(reply, limit=TELEGRAM_LIMIT):
                await update.message.reply_text(chunk)

    async def start(self) -> None:
        self._app = Application.builder().token(self.token).build()
        # TEXT for plain messages, ATTACHMENT so files/photos/voice notes
        # (with or without a caption) reach the handler too — they become
        # goal attachments via IncomingMessage.attachments.
        self._app.add_handler(MessageHandler(
            (filters.TEXT | filters.ATTACHMENT) & ~filters.COMMAND,
            self._on_message))
        log.info("Telegram channel started")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def send(self, user_id: str, text: str) -> None:
        if self._app is None:
            raise RuntimeError("channel not started")
        from .formatting import split_for_discord
        for chunk in split_for_discord(text, limit=TELEGRAM_LIMIT):
            await self._app.bot.send_message(chat_id=int(user_id), text=chunk)

    async def send_threaded(
        self, user_id: str, text: str, *, reply_to: str | None = None,
    ) -> None:
        if self._app is None:
            raise RuntimeError("channel not started")
        kwargs = {"chat_id": int(user_id)}
        if reply_to:
            try:
                kwargs["reply_to_message_id"] = int(reply_to)
            except (TypeError, ValueError):
                pass  # un-threadable id -> plain send
        from .formatting import split_for_discord
        for chunk in split_for_discord(text, limit=TELEGRAM_LIMIT):
            # Every chunk carries the thread anchor so the whole reply stays
            # attached to the message that asked for it.
            await self._app.bot.send_message(text=chunk, **kwargs)

    async def stop(self) -> None:
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
