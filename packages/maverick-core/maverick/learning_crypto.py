"""Strict at-rest codec for retained client-derived learning stores.

The general encryption helpers deliberately tolerate pre-migration plaintext.
Firm learning cannot: reflexions, dream insights, user notes, rehearsals, and
distilled skills are prompt-bearing cross-run memory. In the secure posture a
record is usable only when it carries an authenticated at-rest seal.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def protected_learning_enabled() -> bool:
    """Whether client-derived learning must be authenticated ciphertext."""
    try:
        from .security_defaults import secure_by_default

        secure = bool(secure_by_default())
    except Exception:
        secure = True
    try:
        from .crypto_at_rest import at_rest_enabled

        encrypted = bool(at_rest_enabled())
    except Exception:
        encrypted = secure
    return secure or encrypted


def encode_text(text: str) -> str:
    """Return a disk-safe token, raising if required encryption is unavailable."""
    value = str(text)
    if not protected_learning_enabled():
        return value
    from .crypto_at_rest import seal_to_str

    return seal_to_str(value)


def decode_text(value: str) -> str | None:
    """Open one token; withhold plaintext, corrupt, or wrong-key data."""
    raw = str(value)
    from .crypto_at_rest import is_sealed_str, unseal_from_str

    sealed = is_sealed_str(raw)
    if protected_learning_enabled() and not sealed:
        return None
    if not sealed:
        return raw
    try:
        return unseal_from_str(raw)
    except Exception:
        return None


def encode_json_record(record: Mapping[str, Any]) -> str:
    """Serialize and seal one complete NDJSON record."""
    return encode_text(json.dumps(dict(record), default=str, sort_keys=True))


def decode_json_record(value: str) -> dict[str, Any] | None:
    """Authenticate, decrypt, and parse one complete NDJSON record."""
    text = decode_text(value.strip())
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "protected_learning_enabled",
    "encode_text",
    "decode_text",
    "encode_json_record",
    "decode_json_record",
]
