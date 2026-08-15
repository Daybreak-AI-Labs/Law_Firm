"""Calendar tool: create_event must preserve the absolute time of
tz-aware timestamps.

Regression: DTSTART/DTEND were formatted with strftime('%Y%m%dT%H%M%SZ'),
which ignores tzinfo — an event given as 10:00-05:00 was written to the
CalDAV server as 10:00 UTC (five hours early) while the tool reported
success at the intended time.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _fake_caldav(monkeypatch):
    """Wire a fake caldav module + creds; return the calendar mock."""
    monkeypatch.setenv("CALDAV_URL", "https://cal.test")
    monkeypatch.setenv("CALDAV_USER", "me@test")
    monkeypatch.setenv("CALDAV_PASSWORD", "pw")
    fake_caldav = types.ModuleType("caldav")
    fake_calendar = MagicMock()
    fake_saved = MagicMock()
    fake_saved.url = "https://cal.test/event/1"
    fake_calendar.save_event = MagicMock(return_value=fake_saved)
    fake_principal = MagicMock()
    fake_principal.calendars = MagicMock(return_value=[fake_calendar])
    fake_client = MagicMock()
    fake_client.principal = MagicMock(return_value=fake_principal)
    fake_caldav.DAVClient = MagicMock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, "caldav", fake_caldav)
    return fake_calendar


def test_create_event_converts_utc_offset(monkeypatch):
    cal = _fake_caldav(monkeypatch)
    from maverick.tools.calendar_tool import calendar_tool
    out = calendar_tool().fn({
        "op": "create_event",
        "title": "Standup",
        "start": "2026-07-01T10:00:00-05:00",
        "end": "2026-07-01T11:00:00-05:00",
    })
    assert "created event" in out
    ical = cal.save_event.call_args.args[0]
    assert "DTSTART:20260701T150000Z" in ical  # 10:00-05:00 == 15:00 UTC
    assert "DTEND:20260701T160000Z" in ical
    # The local wall-clock fields mislabeled as UTC must be gone.
    assert "DTSTART:20260701T100000Z" not in ical


def test_create_event_keeps_utc_and_naive_unchanged(monkeypatch):
    cal = _fake_caldav(monkeypatch)
    from maverick.tools.calendar_tool import calendar_tool
    calendar_tool().fn({
        "op": "create_event",
        "title": "A",
        "start": "2026-07-01T10:00:00Z",  # explicit UTC
        "end": "2026-07-01T11:00:00",     # naive == UTC by contract
    })
    ical = cal.save_event.call_args.args[0]
    assert "DTSTART:20260701T100000Z" in ical
    assert "DTEND:20260701T110000Z" in ical
