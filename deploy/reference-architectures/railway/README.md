# Lightwork on Railway

Dockerfile deploy with a persistent volume and env-driven secrets.

## Steps

1. Create a Railway project from this repo (or `railway init` + `railway up`).
   `railway.json` points the builder at `deploy/docker/Dockerfile` and sets the
   start command to the dashboard on port 8765.
2. **Volume**: attach a volume mounted at `/home/maverick/.maverick` (Service →
   Settings → Volumes). Without it, config/runs/audit reset on every deploy.
3. **Secrets**: set `ANTHROPIC_API_KEY` (and any connector tokens) as service
   variables; Railway injects them as env vars.
4. **Networking**: enable a public domain on port 8765, or keep it private and
   use Railway's private networking from your other services.

## Notes

- `numReplicas` stays 1, including with a Railway Postgres world model, because
  the remaining flow/A2A/audit/learning/policy stores are not all shared. Scale
  separate remote workers instead of the dashboard/control-plane service.
- One-shot goals: `railway run maverick start "your goal"` executes inside the
  service environment with the same image + secrets.
