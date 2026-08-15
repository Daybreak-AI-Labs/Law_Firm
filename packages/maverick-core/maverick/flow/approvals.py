"""Actionable approvals: signed approve/reject links a human can click from a
channel -- Slack, Teams, email, SMS -- to open the exact flow approval pause.

A link carries a stateless HMAC token over the run, decision, expiry, and
the exact approval pause (cursor/prompt/run timestamp). The
dashboard verifies the token and separately requires an authenticated,
authorized approver identity before resuming, so a forwarded token is not a
bearer decision capability. The token reuses the control-plane webhook secret
(:func:`maverick.webhooks.inbound_secret`) so operators configure one key, and
it FAILS CLOSED -- with no secret configured, no token is minted and no link is
posted (the human falls back to the dashboard).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

_DEFAULT_TTL = 7 * 24 * 3600      # a week to act on an approval
_DECISIONS = ("approved", "rejected")


def _secret() -> str | None:
    from ..webhooks import inbound_secret
    return inbound_secret()


def _secret_for_tenant(tenant: str) -> str | None:
    """Resolve the signing key inside the token's tenant scope."""
    if not tenant:
        return _secret()
    try:
        from ..tenant.registry import assert_tenant_active
        assert_tenant_active(tenant)
        from ..paths import reset_tenant, set_tenant
        token = set_tenant(tenant)
    except Exception:
        return None
    try:
        return _secret()
    finally:
        reset_tenant(token)


def _decode_body(body: str) -> dict | None:
    if not body or len(body) > 12_000:
        return None
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sign(material: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def mint_token(run_id: str, decision: str, *, cursor: str | None = None,
               prompt: str = "", updated: float | None = None,
               assignee: str = "", owner: str = "",
               tenant: str | None = None,
               ttl: int = _DEFAULT_TTL, now: float | None = None) -> str | None:
    """A signed, self-expiring token authorising ONE decision on ONE approval
    pause. ``None`` when no secret is configured or inputs are invalid (fail closed)."""
    if tenant is None:
        from ..paths import current_tenant_id
        tenant = current_tenant_id() or ""
    tenant = str(tenant or "")
    secret = _secret_for_tenant(tenant)
    if (not secret or decision not in _DECISIONS or not run_id or not cursor
            or updated is None):
        return None
    exp = int((now if now is not None else time.time()) + ttl)
    raw = json.dumps({"r": str(run_id), "d": decision, "e": exp,
                      "c": str(cursor)[:256], "p": str(prompt)[:2000],
                      "u": float(updated), "t": tenant,
                      "a": str(assignee)[:500], "o": str(owner)[:500]},
                     separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{body}.{_sign(body.encode('ascii'), secret)}"


def verify_token(token: str, *, now: float | None = None) -> dict | None:
    """Return the approval claim for a valid, unexpired, correctly-signed token;
    ``None`` otherwise (no secret / bad format / bad signature / expired)."""
    if not token or len(token) > 16_384 or "." not in token:
        return None
    body, _, sig = token.partition(".")
    payload = _decode_body(body)
    if payload is None:
        return None
    # The tenant is untrusted until the signature verifies, but it is safe to
    # use solely as a key-selection namespace. No run data is read beforehand.
    tenant = str(payload.get("t") or "")
    secret = _secret_for_tenant(tenant)
    try:
        valid_sig = bool(secret) and hmac.compare_digest(
            sig, _sign(body.encode("ascii"), secret or ""))
    except (UnicodeEncodeError, ValueError):
        return None
    if not valid_sig:
        return None
    run_id, decision, exp = payload.get("r"), payload.get("d"), payload.get("e")
    cursor, updated = payload.get("c"), payload.get("u")
    if (decision not in _DECISIONS or not run_id or not cursor
            or not isinstance(exp, (int, float)) or not isinstance(updated, (int, float))):
        return None
    if (now if now is not None else time.time()) > exp:
        return None
    return {"run_id": str(run_id), "decision": str(decision),
            "cursor": str(cursor), "prompt": str(payload.get("p", "")),
            "updated": float(updated), "tenant": tenant,
            "assignee": str(payload.get("a") or ""),
            "owner": str(payload.get("o") or "")}


def approval_links(base_url: str, run_id: str, *, cursor: str | None = None,
                   prompt: str = "", updated: float | None = None,
                   assignee: str = "", owner: str = "",
                   tenant: str | None = None,
                   ttl: int = _DEFAULT_TTL, now: float | None = None) -> dict | None:
    """The approve/reject URLs for a run (pointing at ``/flow/approve``), or
    ``None`` when unsigned (no secret) so the caller can fall back to a plain
    notification."""
    approve = mint_token(run_id, "approved", cursor=cursor, prompt=prompt,
                         updated=updated, assignee=assignee, owner=owner,
                         tenant=tenant, ttl=ttl, now=now)
    reject = mint_token(run_id, "rejected", cursor=cursor, prompt=prompt,
                        updated=updated, assignee=assignee, owner=owner,
                        tenant=tenant, ttl=ttl, now=now)
    if not (approve and reject):
        return None
    base = base_url.rstrip("/")
    return {"approve": f"{base}/flow/approve?token={approve}",
            "reject": f"{base}/flow/approve?token={reject}"}


__all__ = ["mint_token", "verify_token", "approval_links"]
