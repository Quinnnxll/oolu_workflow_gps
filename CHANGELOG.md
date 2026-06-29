# Changelog

All notable changes to Workflow-GPS are documented here.

## Unreleased

Desktop shell (`codex/desktop-shell`).

- Added `workflow_gps.desktop`: the local single-user application service
  (`DesktopService`) a desktop UI binds to over a loopback boundary, with frozen,
  secret-free, serializable view-models.
- Task entry and guided-question views; route preview with cost and exclusion
  explanations; confirmation/approval/incident inboxes; workflow timeline,
  cancellation, recovery, and a verifiable audit view.
- Provider connection management over the credential vault (an OS-keychain vault is
  the production adapter); Docker/worker health with trusted-vs-untrusted execution
  labels; offline policy and local data export/deletion.
- Approvals are minted only from an authorized identity session, and the shell has
  no execution path, so the UI cannot bypass backend policy; no view ever carries a
  provider secret.
- Added tests proving a non-developer can complete, pause, resume, inspect, and
  recover a workflow through the service alone, and that the UI can neither bypass
  policy nor expose credentials.

Provider adapters (`codex/provider-adapters`).

- Added `workflow_gps.providers`: contract-tested provider integrations behind a
  credential vault boundary.
- Implemented a Google authorization-code/OIDC adapter (PKCE, scope mapping,
  callback validation, code exchange, refresh, revocation).
- Implemented OpenAI (with organization/project service-identity headers) and
  Anthropic (API-key and managed enterprise gateway) adapters.
- Added a shared request pipeline: capability discovery, a token-bucket rate
  limiter, spend budgets, request ids, idempotency keys (with replay caching),
  retries with classified errors, and HTTP-status → error classification.
- Kept credentials exclusively in the `SecretVault`; adapters hold references and
  mint auth headers only at call time, with redaction for logs.
- Added a single capability/revocation/idempotency/secret-leakage contract suite
  run against every adapter through an injected sandbox/remote-mock transport, plus
  per-provider flow, retry, rate-limit, budget, and service-identity tests.

Worker control plane (`codex/worker-control-plane`).

- Added `workflow_gps.worker`: a control plane that does planning and dispatch but
  holds **no execution backend and no credentials**, separated from workers that
  run code.
- Added signed, expiring, audience-bound, single-use worker task leases (HMAC),
  verified against a revocation/consumption ledger so lost (forged), duplicated
  (replayed), expired, or revoked leases cannot execute. The ledger has an
  in-memory and a durable SQLite implementation (single-use survives restarts).
- Added an isolation policy: untrusted synthesized code may run only on Docker (or
  a stronger restricted worker); the subprocess backend is restricted to explicitly
  trusted local skills. The worker enforces it before executing.
- Added worker health, capacity, cancellation (revokes the lease), wall-clock
  timeout, and failure-based quarantine.
- Added outbound-only local agents for desktop/private-network resources: they poll
  the control plane (no inbound port) and resolve local credentials themselves, so
  the control plane never receives them.
- Added tests proving the two exit-gate guarantees: the control plane never
  executes or holds credentials, and lost/duplicated/expired/revoked leases cannot
  execute.

Identity and RBAC (`codex/identity-rbac`).

- Added `workflow_gps.identity`: enforceable identity and authority replacing the
  simulation-only seams.
- Validate OIDC assertions against configured providers (issuer, audience, expiry,
  not-before; `alg: none` and algorithm-confusion rejected) behind a pluggable
  `SignatureVerifier` port — a stdlib HMAC verifier ships for local/test use; a
  JWKS-backed asymmetric verifier is the production adapter.
- Added tenant, organization, membership, group, role, and authority-grant records
  in a versioned, tenant-isolated SQLite store; every query is tenant-scoped.
- Derive reviewer/approver authority from stored grants and group roles, never from
  token text; `IdentityApprovalAuthority` mints an `ApprovalRecord` only from an
  authorized, verified session.
- Added service and device identities, server-issued sessions with expiry and
  revocation, and step-up authentication via authentication-assurance levels.
- Added policy tests for cross-tenant access, expired grants, self-approval,
  confused-deputy scope mismatch, step-up, and session expiry/revocation — proving
  no caller can self-verify an identity, self-assign a role, or reach another tenant.

Durable runtime (`codex/durable-runtime`).

- Added `workflow_gps.durable`: a restart-safe, multi-process workflow runtime
  behind deployment-neutral ports, with a versioned local SQLite adapter (the same
  table/lease/idempotency contract a PostgreSQL deployment implements).
