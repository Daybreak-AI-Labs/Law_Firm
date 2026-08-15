# Reference architectures

Self-hostable deployment blueprints for the governed Lightwork runtime. Every
one runs the dashboard + control plane from the published container image and
keeps state on a persistent volume; secrets come from the platform's secret
store, never the image. Pick the one that matches where you already run things.

> All four target the same container (`deploy/docker/Dockerfile` /
> `ghcr.io/daybreak-ai-labs/lightwork`), expose the dashboard on port **8765**, and
> use auth-exempt **`/readyz`** readiness and **`/livez`** liveness probes where
> the platform supports both. Keep exactly **one** dashboard/control-plane
> instance, including with `[world_model] backend = "postgres"`; Postgres and
> the queue let you scale the separate `maverick worker` tier, not the web tier.

| Platform | Manifest | State | Secrets |
|---|---|---|---|
| Kubernetes | [`kubernetes/maverick.yaml`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/deploy/reference-architectures/kubernetes/maverick.yaml) | PVC (`ReadWriteOnce`) | `Secret` → `envFrom` |
| AWS ECS (Fargate) | [`ecs/task-definition.json`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/deploy/reference-architectures/ecs/task-definition.json) | EFS volume | Secrets Manager → `secrets` |
| Fly.io | [`flyio/fly.toml`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/deploy/reference-architectures/flyio/fly.toml) | Fly volume at `/state` | `fly secrets set` |
| Railway | [`railway/railway.json`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/deploy/reference-architectures/railway/railway.json) | Railway volume at `/state` | service variables |

## Kubernetes

```bash
kubectl apply -f deploy/reference-architectures/kubernetes/maverick.yaml
kubectl -n maverick port-forward svc/maverick 8765:80
```

Namespaced, runs as non-root, readiness-gated on `/readyz` and liveness-gated
on `/livez`, with state on a 10Gi PVC. Put real values into the
`maverick-secrets` Secret from your secret manager (External Secrets Operator /
Sealed Secrets) — the checked-in manifest ships placeholders. Add an
Ingress/Gateway for TLS termination.

## AWS ECS (Fargate)

```bash
aws ecs register-task-definition \
  --cli-input-json file://deploy/reference-architectures/ecs/task-definition.json
```

Fill in `ACCOUNT_ID`, `REGION`, the EFS `fileSystemId`, and the Secrets Manager
ARNs, then create a service behind an ALB whose target group points at
container port 8765. The task-level liveness check uses `/livez`; configure the
ALB target group's readiness check on `/readyz`.

## Fly.io

```bash
fly launch --copy-config --no-deploy
fly volumes create maverick_state --size 10
fly secrets set ANTHROPIC_API_KEY=... MAVERICK_DASHBOARD_TOKEN=...
fly deploy
```

`force_https` is on; `min_machines_running = 1` keeps the single writer alive,
and the service readiness check uses `/readyz`.

## Railway

Create a project from the repo, add a Volume mounted at `/state`, set
`ANTHROPIC_API_KEY` and `MAVERICK_DASHBOARD_TOKEN` in the service variables, and
deploy. Railway injects `$PORT`; the start command binds the dashboard to it and
health-checks `/readyz`.

## Scaling to multi-tenant / multi-worker

These blueprints use one dashboard/control-plane node. To add processing
capacity, see the enterprise architecture (`docs/architecture.md`): the Postgres
world-model backend (tenant isolation + migrations), the `QueueDispatcher`
(arq) worker pool, per-tenant KMS/egress, and the operator console. Once
Postgres + queue are configured, keep one web/control-plane replica (`maverick
dashboard`) and scale only the separate worker tier (`maverick worker`).
