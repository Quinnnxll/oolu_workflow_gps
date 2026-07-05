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

- Reserve release — the holdback is a loan, not a fee: the settlement
  reserve target is now scoped to the chargeback **risk window**
  (`risk_window_days`, default `DEFAULT_RISK_WINDOW_DAYS = 90`; `None`
  restores accumulate-forever). The true-up is symmetric: fresh earnings
  top the reserve up, and accruals that age out of the window release
  their share back to the noder as one more RESERVE entry — paid out on
  the next settlement, so the noder eventually receives 100% of
  undisputed earnings. Aged-out accruals demand no reserve at all
  (only at-risk earnings are held against).
- Dispute deepening — reserve-funded clawbacks, final decisions:
  upholding a dispute still reverses every accrual the event minted
  (CLAWBACK entries, per noder), but a shortfall from already-paid
  earnings is now funded from the noder's RESERVE first — the settlement
  holdback finally doing the job it exists for — via a negative RESERVE
  release entry, so the balance projection stays one formula. Only what
  the reserve cannot cover remains as honest negative balance (debt)
  that future accruals repay before anything pays out again. The
  settlement reserve target now nets clawbacks (reversed earnings no
  longer demand reserve, so a clawback isn't re-collected as a fresh
  top-up). Decisions are final: uphold-after-reject and
  reject-after-uphold raise, the same decision twice is a no-op/replay,
  and both resolutions are audited (`dispute.upheld` with clawed/drawn/
  debt micros, `dispute.rejected`). Uphold reports a per-noder breakdown.
- Settlement cycles + payment-failure containment:
  `SettlementService.settle_all(period_key=...)` settles every noder on
  the ledger (`EarningsLedger.principals()`) for one period — outcomes
  are per-noder and independent, so one processor failure never blocks
  anyone else's payout; the cycle summary (paid/failed/skipped counts
  and paid micros) is appended to the durable audit as
  `settlement.cycle`. A `PaymentError` inside `settle` is now a
  first-class outcome instead of a crash: the batch is marked FAILED for
  the record, the ledger is never debited, and the period's idempotency
  claim is released via the new `IdempotencyLedger.release(key)` — fixing
  a real poisoning bug where a raised `fn` left a claim that replayed
  `None` forever. Re-running the same period IS the retry mechanism:
  paid noders replay their cached receipts (the processor is never
  called twice), failed ones get a fresh attempt with a fresh batch.
- Approver notification — the holds SSE feed:
  `GET /v1/runs/contract/holds/events` streams the tenant's hold
  lifecycle so approvers subscribe instead of polling the listing. Same
  snapshot semantics as the per-run event stream: frames are derived
  from the audit log (`contract.held` is now audited at hold time on
  both surfaces, and held/approved/declined/expired payloads carry the
  tenant), so nothing is invented for the transport and the feed is
  strictly tenant-scoped. Each frame carries `id: <seq>`; `?after=<seq>`
  resumes past frames already seen (SSE Last-Event-ID semantics). The
  request itself sweeps, so an expiry becomes an event, never silence.
- Hold expiry: a held reserved contract carries an `expires_at` stamped
  at submission (the promise made then — TTL changes never retroactively
  extend old holds). Gateway: `GatewayConfig.contract_hold_ttl_seconds`
  (default 7 days; `None` = never), `expires_at` on the 202 response and
  hold listings, and a late decision returns 410 `expired`. Desktop:
  `hold_ttl_seconds` (+ injectable `clock`) ctor knobs, default never.
  Expiry is lazy — `PendingContractStore.sweep_expired` runs on every
  list/inbox and decision, so a stale hold can never rot in the queue or
  be released long after the submitter's intent went cold; each sweep is
  audited per hold as `contract.expired`.
- Gateway hold-for-approval for reserved contracts: `POST
  /v1/runs/contract` no longer 403s a contract with reserved actions —
  it HOLDS it (202 `awaiting_approval` with a `pending_id`, idempotent
  under the Idempotency-Key, budget knobs captured at submission).
  `GET /v1/runs/contract/holds` lists the caller tenant's holds;
  `POST /v1/runs/contract/holds/{pending_id}` decides one. Decisions are
  tenant-scoped (another tenant's hold is a 404 — existence never
  leaks), require approve authority in the hold's own tenant (the
  submitter's own token gets 403 and the hold survives), re-run the
  budget gate on the SUBMITTER's terms and histories (402/409 leave the
  hold intact), and execute with the run bound to the ORIGINAL
  submitter — the approver authorizes, never takes the consumer seat.
  Declining removes the hold; both outcomes are audited with the
  decider's principal. The shared `PendingContractStore` moved to
  `nodeplace.holds` (table `pending_contracts`, records now carry the
  submitting tenant/principal, `list(tenant=...)` filters) and backs
  both surfaces, so gateway holds also survive restarts and every
  process over one database sees one consistent set.
- Held approvals survive restarts: pending reserved contracts moved from
  process memory into the shell's own durable database
  (`desktop.pending.PendingContractStore`, table
  `desktop_pending_contracts`) — a hold is a commitment the user made,
  so it lives with the runs. The record stores the contract as posted
  plus the budget knobs captured at confirm time; the compiled blueprint
  is deliberately NOT persisted (script bodies mint fresh action ids per
  compile) — whichever process decides the hold recompiles once and
  executes exactly what it inspected. A fresh `DesktopService` over the
  same durable connection lists and decides holds made before a restart,
  and every service over that store sees decisions immediately.