- Added a durable task queue with leases, heartbeats, cancellation, retry with
  backoff, dead-lettering, and expired-lease reclaim; idempotent enqueue.
- Added an idempotency ledger so every externally visible mutation runs at most
  once, a transactional outbox (events/notifications staged in the same
  transaction as the state change) with an at-least-once relay, and a hash-linked,
  tamper-evident audit log that implements the `EventSink` port.
- Added durable run-state checkpoints and domain record stores (routes, accounts,
  approvals, incidents, semantic evidence, execution outcomes), content-addressed
  filesystem object storage for large artifacts, and backup/restore/retention/
  deletion workflows.
- Added `DurableWorkflowService` tying it together: a checkpoint and its
  announcement commit atomically; a crashed worker's task is reclaimed and
  re-driven from the last checkpoint without duplicating effects.
- Added tests proving restart loses or duplicates nothing (lease reclaim plus
  idempotent re-drive) and that approval/incident/audit records reconstruct the
  complete, verifiable execution history from storage alone.

Unified orchestrator (`codex/unified-orchestrator`). See ADR-0002.

- Added `workflow_gps.orchestrator`: one deterministic, resumable runtime that
  drives a workflow through intake, guided clarification, semantic grounding,
  route optimization, human-control evaluation, confirmation/approval waits,
  execution, outcome monitoring, automatic recovery or incident escalation, and
  finalization with route learning.
- Defined one versioned, serializable run state (`RunState`,
  `ORCHESTRATOR_SCHEMA_VERSION`) that round-trips losslessly; pause/resume and
  durability reduce to saving and reloading it.
- Added pause/resume for clarification, confirmation, approval, and incidents,
  with deployment-neutral ports and deterministic offline adapters that compose
  the existing skill core (Requirement and Constraint Compiler, the
  `ActionExecutor` contract, and `ExecutionOutcome`).
- Made execution safety a property of the state: the execution phase re-derives a
  hard preflight guard (requirements resolved, route not excluded, human control
  satisfied, capabilities available) on every attempt, including post-incident
  retries, so no path bypasses preflight controls.
- Added a versioned local run-state store (`workflow_runs`) through the shared
  migration runner, plus `wfgps workflow-list` / `wfgps workflow-status`.
- Added end-to-end tests for autonomous, confirmed, dual-approved, recovered, and
  escalated workflows, full serialization survival across every pause, durable
  store reopen, and preflight/capability bypass prevention.

## 0.2.0 - 2026-06-29

Stabilization baseline (`codex/stabilize-v0.2-baseline`).

- Reconciled the root and `src/` packaging into a single canonical `pyproject.toml`
  with the `wfgps` console entry point and `engine`/`docker`/`dev` extras; removed
  the duplicate `src/pyproject.toml`. There is now one supported install command:
  `pip install -e ".[engine]"`.
- Added a shared SQLite migration runner (`workflow_gps.persistence`) backed by
  `PRAGMA user_version`, and versioned every persisted schema (script cache,
  learned replies, local knowledge, crowd quarantine, skill catalog + idempotency
  ledger) through it, with a forward-compatibility guard against newer databases.
- Added forward/rollback migration tests, fresh-environment installation and CLI
  smoke tests, and a secret-hygiene test asserting no secrets reach persisted
  records, logs, fixtures, or examples.
- Configured Ruff and fixed repository-wide lint findings; formatted the tree.
- Documented experimental versus production-capable adapters in
  `docs/ADAPTER_MATURITY.md`.

Also included in this release candidate (previously unreleased):

- Added a model-free deterministic reply engine with context-gated templates.
- Added an official Telegram Bot API adapter for private text chats and a channel protocol for future LINE and other messaging adapters.
- Added local SQLite reply learning from manual Telegram Business replies, scoped per Business connection, with bot-loop prevention and short-lived pairing state.
- Added the portable skill-core foundation, ADR-0001, versioned domain records and ports, local/in-memory/remote-mock skill stores, safe skill inspection commands, and the Requirement and Constraint Compiler.
- Added an exact CLI demonstration compiler and safety-gated runtime with executable allow-lists, reduced environments, workspace fingerprints, write approvals, idempotency, timeouts, and artifact validation.

## 0.1.0 - 2026-06-28

- Stabilized the graph engine, execution contract, tier routing, self-healing dependency loop, and CLI.
- Added an opt-in local SQLite script cache that can skip synthesis for identical tasks.
- Added conservative cache signatures across prompt policy, routing models, backend configuration, package index, engine version, and schema version.
- Added cache outcome fields to graph state, workflow results, and JSON CLI output.
- Kept caching disabled by default and documented the release roadmap and security boundaries.
