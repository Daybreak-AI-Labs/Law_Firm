"""Channel adapters for Lightwork.

A channel normalizes incoming messages from any platform into a shared
``IncomingMessage`` shape, hands it to the orchestrator, and routes the
response back. This is the surface Lightwork uses to power phone-companion
mode — the agent itself runs on Desktop or VPS, and channels give a
phone (or any other client) a way to talk to it.

Server-wired adapters (16; registered in ``maverick.server._WIRES`` and
enableable from the installer, each requiring its provider's credentials and a
per-sender allowlist, default-deny):
  - telegram, discord, slack, signal, email, matrix
  - bluesky, mastodon, irc, threads, rcs, voice
  - whatsapp + sms (Twilio + public webhook), whatsapp_cloud (Meta Graph API)
  - imessage (macOS only)
``cli`` is the interactive terminal channel (used directly, not hosted by
``maverick serve``); ``glasses`` is fronted by the external webhook relay.
Opt-in helpers layer on the base channels rather than being separate adapters:
email_v2 (IMAP IDLE + threading), discord_stages (Stage voice), streaming_voice
(barge-in), and rich_render (KaTeX/Mermaid — applied by ``server.add_channel``
when ``[channels] rich_render`` is on). signal needs signal-cli on PATH; matrix
needs matrix-nio.
"""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

from .base import Channel, Handler, IncomingMessage, Reply, as_reply

try:
    __version__ = _distribution_version("maverick-channels")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.1.7"
__all__ = ["Channel", "IncomingMessage", "Handler", "Reply", "as_reply"]
