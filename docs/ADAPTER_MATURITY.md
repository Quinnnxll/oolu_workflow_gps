# Adapter maturity

Workflow-GPS is built from swappable seams (`Protocol`s) with multiple
implementations behind each one. This document states, per seam, which
implementations are **production-capable** today and which are **experimental**
or **test-only**. It is the authoritative source for the "experimental vs.
production-capable adapters" stabilization gate.

Maturity levels:

- **Production-capable** — safe to depend on for the local single-user alpha;
  contract-tested; no known correctness or safety gaps for its stated use.
- **Experimental** — functional and tested offline, but not yet hardened for the
  scenario it ultimately targets (durability, identity, live network, etc.).
- **Test-only / simulation** — exists to support offline tests or to model a
  future boundary. Never wire these into a real deployment.

## Execution backends (`runtime/`)

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `LocalDockerBackend` | `runtime/isolation.py` | Production-capable (local) | The real isolation boundary: ephemeral, non-root, read-only rootfs, resource-capped, network severed before execution. Requires the sandbox image. |
| `SubprocessBackend` | `runtime/isolation.py` | Experimental / dev-only | **No isolation** — shares host kernel and network. Acceptable only for trusted intents during development; never for untrusted code. |
| `StubBackend` | `runtime/backend.py` | Test-only | Deterministic fake for offline tests. |

## Model gateway (`routing/`)

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `LiteLLMGateway` | `routing/gateway.py` | Production-capable | Talks to any OpenAI-compatible endpoint (local vLLM/Ollama/LM Studio or the hosted OpenAI API). Credentials come from the environment, never persisted. |
| `FakeGateway` | `routing/gateway.py` | Test-only | Scripted responses for offline tests. |

## Knowledge layer (`knowledge/`)

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `NoopKnowledgeClient` | `knowledge/client.py` | Production-capable | The offline default. The engine must navigate fully without a knowledge layer. |
| `LocalKnowledgeClient` | `knowledge/client.py` | Production-capable (local) | Versioned SQLite store; every value passes the scrubbing gate before storage. |
| `RemoteKnowledgeClient` | `knowledge/remote.py` | Experimental | Crowd-intelligence over HTTP with a local quarantine ledger. Background sync, trust-floor, and scrubbing are implemented but the server contract and live operation are not yet hardened. Opt-in only. |

## Reply channels (`replies/`)

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `LocalLearnedReplyStore` | `replies/learned.py` | Production-capable (local) | Versioned SQLite learning scoped per connection; bot-loop prevention. |
| `TelegramAdapter` | `replies/channels/telegram.py` | Experimental | Uses the official Telegram Bot API. Personal-account replies require a bot connected to Telegram Business. Validated against the live API surface but not yet load- or failure-tested. |
| LINE and other channels | `replies/channels/base.py` | Not implemented | `ChannelAdapter` is the intended port; no concrete adapter ships yet. |

## Skill stores and execution (`skills/`)

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `LocalSkillStore` / `LocalExecutionStore` | `skills/store.py` | Production-capable (local) | Versioned SQLite catalog + idempotency ledger sharing one migration history. |
| `InMemorySkillStore` / `InMemoryExecutionStore` | `skills/store.py` | Test-only | Non-durable. |
| `RemoteMockSkillStore` / `RemoteMockExecutionStore` | `skills/store.py` | Test-only / simulation | Model a network boundary by storing serialized JSON only. Not a real remote backend. |
| `CliActionExecutor` | `skills/cli_adapter.py` | Experimental | Runs allow-listed local commands with `shell=False`. **Not an OS sandbox** — allow-listed commands must be trusted. Untrusted execution belongs in the Docker backend or a future restricted worker. |

## Unified orchestrator (`orchestrator/`)

