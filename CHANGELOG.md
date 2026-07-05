# Changelog

All notable changes to Workflow-GPS are documented here.

## Unreleased

Reward & pricing system (`claude/oolu-workflow-planning-review`) — the
economic layer for Noders and route planning; design in
`docs/REWARD_PRICING_DESIGN.md`.

- Added `nodeplace.economics`: `CandidateAssembler` joins the registry,
  metering ledger (verified successes + measured provider cost), audit log
  (real failure counts via run bindings), and rating store into
  `CandidateEconomics` + `RewardSignals` per listing, with substitutes
  computed per class key; listing tags (`class:`, `market:`) carry market
  classification, and the contribute endpoint now accepts a `pricing` ask.
- Added gateway routes `GET /v1/market/candidates` (utility-ranked live
  candidates with cleared-price breakdowns and reward multipliers;
  read-only — browsing previews prices without moving the book) and
  `POST /v1/market/quotes` (full workflow quote from live economics;
  previews by default, never a ledger write), documented in the OpenAPI.
- `PriceBook.clear` and `QuoteEngine.quote` gained preview modes
  (`commit=False` / `commit_prices=False`) so read paths cannot shift
  market reference prices.
- `POST /v1/runs` accepts an optional `node_version_id`: the gateway
  assembles that version's live economics, clears the price (committing —
  a real run moves the market), and binds the run to its noder shares via
  `build_run_binding` inside the idempotent submit, before returning. The
  metering deriver turns the binding into earnings only when the audit log
  shows a platform-verified success for that run — closing the last manual
  gap between quoting and the exactly-once earnings pipeline. Unlisted or
  revoked versions are refused; plain runs are untouched.

- Added `nodeplace.market`: node pricing classes (commodity / workflow /
  professional / regulated pass-through), `CostVector`, and a persisted
  `PriceBook` that clears asks through cost floor -> competition pull ->
  value anchor -> per-class damping bands, with an explainable
  `ClearedPrice` breakdown. Regulated fees pass through untouched.
  Route economics (`utility`, `rank_candidates`) score candidates by
  platform-verified quality per retry-adjusted dollar under four quote
  modes (budget/standard/premium/certified) — never by self-declared
  quality.
- Added `nodeplace.rewards`: bounded reward multipliers from non-gameable
  signals (ratings reputation, metered reliability, scarcity, maintenance,
  commodity decay), class-aware platform commission (lowest for scarce
  professional supply, zero on pass-through), geometric lineage royalties
  for derived nodes, and `build_run_binding` — the bridge into the
  exactly-once metering -> billing -> ledger -> settlement pipeline, so
  money still moves only on platform-verified success and every split
  conserves to the micro.
- Added `nodeplace.quotes`: `QuoteEngine` with subscription coverage vs
  outside-plan pass-through lines, retry-adjusted budget projection,
  accumulating budget/quota warnings, per-step noder payout *previews*
  (forecasts, clearly labeled — never ledger entries), and usage settling.

Adaptive planning (`claude/oolu-workflow-planning-review`) — implements the
typed-capability-graph proposal in `docs/WORKFLOW_PLANNING_REVIEW.md`; the
planner now grows automatically with the user's executions and learned skills.

- Confirmed runs feed the TraceStore: `execute_contract` accepts a
  `trace_store` (+ `trace_context`) and records one node-granular trace
  per run — each top-level child's verdict (a child succeeds only if
  every action it contributed did), the price it actually cleared at as
  its cost EWMA, and completion order into the precedence matrix — under
  the same `route:{name}` keys the assembler scores by.
  `compile_with_owners` (orchestrator) returns the blueprint plus an
  action-to-child attribution map from ONE compile pass (script bodies
  mint fresh action ids per compile, so a second pass would not match);
  `compile_runnable` now returns a `CompiledContract` carrying both.
  On the pick side, `preview_assembly` folds the caller's own history
  into each contract's `NodeStats` (evidence adds; the personally paid
  cost supersedes the listed one). Gateway: new `trace_store` ctor param,
  bucketed per tenant (`trace_context=tenant_id`) so one tenant's
  failures personalize only their own picks; desktop: `trace_store` ctor
  param on the shell (single user: the global bucket). Every confirmed
  run sharpens the next assembly — no separate training step.
