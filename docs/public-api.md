# The public execution API

When the node and path database is worth calling, callers are programs.
This is the surface they use: machine credentials, a scoped slice of the
gateway, and signed webhooks so nobody polls.

## 1. Get a key (interactive, once)

A signed-in user (never another key) mints keys:

```bash
curl -X POST https://your-host/v1/api-keys \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "acme-integration", "scopes": ["runs:submit", "runs:read"]}'
# -> { "key_id": "...", "secret": "oolu_sk_...", ... }
```

The `secret` appears in that response and nowhere else — the server stores
only its SHA-256 hash. Keys are listed (without secrets) at
`GET /v1/api-keys` and revoked with `DELETE /v1/api-keys/{key_id}`.

## 2. What a key can reach — and cannot

Keys ride the same `Authorization: Bearer` header. The gateway recognises
the `oolu_sk_` prefix and enforces the key's scopes on every request:

| Scope | Grants |
| --- | --- |
| `runs:submit` | `POST /v1/runs` and the run mutations (answers, confirmation, incidents, cancel) |
| `runs:read` | `GET /v1/runs*` — status, questions, route, audit, SSE events |
| `market:read` | `GET /v1/listings`, `/v1/market/*` previews (candidates, quotes, assemble) |

Everything else — settings, files, payments, chat, Work, nodeplace
contribution, key and webhook management — is **absent by construction**
for keys, whatever scopes they hold. Rate limits apply per tenant exactly
as for interactive callers; runs submitted by a key bind to the key's
owning principal, so accountability and billing land where they should.

## 3. Execute a task

```bash
curl -X POST https://your-host/v1/runs \
  -H "Authorization: Bearer oolu_sk_..." \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-1234" \
  -d '{"intent": "convert the attached invoice export to the tidy schema"}'
# -> 202 { "run_id": "...", "phase": "...", ... }
```

Submission is asynchronous. Follow progress by polling
`GET /v1/runs/{run_id}`, streaming `GET /v1/runs/{run_id}/events` (SSE),
or exporting the verifiable trail at `GET /v1/runs/{run_id}/audit`.
`Idempotency-Key` makes retries safe — the same key returns the same run.

## 4. Get told instead of polling

Register a webhook endpoint (interactive, once):

```bash
curl -X POST https://your-host/v1/webhook-endpoints \
  -H "Authorization: Bearer $USER_TOKEN" \
  -d '{"url": "https://acme.example/hooks/oolu"}'
# -> { "endpoint_id": "...", "secret": "whsec_..." }   # secret shown once
```

Every terminal run event (`workflow.completed`, `workflow.failed`,
`workflow.cancelled`) in your tenant is POSTed to the URL as JSON:

```json
{"type": "workflow.completed", "run_id": "...", "at": "...", "seq": 123}
```

Deliveries are staged durably from the audit log (restart-safe cursor,
bounded retries) and signed with your endpoint's secret using the same
HMAC scheme as OoLu's inbound webhooks — verify with the documented
`X-Webhook-*` headers (timestamped, replay-protected by delivery id)
before trusting a payload.

## 5. Scale notes

- The whole surface is stateless per request over the durable database —
  gateway processes scale horizontally on Postgres (`oolu host
  --database-url`).
- The webhook notifier is a derive-then-deliver pump over the audit log;
  run it on any cadence (`notifier.pump(transport)`), on as many hosts as
  you like — the cursor and keyed deliveries make it replay-safe.
- The full route contract lives at `GET /v1/openapi.json`.
