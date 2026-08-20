# Maverick operations runbook

Quick triage for the alerts in `prometheus-rules.yaml`. Pair with
`docs/operations.md` (the full operator playbook).

## Health surfaces

| Probe | Meaning |
|-------|---------|
| `GET /livez` | process accepts TCP (liveness) |
| `GET /healthz` | deep check: DB writable, provider key present, runner alive — 503 when degraded |
| `GET /readyz` | readiness: the `/healthz` checks PLUS deep checks (client binding, shield-required, agent-trust registry) — 503 when not ready to serve |
| `GET /metrics` | Prometheus series (bearer-gated when a dashboard token is set) |

## Alert: MaverickDown

1. `systemctl status maverick` — stopped, crash-looping, or resource-limited?
2. `journalctl -u maverick -n 200 --no-pager` (logs are JSON when
   `MAVERICK_LOG_FORMAT=json`; each goal line carries `goal_id`/`conversation_id`).
3. Hit `/healthz` from the host — a 503 body names the failing check
   (DB / provider key / runner). Note: the body redacts paths when a dashboard
   token is set.
4. If the service was killed for memory pressure, raise the host/container
   memory limit only after identifying the workload that exhausted it.

## Alert: MaverickConcurrencySaturated

- Expected under sustained load. `maverick_concurrent_goals` is the global
  in-flight gauge; the cap is `MAVERICK_MAX_CONCURRENT_GOALS`.
- Inspect the active matter goals and worker logs before raising the cap. Keep
  exactly one dashboard/control-plane process for the local firm data root.

## Alert: MaverickSpendSpike

1. Check the dashboard's per-goal spend breakdown to identify the source.
2. Tighten the firm-wide `[budget] max_dollars` cap if needed.
3. A runaway loop usually shows as high `maverick_tokens_total` with few
   `maverick_goals_total{status="done"}` — inspect recent goals.

## Alert: MaverickGoalFailureShare

1. Provider outage? Check `/healthz` provider-key check and provider status.
2. Shield over-blocking? Failures with "Output blocked" in logs.
3. A bad recent deploy? Correlate with the installation/update time and restore
   the last reviewed wheel cohort and encrypted backup if needed.

The alert is based on the current terminal-goal status gauge, not a historical
completion rate. Use the audit/event store for a time-windowed incident analysis.

## Alert: MaverickBackendDegraded

1. Check `/healthz`; it reports the world-model failure without treating a
   successful HTTP scrape as application health.
2. Verify the local database and queue-store paths and permissions.
3. Do not silence this as `MaverickDown`: the process is up but can be returning
   empty operational data while client work is unavailable.

## Upgrades

- Migrations are forward-only and run in one SQLite transaction. Automatic
  plaintext rollback snapshots are intentionally disabled. Before an upgrade,
  stop writers and run `maverick backup create [OUTPUT]` to make an authenticated
  encrypted recovery archive; restore it with `maverick backup restore` if needed.
- Online-safe migrations only are allowed through CI
  (`python -m maverick.schema_migrations --ci`), so a rolling upgrade should not
  require downtime — but take a DB backup before major version jumps.

## Client-data lifecycle

- Export an authorized subject's records with `maverick export-user`.
- Erase authorized subject records with `maverick erase`, then verify the
  signed receipt with `maverick erase-verify`.
