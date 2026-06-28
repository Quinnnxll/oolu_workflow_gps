# Workflow-GPS frontend and gateway branch plan

Prepared for the next development session.

## Current readiness decision

Workflow-GPS is ready for a local single-user desktop alpha, but it is not yet ready
to operate as a public multi-tenant HTTP service. The domain architecture and offline
tests are strong; the remaining risk is concentrated in integration, durable state,
identity and authority enforcement, production provider adapters, and worker operations.

Recommended delivery order: stabilize the current backend, connect the vertical slices
into one resumable orchestrator, then build the desktop alpha. Develop the public HTTP
gateway only after durable runtime and identity boundaries are complete.

## 1. `codex/stabilize-v0.2-baseline`

Purpose: create a clean, reproducible baseline before adding another product surface.

Deliverables:

- Commit the current backend as a coherent release candidate.
- Reconcile the root and `src/` packaging configurations into one supported install path.
- Fix repository-wide lint findings.
- Version every SQLite schema and add forward/rollback migration tests.
- Add fresh-environment installation and CLI smoke tests.
- Document experimental versus production-capable adapters.
- Confirm secrets are absent from persisted records, logs, fixtures, and examples.

Exit gate:

- Clean worktree and tagged baseline.
- One documented installation command.
- Full test, lint, packaging, and migration checks pass.

## 2. `codex/unified-orchestrator`

Purpose: turn the independent vertical slices into one end-to-end Workflow-GPS runtime.

Target flow:

```text
intake
-> guided clarification
-> semantic grounding
-> route optimization
-> human-control evaluation
-> confirmation or approval wait
-> execution
-> outcome monitoring
-> automatic recovery or incident escalation
-> finalization and route learning
```

Deliverables:

- Connect task contracts, blueprints, route optimization, semantic edges, human control,
  reserved actions, execution, monitoring, incidents, and feedback.
- Define one versioned, serializable run state.
- Add pause/resume commands for clarification, confirmation, approval, and incidents.
- Prevent direct execution paths that bypass preflight controls.
- Add end-to-end tests for autonomous, confirmed, dual-approved, recovered, and escalated
  workflows.

Exit gate:

- One workflow can survive every supported pause and resume without losing state.
- All executable paths pass policy and capability checks.

## 3. `codex/durable-runtime`

Purpose: make long-running workflows safe across restarts and multiple processes.

Deliverables:

- Introduce PostgreSQL-backed ports for workflow state, routes, accounts, approvals,
  incidents, semantic evidence, and execution outcomes.
- Add a durable task queue, leases, heartbeats, cancellation, and retry policy.
- Add transactional outbox processing for events and notifications.
- Replace in-memory graph checkpoints and audit chains with durable implementations.
- Add idempotency keys to every externally visible mutation.
- Add object storage for large evidence and artifacts.
- Add backup, restore, retention, and deletion workflows.

Exit gate:

- Restarting API and worker processes does not lose or duplicate a workflow.
- Approval, incident, and audit records reconstruct the complete execution history.

## 4. `codex/identity-rbac`

Purpose: replace simulation-only identity and authority seams with enforceable claims.

Deliverables:

- Validate OIDC assertions from configured identity providers.
- Add tenant, organization, membership, group, role, and authority-grant records.
- Derive reviewer and approver authority from signed claims and grants, never caller text.
- Add service identities, device identities, session expiration, revocation, and step-up
  authentication.
- Enforce tenant isolation in every store and query.
- Add policy tests for cross-tenant access, expired grants, self-approval, and confused
  deputy scenarios.

Exit gate:

- No API caller can self-verify an identity, self-assign a role, or access another tenant.

## 5. `codex/worker-control-plane`

Purpose: separate planning and public APIs from privileged execution.

Deliverables:

- Define signed, expiring, audience-bound worker task leases.
- Run synthesized code only in isolated workers.
- Require Docker or stronger isolation for untrusted scripts.
- Restrict the subprocess backend to explicitly trusted local skills.
- Add worker health, capacity, cancellation, timeout, and quarantine behavior.
- Support outbound-only local agents for desktop and private-network resources.

Exit gate:

- The control plane never executes untrusted code or holds local desktop credentials.
- Lost, duplicated, expired, or revoked leases cannot execute actions.

## 6. `codex/provider-adapters`

Purpose: replace provider simulations with contract-tested production integrations.

Deliverables:

- Implement Google authorization-code/OIDC exchange, provider scope mapping, refresh,
  revocation, and callback validation.
- Implement OpenAI project/API-key and organization-managed service identity adapters.
- Implement Anthropic API-key and managed enterprise gateway adapters.
- Add capability discovery, rate limits, budgets, request identifiers, retries, and
  provider error classification.
- Keep credentials exclusively behind the vault/gateway boundary.
- Add sandbox and remote-mock contract tests before optional live tests.

Exit gate:

- Provider adapters pass the same capability, revocation, idempotency, and secret-leakage
  contract suite.

## 7. `codex/desktop-shell`

Purpose: ship the first usable product surface as a local single-user alpha.

Recommended composition:

```text
desktop UI
-> local loopback API or named pipe
-> unified Workflow-GPS service
-> SQLite and filesystem artifacts
-> operating-system credential vault
-> isolated local worker
```

Deliverables:

- Task entry and guided-question UI.
- Route preview with cost and exclusion explanations.
- Confirmation, approval, and incident inboxes.
- Workflow timeline, cancellation, recovery, and audit views.
- Provider connection management using the OS credential vault.
- Docker/worker health and clear trusted-versus-untrusted execution labeling.
- Offline policy and local data export/deletion.

Exit gate:

- A non-developer can complete, pause, resume, inspect, and recover a local workflow.
- The UI cannot bypass backend policy or expose provider credentials.

## 8. `codex/http-gateway`

Purpose: add a private HTTP control-plane prototype after durable runtime and identity are
ready.

Recommended composition:

```text
HTTP API
-> PostgreSQL transaction and outbox
-> durable queue
-> isolated remote or local worker
-> event stream
-> status/SSE API
```

Deliverables:

- Versioned REST/OpenAPI contracts for contracts, runs, questions, routes, approvals,
  incidents, provider connections, and feedback.
- OIDC authentication, tenant-aware RBAC, quotas, rate limits, and request idempotency.
- Asynchronous job submission; do not expose `wfgps run` as a long synchronous request.
- SSE or WebSocket progress, verified webhooks, pagination, and cancellation.
- Security headers, CORS/CSRF policy, audit export, operational metrics, and tracing.
- Deployment and disaster-recovery documentation.

Exit gate:

- Multi-process and cross-tenant tests pass under concurrent load.
- Restart, retry, timeout, duplicate submission, and webhook replay tests pass.

## Recommended sequence tomorrow

1. Create and finish `codex/stabilize-v0.2-baseline`.
2. Start `codex/unified-orchestrator` and define the canonical run-state ADR.
3. Choose the desktop shell technology only after the local service boundary is fixed.
4. Avoid starting the public HTTP gateway until durable runtime and identity/RBAC land.

## Product recommendation

Build the desktop alpha first. It matches the current local-first architecture, provides
fast user feedback, and avoids prematurely committing to multi-tenant operations. Treat the
HTTP gateway as a control-plane project, not a thin web wrapper around the CLI.
