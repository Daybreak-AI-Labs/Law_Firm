"""Branded HTML for the concierge's outbound email.

The agent writes plain text (other tests assert on those bodies verbatim);
this module dresses the SAME text as the message a requester actually sees.
Email-client-safe by construction: table-based outer layout, inline styles
only, no external assets. Body text is HTML-escaped BEFORE the linkifier
runs, so vendor-supplied strings render inert.
"""
from __future__ import annotations

import html
import re

# The demo's document brand.
NAVY = "#21426F"
ACCENT = "#4F7BD0"
INK = "#1A2433"
MUTED = "#5B6575"
HAIRLINE = "#D9DEE8"
_FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
         "Arial, sans-serif")

# Runs over ESCAPED text: raw `<`/`>` cannot occur inside a match, and an
# `&amp;` in a query string is already correct HTML for an href attribute.
_URL = re.compile(r"https?://[^\s<>]+")
# Sentence punctuation (and escaped brackets/quotes) that ends prose, not URLs.
_TRAILING = (".", ",", ";", ":", "!", "?", ")", "&gt;", "&lt;", "&quot;", "&#x27;")


def _link(match: re.Match[str]) -> str:
    url, trail = match.group(0), ""
    while url.endswith(_TRAILING):
        for stop in _TRAILING:
            if url.endswith(stop):
                url, trail = url[: -len(stop)], stop + trail
                break
    return f'<a href="{url}" style="color:{ACCENT};">{url}</a>{trail}'


def _paragraphs(body: str) -> str:
    blocks = []
    for para in re.split(r"\n\s*\n", body.strip()):
        text = _URL.sub(_link, html.escape(para)).replace("\n", "<br>")
        blocks.append(f'<p style="margin:0 0 14px;font-size:15px;'
                      f'line-height:1.6;color:{INK};">{text}</p>')
    return "\n".join(blocks)


def email_html(subject: str, body: str) -> str:
    """The branded text/html alternative for one outbound message."""
    footer = ("Sent by the Lightwork Privacy Concierge &middot; "
              "replies go straight back to the agent")
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F2F4F8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
           style="width:100%;max-width:600px;background:#FFFFFF;
                  border:1px solid {HAIRLINE};border-radius:8px;font-family:{_FONT};">
      <tr><td style="background:{NAVY};border-radius:8px 8px 0 0;padding:22px 28px;">
        <div style="color:#C9D6EC;font-size:11px;font-weight:600;letter-spacing:.14em;
                    text-transform:uppercase;">Lightwork &middot; Daybreak Labs</div>
        <div style="color:#FFFFFF;font-size:19px;font-weight:700;line-height:1.35;
                    padding-top:6px;">{html.escape(subject)}</div>
      </td></tr>
      <tr><td style="padding:26px 28px 12px;">
{_paragraphs(body)}
      </td></tr>
      <tr><td style="padding:14px 28px 22px;border-top:1px solid {HAIRLINE};">
        <div style="color:{MUTED};font-size:12px;line-height:1.5;">{footer}</div>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