- Desktop confirm button: `DesktopService.confirm_assembly` runs the
  contract the preview returned — through the shared
  `nodeplace.execution.execute_contract`, the exact code path behind the
  gateway's `POST /v1/runs/contract` (extracted in this change), so there
  is one place where contract runs turn into money: committed per-node
  clearing, one aggregate lineage-weighted `RunBinding`, and the
  deriver-payable `workflow.executed` audit event. Served over the
  loopback at `POST /v1/assembly/confirm`; reserved actions are refused
  with 403 (`ReservedActionsError`, a `PermissionError`), executors are
  backend-configured (never UI-supplied), and a client `confirm_id` makes
  the click idempotent — double-clicks replay the first result without
  re-executing.
- Direct contract execution + desktop assembly preview: `POST
  /v1/runs/contract` takes the contract `/v1/market/assemble` returned,
  compiles it to a DAG blueprint (`contract_to_blueprint`), and executes it
  on the gateway's configured `contract_executors` (`DagRouteRunner`) —
  every marketplace node in the subgraph clears at a *committed* price and
  the run gets one aggregate `RunBinding` whose shares merge each node's
  lineage split weighted by its cleared price, so the metering deriver pays
  every noder in the chain from the same platform-verified audit event.
  Reserved (irreversible) actions are refused with 403 — those still
  require the orchestrator's approval flow. The shared preview computation
  moved to `nodeplace.assembly.preview_assembly`, and the desktop shell
  surfaces it: `DesktopService.assembly_preview` (optional
  `market`/`price_book` wiring) maps it into the secret-free
  `AssemblyPreviewView`, served over the loopback at
  `POST /v1/assembly/preview` — read-only, prices never commit.
- Slot vocabularies on listings + goal-based assembly over the marketplace:
  `Listing` gains typed `consumes`/`produces` slots — declared at
  contribution (service + gateway body fields) or derived from the skill
  itself (induced parameters -> consumes, artifact validators ->
  produces). `CandidateAssembler.contracts(query)` turns every active
  public listing into an assembler-ready `NodeContract` (listing slots as
  typed I/O, the sanitized skill's actions as the executable body,
  verified history as stats). `POST /v1/market/assemble` backward-chains
  a goal's wanted slots through those vocabularies and returns the
  assembled subgraph contract with per-node cleared-price previews and
  lineage-aware payout previews — read-only: the price book never moves,
  and no money does either. Missing slots report honestly, or
  (`fill_gaps: true`) become synthesized script gap nodes.
- Goal-directed assembly: `orchestrator/assembler.py` adds
  `ContractAssembler` — give it a `GoalSpec` (wanted slots + slots on hand)
  and a contract library (a list, or a callable over a live registry via
  `contract_from_registered`, which carries trace-store history), and it
  backward-chains producers by verified success (deterministic, or
  Thompson-sampled with an `rng`), skips what is on hand, dedupes shared
  producers, and returns one `SubgraphBody` contract whose ordering falls
  out of slot flow at compile time. Unproducible slots are reported as
  `missing` — or, with `fill_gaps_with_scripts=True`, become synthesized
  `ScriptBody` gap nodes the node-cached script runner realizes and
  memoizes at execution time.
- Lineage records on `NodeVersion`: `contribute(derived_from=...)` (service
  and gateway) records the parent and its ancestors as immutable
  `LineageRecord`s (levels shift by one per generation, capped at
  `MAX_LINEAGE_DEPTH=5`; unknown parents are refused). When a marketplace
  run binds (`POST /v1/runs` with `node_version_id`), royalty ancestors now
  fill automatically from the version's recorded lineage
  (`CandidateAssembler.lineage_for`) instead of caller input — derivation
  provenance is the source of truth, and the geometric royalty split pays
  upstream noders on every verified success.