The orchestrator core (`WorkflowOrchestrator`, `RunState`) is
production-capable for the local single-user alpha: the run state is versioned and
serializable, and the execution preflight guard is contract-tested. The default
stage adapters that ship with it are deliberately deterministic and offline (see
ADR-0002); each is the seam where a richer implementation lands on a later branch.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `WorkflowOrchestrator` / `RunState` | `orchestrator/engine.py`, `state.py` | Production-capable (local) | Versioned, serializable run state; pause/resume; hard preflight guard re-derived on every execution. |
| `LocalRunStateStore` | `orchestrator/store.py` | Production-capable (local) | Versioned SQLite run-state store via the shared migration runner. |
| `ActionExecutorRouteRunner` | `orchestrator/adapters.py` | Experimental | Executes a route through the `ActionExecutor` contract; isolation is the executor's responsibility (use the Docker backend for untrusted code). |
| `RiskBasedHumanControl`, `LeastCostRouteOptimizer`, `CapabilityGrounder`, `StatusOutcomeMonitor`, `BoundedRetryRecovery` | `orchestrator/adapters.py` | Experimental | Deterministic default policies; tunable but not yet hardened for production decisioning. |
| `StaticIntaker` | `orchestrator/adapters.py` | Test-only | Returns a pre-built brief. Natural-language intake is a model-backed adapter on a later branch. |
| `InMemoryRunStateStore` | `orchestrator/store.py` | Test-only | Non-durable (still serializes through JSON). |

## Durable runtime (`durable/`)

The durability *semantics* — leased queue, idempotency ledger, transactional
outbox, hash-linked audit, content-addressed artifacts — are contract-tested and
production-shaped. They ship today as a **local SQLite + filesystem** adapter,
which is genuinely restart-safe for a single-host deployment; the multi-process,
multi-host production target is a PostgreSQL + object-store adapter implementing
the same ports.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `DurableConnection` / `DurableTaskQueue` / `IdempotencyLedger` / `TransactionalOutbox` / `DurableAuditLog` | `durable/` | Production-capable (single host) | Versioned SQLite; leases/heartbeats/retry; exactly-once effects; tamper-evident audit. Single-writer concurrency only. |
| `DurableRunStateStore` / `DurableRecordStore` | `durable/records.py` | Production-capable (single host) | Durable checkpoints and domain records for history reconstruction. |
| `FilesystemArtifactStore` | `durable/artifacts.py` | Production-capable (single host) | Content-addressed local object storage; an S3/GCS adapter is the multi-host target. |
| `DurableWorkflowService` | `durable/service.py` | Experimental | Restart-safe orchestration wrapper; sync and queue-driven modes are tested, but production hardening (back-pressure, multi-worker fairness) is pending. |
| PostgreSQL + object-store adapter | — | Not implemented | The multi-process production target for every port above; scoped to deployment. |

## Identity and RBAC (`identity/`)

Identity is established only from a signature-verified OIDC assertion turned into
an expiring, revocable session; authority is derived from stored tenant/role/grant
records (never token text); every store query is tenant-scoped. The model and
policy engine are contract-tested. The token *signature* verifier is the seam where
production crypto lands.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `OidcValidator` / `SessionManager` / `AuthorityResolver` / `IdentityStore` | `identity/` | Production-capable (logic) | Claim validation, tenant isolation, role/grant resolution, step-up, expiry/revocation — all tested. |
| `IdentityApprovalAuthority` | `identity/service.py` | Production-capable (local) | Mints an `ApprovalRecord` only from an authorized, verified session. |
| `Hs256Verifier` / `Hs256Signer` | `identity/tokens.py` | Test-only / local-symmetric | Stdlib HMAC. Real IdPs sign asymmetrically; do not use HS256 for production identity. |
| JWKS asymmetric verifier (RS256/ES256) | — | Not implemented | The production `SignatureVerifier` adapter (optional crypto dependency); the validation logic is unchanged. |

## Worker control plane (`worker/`)

Separates planning/dispatch from privileged execution. The control plane holds no
backend and no credentials; signed single-use leases authorize execution; workers
enforce isolation; outbound-only local agents serve desktop/private resources. The
lease and isolation semantics are contract-tested; the seam to real execution is
the `WorkerExecutor` (a runtime-backend wrapper).

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `ControlPlane` / `LeaseSigner` / `LeaseVerifier` | `worker/` | Production-capable (logic) | Signed, expiring, audience-bound, single-use leases; dispatch with health/capacity/quarantine; cancellation via revocation. |
| `LocalLeaseLedger` | `worker/ledger.py` | Production-capable (single host) | Durable single-use + revocation; the guarantee survives restarts. |
| `IsolationPolicy` | `worker/policy.py` | Production-capable | Untrusted code → Docker/restricted-worker only; subprocess → trusted local skills. |
| `LocalAgent` | `worker/local_agent.py` | Experimental | Outbound-only desktop/private-network agent holding local credentials; transport (HTTP long-poll/SSE) is the production seam. |
| `Worker` + `WorkerExecutor` | `worker/worker.py` | Experimental | Verifies, enforces isolation, runs under a timeout. `StubWorkerExecutor` is test-only; a real executor wraps a runtime `ExecutionBackend`. |
| HMAC lease signing | `worker/leases.py` | Production-capable (first-party) | Symmetric keys are appropriate between a control plane and its own workers; per-worker keys are a natural extension. |

