# Operations runbook

When something is wrong, start here. Each section is one symptom with a
checked playbook.

## Endpoints

| Endpoint | Auth | What it tells you |
|---|---|---|
| `GET /livez` | none | Process is alive (TCP accepts). |
| `GET /healthz` | none | Deep checks: DB writable, LLM key present, runner alive. Returns 503 if degraded. |
| `GET /readyz` | none | Traffic admission: all `/healthz` checks plus client binding, required Shield, and agent-trust registry checks. Returns 503 when the process is healthy but not safe to receive work. |
| `GET /metrics` | bearer (when set) | Prometheus text format: goals by status, $ spent, tokens, concurrent goals. |
| `GET /api/v1/spend` | bearer | Total spend + recent episode list. |

`MAVERICK_DASHBOARD_TOKEN` gates `/api/v1/*` and `/metrics`. The three
health endpoints (`livez`, `healthz`, `readyz`) and API-documentation
routes (`openapi.json`, `docs`, `redoc`, `docs/oauth2-redirect`) are
always public so monitoring and API discovery work on a locked-down VPS.

Treat `/healthz` and `/readyz` as different signals. A green `/healthz`
with a 503 `/readyz` means the process and its dependencies are alive,
but a deployment safety precondition is missing; keep the instance out of
service and act on the failed readiness check instead of restarting it.

## "The service is hung"

```sh
# Is it accepting connections?
curl -sS http://127.0.0.1:8765/livez
# {"status":"ok"} → process alive. Continue.
# (no response / timeout) → process dead or wedged. Jump to "process died".

# Are the deep checks green?
curl -sS http://127.0.0.1:8765/healthz | jq
# {"status":"degraded","checks":{"db":"fail: ...","llm_key":"ok",...}}
# → look at the failing check and act on its message.

# How many goals are in flight right now?
curl -sS http://127.0.0.1:8765/metrics | grep maverick_concurrent_goals
# maverick_concurrent_goals 2   ← 2/3 slots used; not maxed.
```

If `/healthz` says `db: fail: ... database is locked`, a long-running
write transaction is blocking. Restart the maverick service to release.

## Process died

```sh
# What does systemd say?
systemctl status maverick

# Last 200 lines:
journalctl -u maverick -n 200 --no-pager

# If crash-looping (Restart= triggered too many times), unit enters
# 'failed'. To clear:
systemctl reset-failed maverick
systemctl start maverick
```

The systemd unit (`deploy/vps/maverick.service`) sets
`StartLimitBurst=5` over `StartLimitIntervalSec=300`. After 5 crashes
in 5 minutes the unit enters `failed` rather than respawning forever
and burning API credits.

## Goals stuck in `active` after a crash

This is now handled automatically: on every `maverick dashboard`
startup, `reclaim_orphan_goals()` flips rows
from `active`/`pending` to `blocked` with result
`[process restarted mid-run]`.

If you see active goals that *should* be running, check `/metrics`:
`maverick_concurrent_goals` should be > 0. If it's 0 but rows show
`active`, the reclaim hook didn't fire — open an issue.

## API credits burning unexpectedly

```sh
# Total spend over all time:
curl -sS -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8765/api/v1/spend | jq

# Per-goal breakdown (most recent first):
curl -sS -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8765/api/v1/spend?limit=20" | jq
```

Hard cap: `~/.maverick/config.toml` → `[budget] max_dollars`.

## Encrypted backup and transactional restore

Provision two independent 32-byte secrets in the operator secret manager and
inject them only into the local operator process as
`MAVERICK_BACKUP_SIGNING_KEY` and `MAVERICK_BACKUP_ENCRYPTION_KEY` (hex or
base64). Never place either value below the Maverick data root. Then create and
verify a complete client snapshot:

```sh
maverick backup create /operator-custody/acme-2026-08-17.mvkb
maverick backup verify /operator-custody/acme-2026-08-17.mvkb
```

The snapshot uses SQLite's online backup API, excludes backup/key/restore
transaction directories, signs the bounded manifest, and encrypts the complete
compressed archive with streaming AES-256-GCM. `verify` authenticates and
decrypts the complete envelope into a private temporary file before parsing it.

