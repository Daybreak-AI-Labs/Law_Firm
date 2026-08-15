"""In-process SMTP capture server for the PIA Concierge demo.

The production email tool (maverick.tools.email_tool) sends over SMTP with
STARTTLS + AUTH. This sink speaks exactly that so the *real* tool delivers here
unmodified — a genuine SMTP+STARTTLS+AUTH transaction — with no external binary
(Mailpit/aiosmtpd) required: stdlib asyncio + a throwaway self-signed cert from
`cryptography` (already a platform dependency). Captured messages live in memory
and are rendered by the concierge's /mail inbox.

Demo scaffolding, not a product component.
"""
from __future__ import annotations

import asyncio
import email
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header


@dataclass
class Captured:
    to: str
    sender: str
    subject: str
    body: str
    html: str = ""          # text/html alternative; empty when plain-only
    at: float = field(default_factory=time.time)


# Shared in-memory inbox; the concierge app imports INBOX to render /mail.
INBOX: list[Captured] = []


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _store(raw: bytes) -> None:
    msg = email.message_from_bytes(raw)
    body = html_alt = ""
    if msg.is_multipart():
        # Keep BOTH renderings: text/plain (the agent's words, asserted on by
        # tests) and text/html (the branded alternative /mail displays).
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            text = payload.decode("utf-8", "replace") if payload else ""
            if ctype == "text/plain" and not body:
                body = text
            elif ctype == "text/html" and not html_alt:
                html_alt = text
    else:
        payload = msg.get_payload(decode=True)
        body = payload.decode("utf-8", "replace") if payload else str(msg.get_payload())
    INBOX.insert(0, Captured(
        to=_decode(msg.get("To", "")),
        sender=_decode(msg.get("From", "")),
        subject=_decode(msg.get("Subject", "")),
        body=body,
        html=html_alt,
    ))


def _selfsigned_context() -> ssl.SSLContext | None:
    """A throwaway server cert so STARTTLS works. smtplib's default STARTTLS
    context does not verify the cert, so self-signed is fine for the sink."""
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        certf = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        keyf = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        certf.write(cert.public_bytes(serialization.Encoding.PEM))
        certf.flush()
        keyf.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        keyf.flush()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certf.name, keyf.name)
        return ctx
    except Exception as exc:  # pragma: no cover
        print(f"[mailsink] STARTTLS unavailable ({exc}); plaintext only")
        return None


_TLS_CTX: ssl.SSLContext | None = None


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    async def send(line: str) -> None:
        writer.write((line + "\r\n").encode())
        await writer.drain()

    await send("220 pia-concierge-mailsink ESMTP")
    data_lines: list[bytes] = []
    in_data = False
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            if in_data:
                if raw.rstrip(b"\r\n") == b".":
                    _store(b"".join(data_lines))
                    data_lines = []
                    in_data = False
                    await send("250 OK: queued")
                else:
                    line = raw[1:] if raw.startswith(b"..") else raw
                    data_lines.append(line)
                continue

            verb = raw.split(None, 1)[0].upper() if raw.strip() else b""
            if verb in (b"EHLO", b"HELO"):
                # Advertise STARTTLS + AUTH PLAIN so the real tool's flow works.
                writer.write(b"250-pia-concierge-mailsink\r\n")
                if _TLS_CTX is not None:
                    writer.write(b"250-STARTTLS\r\n")
                writer.write(b"250 AUTH PLAIN LOGIN\r\n")
                await writer.drain()
            elif verb == b"STARTTLS" and _TLS_CTX is not None:
                await send("220 Ready to start TLS")
                await writer.start_tls(_TLS_CTX)
            elif verb == b"AUTH":
                # One-shot PLAIN carries creds inline; LOGIN would need challenges.
                if b"LOGIN" in raw.upper():
                    await send("334 VXNlcm5hbWU6")            # "Username:"
                    await reader.readline()
                    await send("334 UGFzc3dvcmQ6")            # "Password:"
                    await reader.readline()
                await send("235 2.7.0 Authentication successful")
            elif verb == b"MAIL":
                await send("250 OK")
            elif verb == b"RCPT":
                await send("250 OK")
            elif verb == b"DATA":
                in_data = True
                await send("354 End data with <CR><LF>.<CR><LF>")
            elif verb == b"RSET":
                data_lines = []
                in_data = False
                await send("250 OK")
            elif verb == b"NOOP":
                await send("250 OK")
            elif verb == b"QUIT":
                await send("221 Bye")
                break
            else:
                await send("250 OK")
    except Exception:  # pragma: no cover - never crash the demo on a mail hiccup
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_mailsink(host: str = "127.0.0.1", port: int = 1025) -> asyncio.AbstractServer:
    global _TLS_CTX
    _TLS_CTX = _selfsigned_context()
    return await asyncio.start_server(_handle, host, port)