## HTTP gateway (`gateway/`)

A private, tenant-aware control-plane prototype written as a transport-agnostic
application over `Request`/`Response`, on the durable runtime. Auth, RBAC, quotas,
rate limits, idempotency, pagination, webhook verification, and the versioned
contract are contract-tested; the live HTTP server binding and a streaming event
transport are the production seams.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `GatewayApp` | `gateway/app.py` | Experimental (prototype) | OIDC auth, tenant-scoped RBAC, per-tenant quotas/rate limits, idempotent async submission, pagination, SSE, audit export. Not yet load-hardened. |
| `WebhookSigner` / `WebhookVerifier` | `gateway/webhooks.py` | Production-capable (logic) | HMAC signing with timestamp tolerance and delivery-id replay protection. |
| OpenAPI document | `gateway/openapi.py` | Production-capable | Versioned `/v1` contract served at `/v1/openapi.json`. |
| WSGI/ASGI binding + live SSE transport | — | Not implemented | The production server adapter that maps real HTTP onto `Request`/`Response` and streams events. |
| PostgreSQL durable backend | — | Not implemented | The gateway runs on the durable runtime; the multi-host production store is the Postgres adapter (see Durable runtime). |

## Desktop shell (`desktop/`)

`DesktopService` is the local loopback boundary a desktop UI binds to. It exposes
every screen as a frozen, secret-free view-model and routes every action through
the backend's own gates (orchestrator preflight, durable resume, identity-minted
approvals). It has no execution path and never surfaces a credential. The service
and views are contract-tested; the actual GUI and loopback transport are the
product seams.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `DesktopService` | `desktop/service.py` | Production-capable (local logic) | Task entry, clarification, route preview, inboxes, timeline, cancel, audit, provider connections, worker health, export/deletion — all through backend gates. |
| View-models | `desktop/views.py` | Production-capable | Frozen, JSON-serializable, secret-free projections. |
| Desktop UI + loopback transport | — | Not implemented | The GUI (e.g. Tauri/Electron/Qt) and the loopback API/named-pipe binding are the product surface built on this service. |
| OS credential vault | `providers/vault.py` (stand-in) | Experimental | The shell uses the in-memory `SecretVault`; an OS-keychain-backed vault is the production adapter. |

## Provider adapters (`providers/`)

Provider integrations share one request pipeline (capability discovery, rate
limits, budgets, request ids, idempotency, retries, error classification) and keep
credentials in the `SecretVault` — adapters hold references and mint auth headers
only at call time. Every adapter passes the same
capability/revocation/idempotency/secret-leakage contract suite. The integration
*logic* is contract-tested; the seam to the network is the injected `HttpTransport`
(a real HTTP client in production, a sandbox/remote-mock in tests).

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `SecretVault` | `providers/vault.py` | Production-capable (local) | In-memory credential boundary with revocation and redaction; a KMS/secret-manager-backed vault is the production adapter. |
| `GoogleOAuthAdapter` | `providers/google.py` | Production-capable (logic) | Authorization-code + PKCE, scope mapping, callback validation, exchange, refresh, revocation. Needs a real `HttpTransport` wired in. |
| `OpenAiAdapter` | `providers/apikey.py` | Production-capable (logic) | API key plus organization/project service-identity headers. |
| `AnthropicAdapter` | `providers/apikey.py` | Production-capable (logic) | `x-api-key` direct, or `Authorization: Bearer` via the managed enterprise gateway. |
| `HttpTransport` (real HTTP client) | — | Not implemented | The production transport adapter; an `httpx`/`requests` wrapper. Until wired, adapters run only against an injected sandbox/mock transport. |

The legacy model credential path remains an environment-provided API key consumed
by `LiteLLMGateway`; it is never written to any persisted record, log, or fixture
(see `tests/test_secret_hygiene.py`).