- Loopback route for the approval decision:
  `POST /v1/assembly/approvals/{pending_id}` with `{"approved": bool}`
  decides a held reserved contract from the desktop UI. The loopback
  stays a no-auth boundary with one deliberate exception: this route
  REQUIRES an `Authorization: Bearer` token, which
  `DesktopService.decide_assembly` turns into a verified identity
  session (`SessionManager.login`) before handing off to
  `approve_assembly` — caller text never becomes authority. Missing/bad
  token -> 401, valid-but-unauthorized principal -> 403 (the hold
  survives every failed attempt), missing `approved` field -> 400,
  unknown or already-decided hold -> 404, no session manager wired ->
  404. New `session_manager` ctor hook on the shell.
- Desktop reserved contracts become approvable inbox tasks: confirming a
  contract with reserved (irreversible) actions no longer 403s — it is
  HELD (`awaiting_approval`) and appears in the inbox as kind
  `contract-approval`, naming the reserved operations.
  `DesktopService.approve_assembly(pending_id, session=...)` decides it:
  approval mints from a verified identity session (same
  `IdentityApprovalAuthority` gate as run approvals — an unauthorized
  session raises and the hold survives), re-runs the budget gate (prices
  may have moved while held; approval grants the reserved actions, not
  the money), then executes through the shared money path; declining
  removes it. Both outcomes are audited with the decider's principal.
  `nodeplace.execution` splits `compile_contract` (no reserved gate, for
  approval flows) + `reserved_operations` out of `compile_runnable`
  (which still refuses — the gateway's unattended path is unchanged).
- Recency decay on spending profiles: history weighs `recency_decay`
  (default 0.9) less per run back, so comfort tracks where spending is
  *trending*. `SpendingProfile.typical` is now a recency-weighted median,
  and the ceiling is driven by `recent_peak` — a decaying maximum — so
  one lavish run long ago stops waving outliers through as it ages, and
  a user who has tightened gets a ceiling that followed them down; `peak`
  stays the raw historical maximum for honest display. Applies to global
  and class profiles alike (histories are most-recent-first, as
  `consumer_spend` returns them); `recency_decay: 1.0` in the budget
  policy restores flat history exactly.
- Per-goal-class spending profiles: behavioral budgets are judged within
  the plan's own class of goal — spending lucratively on gifts while
  keeping everyday automation tight is two different spenders, and
  neither habit loosens (or flags) the other. `RunBinding` gains a
  `goal_class` (the class key of the run's costliest child, stamped by
  `execute_contract` and `build_run_binding`), `consumer_spend` filters
  by it, and `estimate_contract_gross` returns a `ContractEstimate`
  (gross + dominant class). `assess_budget` is class-first: a class with
  enough history REPLACES the global profile for the behavioral check
  (reasons name the class); a class with thin history falls back to the
  global profile — so a first lavish run in a new class gets exactly one
  review, then the class speaks for itself. Verdicts carry `goal_class`
  and `class_profile`; `preview_assembly` takes a `spend_lookup`
  (class -> history) since the plan's class is only known after assembly.
- Cost-aware assembly budgets (`nodeplace.budget`): three signals with
  three authorities judge an assembled plan's estimated cost. A
  caller-set `hard_cap` refuses outright (`BudgetExceededError` -> 402
  `budget_exceeded`; no acknowledgement overrides it); a user-set
  `review_threshold` holds the run (`ReviewRequiredError` -> 409
  `review_required`) until `review_acknowledged: true`; and a
  **behavioral comfort ceiling** learned from the user's own committed
  run grosses (`AttributionStore.consumer_spend`; review above BOTH
  median x multiplier AND their demonstrated peak, never judged on
  fewer than 3 runs) flags outliers even with no declared budget.
  The linked wallet is deliberately the weakest signal — its balance may
  be a slice of the user's true assets, so it NEVER caps or scales the
  budget: an estimate above the remaining balance only adds a review
  reason, and a large balance grants nothing. Estimation
  (`estimate_contract_gross`) clears in preview mode, so the gate runs
  BEFORE any price commits or binding writes. Reasons accumulate across
  all signals like quote warnings. Wired everywhere: verdicts ride
  `/v1/market/assemble` and the desktop preview (`budget` field, from a
  `budget` request object / `budget_cap` + `review_threshold` params);
  enforcement guards `POST /v1/runs/contract` and the desktop confirm
  (403 at the loopback); `wallet_lookup` ctor hooks on both surfaces.
- Trace-derived learned orderings in assembled subgraphs: when the
  caller's own runs consistently completed one child before another
  (`TraceStore.derive_edges`: enough observations, one direction nearly
  always, transitively reduced), `preview_assembly` stamps that order
  onto the assembled contract as `provenance="learned"` `ContractEdge`s —
  which the compiler already turns into real dependencies, so the
  scheduler stops racing steps the user's history says are ordered. Slot
  flow outranks statistics: learned edges that data-flow or explicit
  edges already imply or contradict are dropped (a contradiction stays
  parallelism, never a learned cycle), and ambiguous child names are left
  out. Surfaced as `learned_order` (`[{"first", "then"}, ...]`) on the
  assemble response and the desktop `AssemblyPreviewView`.
- Thompson-sampled assembly (`explore: true`): `preview_assembly` accepts
  an `rng` and passes it to `ContractAssembler`, so producer picks are
  sampled from the same personalized Beta posteriors instead of taken
  greedily — unproven alternatives get chances proportional to their
  remaining uncertainty, and exploration collapses onto the winner as
  confirmed runs accumulate. Opt-in per request: `explore: true` on
  `POST /v1/market/assemble` and on the desktop's
  `POST /v1/assembly/preview` (`DesktopService.assembly_preview(...,
  explore=True)`); the default stays deterministic (best posterior mean,
  stable tie-breaks) — the right mode for a preview the user is about to
  pay for. The gateway and shell hold a seedable `rng` (ctor param).
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
