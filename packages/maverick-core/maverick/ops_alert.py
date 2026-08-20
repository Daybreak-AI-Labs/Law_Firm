"""Local-only structural alerts for deployment safety events.

The retained firm runtime has no automatic notification transport. Safety
controls still emit a high-severity local log record, but arbitrary event
content is never logged and this module performs no network I/O.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

_EVENT_FIELDS = {
    "killswitch_tripped": {"source", "reason_bytes", "reason_sha256"},
    "provider_cost_cap_exhausted": {
        "provider",
        "spent_dollars",
        "cap_dollars",
        "period",
    },
}
_LEVELS = {
    "critical": logging.CRITICAL,
    "high": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
}
_SAFE_SOURCES = {"manual", "operator", "system", "test"}
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_fields(event: str, fields: Mapping[str, Any] | None) -> dict[str, Any]:
    allowed = _EVENT_FIELDS.get(event, set())
    source = fields if isinstance(fields, Mapping) else {}
    result: dict[str, Any] = {}
    for name in allowed:
        value = source.get(name)
        if name == "source" and isinstance(value, str) and value in _SAFE_SOURCES:
            result[name] = value
        elif name == "provider" and isinstance(value, str) and _TOKEN_RE.fullmatch(value):
            result[name] = value
        elif name == "period" and isinstance(value, str) and _PERIOD_RE.fullmatch(value):
            result[name] = value
        elif name == "reason_sha256" and isinstance(value, str) and _SHA256_RE.fullmatch(value):
            result[name] = value
        elif name == "reason_bytes" and isinstance(value, int) and not isinstance(value, bool):
            result[name] = max(0, value)
        elif name in {"spent_dollars", "cap_dollars"} and isinstance(value, (int, float)):
            numeric = float(value)
            if math.isfinite(numeric):
                result[name] = round(max(0.0, numeric), 6)
    return result


def alert(
    event: str,
    *,
    severity: str = "high",
    fields: Mapping[str, Any] | None = None,
) -> bool:
    """Write one privacy-minimized local alert and never perform egress."""
    event_name = event if isinstance(event, str) and event in _EVENT_FIELDS else "unknown"
    severity_name = severity if severity in _LEVELS else "high"
    record: dict[str, Any] = {
        "event": event_name,
        "severity": severity_name,
        "fields": _safe_fields(event_name, fields),
    }
    if event_name == "unknown":
        encoded = str(event).encode("utf-8")
        record["event_bytes"] = len(encoded)
        record["event_sha256"] = hashlib.sha256(encoded).hexdigest()
    try:
        log.log(
            _LEVELS[severity_name],
            "ops_alert %s",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
        return True
    except Exception:  # pragma: no cover - logging must never break a safety gate
        log.warning("ops_alert local logging failed", exc_info=True)
        return False


__all__ = ["alert"]
