"""Platform mailer: [email]/env config reuse, header-injection guard, kill
switch, and the injectable transport. See maverick/mailer.py."""
from __future__ import annotations

import pytest
from maverick import mailer


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("EMAIL_USER", "EMAIL_APP_PASSWORD", "EMAIL_SMTP_HOST",
                "EMAIL_SMTP_PORT", "MAVERICK_EMAIL_DISABLE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})


def _creds(monkeypatch, **extra):
    monkeypatch.setenv("EMAIL_USER", "sender@daybreak.example")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "app-pw")  # pragma: allowlist secret
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_unconfigured_by_default():
    assert mailer.configured() is False
    with pytest.raises(mailer.MailerError, match="no sending account"):
        mailer.send("to@x.co", "hi", "body")


def test_env_credentials_configure_it(monkeypatch):
    _creds(monkeypatch)
    assert mailer.configured() is True


def test_config_table_credentials_configure_it(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {
        "email": {"user": "sender@daybreak.example",
                  "app_password": "pw",  # pragma: allowlist secret
                  "smtp_host": "mail.example", "smtp_port": 587}})
    assert mailer.configured() is True
    seen = {}

    def transport(host, port, user, pw, msg):
        seen.update(host=host, port=port, user=user, to=msg["To"])
    mailer.send("to@x.co", "hi", "body", transport=transport)
    assert seen == {"host": "mail.example", "port": 587,
                    "user": "sender@daybreak.example", "to": "to@x.co"}


def test_kill_switch_wins(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setenv("MAVERICK_EMAIL_DISABLE", "1")
    assert mailer.configured() is False
    with pytest.raises(mailer.MailerError, match="disabled"):
        mailer.send("to@x.co", "hi", "body", transport=lambda *a: None)


def test_header_injection_blocked(monkeypatch):
    _creds(monkeypatch)
    boom = []
    for to, subject in (("v@x.co\nBcc: evil@x.co", "hi"),
                        ("v@x.co", "hi\r\nBcc: evil@x.co")):
        with pytest.raises(mailer.MailerError, match="header injection"):
            mailer.send(to, subject, "b", transport=lambda *a: boom.append(1))
    assert not boom                       # transport never reached


def test_smtp_failure_surfaces_as_mailer_error(monkeypatch):
    _creds(monkeypatch)

    def transport(*a):
        raise ConnectionRefusedError("nope")
    with pytest.raises(mailer.MailerError, match="smtp send failed"):
        mailer.send("to@x.co", "hi", "body", transport=transport)


def test_message_content(monkeypatch):
    _creds(monkeypatch)
    got = {}

    def transport(host, port, user, pw, msg):
        got["msg"] = msg
    mailer.send("to@x.co", "Subject line", "the body\n", transport=transport)
    msg = got["msg"]
    assert msg["From"] == "sender@daybreak.example"
    assert msg["Subject"] == "Subject line"
    assert "the body" in msg.get_content()
