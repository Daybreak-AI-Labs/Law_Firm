"""Email channel: IMAP poll for incoming, SMTP for outgoing.

v0.1.1 fix: both IMAP and SMTP calls now have a 30-second connect
timeout. A wedged Gmail connection no longer pins the channel thread
forever.

Set up:
  1. Use an account with an app password (Gmail / Fastmail / etc.)
  2. Set in config:
        [channels.email]
        enabled = true
        imap_host = "imap.gmail.com"
        imap_user = "${EMAIL_USER}"
        imap_password = "${EMAIL_APP_PASSWORD}"
        smtp_host = "smtp.gmail.com"
        smtp_port = 465
        smtp_user = "${EMAIL_USER}"
        smtp_password = "${EMAIL_APP_PASSWORD}"

No extra dependencies needed (uses stdlib `imaplib` + `smtplib`).
"""
from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import logging
import os
import re
import smtplib
from email.message import EmailMessage

from .base import Channel, IncomingMessage, backoff_delay, is_allowed, normalize_allowlist

log = logging.getLogger(__name__)

IMAP_TIMEOUT = 30.0
SMTP_TIMEOUT = 30.0

# Verdicts parsed out of the trusted `Authentication-Results` header the
# receiving MX stamps on. We only act on an EXPLICIT negative (the strong spoof
# signal); a domain that publishes no SPF/DKIM yields no verdict and is left to
# the allowlist (that gap is inherent to IMAP -- there's no relay proof like the
# Twilio/Meta HMAC channels have).
_SPF_RE = re.compile(r"\bspf=(\w+)")
_DKIM_RE = re.compile(r"\bdkim=(\w+)")
# The authserv-id (first token before the first ';') identifies the ADMD that
# stamped a given Authentication-Results header. Per RFC 8601 we may only trust
# the header our own receiving MX added; lower headers are message body that any
# upstream hop -- including the sender -- can forge.
_AUTHSERV_RE = re.compile(r"\s*([^\s;]+)")


# Reply-noise trimming: a reply carries the whole prior thread as quoted
# text, re-ingested on EVERY message — the classic email token multiplier.
# Markers cut the quoted history; the RFC 3676 "-- " line cuts the signature.
# 300-char middle: long attribution lines (many recipients, localized dates)
# blow past 200 and a missed match re-ingests the whole thread. This marker
# precedes ">"-quoted text, so its cut is guarded by _only_quotes_below —
# bottom-posted answers AFTER the quote block must survive.
_ON_WROTE_RE = re.compile(r"^\s*On .{0,300}wrote:\s*$", re.IGNORECASE)
# Outlook/forward dividers precede an UNQUOTED copy of the old mail (headers
# then body), so these cut unconditionally — bottom-posting below them is not
# a convention.
_DIVIDER_CUT_RES = (
    re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE),
)
_HTML_BREAK_RE = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h[1-6])\s*/?\s*>",
                            re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _drop_html_raw_text(html: str) -> str:
    """Remove script/style blocks with a bounded, single-pass scanner."""
    text = html or ""
    lower = text.lower()
    out: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        tag_start = text.find("<", pos)
        if tag_start < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:tag_start])
        tag_name_start = tag_start + 1
        tag_name_end = tag_name_start
        while tag_name_end < length and lower[tag_name_end].isalpha():
            tag_name_end += 1
        tag_name = lower[tag_name_start:tag_name_end]
        if tag_name not in {"script", "style"}:
            out.append(text[tag_start:tag_name_end])
            pos = tag_name_end
            continue
        if tag_name_end < length and lower[tag_name_end] not in "\t\n\r\f />":
            out.append(text[tag_start:tag_name_end])
            pos = tag_name_end
            continue
        tag_end = text.find(">", tag_name_end)
        if tag_end < 0:
            break
        close_start = lower.find(f"</{tag_name}", tag_end + 1)
        if close_start < 0:
            break
        close_end = text.find(">", close_start + len(tag_name) + 2)
        if close_end < 0:
            break
        pos = close_end + 1
    return "".join(out)


def _strip_html(html: str) -> str:
    """HTML body -> plain text (tags/CSS/JS are 3-10x the visible text)."""
    import html as _htmllib
    text = _drop_html_raw_text(html or "")
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _htmllib.unescape(text)
    # collapse the whitespace the markup structure leaves behind
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _only_quotes_below(lines: list[str], start: int) -> bool:
    """True when everything below ``start`` is quoted/blank — i.e. cutting
    there loses no fresh content. A bottom-posted reply (real answer BELOW
    the ``On ... wrote:`` attribution and its quote block) fails this check,
    so the marker cut is skipped and the answer survives."""
    return all(
        not ln.strip() or ln.lstrip().startswith(">")
        for ln in lines[start:]
    )


