"""Partner fleet registry — the client deployments a partner operates.

A partner (an advisory firm running Lightwork agents for its clients) tracks
each client deployment here: name, base URL, an optional bearer token, and
the white-label theme it runs. Probe results (health, latency, version, the
agent's own counted value ledger) are stored alongside so the console shows
live fleet state without a background poller.

JSON on disk under the tenant-aware data dir; atomic writes under a process
lock. Tokens live in this file — the API never echoes them back out.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from maverick.paths import data_dir

_LOCK = threading.Lock()


def _path() -> Path:
    return data_dir("partner") / "tenants.json"


def _read() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _write(rows: list[dict]) -> None:
    # Rows carry per-tenant bearer tokens: private directory + 0600 file,
    # atomic like the other credential-bearing stores.
    from maverick.file_lock import atomic_write_text, ensure_private_directory
    p = _path()
    ensure_private_directory(p.parent)
    atomic_write_text(p, json.dumps(rows, indent=2), mode=0o600)


def list_tenants() -> list[dict]:
    with _LOCK:
        return _read()


def upsert_tenant(*, name: str, base_url: str, token: str = "",
                  theme: str = "", notes: str = "",
                  tenant_id: str = "") -> dict:
    with _LOCK:
        rows = _read()
        row = next((r for r in rows if r["id"] == tenant_id), None)
        if row is None:
            row = {"id": uuid.uuid4().hex[:10],
                   "created_at": time.time(), "last_check": None}
            rows.append(row)
        row.update({"name": name.strip(), "base_url": base_url.rstrip("/"),
                    "token": token, "theme": theme.strip(),
                    "notes": notes.strip()})
        _write(rows)
        return row


def delete_tenant(tenant_id: str) -> bool:
    with _LOCK:
        rows = _read()
        kept = [r for r in rows if r["id"] != tenant_id]
        if len(kept) == len(rows):
            return False
        _write(kept)
        return True


def record_check(tenant_id: str, result: dict) -> None:
    with _LOCK:
        rows = _read()
        for r in rows:
            if r["id"] == tenant_id:
                r["last_check"] = result
                _write(rows)
                return


def public_row(row: dict) -> dict:
    """The API view: everything except the bearer token."""
    out = {k: v for k, v in row.items() if k != "token"}
    out["has_token"] = bool(row.get("token"))
    return out
