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

## Provider credential adapters

There are **no production provider authorization adapters yet** (Google OIDC,
OpenAI project keys, Anthropic enterprise gateway). These are scoped to the
later `codex/provider-adapters` branch. Today the only model credential path is
an environment-provided API key consumed by `LiteLLMGateway`; it is never
written to any persisted record, log, or fixture (see `tests/test_secret_hygiene.py`).