def _trim_reply_noise(body: str) -> str:
    """Drop quoted-reply history and the signature block from one message.

    Cuts at a reply/forward marker only when nothing but quoted text follows
    it (top-posted replies — the dominant style); at the RFC 3676 ``-- ``
    signature delimiter (dash-dash-SPACE only: a bare ``--`` is a common
    prose divider and cutting there dropped real body text); then drops a
    trailing run of ``>``-quoted lines. Bottom-posted and interleaved
    answers are preserved. Fail-safe: an all-quote body returns unchanged,
    never empty.
    """
    text = body or ""
    lines = text.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        if any(m.match(line) for m in _DIVIDER_CUT_RES):
            cut = i
            break
        if _ON_WROTE_RE.match(line) and _only_quotes_below(lines, i + 1):
            cut = i
            break
        if line.rstrip("\r") == "-- ":
            cut = i
            break
    kept = lines[:cut]
    while kept and (not kept[-1].strip() or kept[-1].lstrip().startswith(">")):
        kept.pop()
    trimmed = "\n".join(kept).strip()
    return trimmed or text


def _authentication_verdict(msg) -> str:
    """Classify a message's inbound authentication as 'pass' | 'fail' | 'none'.

    'fail' means the receiving server evaluated SPF/DKIM and the result was an
    explicit failure (spf=fail/softfail or dkim=fail) with nothing passing --
    i.e. the From is very likely forged. 'none' covers no Authentication-Results
    header, or only neutral/none results (a domain without published records).

    Only the TOPMOST Authentication-Results header is evaluated (RFC 8601):
    the receiving MX prepends its own result, so the genuine verdict is first.
    Lower headers are part of the message body and trivially forgeable -- joining
    all of them let a forged ``spf=pass`` from an attacker-controlled relay mask
    the trusted MX's ``spf=fail``. If ``EMAIL_TRUSTED_AUTHSERV_ID`` is set, the
    topmost header is only trusted when its authserv-id matches (a stricter
    guard against a hop that prepends a header above our own MX's).
    """
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return "none"
    trusted = _trusted_authserv_id()
    header = None
    if trusted:
        for h in headers:
            authserv = _AUTHSERV_RE.match(str(h))
            if authserv and authserv.group(1).lower() == trusted:
                header = str(h)
                break
        if header is None:
            # No header carries our trusted authserv-id -- treat as unevaluated
            # rather than trusting a stranger's verdict.
            return "none"
    else:
        # No configured trust anchor: trust only the topmost header, which the
        # receiving MX stamps on. Never join lower (forgeable) headers in.
        header = str(headers[0])
    text = header.lower()
    spf = _SPF_RE.findall(text)
    dkim = _DKIM_RE.findall(text)
    if "pass" in spf or "pass" in dkim:
        return "pass"
    if any(v in ("fail", "softfail") for v in spf) or "fail" in dkim:
        return "fail"
    return "none"


def _trusted_authserv_id() -> str:
    """Optional configured authserv-id whose Authentication-Results we trust.

    Defaults to empty (trust the topmost header). Set
    ``EMAIL_TRUSTED_AUTHSERV_ID`` to the receiving MX's authserv-id to require an
    exact match before any verdict is honored.
    """
    return (os.environ.get("EMAIL_TRUSTED_AUTHSERV_ID") or "").strip().lower()


