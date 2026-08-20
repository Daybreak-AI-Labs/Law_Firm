"""Explicit operator-mode adapter for legacy self-harness mechanics tests."""
from __future__ import annotations

from collections.abc import Mapping

from maverick import self_harness

TEST_MATTER_ID = 101
TEST_OWNER = "user:test-attorney"


def scoped_records(records):
    """Stamp historical synthetic rows with one exact test-only legal scope."""
    selected = []
    for raw in records or []:
        record = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(record, Mapping):
            selected.append(record)
            continue
        selected.append({
            **record,
            "matter_id": TEST_MATTER_ID,
            "owner": TEST_OWNER,
        })
    return selected


def run_operator_harness(records, **kwargs):
    """Exercise the apply transaction through an explicit approved operator call."""
    kwargs.setdefault("project_id", TEST_MATTER_ID)
    kwargs.setdefault("owner", TEST_OWNER)
    kwargs.setdefault("apply_promotions", True)
    kwargs.setdefault("promotion_authorize", lambda: True)
    return self_harness.run_self_harness(scoped_records(records), **kwargs)
