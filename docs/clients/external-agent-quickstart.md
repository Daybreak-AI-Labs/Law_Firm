# External agent quickstart (any runtime)

Bring an agent that runs on **any** other stack — an OpenAI tool-loop,
LangChain, a scheduled job, a home-grown runtime — under Lightwork
governance through the [external-agent gateway](../external-agents.md).
The pattern is **screen-then-act, report-after**:

1. **Screen** every consequential action *before* taking it —
   `POST /api/v1/external/screen` applies the enrolled ceilings (tools,
   risk, budget), Shield input scanning, and the approval floor.
2. **Act** only on `allowed: true`. A `requires_approval` verdict parks a
   real approval that a human decides in the dashboard queue while you
   poll `GET /api/v1/external/approvals/{id}`.
3. **Report** the run once at the end (`POST /api/v1/external/runs`) so it
   lands on the Operating Record — or open a **live run**
   (start / heartbeat / finish) so the work is visible, and stoppable,
   *while* it happens.

Everything below is plain HTTPS against the dashboard server; the wire
contract is the importable `GET /api/v1/external/openapi.json`. Before you
start, [enroll the agent and mint its `rest` bearer](../external-agents.md)
on the /external-agents page. Token values in this page are placeholders —
never commit a real one.

## Python helper

Copy-paste; the only dependency is `requests`.

```python
# lightwork_client.py — screen-then-act, report-after
import requests


class Lightwork:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/") + "/api/v1/external"
        self.headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(self.base + path, json=body,
                          headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def screen(self, tool: str, detail: str = "", risk: str = "low") -> dict:
        v = self._post("/screen", {"tool": tool, "detail": detail, "risk": risk})
        if not v.get("allowed") and not v.get("requires_approval"):
            raise PermissionError(f"{v.get('rule')}: {v.get('reason')}")
        return v

    def report(self, title: str, outcome: str,
               idempotency_key: str = "", **fields) -> dict:
        return self._post("/runs", {"title": title, "outcome": outcome,
                                    "idempotency_key": idempotency_key, **fields})

    def start(self, title: str, **fields) -> dict:
        return self._post("/runs/start", {"title": title, **fields})

    def heartbeat(self, goal_id: int) -> bool:
        return bool(self._post(f"/runs/{goal_id}/heartbeat", {}).get("continue"))

    def finish(self, goal_id: int, outcome: str, **fields) -> dict:
        return self._post(f"/runs/{goal_id}/finish", {"outcome": outcome, **fields})

    def memory_ingest(self, kind: str, goal_text: str, **fields) -> dict:
        return self._post("/memory/ingest",
                          {"kind": kind, "goal_text": goal_text, **fields})

    def memory_recall(self, query: str, domain: str = "") -> dict:
        return self._post("/memory/recall", {"query": query, "domain": domain})

    def approval(self, approval_id: int) -> dict:
        r = requests.get(f"{self.base}/approvals/{approval_id}",
                         headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()
```

Semantics worth knowing:

- `screen()` raises `PermissionError` on a **hard deny** — the message
  carries the gateway's `rule` and `reason` (e.g. `capability: tool
  send_contract is denied`). An approval-floor park is *returned*, not
  raised: check `requires_approval` and poll `approval(v["approval_id"])`
  until `status` is `approved` or `denied`.
- `report()` / `finish()` take an `idempotency_key` — send a stable key per
  logical run and a retried report returns the original record instead of
  double-counting spend.
- `start()` returns `{goal_id, heartbeat_seconds}`; call `heartbeat()` at
  least that often. `False` is the **kill switch** — stop working now.
- `memory_ingest(kind, goal_text, ...)` (`kind` is `success` / `failure` /
  `lesson`) and `memory_recall(query)` reach the governed learning plane
  when `[fleet_memory]` is enabled; an empty `context` with a `reason` is a
  refusal, not an error.

## TypeScript helper

The same class over `fetch` (Node 18+, Bun, Deno, edge runtimes):

```ts
// lightwork.ts — screen-then-act, report-after
export class Lightwork {
  private base: string;
  private headers: Record<string, string>;

