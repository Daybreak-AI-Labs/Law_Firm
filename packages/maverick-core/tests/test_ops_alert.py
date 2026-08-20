import hashlib
import inspect
import logging
from contextlib import nullcontext
from pathlib import Path

import pytest
from maverick import ops_alert

CORE_PACKAGE = Path(__file__).resolve().parents[1] / "maverick"


def test_outbound_notification_transports_are_not_shipped() -> None:
    retired = ("notifications.py", "notification_batcher.py", "push_v2.py")
    assert not [name for name in retired if (CORE_PACKAGE / name).exists()]


def test_alert_is_local_only_and_drops_unapproved_fields(caplog) -> None:
    secret = "DISTINCTIVE CLIENT ALERT TEXT"  # pragma: allowlist secret
    with caplog.at_level(logging.CRITICAL, logger="maverick.ops_alert"):
        assert ops_alert.alert(
            "killswitch_tripped",
            severity="critical",
            fields={
                "source": "operator",
                "reason_bytes": len(secret.encode("utf-8")),
                "reason_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                "raw_reason": secret,
            },
        )

    assert "killswitch_tripped" in caplog.text
    assert secret not in caplog.text
    source = inspect.getsource(ops_alert)
    assert "httpx" not in source
    assert "notifications" not in source


def test_unknown_event_is_digest_only(caplog) -> None:
    secret = "client-name-in-an-untrusted-event"  # pragma: allowlist secret
    expected = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    with caplog.at_level(logging.ERROR, logger="maverick.ops_alert"):
        assert ops_alert.alert(secret)

    assert secret not in caplog.text
    assert expected in caplog.text


def test_malformed_structural_field_is_dropped(caplog) -> None:
    with caplog.at_level(logging.CRITICAL, logger="maverick.ops_alert"):
        assert ops_alert.alert(
            "killswitch_tripped",
            severity="critical",
            fields={"source": ["not", "a", "token"]},
        )

    assert '"fields":{}' in caplog.text


def test_killswitch_reason_never_enters_log_audit_or_alert(monkeypatch, caplog) -> None:
    from maverick import audit, killswitch

    reason = "DISTINCT CLIENT MATTER REASON"
    expected = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    audits: list[tuple[str, dict]] = []
    alerts: list[tuple[str, dict]] = []
    monkeypatch.setattr(killswitch, "_in_process_halt", None)
    monkeypatch.setattr(killswitch, "_authority_barrier", nullcontext)
    monkeypatch.setattr(
        audit,
        "audit_event",
        lambda kind, **payload: audits.append((kind, payload)) or True,
    )
    monkeypatch.setattr(
        ops_alert,
        "alert",
        lambda event, **kwargs: alerts.append((event, kwargs)) or True,
    )

    try:
        with caplog.at_level(logging.WARNING, logger="maverick.killswitch"):
            killswitch.halt(reason, source="operator")
    finally:
        monkeypatch.setattr(killswitch, "_in_process_halt", None)

    assert reason not in caplog.text
    assert reason not in repr(audits)
    assert reason not in repr(alerts)
    assert expected in caplog.text
    assert audits[0][1] == {
        "source": "operator",
        "reason_bytes": len(reason.encode("utf-8")),
        "reason_sha256": expected,
    }
    assert alerts[0] == (
        "killswitch_tripped",
        {
            "severity": "critical",
            "fields": {
                "source": "operator",
                "reason_bytes": len(reason.encode("utf-8")),
                "reason_sha256": expected,
            },
        },
    )


def test_provider_cap_alerts_once_per_period(monkeypatch) -> None:
    from maverick import provider_cost_cap as cap

    alerts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ops_alert,
        "alert",
        lambda event, **kwargs: alerts.append((event, kwargs)) or True,
    )
    cap._alerted.clear()
    monkeypatch.setattr(
        cap,
        "check",
        lambda provider, **kw: cap.CapStatus(
            allowed=False,
            spent=10.0,
            cap=5.0,
            remaining=0.0,
        ),
    )

    with pytest.raises(cap.ProviderCapExceeded):
        cap.enforce("anthropic", now=0)
    with pytest.raises(cap.ProviderCapExceeded):
        cap.enforce("anthropic", now=0)

    assert alerts == [
        (
            "provider_cost_cap_exhausted",
            {
                "severity": "critical",
                "fields": {
                    "provider": "anthropic",
                    "spent_dollars": 10.0,
                    "cap_dollars": 5.0,
                    "period": "1970-01-01",
                },
            },
        )
    ]
