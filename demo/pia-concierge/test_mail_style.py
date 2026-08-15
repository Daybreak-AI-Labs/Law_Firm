"""The branded outbound mail: composer, backend seam, and inbox capture.

mail_style dresses the agent's plain text as a branded text/html alternative;
backend.send_email carries BOTH parts (the plain body stays verbatim — other
tests assert on it); mailsink captures the pair; /mail renders the HTML in an
inert sandboxed iframe.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
from email.message import EmailMessage
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# Same collision hygiene as the other loaders in this suite: the agent SKUs
# share module names, so purge before importing fresh (see test_standalone).
_SHARED_MODULES = ("backend", "capabilities", "license_kit", "mailsink",
                   "value_ledger", "store", "pia_engine", "dsar_engine",
                   "ot_mock", "onetrust_client", "notice_check",
                   "contract_guard", "serve_standalone", "mail_style")


def _purge_shared_modules():
    for _m in _SHARED_MODULES:
        sys.modules.pop(_m, None)


def _load(name: str):
    """Import a concierge module from this directory (cached if present)."""
    sys.path.insert(0, str(HERE))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(HERE))


# --------------------------------------------------------------------------- #
# The composer: escaping + linkify + brand furniture.
# --------------------------------------------------------------------------- #
def test_email_html_escapes_injection_and_linkifies():
    _purge_shared_modules()
    mail_style = _load("mail_style")
    body = ("Vendor says: <script>alert('pwn')</script> & more\n"
            "second line\n\n"
            "Open http://127.0.0.1:8890/intake/abc?x=1&y=2 to continue.")
    doc = mail_style.email_html("Review <Initech>", body)
    assert "<script>" not in doc                          # inert, escaped
    assert "&lt;script&gt;alert(&#x27;pwn&#x27;)&lt;/script&gt;" in doc
    assert "Review &lt;Initech&gt;" in doc                # subject escaped too
    assert '<a href="http://127.0.0.1:8890/intake/abc?x=1&amp;y=2"' in doc
    assert "second line" in doc and "<br>" in doc         # single \n -> <br>
    assert doc.count("<p ") == 2                          # blank line -> new <p>
    assert "Lightwork &middot; Daybreak Labs" in doc      # wordmark
    assert "Sent by the Lightwork Privacy Concierge" in doc


# --------------------------------------------------------------------------- #
# The message shape send_email composes on the platform path.
# --------------------------------------------------------------------------- #
def test_composed_message_is_multipart_alternative_with_verbatim_plain():
    _purge_shared_modules()
    mail_style = _load("mail_style")
    subject = "Privacy review needed"
    body = "Hello Alice,\n\nPlease open http://127.0.0.1:8890/intake/x today.\n"
    msg = EmailMessage()
    msg["From"] = "privacy-office@company.com"
    msg["To"] = "alice@example.test"
    msg["Subject"] = subject
    msg.set_content(body)                                 # first part: plain
    msg.add_alternative(mail_style.email_html(subject, body), subtype="html")
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content() == body                 # byte-identical
    assert "Lightwork &middot; Daybreak Labs" in parts[1].get_content()


# --------------------------------------------------------------------------- #
# The sink keeps both renderings.
# --------------------------------------------------------------------------- #
def test_store_captures_both_plain_and_html_parts():
    _purge_shared_modules()
    mailsink = _load("mailsink")
    msg = EmailMessage()
    msg["From"] = "privacy-office@company.com"
    msg["To"] = "alice@example.test"
    msg["Subject"] = "Both parts"
    msg.set_content("the plain body\n")
    msg.add_alternative("<p>the html body</p>", subtype="html")
    mailsink._store(msg.as_bytes())
    cap = mailsink.INBOX[0]
    assert cap.body == "the plain body\n"
    assert "<p>the html body</p>" in cap.html


def test_store_plain_only_message_leaves_html_empty():
    _purge_shared_modules()
    mailsink = _load("mailsink")
    msg = EmailMessage()
    msg["From"] = "a@example.test"
    msg["To"] = "b@example.test"
    msg["Subject"] = "Plain"
    msg.set_content("just text\n")
    mailsink._store(msg.as_bytes())
    cap = mailsink.INBOX[0]
    assert cap.body == "just text\n"
    assert cap.html == ""


# --------------------------------------------------------------------------- #
# The real seam, standalone SKU: send_email -> INBOX, and /mail rendering.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sa_app(tmp_path_factory):
    data = tmp_path_factory.mktemp("mail-style-data")
    os.environ["PIA_STANDALONE"] = "1"
    os.environ["PIA_DATA_DIR"] = str(data)
    sys.path.insert(0, str(HERE))
    _purge_shared_modules()
    sys.modules.pop("app", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "pia_mail_style_app", HERE / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pia_mail_style_app"] = module
        spec.loader.exec_module(module)
        # Cache the optional composer so send_email's by-name import resolves
        # after this fixture drops the demo dir from sys.path.
        importlib.import_module("mail_style")
        yield module
    finally:
        sys.path.remove(str(HERE))
        os.environ.pop("PIA_STANDALONE", None)


def test_standalone_send_email_delivers_branded_html_to_inbox(sa_app):
    sa_app.INBOX.clear()
    body = "Hi Bob,\n\nStart here: http://127.0.0.1:8890/intake/i-42\nThanks."
    result = sa_app.backend.send_email("bob@example.test", "Action <needed>", body)
    assert result == "ok (standalone: delivered to inbox)"
    cap = sa_app.INBOX[0]
    assert cap.body == body                       # the agent's text, verbatim
    assert "Action &lt;needed&gt;" in cap.html    # branded + escaped
    assert '<a href="http://127.0.0.1:8890/intake/i-42"' in cap.html


def test_mail_page_renders_html_in_sandboxed_iframe(sa_app):
    from starlette.testclient import TestClient
    sa_app.INBOX.clear()
    body = "Hello,\n\nOpen http://127.0.0.1:8890/intake/i-7 to begin."
    sa_app.backend.send_email("alice@example.test", "Privacy intake", body)
    page = TestClient(sa_app.app).get("/mail")
    assert page.status_code == 200
    text = page.text
    assert 'sandbox=""' in text and 'id="r-html"' in text   # inert frame
    assert "MAIL_HTML" in text and "MAIL_FROM" in text
    assert "\\u003ctable" in text     # branded html JSON-escaped, never raw


# --------------------------------------------------------------------------- #
# The real seam, platform SKU: a genuine SMTP+STARTTLS delivery into the sink
# carries both parts, plain part intact.
# --------------------------------------------------------------------------- #
def test_platform_send_email_delivers_multipart_over_real_smtp(monkeypatch):
    monkeypatch.delenv("PIA_STANDALONE", raising=False)
    monkeypatch.delenv("MAVERICK_EMAIL_DISABLE", raising=False)
    pytest.importorskip("maverick.world_model")
    _purge_shared_modules()
    mailsink = _load("mailsink")
    mail_style = _load("mail_style")
    backend = _load("backend")
    assert backend.STANDALONE is False

    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(mailsink.start_mailsink("127.0.0.1", 0))
    port = server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("EMAIL_USER", "privacy-office@company.com")
        monkeypatch.setenv(
            "EMAIL_APP_PASSWORD", "demo-app-password")  # pragma: allowlist secret
        monkeypatch.setenv("EMAIL_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("EMAIL_SMTP_PORT", str(port))  # non-465 -> STARTTLS
        body = "Hello Alice,\n\nOpen http://127.0.0.1:8890/intake/x now.\n"
        result = backend.send_email("alice@example.test", "Privacy review", body)
        assert result.startswith("sent to alice@example.test")
        cap = mailsink.INBOX[0]
        assert cap.body.replace("\r\n", "\n") == body     # plain part intact
        want = mail_style.email_html("Privacy review", body)
        assert cap.html.replace("\r\n", "\n").rstrip("\n") == want.rstrip("\n")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()