class EmailChannel(Channel):
    name = "email"

    def __init__(
        self,
        handler,
        imap_host: str,
        imap_user: str,
        imap_password: str,
        smtp_host: str,
        smtp_user: str,
        smtp_password: str,
        smtp_port: int = 465,
        poll_interval: int = 30,
        allowed_user_ids=None,
    ):
        super().__init__(handler)
        # Without an allowlist, ANY inbound sender could drive the agent.
        # Addresses compared case-insensitively. Require one.
        self.allowed_user_ids = {
            a.lower() for a in normalize_allowlist(allowed_user_ids, "EMAIL_ALLOWED_USER_IDS")
        }
        if not self.allowed_user_ids:
            raise ValueError("Set EMAIL_ALLOWED_USER_IDS to restrict access")
        self.imap_host = imap_host
        self.imap_user = imap_user
        self.imap_password = imap_password
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.poll_interval = poll_interval
        self._stop = False

    async def start(self) -> None:
        log.info("Email channel polling %s every %ds", self.imap_host, self.poll_interval)
        errors = 0
        while not self._stop:
            try:
                messages = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch_unseen),
                    timeout=IMAP_TIMEOUT * 2,
                )
                errors = 0
            except asyncio.TimeoutError:
                log.warning("IMAP poll timed out; continuing")
                messages = []
                errors += 1
            except Exception:  # pragma: no cover
                log.exception("email poll failed")
                messages = []
                errors += 1
            for from_addr, subject, body, attachments in messages:
                if not is_allowed((from_addr or "").lower(), self.allowed_user_ids):
                    log.warning("unauthorized email access: from=%s", from_addr)
                    continue
                text = f"Subject: {subject}\n\n{body}" if subject else body
                if not text.strip():
                    # Attachment-only email: give the swarm a working brief.
                    text = "Process the attached file(s)."
                msg = IncomingMessage(
                    user_id=from_addr, text=text, channel="email",
                    attachments=attachments,
                )
                try:
                    reply = await self.dispatch_text(msg)
                except Exception:  # pragma: no cover
                    # Generic reply; raw exception detail (possible secret) is
                    # logged above, not emailed back to the sender.
                    log.exception("handler error")
                    reply = "⚠ An internal error occurred."
                reply_subject = f"Re: {subject}" if subject else "Bjerken and Day"
                # A single SMTP send failure must not abort the batch —
                # otherwise already-handled messages get reprocessed (and
                # re-run the swarm) on the next poll.
                try:
                    await self.send(from_addr, reply, subject=reply_subject)
                except Exception:
                    log.exception("email send failed for %s", from_addr)
            await asyncio.sleep(backoff_delay(self.poll_interval, errors))

    @staticmethod
    def _extract_attachments(msg) -> list[dict]:
        """File parts of one message as IncomingMessage.attachments dicts.

        Any part with a filename (or an explicit attachment disposition)
        becomes ``{"filename", "mime", "data"}``; the server stores them as
        goal attachments under the same size/mime/magic-byte rules as a
        dashboard upload. Bounded (count + per-file bytes) so one hostile
        email can't balloon the poll loop.
        """
        out: list[dict] = []
        if not msg.is_multipart():
            return out
        for part in msg.walk():
            if len(out) >= 10:
                break
            filename = part.get_filename()
            disposition = (part.get_content_disposition() or "").lower()
            if not filename and disposition != "attachment":
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, (bytes, bytearray)) or not payload:
                continue
            if len(payload) > 100 * 1024 * 1024:
                continue  # attachments.store would reject it anyway
            out.append({
                "filename": str(filename or "attachment.bin"),
                "mime": (part.get_content_type()
                         or "application/octet-stream").lower(),
                "data": bytes(payload),
            })
        return out

    def _fetch_unseen(self) -> list[tuple[str, str, str, list[dict]]]:
        out: list[tuple[str, str, str, list[dict]]] = []
        with imaplib.IMAP4_SSL(self.imap_host, timeout=IMAP_TIMEOUT) as mail:
            mail.login(self.imap_user, self.imap_password)
            mail.select("INBOX")
            _, data = mail.search(None, "UNSEEN")
            for num in data[0].split():
                # BODY.PEEK does NOT implicitly set \Seen; we mark it explicitly
                # below so the message is never re-fetched by a second poller or
                # after a restart (which would re-drive the swarm at real cost).
                _, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                if not msg_data or not msg_data[0]:
                    continue
                payload = msg_data[0][1]
                if not isinstance(payload, (bytes, bytearray)):
                    continue
                m = email.message_from_bytes(payload)
                from_addr = email.utils.parseaddr(m.get("From", ""))[1]
                subject = m.get("Subject", "")
                body = self._extract_body(m)
                # Claim the message before handing it off: at-most-once delivery
                # (a crash mid-dispatch drops one message rather than looping the
                # swarm forever). Mirrors the dedup the other channels do.
                try:
                    mail.store(num, "+FLAGS", "\\Seen")
                except Exception:  # pragma: no cover - flag store best-effort
                    log.warning("email: could not mark message %s seen", num)
                # The allowlist downstream trusts the From address verbatim, but
                # an IMAP From is unauthenticated and trivially forgeable. If the
                # receiving server evaluated SPF/DKIM and it explicitly failed,
                # the From is forged -- drop it before it can impersonate an
                # allowlisted sender. (Marked \Seen above so it isn't re-fetched.)
                if _authentication_verdict(m) == "fail":
                    log.warning(
                        "email: rejecting message with failed SPF/DKIM from=%s",
                        from_addr,
                    )
                    continue
                attachments = self._extract_attachments(m)
                if from_addr and (body or attachments):
                    out.append((from_addr, subject, body, attachments))
        return out

    def _extract_body(self, msg) -> str:
        """Plain text of one message, TRIMMED for context entry.

        The body becomes goal text the whole swarm reads, so per-message noise
        is dropped here: quoted-reply history (every reply in a thread carries
        the entire prior thread), the signature block, and raw HTML markup
        (tags/CSS are 3-10x the visible text). text/plain is preferred; an
        HTML-only message falls back to stripped HTML instead of being lost.
        """
        if msg.is_multipart():
            html_fallback = ""
            for part in msg.walk():
                ctype = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not isinstance(payload, (bytes, bytearray)):
                    continue
                if ctype == "text/plain":
                    return _trim_reply_noise(
                        payload.decode(errors="replace").strip())
                if ctype == "text/html" and not html_fallback:
                    html_fallback = _strip_html(payload.decode(errors="replace"))
            return _trim_reply_noise(html_fallback)
        payload = msg.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode(errors="replace").strip()
        else:
            text = str(payload or "").strip()
        if (msg.get_content_type() or "").lower() == "text/html":
            text = _strip_html(text)
        return _trim_reply_noise(text)

    async def send(self, user_id: str, text: str, subject: str = "Bjerken and Day") -> None:
        await asyncio.to_thread(self._send_sync, user_id, text, subject)

    def _send_sync(self, to_addr: str, text: str, subject: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.smtp_user
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(text)
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.login(self.smtp_user, self.smtp_password)
            smtp.send_message(msg)

    async def stop(self) -> None:
        self._stop = True