For disaster recovery, stop the dashboard and workers so no application writer
can race the operation, verify the artifact, then deliberately restore it:

```sh
systemctl stop maverick
maverick backup verify /operator-custody/acme-2026-08-17.mvkb
maverick backup restore /operator-custody/acme-2026-08-17.mvkb
systemctl start maverick
```

Restore prompts for confirmation (automation must pass `--yes` deliberately),
verifies the client/schema boundary, authenticates the whole encrypted archive
before restore staging, and journals the file-set application so a partial
failure rolls back. `--force` overrides only client/schema compatibility; it
never bypasses cryptography. There is no unencrypted or unsigned restore path.

- **Single-writer enforcement:** this is no longer a convention. On startup the
  control plane takes an exclusive advisory lock on `control-plane.lock` in its
  data root and holds it for the process lifetime; a second process refuses to
  start and reports the pid, host and uptime from the atomically published
  `control-plane-holder.json` diagnostic record. This record is observability;
  the OS lock remains the authority. Separating them also keeps diagnostics
  readable on Windows, where byte-range locks deny reads of the locked byte.
  The lock is released by the kernel when the holder exits, so a `Recreate`
  rollout hands over cleanly and there is no stale lock to clear. `/readyz`
  reports the posture
  under `replica_safety`. If the data root sits on a filesystem whose advisory
  locks are a no-op (some NFS and overlay mounts), startup **fails closed**
  rather than assume exclusivity — move the volume, or set
  `MAVERICK_ALLOW_MULTIPLE_CONTROL_PLANES=1` (equivalently
  `[deployment] allow_multiple_control_planes = true`) if you have guaranteed
  single-writer access another way. The override is reported on `/readyz` as
  `unenforced`, never silently honoured.

## Learning-state operations

The learning loops persist state under `~/.maverick/` (reflexions, dreams/,
learned-skills/, *_stats.json). Operational handles:

- **Schedule**: `maverick dream` from cron/systemd (nightly is typical). Each
  CLI run snapshots every learned store first.
- **Inspect before it acts**: `maverick dream --dry-run` reports exact
  would-be changes without writing.
- **Roll back**: `maverick dream --list-snapshots` then
  `maverick dream --rollback latest|<name>` restores all learned stores
  wholesale (including removing skills learned after the snapshot).
- **Audit**: every dream cycle writes a `learning_update` row to the signed
  audit log; `maverick audit verify` covers learned state like tool calls.
- **Tenant note**: with an active tenant, all learned stores live under that
  tenant's data dir — back up per tenant.

## Rotating API keys without downtime

```sh
# Edit the new key in:
sudo -u maverick vim ~maverick/.maverick/.env
# (chmod 600 is enforced by the installer.)

# Hot reload:
systemctl reload maverick
# If the systemd unit doesn't support reload, restart:
systemctl restart maverick
```

## Logs

Plain text by default. Set `MAVERICK_LOG_FORMAT=json` for structured
output suitable for Loki / CloudWatch / Datadog:

```sh
MAVERICK_LOG_FORMAT=json MAVERICK_LOG_LEVEL=DEBUG maverick dashboard
```

Every log line emitted during a goal run carries `goal_id` and
`conversation_id`, so you can grep:

```sh
journalctl -u maverick --since "1 hour ago" -o json \
  | jq 'select(.MESSAGE | fromjson? | .goal_id == 42)'
```

## Setting up `/metrics` scrape

Prometheus config:

```yaml
scrape_configs:
  - job_name: maverick
    metrics_path: /metrics
    bearer_token: <MAVERICK_DASHBOARD_TOKEN>
    static_configs:
      - targets: ["maverick.example.com:8765"]
```

Useful queries:

- `sum(maverick_goals_total{status="done"})` — current completed-goal rows
- `maverick_concurrent_goals / maverick_max_concurrent_goals` —
  saturation
- `increase(maverick_cost_dollars_total[1h])` — spend rate
