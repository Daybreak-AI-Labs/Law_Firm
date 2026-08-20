# maverick-dashboard

Local law-firm web dashboard for Maverick. The retained surface is deliberately
small and matter-bound:

- Matter intake, party records, membership, jurisdiction, and per-matter egress.
- Authenticated goal creation, status, attachments, pending questions, and cancel.
- Attorney signoff, feedback, deliverable editing/history, and revocable sharing.
- Matter-scoped chat and deliverable views.
- Audit tail/search plus the firm-wide halt control.

The inherited Skills, Facts, provider/tool/channel inventory, external-plugin,
MCP, marketplace, billing, fleet, workflow-builder, and generic admin pages are
not part of this package.

## Design

- FastAPI + Jinja2. No React, no build step.
- Server binds to `127.0.0.1` by default.
- To bind publicly (`--host 0.0.0.0`), set `MAVERICK_DASHBOARD_TOKEN`
  and send `Authorization: Bearer <token>`. Query-token auth was
  removed in the council security pass because it leaks via Referer
  and access logs.
- Baseline browser security headers (X-Frame-Options DENY,
  X-Content-Type-Options nosniff, Referrer-Policy same-origin,
  Cross-Origin-Opener-Policy same-origin) are applied to every response.
- WorldModel is held as a singleton per DB path so each request
  doesn't reopen SQLite + reapply migrations.

## Run

```bash
pip install -e ./packages/maverick-dashboard
maverick-dashboard          # listens on http://127.0.0.1:8765
```

Or via the core CLI:

```bash
maverick dashboard
```
