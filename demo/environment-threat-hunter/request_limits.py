"""Bound and validate untrusted JSON before standalone threat detection."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 512 * 1024
MAX_DEPTH = 8
MAX_OBJECT_KEYS = 64
MAX_ARRAY_ITEMS = 512
MAX_STRING_BYTES = 256 * 1024
MAX_TOTAL_KEYS = 4096
MAX_EVENTS = 256
MAX_EVENT_BYTES = 32 * 1024
MAX_EVENT_KEYS = 256
FORBIDDEN_RAW_KEYS = frozenset(
    {"raw", "raw_event", "raw_events", "raw_payload", "raw_telemetry"}
)
MAX_SIGMA_RULES = 128
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SIGMA_LEVELS = frozenset({"informational", "low", "medium", "high", "critical"})


@dataclass
class PayloadError(ValueError):
    detail: str
    status_code: int = 422

    def __str__(self) -> str:
        return self.detail


def _declared_length(request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise PayloadError("invalid Content-Length header") from exc
    if length < 0:
        raise PayloadError("invalid Content-Length header")
    return length


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80 if scheme == "http" else None


def _validate_json_boundary(request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise PayloadError("Content-Type must be application/json", status_code=415)
    origin = request.headers.get("origin")
    if origin is None:
        return
    parsed = urlsplit(origin)
    request_host = (request.url.hostname or "").lower()
    origin_host = (parsed.hostname or "").lower()
    try:
        origin_port = parsed.port
        request_port = request.url.port
    except ValueError as exc:
        raise PayloadError("browser Origin has an invalid port", status_code=403) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.scheme != request.url.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or bool(parsed.query or parsed.fragment)
        or request_host not in _LOOPBACK_HOSTS
        or origin_host not in _LOOPBACK_HOSTS
        or _effective_port(parsed.scheme, origin_port)
        != _effective_port(request.url.scheme, request_port)
    ):
        raise PayloadError("browser requests must use the same loopback origin", status_code=403)


def _validate_shape(
    value,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
    max_total_keys: int = MAX_TOTAL_KEYS,
) -> None:
    if depth > MAX_DEPTH:
        raise PayloadError(f"JSON nesting exceeds {MAX_DEPTH} levels")
    if counter is None:
        counter = [0]
    if isinstance(value, dict):
        if len(value) > MAX_OBJECT_KEYS:
            raise PayloadError(f"JSON objects may contain at most {MAX_OBJECT_KEYS} keys")
        counter[0] += len(value)
        if counter[0] > max_total_keys:
            raise PayloadError(f"JSON value may contain at most {max_total_keys} keys")
        for key, item in value.items():
            if len(str(key).encode("utf-8")) > 256:
                raise PayloadError("JSON object keys may not exceed 256 bytes")
            _validate_shape(
                item,
                depth=depth + 1,
                counter=counter,
                max_total_keys=max_total_keys,
            )
    elif isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise PayloadError(f"JSON arrays may contain at most {MAX_ARRAY_ITEMS} items")
        for item in value:
            _validate_shape(
                item,
                depth=depth + 1,
                counter=counter,
                max_total_keys=max_total_keys,
            )
    elif isinstance(value, str) and len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise PayloadError(f"JSON strings may not exceed {MAX_STRING_BYTES} bytes")


def reject_raw_fields(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in FORBIDDEN_RAW_KEYS:
                raise PayloadError(f"field {key!r} is not accepted at this boundary")
            reject_raw_fields(item)
    elif isinstance(value, list):
        for item in value:
            reject_raw_fields(item)


def validate_events(events) -> list[dict]:
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise PayloadError("ingested events must be a list of objects")
    if len(events) > MAX_EVENTS:
        raise PayloadError(f"ingested events may contain at most {MAX_EVENTS} items")
    for event in events:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise PayloadError(f"each event may contain at most {MAX_EVENT_BYTES} bytes")
        _validate_shape(event, counter=[0], max_total_keys=MAX_EVENT_KEYS)
        if "bytes_out" in event:
            number = event["bytes_out"]
            if (
                not isinstance(number, (int, float))
                or isinstance(number, bool)
                or not math.isfinite(float(number))
            ):
                raise PayloadError("event field 'bytes_out' must be a finite JSON number")
        if "userIdentity" in event and not isinstance(event["userIdentity"], dict):
            raise PayloadError("event field 'userIdentity' must be an object")
        if "objectRef" in event and not isinstance(event["objectRef"], dict):
            raise PayloadError("event field 'objectRef' must be an object")
        if "sourceIPs" in event and (
            not isinstance(event["sourceIPs"], list)
            or not all(isinstance(item, str) for item in event["sourceIPs"])
        ):
            raise PayloadError("event field 'sourceIPs' must be a list of strings")
    return events


def validate_sigma_rules(value) -> list[dict] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_SIGMA_RULES:
        raise PayloadError(f"sigma_rules must be a list of at most {MAX_SIGMA_RULES} objects")
    for rule in value:
        if not isinstance(rule, dict):
            raise PayloadError("each Sigma rule must be an object")
        for field, limit in (("id", 160), ("title", 300)):
            item = rule.get(field)
            if not isinstance(item, str) or not item.strip() or len(item) > limit:
                raise PayloadError(f"Sigma rule {field} must contain 1 to {limit} characters")
        level = rule.get("level", "medium")
        if not isinstance(level, str) or level.lower() not in _SIGMA_LEVELS:
            raise PayloadError("Sigma rule level is unsupported")
        tags = rule.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 64 or not all(
            isinstance(item, str) and len(item) <= 160 for item in tags
        ):
            raise PayloadError("Sigma rule tags must be a bounded list of strings")
        detection = rule.get("detection")
        if not isinstance(detection, dict):
            raise PayloadError("Sigma rule detection must be an object")
        if set(detection).difference({"selection", "condition"}):
            raise PayloadError("standalone Sigma rules support only detection.selection")
        if detection.get("condition", "selection") != "selection":
            raise PayloadError("standalone Sigma condition must be 'selection'")
        selection = detection.get("selection")
        if not isinstance(selection, dict) or not selection:
            raise PayloadError("Sigma rule selection must be a non-empty object")
        for raw_key, expected in selection.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 256:
                raise PayloadError("Sigma selection fields must be bounded strings")
            _key, separator, modifier = raw_key.partition("|")
            if separator and modifier != "contains":
                raise PayloadError(f"unsupported Sigma modifier: {modifier}")
            candidates = expected if isinstance(expected, list) else [expected]
            if not candidates or len(candidates) > 128 or not all(
                isinstance(item, (str, int, float, bool)) and not isinstance(item, dict)
                for item in candidates
            ):
                raise PayloadError("Sigma selection values must be bounded scalar values")
    return value


async def read_bounded_json(request):
    _validate_json_boundary(request)
    declared = _declared_length(request)
    if declared is not None and declared > MAX_REQUEST_BYTES:
        raise PayloadError(
            f"request body exceeds {MAX_REQUEST_BYTES} bytes",
            status_code=413,
        )
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
            raise PayloadError(
                f"request body exceeds {MAX_REQUEST_BYTES} bytes",
                status_code=413,
            )
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError("request body must be valid JSON") from exc
    _validate_shape(value)
    if not isinstance(value, dict):
        raise PayloadError("request body must be a JSON object")
    return value


def require_expected_revision(value: dict) -> int:
    if "expected_revision" not in value:
        raise PayloadError("expected_revision is required")
    revision = value["expected_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PayloadError("expected_revision must be a non-negative integer")
    return revision


__all__ = [
    "PayloadError",
    "read_bounded_json",
    "reject_raw_fields",
    "require_expected_revision",
    "validate_events",
    "validate_sigma_rules",
]
