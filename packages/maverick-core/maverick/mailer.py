"""Platform outbound mail — system emails the *product* sends (invite links).

Distinct from the agent-facing email tool (:mod:`maverick.tools.email_tool`,
which an agent drives as a capability) and the email *channel* (inbound IMAP +
conversational replies): this is the small, boring sender for platform
notices — an invite link, and later password-reset-style flows. It deliberately
**reuses the same ``[email]`` config / env the email tool documents**
(``EMAIL_USER`` / ``EMAIL_APP_PASSWORD`` / ``EMAIL_SMTP_HOST`` /
``EMAIL_SMTP_PORT``), so a deployment configures ONE sending account and both
surfaces work — no second credential section to drift (kernel rule 5: the knob
already exists, as does its wizard step).

stdlib only (``smtplib`` + ``email.message``), no new deps. Same safety rails
as the tool: CR/LF header-injection rejection, the ``MAVERICK_EMAIL_DISABLE``
kill switch, SSL on port 465 / STARTTLS otherwise, bounded timeout. Failures
raise :class:`MailerError` with a compact reason — callers own the fail-soft
decision (an invite that can't be emailed still shows its copyable link).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SMTP_TIMEOUT = 30.0


class MailerError(RuntimeError):
    """A send failed (bad config, refused headers, SMTP error)."""


def _cfg(key: str, env: str, default: str = "") -> str:
    """Env-first lookup, then the ``[email]`` config table (same precedence as
    the email tool, so one account configuration serves both)."""
    import os
    val = os.environ.get(env, "").strip()
    if val:
        return val
    try:
        from .config import load_config
        cfg = (load_config() or {}).get("email") or {}
        return str(cfg.get(key, default) or default).strip()
    except Exception:  # noqa: BLE001 - config trouble => act unconfigured
        return default


def configured() -> bool:
    """True when a sending account is set up (user + app password present) and
    the kill switch is off. The invite flow checks this to decide between
    "emailed" and "copy this link" UX — never an error path."""
    import os
    if os.environ.get("MAVERICK_EMAIL_DISABLE") == "1":
        return False
    return bool(_cfg("user", "EMAIL_USER") and _cfg("app_password", "EMAIL_APP_PASSWORD"))


def send(to: str, subject: str, body: str, *, transport=None) -> None:
    """Send a plain-text platform email. Raises :class:`MailerError` on any
    problem; never returns partial success. ``transport`` is injectable for
    tests: a callable ``(host, port, user, password, msg) -> None``."""
    import os
    if os.environ.get("MAVERICK_EMAIL_DISABLE") == "1":
        raise MailerError("email send disabled by MAVERICK_EMAIL_DISABLE=1")
    user = _cfg("user", "EMAIL_USER")
    pw = _cfg("app_password", "EMAIL_APP_PASSWORD")
    if not user or not pw:
        raise MailerError("no sending account configured "
                          "(EMAIL_USER + EMAIL_APP_PASSWORD / [email])")
    host = _cfg("smtp_host", "EMAIL_SMTP_HOST", "smtp.gmail.com")
    port_raw = _cfg("smtp_port", "EMAIL_SMTP_PORT", "465")
    try:
        port = int(port_raw)
    except ValueError as e:
        raise MailerError(f"bad smtp_port: {port_raw!r}") from e
    to = (to or "").strip()
    subject = (subject or "").strip()
    if not to or not subject:
        raise MailerError("send requires a recipient and a subject")
    # Reject CR/LF in header fields: a newline smuggles extra headers (e.g. a
    # hidden Bcc:) into the message. Same guard as the email tool.
    for field, val in (("to", to), ("subject", subject)):
        if "\r" in val or "\n" in val:
            raise MailerError(f"newline in email `{field}` (header injection blocked)")

    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    sender = transport or _smtp_transport
    try:
        sender(host, port, user, pw, msg)
    except MailerError:
        raise
    except Exception as e:  # noqa: BLE001 - surface a compact, typed failure
        raise MailerError(f"smtp send failed: {type(e).__name__}: {e}") from e
    log.info("mailer.send: %s subject=%r", to, subject)


def _smtp_transport(host: str, port: int, user: str, pw: str, msg) -> None:
    import smtplib
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