  constructor(baseUrl: string, token: string) {
    this.base = baseUrl.replace(/\/+$/, "") + "/api/v1/external";
    this.headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    };
  }

  private async post(path: string, body: unknown): Promise<any> {
    const r = await fetch(this.base + path, {
      method: "POST", headers: this.headers, body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  async screen(tool: string, detail = "", risk = "low") {
    const v = await this.post("/screen", { tool, detail, risk });
    if (!v.allowed && !v.requires_approval)
      throw new Error(`denied (${v.rule}): ${v.reason}`);
    return v;
  }

  async report(title: string, outcome: "success" | "failure",
               fields: Record<string, unknown> = {}) {
    return this.post("/runs", { title, outcome, ...fields });
  }

  async start(title: string, fields: Record<string, unknown> = {}) {
    return this.post("/runs/start", { title, ...fields });
  }

  async heartbeat(goalId: number): Promise<boolean> {
    return (await this.post(`/runs/${goalId}/heartbeat`, {})).continue === true;
  }

  async finish(goalId: number, outcome: "success" | "failure",
               fields: Record<string, unknown> = {}) {
    return this.post(`/runs/${goalId}/finish`, { outcome, ...fields });
  }

  async memoryIngest(kind: "success" | "failure" | "lesson",
                     goalText: string, fields: Record<string, unknown> = {}) {
    return this.post("/memory/ingest", { kind, goal_text: goalText, ...fields });
  }

  async memoryRecall(query: string, domain = "") {
    return this.post("/memory/recall", { query, domain });
  }

  async approval(approvalId: number) {
    const r = await fetch(`${this.base}/approvals/${approvalId}`,
                          { headers: this.headers });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }
}
```

## Wiring it into an OpenAI tool-loop

Screen inside the tool dispatch, heartbeat once per model turn, finish at
the end. A governance deny becomes the *tool result*, so the model can
re-plan instead of crashing:

```python
import json
from openai import OpenAI
from lightwork_client import Lightwork

lw = Lightwork("https://lightwork.example.com", "lw-rest-EXAMPLE")
client = OpenAI()

TOOLS = [{"type": "function", "function": {
    "name": "send_email",
    "description": "Send an email.",
    "parameters": {"type": "object", "properties": {
        "to": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"]}}}]

def send_email(to: str, body: str) -> str:
    ...  # your actual integration

messages = [{"role": "user", "content": "Send the Q3 summary to finance."}]
goal_id = lw.start("Q3 summary email", department="finance")["goal_id"]
tool_calls = 0

while True:
    resp = client.chat.completions.create(
        model="gpt-4o", messages=messages, tools=TOOLS)
    msg = resp.choices[0].message
    if not msg.tool_calls:
        break
    messages.append(msg)
    for call in msg.tool_calls:
        args = json.loads(call.function.arguments)
        try:
            lw.screen(call.function.name,                       # ask FIRST
                      detail=f"email to {args.get('to', '?')}", risk="medium")
            result = send_email(**args)
        except PermissionError as e:
            result = f"blocked by governance: {e}"              # let it re-plan
        tool_calls += 1
        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": result})
    if not lw.heartbeat(goal_id):                               # kill switch
        break

lw.finish(goal_id, "success", summary=msg.content or "",
          tool_calls=tool_calls, idempotency_key=f"q3-email-{goal_id}")
```

For a fire-and-forget job that reports only after the fact, skip the
start/heartbeat/finish trio and end with a single
`lw.report(title, "success", steps=[...], cost_dollars=..., idempotency_key=...)`.

## Governed execution (optional)

With `[external_agents] connectors` set (e.g. `["servicenow"]`), your agent
can go beyond asking permission: **Lightwork performs the action itself**
through its SSRF-pinned, egress-allowlisted connector path, with
PREPARE/COMMIT receipts around every effect. Reads (`op: "get"`) run
immediately; a write parks a **digest-bound** approval — once a human
approves, re-send the *identical* request to commit (any change voids it):

```bash
curl -sS -X POST https://lightwork.example.com/api/v1/external/execute \
  -H "Authorization: Bearer lw-rest-EXAMPLEEXAMPLE" \
  -H "Content-Type: application/json" \
  -d '{"connector": "servicenow", "op": "post", "path": "/api/now/table/incident",
       "body": {"short_description": "Renewal follow-up"}}'
# -> {"rule": "approval_required", "approval_id": 57, "execution_id": "kF3...", ...}

curl -sS -X POST https://lightwork.example.com/api/v1/external/executions/kF3.../commit \
  -H "Authorization: Bearer lw-rest-EXAMPLEEXAMPLE" \
  -H "Content-Type: application/json" \
  -d '{"connector": "servicenow", "op": "post", "path": "/api/now/table/incident",
       "body": {"short_description": "Renewal follow-up"}}'
# -> {"rule": "executed", "status": "executed", ...}
```

Full semantics — enabling, connector credentials, `preview`, the 24-hour
TTL, and troubleshooting — in [governed
execution](../external-agents.md#governed-execution).

## Platform-native credentials (optional)

The minted bearer is not the only way in. If the operator pins identity
material on your trust entry, the same endpoints accept [platform-native
credentials](../external-agents.md#platform-native-identity): a
connected-app **JWT** (send it as `Authorization: Bearer <jwt>`; its `sub`
must be your agent id), a webhook-format **HMAC**, or a pinned-key
**Ed25519** request signature. The signed schemes drop the `Authorization`
header and sign the raw body instead:

```python
# HMAC: sha256=hex(HMAC-SHA256(secret, b"<ts>." + raw_body))
import hashlib, hmac, json, time
raw = json.dumps({"tool": "send_email", "risk": "medium"}).encode()
ts = str(int(time.time()))
mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256)
headers = {"X-Lightwork-Agent-Id": "sf-quotebot",
           "X-Maverick-Timestamp": ts,
           "X-Maverick-Signature": "sha256=" + mac.hexdigest(),
           "Content-Type": "application/json"}
```

```python
# Ed25519: sign the domain-separated envelope with your private key
import secrets
nonce = secrets.token_urlsafe(16)                       # single-use
digest = hashlib.sha256(raw).hexdigest()
msg = (f"lightwork-external-request-v1|sf-quotebot|{ts}|{nonce}|"
       f"{digest}".encode())
headers = {"X-Lightwork-Agent-Id": "sf-quotebot",
           "X-Maverick-Timestamp": ts,
           "X-Lightwork-Nonce": nonce,
           "X-Lightwork-Request-Signature": private_key.sign(msg).hex(),
           "Content-Type": "application/json"}
```

Send the *exact* bytes you signed as the request body (in a Python client,
`maverick.external_identity.envelope_message` builds the Ed25519 message
for you). Timestamps have a 300-second freshness window, so sign at send
time; auth failures stay a generic `401` and the server log names the
reason.

## Operational notes

- **Rate limits** (per agent, per minute): `screen` 120, `runs` 60,
  `approvals` 120, `heartbeat` 240, `memory` 60. A `429` carries
  `Retry-After: 60` — back off and retry.
- **Auth failures are always `401`** — mint a fresh `rest` credential
  (rotation replaces the old token immediately).
- Repeated misbehavior denials auto-contain the agent; an administrator
  releases it on the /external-agents page.

## See also

- [External agents (BYOA)](../external-agents.md) — enrollment, ceilings,
  budget semantics, live runs, and the full troubleshooting table
- `GET /api/v1/external/openapi.json` — the importable machine-readable
  contract for these endpoints
