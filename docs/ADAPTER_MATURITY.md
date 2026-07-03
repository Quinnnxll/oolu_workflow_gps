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
| `discover_tools` / `resolve_file` (+ `DEFAULT_TOOL_CATALOG`) | `skills/discovery.py` | Production-capable (local) | Environment grounding for local automation: probes `PATH` against a curated catalog of task tools (ffmpeg, pandoc, jq, curl, git, …) and returns each present tool's **resolved absolute path** + category/tags; `resolve_file` locates a named file within a workspace. This is what lets the system act without the user knowing tool/file paths — discovered paths auto-populate the CLI allow-list and are exposed at `GET /v1/tools`. Deliberately excludes shells/interpreters so discovery never auto-allow-lists arbitrary code; execution still passes the human-control gate. |
| `BrowserActionExecutor` | `skills/browser.py` | Experimental | Playwright/Chromium `ActionExecutor` for `web.*` skills — **no Docker required**. Runs a skill as one `run` action (a hardened step list: goto/click/fill/select_option/wait_for/read_text/read_rows/submit) in an isolated browser context with a per-run **network allow-list** (off-host requests aborted), secret-free evidence, and idempotent replay. Step strings substitute `{{param}}`. Behind the `browser` extra; contract-tested against local HTML with real Chromium. Hardening (persistent-profile isolation, download capture, OTP source) is the remaining work. |
| `SkillRegistry` (+ `SkillContextBuilder`) | `skills/registry.py`, `skills/context.py` | Production-capable (local) | Versioned, content-addressed local SQLite catalog keyed by `(skill_id, semver)`; immutable released versions (content hash ignores volatile timestamps/counters so re-loads are idempotent); keyword/tag search. `SkillContextBuilder` renders only the top-`max_context_tools` matching tools (from `models.yaml`) into the fast tier's prompt so context stays small and selection is sub-second. |
| `PlanningContextBuilder` (+ `select_tools`, `render_tool_env`) | `skills/context.py` | Production-capable (local) | Folds *both* the relevant registered skills and the relevant discovered **local tools** (name/path/category) into one intake prompt, each filtered to the intent's top-k, so the fast tier grounds a request onto what is actually installed here. `assembly.build_planning_context(registry=…, tools=…, discover=True)` builds the provider (auto-discovering local tools when asked); `build_intake_model` injects it as the intake model's `context_provider`. |
| Skill packs (`load_skill_pack`, `load_starter_pack`) + starter pack | `skills/pack.py`, `skills/packs/starter.yaml` | Production-capable (local) | Declarative YAML skill packs (stable `skill_id`, semver, tags, parameters, actions) register into the `SkillRegistry` idempotently; `wfgps skill-register [PACK|--starter]` loads them. The shipped starter pack seeds reviewed web/HTTP skills so the registry, `/v1/skills`, and fast-tier retrieval are non-empty out of the box; the `web.*` skills are now executable `run` step-lists driven by `BrowserActionExecutor` (Docker-free), while the 2FA and HTTP skills remain descriptors until their executors land. |
| `SkillsServer` | `skills/server.py` | Experimental | Loopback ASGI exposing `GET /v1/skills` (list/search), `GET /v1/skills/{id}`, and `POST /v1/skills/execute` (runs a skill's actions through wired executors; refuses irreversible actions, which must go through the run/approval flow). Served by `wfgps serve` (behind the `serve` extra). No auth — loopback only. |
| `SkillLearner` (+ `scrub_demonstration`) | `skills/learner.py` | Experimental | The learn-by-demonstration spine: a consented `Demonstration` becomes a private skill via scrub → `DemonstrationCompiler` → **sandbox-verify** → `SkillRegistry.register`. Registration is gated on verification (the compiled actions must succeed through an injected sandbox executor), so an unverified skill never enters the registry silently; PII/secrets are masked (`scrub_demonstration`) before any learning, and re-learning the same task is idempotent (stable `learned.<slug>` id; content hash covers behaviour, not provenance). |
| `DemonstrationRecorder` (+ `LogSource`, `DurableAuditLogSource`, `select_best`) | `skills/recorder.py` | Experimental | The learner's input seam: captures a GUI demonstration and the correlated **backend system log** over the same window and folds both into one `Demonstration` (a merged timeline + reliability/efficiency metrics: duration, backend-event/error counts, success). The GUI `ObserverAdapter` is pluggable; the backend log comes from any `LogSource` — `DurableAuditLogSource` reads the durable audit stream structurally (only event type/run/seq, never payload data). `select_best` picks the fastest reliable variant across recordings of the same task. |
| `BrowserObserver` | `skills/browser_observer.py` | Experimental | The concrete GUI `ObserverAdapter`: injects page listeners (via an exposed binding) that capture a developer's real navigate/click/fill/select/submit as `ActionEvent`s in the same vocabulary `BrowserActionExecutor` replays — so a watched browser demonstration compiles (learner `mode="actions"`) into a re-runnable skill. Password field values are masked at capture; the learner scrubs the rest. The Playwright `page` is injected, so the module imports no Playwright; contract-tested against real Chromium (capture + feeds the learner) and headless (payload mapping). |

## Unified orchestrator (`orchestrator/`)

The orchestrator core (`WorkflowOrchestrator`, `RunState`) is
production-capable for the local single-user alpha: the run state is versioned and
serializable, and the execution preflight guard is contract-tested. The default
stage adapters that ship with it are deliberately deterministic and offline (see
ADR-0002); each is the seam where a richer implementation lands on a later branch.
Intake, route generation, and a runnable assembly are now filled:
`ModelBackedIntaker` replaces the test-only `StaticIntaker` with real
natural-language intake (with a deterministic offline fallback);
`SkillRegistryPlanner` generates candidate blueprints from a skill registry; and
`workflow_gps.assembly.build_desktop_runtime` wires the whole durable +
orchestrator + desktop stack from `Settings` (previously only assemblable in
tests). A deployment with no skills/executors wired runs planning-only and
terminates a run with "no executable route is configured yet" rather than faking
success — real backends are the remaining seam.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `WorkflowOrchestrator` / `RunState` | `orchestrator/engine.py`, `state.py` | Production-capable (local) | Versioned, serializable run state; pause/resume; hard preflight guard re-derived on every execution. |
| `LocalRunStateStore` | `orchestrator/store.py` | Production-capable (local) | Versioned SQLite run-state store via the shared migration runner. |
| `ActionExecutorRouteRunner` | `orchestrator/adapters.py` | Experimental | Executes a route through the `ActionExecutor` contract; isolation is the executor's responsibility (use the Docker backend for untrusted code). |
| `RiskBasedHumanControl`, `LeastCostRouteOptimizer`, `CapabilityGrounder`, `StatusOutcomeMonitor`, `BoundedRetryRecovery` | `orchestrator/adapters.py` | Experimental | Deterministic default policies; tunable but not yet hardened for production decisioning. |
| `SkillRegistryPlanner` (+ `RegistryGrounder`, `classify_risk`) | `orchestrator/planner.py` | Experimental | Generates candidate blueprints from a set of `ReusableSkill`s and resolves their capabilities; a verb heuristic assigns per-action risk (read/write/irreversible) so writes gate on confirmation and irreversible actions are reserved for approval. The heuristic and 1-skill→1-blueprint mapping are the tunable parts. |
| `build_desktop_runtime` / `DesktopRuntime` (+ `build_cli_executor`, `build_intake_model`) | `assembly.py` | Experimental | The production assembly: one call wires the durable stack + orchestrator + `DesktopService` from `Settings`. Route planning and execution are injectable seams (`skills`/`blueprints`/`executors`); planner-less deployments run intake→clarification then terminate honestly with no executable route. |
| `ModelBackedIntaker` (+ `HeuristicIntaker`) | `orchestrator/intake.py` | Production-capable (local) | Natural-language intake: turns a free-text intent into a structured `RequirementBrief`. Upholds the system's safety lines — never binds a parameter value (only suggests, so provenance is preserved), never lets the model self-authorize (a brief from intake is always `GUIDED`), and never lets a bad or absent model turn kill the run (degrades to the deterministic `HeuristicIntaker`). Contract-tested offline with a fake model. |
| `LiteLLMIntakeModel` | `orchestrator/intake.py` | Production-capable (logic) | The live `IntakeModel` over LiteLLM (any OpenAI-compatible endpoint); lazily imported behind the `engine` extra, credentials from the environment. `FakeModel`-style injection keeps intake testable with no network. |
| `StaticIntaker` | `orchestrator/adapters.py` | Test-only | Returns a pre-built brief. Superseded for real intake by `ModelBackedIntaker`; retained for deterministic scenario tests. |
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
| `PostgresDurableConnection` | `durable/postgres.py` | Production-capable (multi-process) | The multi-process production target for the durable ports. Runs the existing stores unmodified via a dialect shim; exactly-once leasing and idempotency hold across separate connections under READ COMMITTED. Contract-tested against live PostgreSQL (`tests/test_durable_postgres.py`). Behind the `postgres` extra. |
| Object-store (S3/GCS) artifact adapter | — | Not implemented | The multi-host artifact target; scoped to deployment. |

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
| `JwksVerifier` (RS256/ES256) | `identity/jwks.py` | Production-capable | The production `SignatureVerifier`: JWKS fetch/cache with kid-rotation refresh, algorithm pinning, PKCS1v15/SHA256 and raw-`r\|\|s` ES256. Validation logic in `OidcValidator` is unchanged. Behind the `oidc` extra. |
| `assert_production_identity` | `identity/tokens.py` | Production-capable | Config-time guard that rejects any provider using a symmetric (HS*) verifier, so HS256 cannot reach a production deployment. |

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
| `Worker` + `WorkerExecutor` | `worker/worker.py` | Experimental | Verifies, enforces isolation, runs under a timeout. `StubWorkerExecutor` is test-only. |
| `BackendWorkerExecutor` | `worker/execution.py` | Production-capable (logic) | The real `WorkerExecutor`: runs a task's script through a runtime `ExecutionBackend` (Docker in production), builds the `ExecutionRequest` from the task payload (script/dependencies/limits/env), and maps the `ExecutionResult` to the worker's `{status, evidence, error}` — surfacing an isolation-machinery `BackendError` as a `WorkerError` (500), never a silent script "failure". `backend_kind` is the policy name (`docker`) so the control plane places untrusted code only here. `assembly.build_docker_worker_executor` wires it over `LocalDockerBackend` (requires the `docker` extra + a reachable daemon; fails loudly otherwise). Contract-tested via the runtime `StubBackend` and through the control plane; the live Docker path is `needs_docker`. |
| `RemoteWorkerActionExecutor` (+ `WorkerTransport`, `InProcessWorkerTransport`) | `skills/remote.py` | Experimental | Bridges the `ActionExecutor` contract onto the worker control plane: an action is dispatched as a signed, single-use lease and run on a worker whose backend the isolation policy permits — so **untrusted code runs on the worker's Docker, never the desktop**. Untrusted tasks with no isolated worker are `BLOCKED` (never run locally); trusted tasks may use subprocess. `InProcessWorkerTransport` models the network as a call (same-host / tests). `assembly.build_worker_executor` wires a same-host pool from an injected `WorkerExecutor`. |
| `HttpWorkerTransport` + `WorkerHttpApp` | `worker/http.py` | Production-capable (logic) | The real network `WorkerTransport`: the control-plane side POSTs `{lease, payload}` to a worker over the injected `HttpTransport` port (a bearer lease; JSON result), and `WorkerHttpApp` is the ASGI endpoint a cloud worker runs — it verifies the lease, enforces isolation, executes, and maps lease/isolation/execution failures to 401/403/500. Contract-tested client, server, and full client→HTTP→worker loopback. `assembly.build_remote_worker_executor` wires the control-plane side to a pool of worker URLs. Needs a real `HttpxTransport` (the `http` extra) and a Docker-backed `WorkerExecutor` on the worker host. |
| `RemoteRevocationLedger` + `RevocationHttpApp` | `worker/http.py` | Production-capable (logic) | Cross-host lease revocation: the control plane is the revocation authority (`ControlPlane.is_revoked`, served by `RevocationHttpApp` at `GET /leases/{id}/revoked`), and a remote worker's `LeaseVerifier` uses `RemoteRevocationLedger` — single-use consumption stays local, but `is_revoked` is answered by the control plane before executing. A `cancel()` on the control plane therefore blocks the lease on a different host; if the authority is unreachable the lease is refused (fail-closed via `RevocationUnavailable`). Contract-tested end to end (cancel → worker rejects). |
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
| `GatewayASGI` + chat frontend | `gateway/asgi.py`, `gateway/frontend/` | Production-capable (functional) | ASGI binding that maps real HTTP onto `Request`/`Response`, serves the SSE snapshot, and (ADR-0004) upgrades the same `/v1/runs/{id}/events` route to a live **WebSocket** push of audit-derived event frames — validated bearer token (subprotocol or `access_token`), cross-tenant-guarded, incremental by audit `seq`, with the SSE snapshot kept as the polling fallback. Full run lifecycle and the live transport contract-tested through the ASGI surface (`tests/test_gateway_asgi.py`). The event push still polls the durable audit log; a durable subscription and a designed UI are the remaining product polish. |
| PostgreSQL durable backend | `durable/postgres.py` | Production-capable (multi-process) | The gateway runs on the durable runtime; the multi-host production store is `PostgresDurableConnection` (see Durable runtime). |

## Metering (`metering/`)

The accounting substrate. One immutable `MeteringEvent` is derived per verified
successful execution from the hash-linked audit log, keyed by the execution's
idempotency key so replay/retry never duplicates. P0 records the fact; P1
(`v0.4.0`) adds attribution (who supplied the node) and the money *facts* gross
`G` / provider_cost `C_p` — still recorded, never charged.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `MeteringLedger` | `metering/store.py` | Production-capable | Append-only event ledger keyed unique on the execution idempotency key; runs on both the SQLite and PostgreSQL durable connections. Records `G`/`C_p`/version/consumer facts; charges nothing. |
| `MeteringDeriver` | `metering/deriver.py` | Production-capable | Idempotent derivation of `MeteringEvent` from verified `workflow.executed` audit events; failure/block/cancel are never metered. Joins a `RunBinding` to attribute the event. |
| `AttributionStore` | `metering/attribution.py` | Production-capable | Run→node bindings and per-noder `AttributionRecord` (weight `w_i`, multiplier `μ_i`); idempotent, derived only from verified success. |

## Nodeplace (`nodeplace/`)

The two-sided registry (P1). Contributing is opt-in and revocable; a private
workflow is never published and never leaves local storage. A published
`NodeVersion` is a content-addressed, secret-free artifact (sanitized through the
`knowledge/scrubbing` gate). Contribute-time review makes isolation and
reserved-action/approval gates mandatory for nodes.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `RegistryStore` / `NodeplaceService` | `nodeplace/store.py`, `service.py` | Production-capable | `Node`/`NodeVersion`/`Listing`/`PricingPolicy`; opt-in sanitized contribute, content-addressed reuse, visibility, discovery/search, owner-scoped revoke/publish. SQLite + PostgreSQL. |
| `NodeSafetyGate` | `nodeplace/safety.py` | Production-capable | Mandatory-isolation + reserved-action/approval review at contribute and publish, reusing the worker `IsolationPolicy`. |
| `RatingService` / `mu_from_ratings` | `nodeplace/ratings.py`, `reputation.py` | Production-capable | Verified-run-gated ratings (proof from the metering trail, never client-claimed); reputation feeds the pricing multiplier `μ`. |

## Billing (`billing/`)

Real money in P2: consumers are charged, noders are paid out, refunds/disputes
claw back, and abuse is contained. The reward formula runs in exact integer
micro-units so `Platform + Σ Noder == N` holds bit-exactly, and every money-moving
path is refused on local-only infra (production PostgreSQL + asymmetric identity
required — invariant #8). Payments are never built in-house: all movement goes
through the `PayoutAdapter` (Stripe Connect over the injected `HttpTransport`),
and raw card data is never touched. Charge/settlement/dispute effects are
exactly-once via the durable idempotency ledger.

| Adapter | Module | Maturity | Notes |
| --- | --- | --- | --- |
| `PricingEngine` | `billing/pricing.py` | Production-capable | Pure `event → (N, PlatformEarning, {NoderEarning_i})`; conservation exact; non-negative; multi-noder split by normalized `w_i·μ_i`. |
| `EarningsLedger` / `BalanceProjection` | `billing/ledger.py` | Production-capable | Append-only ledger (accrual/reserve/clawback/payout); `NoderBalance` is a pure projection, never edited in place. SQLite + PostgreSQL. |
| `require_production_money` | `billing/guard.py` | Production-capable | Refuses charge/payout unless the durable is PostgreSQL and identity is asymmetric. |
| `ChargingService` | `billing/charging.py` | Production-capable | Charges G on verified success and accrues per noder after holdback H; exactly-once via durable idempotency; consults `FraudSignals`. |
| `SettlementService` | `billing/settlement.py` | Production-capable | Reserve R on cleared gross; pays out ≥ T to a KYC-verified account, below T rolls forward; idempotent per (noder, period). |
| `DisputeService` | `billing/disputes.py` | Production-capable | Refund/chargeback/dispute → compensating append-only CLAWBACK; recovered from reserve/future earnings. |
| `DefaultFraudSignals` | `billing/fraud.py` | Production-capable | Self-dealing exclusion, replayed-success rejection, velocity throttle. |
| `StripeConnectAdapter` | `billing/payout.py` | Production-capable (logic) | Charge/payout/KYC over the injected `HttpTransport`; needs a live transport + Stripe keys wired in production. `FakePayoutAdapter` is test/dev only. |

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
| `DesktopLoopbackApp` (loopback transport) | `desktop/loopback.py` | Experimental | The ADR-0004 loopback binding: an ASGI app exposing the `DesktopService` view-models over `127.0.0.1` with **no auth** (task submit/clarify/answer, inbox, timeline, route preview, confirm, resolve-incident, cancel, audit, worker health, offline policy, plus the skill library), and a **WebSocket live timeline**. Secret-free, no execution path; the multi-tenant OIDC gateway remains the door for web/mobile. Contract-tested through the ASGI surface. Approvals (identity-gated) are intentionally not exposed on the loopback. |
| `wfgps desktop` (loopback server) | `cli.py` | Experimental | Serves `DesktopLoopbackApp` over uvicorn (behind the `serve` extra); refuses any non-loopback `--host`. The frontend's Vite dev proxy and the packaged Tauri sidecar both bind here. |
| Desktop UI (Tauri + React) | `desktop-app/frontend`, `desktop-app/src-tauri` | Not implemented (scaffolded) | React/Vite UI (task entry → clarification → confirm/incident → live WebSocket timeline, inbox, skill library) inside a Tauri v2 shell that spawns the `wfgps desktop` sidecar on a free loopback port and injects its origin into the webview. Not built here (no Rust/Node toolchain); the source is complete and CI-buildable. |
| Windows installer (`OoLu-Setup.exe`) | `desktop-app/sidecar/wfgps.spec`, `.github/workflows/desktop-windows.yml` | Not implemented (scaffolded) | GitHub Actions on `windows-latest`: PyInstaller bundles the `wfgps` sidecar, `tauri build` emits an NSIS installer. Produced by CI only; not signed. |
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
| `HttpxTransport` (real HTTP client) | `providers/transport.py` | Production-capable | The production `HttpTransport`: an httpx wrapper honouring the declared content type (form for OAuth token endpoints, JSON otherwise), mapping transport failures to a retryable 503, and taking TLS/proxy from the environment. Behind the `http` extra. |

The legacy model credential path remains an environment-provided API key consumed
by `LiteLLMGateway`; it is never written to any persisted record, log, or fixture
(see `tests/test_secret_hygiene.py`).
