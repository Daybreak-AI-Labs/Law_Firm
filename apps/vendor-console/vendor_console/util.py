"""Small request helpers shared across route modules."""
from __future__ import annotations

import hashlib
import os

from fastapi import Request


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def client_ip(request: Request) -> str:
    """Peer IP. ``X-Forwarded-For`` is trusted ONLY when the operator opts in
    (``VENDOR_CONSOLE_TRUST_PROXY=1``) — otherwise a client could forge the IP
    written into the tamper-evident audit log. Defaults to the direct peer."""
    if _truthy("VENDOR_CONSOLE_TRUST_PROXY"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()   # rightmost = nearest trusted hop
    return request.client.host if request.client else ""


def secure_cookie(request: Request) -> bool:
    """Whether to set the session cookie's ``Secure`` flag. **Fail-closed**: on
    for every real host, off only for loopback dev or an explicit
    ``VENDOR_CONSOLE_INSECURE`` opt-out — so a deployment behind a TLS proxy
    never silently issues an admin cookie that rides plaintext."""
    if _truthy("VENDOR_CONSOLE_INSECURE"):
        return False
    return (request.url.hostname or "") not in _LOOPBACK


def same_origin(request: Request) -> bool:
    """CSRF defence-in-depth: reject a cross-site Origin on a mutating request.
    A missing Origin (non-browser client, same-origin navigation) is allowed —
    the SameSite=Lax cookie is the primary guard; this backs it up."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    from urllib.parse import urlparse
    return urlparse(origin).netloc == request.url.netloc