- NodeContract unification (build-order item 6 — the review is complete):
  `skills/contract.py` defines the one node schema the three vocabularies
  converge on — typed `Slot` consumes/produces, three body kinds
  (`ActionsBody` | `ScriptBody` | `SubgraphBody`), the existing
  `ConstraintSpec` preconditions/validators, a verified-history `NodeStats`
  snapshot, a `fallback` contract, and the canonical `classify_risk` (the
  orchestrator now re-exports it). `NodeContract.from_skill`/`to_skill`
  round-trip losslessly; `derive_data_edges` orders subgraph children from
  slot unification (unrelated children stay parallel; mutual production is
  rejected as a cycle, never silently reordered).
  `orchestrator/contract.py::contract_to_blueprint` compiles any contract
  into an executable DAG blueprint: script bodies become node-cached
  `NodeScriptRunner` actions keyed by the contract id, subgraphs flatten
  recursively, and fallback contracts become repair branches. The
  scheduler's fallback substitution now gates dependents on the *entire*
  multi-step repair (all of a failed trigger's fallback targets), and a
  route only counts repaired when the whole repair verified.
- Node-granular script caching (build-order item 4):
  `runtime/script_node.py` adds `NodeScriptRunner`, an `ActionExecutor`
  (adapter `"script"`) that makes synthesized code a third node body kind
  inside DAG blueprints. Scripts memoize per node — cache key = node key +
  slot-binding fingerprint + environment fingerprint
  (`cache.NodeScriptSignature`), never the parent intent — so the same
  sub-task recurring across different workflows hits the same entry. Hits
  run the cached script straight on the backend (no gateway call); on a
  miss or environment drift only that node re-synthesizes, via
  `GraphEngineSynthesizer` driving the graph engine's full recalculating
  loop for the single node goal. Every synthesis is verified by executing
  through the runner's own backend before it is reported or cached, and a
  repaired script replaces the stale entry on verified success.

- `Blueprint` is a real partial order: `BlueprintEdge` (`before`/`fallback`
  relations, `sop`/`learned`/`data` provenance) plus an `ordering` mode —
  `sequential` (backward-compatible default) chains actions and layers
  explicit edges on top; `graph` runs unrelated actions in parallel.
- Added `orchestrator.scheduler.DagRouteRunner`: a readiness scheduler
  (drop-in `WorkflowExecutor`) with transitive failure cascade (no deadlocks),
  substitution-semantics fallback branches (a repaired failure keeps the
  route green and downstream nodes wait on the repair), per-action timeouts
  via the executor `cancel` hook, cycle/capability preflight, and optional
  per-run trace recording.
- Added `knowledge.traces.TraceStore`: private, SQLite-persisted execution
  statistics — per-node Beta success posteriors (context-bucketed), a
  precedence matrix that recovers a DAG from linear traces under a
  consistency threshold, and per-node cost EWMAs. Replaces sequence
  memorization; statistics accumulate across sessions with no training step.
- Added `orchestrator.adaptive`: `AdaptivePlanner` (blueprints rebuilt from
  the live `SkillRegistry` on every plan, learned edges promoted only with
  sufficient evidence, SOPs compiled in), `ThompsonRouteOptimizer` (route
  choice by sampling the user's own success posteriors, cost as tiebreak),
  `TraceFeedbackSink`, and `apply_sop_to_blueprint`.
- Added `skills.sop`: declarative YAML SOPs (`require_order`, `forbid`,
  `approval`, `require_verify`, `risk_budget`) compiled into hard edges,
  reserved actions, exclusions, and skill validators — human structure the
  learner can never overwrite.
- Generalizing compiler: `DemonstrationCompiler.compile_generalized` diffs
  repeated demonstrations into typed slots (varying values become
  parameters, identical variations unify, workspace paths are templated to
  `{workspace}`), `bind_parameters` rebinds them, and
  `SkillLearner.generalize` runs the same scrub -> compile -> verify ->
  register gate as exact learning.

HTTP gateway (`codex/http-gateway`).

- Added `workflow_gps.gateway`: a private, tenant-aware HTTP control-plane prototype
  as a transport-agnostic application over `Request`/`Response` (a WSGI/ASGI binding
  is the production seam), sitting on the durable runtime.
- Versioned REST surface (`/v1`) for runs/contracts, questions, routes, approvals,
  incidents, provider connections, and feedback, with a served OpenAPI document.
- OIDC bearer authentication, tenant-aware RBAC, per-tenant quotas and token-bucket
  rate limits, and request idempotency (duplicate submissions return one run).
- Asynchronous run submission (`202` + run id; progress via status, SSE event
  stream, or audit export) — never a long synchronous request.
- Verified, replay-protected webhooks (HMAC + timestamp tolerance + delivery-id
  dedupe), pagination, cancellation, security headers, and CORS; operational
  metrics endpoint.
- Added tests for multi-process and cross-tenant behaviour, restart, duplicate
  submission, rate-limit/quota, RBAC, the full clarification/confirmation/approval
  flow, and webhook replay.

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
