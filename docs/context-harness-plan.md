# Unleashing the model — the context-harness build plan

Status: Proposed. Scope: why node creation with external LLM APIs underperforms
the frontier coding harnesses (Claude Code, Codex, Cursor), and the building
phases that close the gap by fixing the harness — not the model.

Companion reading: `docs/model-seats.md` (the seat discipline this plan extends),
`docs/node-generation.md` (the authoring contract), and
`docs/WORKFLOW_PLANNING_REVIEW.md` (earlier candid findings this plan builds on).
The external reference is the *Continuity Context Harness* specification
(canonical context packs, budgeted retrieval, canonical model interface,
verify-then-write-back); section 4 maps its vocabulary onto OoLu's modules.

---

## 1. The diagnosis — the model was never the bottleneck

Node creation is unstable, spends far too little effort, and performs
unreliably **because the harness starves the model four ways at once**. The
same frontier models behind Claude Code and Cursor are being consulted through
a keyhole. Every claim below is anchored in the current source.

### 1.1 Effort starvation

The `node.build` seat — the call that authors an entire node function — runs at
a fraction of the effort a frontier coding harness would grant:

- **1024 output tokens for a whole program.** `ChatModelRouter` defaults
  `max_tokens=1024` (`providers/chatmodel.py:135`), the Anthropic adapter
  defaults the same (`providers/apikey.py:256`), and neither production
  construction overrides it (`gateway/app.py:5273-5288`,
  `orchestrator/assembly.py:832`). The author must fit a numbered plan, an
  `IO:` JSON line, *and* a complete self-contained Python script inside 1024
  tokens. The existence of `_TRUNCATED_FENCE_RE`
  (`routing/gateway.py:57-59`) — a regex written to salvage scripts cut off at
  `finish_reason=length` — is the codebase admitting truncation is routine.
  Meanwhile the runtime synthesis tier gets 4096 (`routing/matrix.py:36-53`).
- **The fast tier by default.** Authoring follows `model.tier` (default
  `fast`) unless `model.build_tier` overrides it (`gateway/app.py:5261-5269`,
  `settings_node.py:292-309`). Default node authoring is Haiku/`gpt-4o-mini`
  writing production code one-shot.
- **No reasoning effort anywhere.** No `thinking`, `budget_tokens`, or
  `reasoning_effort` parameter is constructed on any path (grep-confirmed).
  "Reasoning tier" only means "a bigger model." The frontier harnesses spend
  thousands of thinking tokens per edit; OoLu spends zero.
- **No sampling control on authoring.** Neither temperature nor top_p is sent
  on the chat/authoring path (`providers/apikey.py:165-190`,
  `providers/chatmodel.py:531-614`) — code generation at provider-default
  temperature (~1.0), with the synthesis stack's careful ladder
  (temp 0.1→bump→escalate, `routing/matrix.py:148-213`) not applying here.
- **One shot, no loop, for most models.** The agentic `NodeAuthorAgent`
  (`author.py:92-219`, `max_steps=6`) engages only when the seated model
  exposes tool calling (`gateway/app.py:5324`); otherwise
  `author_node_function` (`chat.py:1094-1158`) is a single `reply()` with no
  retry, no self-correction, no escalation.

### 1.2 Context starvation

Claude Code and Cursor push a curated map of the workspace into every request.
OoLu's author writes nearly blind:

- The one-shot author sees **the system prompt and the goal sentence, nothing
  else** (`chat.py:1094-1158`) — no existing node contracts, no slot
  vocabulary in use, no upstream/downstream output shapes, no examples of
  verified node functions, no lessons.
- The agentic author may *pull* context, but only if it chooses to call the
  tools, and the taps are narrow: the catalog is capped at 40 nodes
  (`gateway/app.py:5412`), upstream evidence at the last 3 runs
  (`gateway/app.py:5432`).
- The synthesis engine's prompt is deliberately three messages — system +
  intent + latest error (`routing/prompting.py:135-183`). Only the *latest*
  error is shown even though `error_history` is fully retained
  (`models/state.py:92`); the model can re-make a mistake from two rounds ago.
- Reuse is exact-match only: the script cache keys on the exact intent
  sentence (`cache/signature.py:20-45`); a paraphrase misses. Prior verified
  scripts never enter a prompt as examples.
- Chat context is the client's last 20 turns (`gateway/app.py:1661`), no
  summarization or compaction anywhere (grep-confirmed), no token counting
  (`tiktoken`/`count_tokens` absent), no context-window management at all.

### 1.3 No verification at birth

The platform's proudest muscle — verify-by-execution — is not exercised where
instability is felt:

- The one-shot path publishes after `mock_smells` alone (`chat.py:1139-1158`):
  no `screen_script`, no `emit_result` presence check, **no execution**. A
  plausible-but-broken script is persisted as `src/main.py` and fails on the
  first real run — which is exactly what "node creation is unstable" looks
  like from the outside.
- A malformed `IO:` line degrades *silently* to
  `{inputs:[], outputs:[result:str]}` (`chat.py:1053-1060`) — a node that
  actually needs inputs is published input-less, and route chaining breaks
  with no warning.
- The runtime has a proper ladder (dependency heal ×3 → model repair ×2 →
  resynthesize, `runtime/script_node.py:434-624`); creation has none of it.

### 1.4 A fragile output channel and two divergent stacks

- Fenced-block scraping (`extract_script`, `routing/gateway.py:64-99`,
  last-block-wins plus "smells like Python" fallback) and prose-regex IO
  parsing (`chat.py:1044-1076`) are the delivery channel for most authoring,
  while a genuine schema-validated tool harness already exists and works
  (`providers/tools.py`: canonical transcripts, `ToolSpec`, argument
  validation, bounded `run_tool_loop`).
- Two parallel LLM stacks — LiteLLM synthesis (`routing/gateway.py`) and
  hand-rolled chat providers (`providers/apikey.py`) — with different
  transports, retry logic, parameter handling, and hardcoded model lists
  (`providers/chatmodel.py:50-59` has no config/env override). Chat retries
  back off with a **no-op sleep** (`providers/base.py:126`); the LiteLLM
  gateway has no request-level retry at all (`routing/gateway.py:166-171`).
  No Anthropic `cache_control` is ever emitted, so the paid path re-pays full
  input cost on every tool-loop step despite the prefix-cache discipline built
  for local vLLM (`routing/prompting.py:1-26`).

### 1.5 What the frontier harnesses do differently

| Capability | Claude Code / Codex / Cursor | OoLu today |
| --- | --- | --- |
| Output budget | 16k–64k tokens + streaming | 1024 (authoring), 4096 (synthesis) |
| Reasoning effort | Extended thinking / effort knobs, escalated on hard tasks | none |
| Context | Pushed, curated, budgeted: repo map, related code, conventions, diagnostics | goal sentence (+ optional pull tools) |
| Loop | Agentic: read → edit → run → observe → repair until green | one shot (default path) |
| Output contract | Native tool calls / structured outputs, schema-enforced | regex over prose (default path) |
| Verification | Runs tests/linters before presenting | deferred to the node's first real run |
| Memory | Persistent task state, compaction that preserves commitments | 20-turn client window, no compaction |

The conclusion writes itself: **the same API key, driven the way the frontier
harnesses drive it, is a different product.** The phases below get there in
order of leverage.

---

## 2. Principles (adopted from the Continuity Context Harness)

These carry the spec's invariants into OoLu's vocabulary; each phase is
measured against them.

1. **Every model receives the same canonical request through an adapter** —
   provider syntax, caching markers, and tool dialects live only in adapters.
2. **Context selection is relevance-based, provenance-aware, and budgeted** —
   context is *pushed* by the harness, not left for the model to pull.
3. **Models reason over context but do not become the source of truth** —
   already OoLu law (`docs/model-seats.md`: "a seat never trusts a model");
   verification by execution decides what output is worth.
4. **Important commitments survive compaction** — exact goals, IO contracts,
   errors, and constraints are preserved verbatim; only chatter compresses.
5. **Effort is proportional to the work** — output budgets, thinking budgets,
   and loop depth scale with task difficulty and escalate on failure, the way
   `routing/matrix.py` already escalates tiers.

---

## 3. The building phases

### Phase 0 — Baseline: measure the seat before re-upholstering it

*Objective: a scoreboard every later phase must move. The repo has Level B
auditions for planning (`benchmarks/level_b_audition.py`) but no numeric
node-authoring benchmark at all.*

- [x] **Node-authoring benchmark** (`benchmarks/node_authoring.py`): a fixed
      suite of goals spanning easy (slugify) to hard (multi-input transforms,
      brokered `http_request` use, downstream-shape matching) plus judgement
      goals that must be declined — landed as 24 goals (21 build + 3
      conversation). Each goal runs the real authoring paths (one-shot
      `author_node_function` or the `NodeAuthorAgent` loop, dispatched the
      way `gateway/app.py:_author_function` does), then verifies by executing
      the function against the real runtime contract (`sandbox_shim`,
      `bindings.json`, envelope parsing) with a refusing web broker. Reports
      per goal: first-pass validity, verified/answer/interface rates,
      truncation (`finish_reason=length`) flags, tokens in/out, cost, wall
      time, heals. A scripted incumbent holds the reference FIT line offline
      (`tests/test_node_authoring_bench.py` pins it); `main()` is the live
      audition in the Level B pattern, with `--max-tokens` to preview the
      Phase 1 ceiling lift.
- [x] **Per-seat effort telemetry**: `ModelCallRecord` now books
      `finish_reason` and `context_chars` per call
      (`billing/model_calls.py`), fed by `ChatModelRouter` on all four paths
      (`providers/chatmodel.py:_book`); rounds per task = records per
      purpose. "Did `node.build` truncate, and how starved was the call?"
      is now answerable from the books alone.
- [x] **Failure taxonomy**: benchmark failures classify into refused,
      no_script, mocked, truncated, bad_interface, missing_dependency,
      contract_violation, script_error, wrong_answer, built_conversation,
      timeout, transport — the acceptance axes for Phases 1–4.

**Deliverable:** a reproducible baseline scoreboard checked into
`benchmarks/`, run against at least one Anthropic and one OpenAI keyed model.
(The bench and its offline reference are in; the keyed baseline runs are the
remaining step — `python benchmarks/node_authoring.py` with a key set.)

Two findings the bench surfaced for Phase 4, documented in its docstring:
production's `_author_verifier` stages no `bindings.json`, so an honest
function that reads its declared inputs cannot pass the production verify
hand today; and it mounts no web exchange, so `http_request` raises
`WebGrantError` there instead of answering the taught status-0 refusal.

---

### Phase 1 — Unstarve the model (days, not weeks — highest leverage per line)

*Objective: remove the artificial ceilings. No architecture changes; only
parameters, defaults, and retry hygiene.*

- [x] **Per-seat generation budgets.** `providers/profiles.py`: a
      `SeatProfile` table keyed by the seat vocabulary — `node.build` /
      `node.repair` / `plan.rebuild` → 16k output tokens + temp 0.2;
      `plan.synthesize` → 8k; `chat.turn` → 4k; `plan.intake` / `plan.route`
      → 2k; unknown purposes → 4k, never the old 1024. `ChatModelRouter`
      resolves the profile from its purpose (constructor `max_tokens` remains
      an explicit override for benches). The OpenAI path now carries
      `max_tokens` + `temperature` too — the per-provider asymmetry is gone.
- [x] **Reasoning effort.** Anthropic `thinking: {type: enabled,
      budget_tokens: 4096}` rides on `reply` calls for thinking-capable
      models (capability-gated, budget floored/fitted under the ceiling,
      temperature correctly dropped beside it); OpenAI reasoning models
      (o-series/gpt-5) get `reasoning_effort` + `max_completion_tokens`.
      One deliberate hold-back: thinking stays OFF on tool consultations
      until the canonical transcript can carry thinking blocks back across
      tool turns (Anthropic requires them re-sent verbatim) — Phase 2 lifts
      this.
- [x] **Default authoring to the reasoning tier.** `model.build_tier` now
      defaults to `reasoning` (`settings_node.py`, `gateway/app.py:_tier_now`);
      `inherit` remains for users who prefer the shared tier. The meter and
      budget caps keep spend honest.
- [x] **Sampling control.** Code seats author at temperature 0.2 on every
      provider that accepts one. (The rut-driven bump ladder waits for
      Phase 4's build transaction — the one-shot path has no failure loop to
      bump inside yet.)
- [x] **Retry hygiene.** The provider backoff is real (`_default_backoff` in
      `providers/base.py`, late-bound so offline tests neutralize it in one
      conftest fixture), and the LiteLLM gateway retries transient failures
      (rate limit / timeout / connection / 5xx, matched by exception name)
      with backoff before surfacing `GatewayError` — via an injectable
      `completion_fn` seam.
- [x] **Prompt caching on the paid path.** The Anthropic system prompt now
      rides as a block with `cache_control: {type: ephemeral}` — the frozen
      prefix (tools + system) gets its breakpoint, so multi-turn authoring
      loops stop re-paying the full prompt every step.
- [x] **Configurable chat-model registry.** `chat_model_for(provider, tier)`
      reads `OOLU_CHAT_MODEL_<PROVIDER>_<TIER>` env overrides at call time,
      falling back to `DEFAULT_MODELS` — a model rename is a config change.
      (Full config-file manifests arrive with the Phase 2 registry.)

**Acceptance:** benchmark truncation rate ≈ 0; first-pass validity up
materially with no other change; per-seat telemetry shows `node.build`
spending an order of magnitude more thinking+output tokens than today.
(Pinned offline by `tests/test_effort_unlock.py` — wire bodies asserted per
provider; the keyed before/after benchmark runs remain the live acceptance.)

---

### Phase 2 — One canonical model interface

*Objective: the spec's `canonical_model_interface` + `model_registry`. Today's
two stacks mean every fix lands twice and every capability check is an
`hasattr` probe (`gateway/app.py:5324`).*

- [x] **Canonical request.** The chat stack now constructs every provider
      request through ONE path: `reply` / `consult` / `structured` all route
      through `_execute` into `_call_provider` / `_call_local`
      (`providers/chatmodel.py`) — `reply` is simply the request with no
      tools, and the per-provider wire branches exist exactly once. The
      neutral transcript gained a provider annex: Anthropic thinking blocks
      ride `ToolReply.thinking_blocks` verbatim, re-attached by the Anthropic
      renderer and shed by every other dialect — which lifted Phase 1's
      hold-back, so the seat's reasoning budget now rides tool consultations
      too. (Remaining, deliberately: the synthesis stack keeps its LiteLLM
      transport and `AssembledPrompt` — same generation vocabulary, separate
      wire — until a later consolidation proves worth the churn.)
- [x] **Model registry manifests.** `providers/registry.py`: declared
      manifests for the tier models, conservative family inference for
      unknown ids (unrecognized local tags = no native tool calling — the
      fenced-code path exists for exactly them), and an
      `OOLU_MODEL_MANIFESTS` JSON overlay for operators. Routing asks the
      manifest, not the object shape: `ChatModelRouter.answering_model()` /
      `manifest_now()` / `consult_ready()`, and the authoring door
      (`gateway/app.py:_author_function`) dispatches on `consult_ready` —
      the old `hasattr(consult)` probe never distinguished models at all
      (every router has `consult`). The adapter capability predicates moved
      into the registry: one table for routing AND wire construction.
- [x] **Structured output as the contract for capable models.**
      `ChatModelRouter.structured(messages, schema=...)`: a schema-forced
      synthetic tool, arguments validated before return, a correction round
      on violation, `StructuredOutputError` instead of a silent default. And
      the one-shot prose channel is honest now: a PRESENT-but-broken `IO:`
      line refuses the build with the problem named
      (`chat.py:parse_node_io_checked`) — an absent line stays lenient for
      the no-tool local models the prose channel serves.
- [x] **Token accounting.** `providers/tokens.py`: `estimate_tokens` /
      `count_request_tokens` — a deterministic, deliberately-conservative
      character heuristic by default, real tiktoken counting for
      OpenAI-family ids when the `tokens` extra is installed. The counting
      seam Phase 3's budgeter compiles against; the provider's own `usage`
      remains the after-the-fact truth the meter books.

**Acceptance:** the spec's tests — "model provider changes mid-task → task
continues from canonical state" (pinned: a thinking+tool transcript renders
onto the OpenAI wire with thoughts shed and the task intact); "a model
proposes an unsupported tool call → rejected before execution" (pinned:
`ToolRouter.dispatch` refuses undeclared names before any handler). One code
path constructs every keyed chat request. All in
`tests/test_canonical_interface.py`.

---

### Phase 3 — Context packs: push the right context into the seat

*Objective: the spec's `context_pack_compiler` + `code_context_engine`,
scoped to the two seats that write code (`node.build`, `plan.synthesize`).
Stop making the model guess what the workspace looks like.*

- [x] **The node-library retrieval.** Similar nodes are retrieved by
      token-overlap cosine over title+goal (`contextpack.similarity`) and
      their verified `src/main.py` read seat-scoped from their drawers
      (`_node_drawer_read`, the `node.build` seat) — retrieval feeds prompts;
      the exact-match cache stays for replay. The ranking is deliberately a
      seam: Phase 5's embedding index replaces the scorer, not the pack.
- [x] **The build context pack** (`src/oolu/contextpack.py`, wired in
      `gateway/app.py:_author_context` for BOTH authoring paths — pushed, not
      pull-only): the slot vocabulary in circulation, **route position**
      (recent verified output shapes of the upstream nodes the goal names —
      the #1 silent failure closed), similar node contracts, and 2–3 verified
      example functions. The pack rides ahead of the request via
      `compose_build_request`; the frozen system contract keeps its cache
      breakpoint. Lessons ride through the compiler's `lessons` port
      (error-pattern wiring lands with Phase 5's write-back). And the **full
      error ledger** now reaches the model: distinct earlier failures render
      in the synthesis action message (`routing/prompting.py:_render_action`,
      still cache-safe — fingerprint pinned) and the runner's second repair
      round carries round one's failure inside the error text
      (`runtime/script_node.py`, no synthesizer signature change).
- [x] **The budgeter.** The pack takes at most 30% of the answering model's
      window (`manifest_now().context_window` when the author exposes it),
      measured with the Phase 2 token seam, compacted in the spec's order —
      verbatim classes (vocabulary, upstream shapes) survive whole; examples
      drop first (lowest score first), then extra contracts, then lessons —
      and **every drop is recorded** in the pack's included/excluded trace,
      logged per call.
- [ ] **Server-side conversation truth.** Deferred to Phase 5 (memory &
      continuity), where the episode summary it needs lives: feed chat turns
      from the persisted `AssistantHistoryStore` rather than the client's
      last-20 window (`gateway/app.py:1661`, `1447-1454`), with a rolling
      summary once the window overflows, under principle 4.

**Acceptance:** benchmark route-position goals now see their upstream shapes
on BOTH paths (`bench_context_pack`, pinned in `tests/test_context_pack.py`);
context-pack traces (included/excluded/tokens) log per call. The live
wrong-shape delta is read off the keyed benchmark runs.

---

### Phase 4 — Verify at birth: the build transaction

*Objective: no node is published without its function having executed
successfully once. Creation gets the same recovery ladder the runtime already
has. This is the phase that kills "node creation is unstable."*

- [x] **One authoring path through one gate.** The one-shot/agentic fork
      remains (the manifest decides the transport a model can be trusted
      with), but every build now converges on the SAME mandatory birth gate
      in `_build_function_node` — the harness drives generate → validate →
      execute → feed errors back for prose-channel models exactly as the
      agent loop does for tool-speakers, with `repair_node_function`
      (`chat.py`) as the correction turn.
- [x] **The build transaction.** `proposed → generated → (repair:…) →
      validated / validated-static → published`, recorded on the hash-chained
      audit log inside the `model.seat` publish event. The gate runs
      `screen_script`, `mock_smells`, the emit_result presence check, and
      interface honesty on EVERY path before `nodeplace.contribute`; sandbox
      verification runs wherever the host carries a script runtime (a host
      without one degrades to `validated-static` — the same posture as
      `require_isolation`). The agent's finish-gate verification is trusted
      for the exact script it delivered, so the run is not paid twice; any
      repaired script re-verifies.
- [x] **The birth-verify primitive.** `NodeScriptRunner.verify_function`:
      the function under test is the function judged — dependency healing
      yes, model repair and resynthesis NO (a substitute passing is not the
      authored function passing, which `execute`'s recovery ladder would
      silently allow); declared output ports are held against the emitted
      payload; and an HONEST structured `emit_error` passes the contract —
      the Phase 0 finding that an honest input-reading function could never
      pass the verify hand, fixed. `_author_verifier` prefers the primitive
      and keeps the legacy execute path for runners without it.
- [x] **Repair at birth.** A gate failure buys two bounded repair rounds —
      the runtime's edit-don't-rewrite discipline (same REPAIR prompt),
      before publish instead of after — then an honest refusal: an
      unpublished node beats an unstable one. `max_steps` 6 → 12 on the
      agent (`author.py`); the seat's spend cap remains the real budget.
      (Tier/thinking escalation inside the loop waits for Phase 6's
      performance-fed routing.)
- [x] **Interface honesty.** Phase 2 made a broken `IO:` line refuse; the
      gate now also holds the door on the sneaky case: a script that READS
      `./bindings.json` while declaring no inputs is named and repaired or
      refused, never published input-less.
- [x] **Keep the walls.** The gate added states, not authority: same seat
      scopes, same consent attestation, same metering purposes; the
      transaction rides the existing `model.seat` audit event.

**Acceptance:** every published node carries ≥1 verified execution at birth
wherever a script runtime exists (pinned in `tests/test_verify_at_birth.py`:
repair-at-birth publishes, unrepairable never publishes, honest errors pass,
the transaction is on the audit log); benchmark "published node first-run
success" ≥95% and the truncated/mocked/wrong-interface buckets emptying are
read off the keyed runs.

---

### Phase 5 — Memory and continuity

*Objective: the spec's memory layers and write-back, so effort compounds
across turns and runs instead of restarting from zero.*

- [x] **Task ledger for builds** (`src/oolu/buildledger.py`). Every build
      outcome is a durable row — goal, script, problem, transaction states —
      through the same `DurableConnection` every other promise rides, never
      the model's transcript. A failed build's state survives unrelated
      turns, restarts, and processes; the gateway records refusals and
      publishes at the gate (`_ledger_note`), lazily over
      `self._durable.conn` with zero constructor plumbing.
- [x] **Write-back with provenance and supersession.** A refused attempt
      admits a lesson citing its attempt row (the provenance); the lessons
      feed the next attempt's context pack through the Phase 3 `lessons`
      port (now wired); and a PUBLISH supersedes the goal's open lessons —
      corrections beat stale warnings, and the ledger never forgets, it only
      supersedes. (Dependency-hint write-back into `knowledge/client.py`
      from birth-verify heals remains open — the gateway does not hold a
      knowledge client today.)
- [x] **Semantic recall upgrades — one scorer, seamed**
      (`src/oolu/retrieval.py`). Words plus character trigrams behind the
      `Embedder` protocol: `contextpack.similarity` and the representative's
      recall both delegate to it (the representative keeps its own stricter
      silence gate — no shared words, no memory), so "normalizing invoices"
      finally recalls "normalize invoice csv". A model-backed embedding
      index implements `Embedder` and upgrades every consumer at once; that
      model integration itself is the remaining half of this box.
- [x] **Focus discipline, shaped around a consent invariant.** The growth
      offer deliberately still lives for exactly one message ("consent
      detached from the question it answered is not consent" — that wall
      stands). What survives an interruption is the WORK: the failed
      attempt's state in the ledger, rehydrated into the pack the moment the
      user returns to the goal — the interrupt stack for the thing that
      actually needed one.
- [ ] **Server-side conversation truth** (carried from Phase 3): feed chat
      turns from the persisted `AssistantHistoryStore` rather than the
      client's last-20 window, with an overflow note preserving the earliest
      standing instructions verbatim — still open; it touches the desktop
      client's pinned `/v1` contract and deserves its own change.

**Acceptance:** the spec's first scenario passes as a test
(`tests/test_memory_continuity.py`): daily chat interrupts a failing build
and the retry resumes knowing exactly what already failed; the supersession
scenario passes as "a publish clears the warning from future packs." The
requirement-correction scenario rides the same supersession rule and lands
fully with server-side conversation truth.

---

### Phase 6 — Multi-model strategies and continuous evaluation

*Objective: the spec's `model_router` strategies, now that one canonical
interface (Phase 2) makes models interchangeable mid-task.*

- [x] **Draft → review for publishing.** The `node.review` seat (declared in
      `seats.py` — reads, never writes, holds no hands — with its own
      generation profile and metering purpose) judges the VERIFIED function
      before it lists: contract fit, the exact-value rule, slot-vocabulary
      reuse (`src/oolu/reviewer.py`; structured delivery when the model
      speaks it, the VERDICT line otherwise). Availability is advisory — no
      reviewer seated, or an unreachable one, publishes exactly as before —
      but a seated reviewer's block is final: the build refuses, the reason
      lands on the ledger as the goal's next lesson, and the transaction
      records `reviewed` / `review-blocked`. The reviewer may be a different
      provider (`OOLU_CHAT_MODEL_*` per seat). (A hard "write-risk nodes
      REQUIRE review" policy waits until operators actually seat reviewers.)
- [ ] **Planner → executor split.** Deliberately open: the build seat
      already defaults to the top tier with a thinking budget, so there is
      no cheaper drafter whose quality is measured yet — the split pays only
      when the audition scoreboard shows a fast drafter earning it. Revisit
      on that evidence.
- [x] **Performance-fed routing — the evidence half.** Build attempts now
      record WHO sat in the seat (`answering_model()` → the ledger's
      `model` column), and `BuildLedger.seat_performance(tenant)` ranks
      models by published/refused outcomes — the per-seat history the plan
      calls for. The automatic demotion policy (a failing model losing the
      seat) stays open until there is volume and an operator-agreed rule;
      the board makes the case visible today.
- [x] **Continuous audition — the ledger half.** `record_report` /
      `audition_history` (`benchmarks/node_authoring.py`) append every
      audition run to a JSONL scoreboard — model, ceiling, rates, taxonomy,
      and the new `cost_per_verified` trend line — one cron/Routine
      invocation per configured model
      (`python benchmarks/node_authoring.py --record data/auditions.jsonl`).
      Landing the trend beside the investor-panel metrics remains open
      (panel integration).

**Acceptance:** provider failover mid-build loses no state (Phase 2's
canonical-state test); per-model FIT lines publish to the scoreboard; the
success and cost-per-verified trends are read off the recorded runs.

---

## 4. Where the harness spec lands in OoLu

| Continuity Context Harness | OoLu home (existing → planned) |
| --- | --- |
| `canonical_model_interface` / adapters | `providers/tools.py` + `routing/gateway.py` → Phase 2 unified request |
| `model_registry` manifests | `providers/chatmodel.py:DEFAULT_MODELS` + `capabilities()` → config-declared manifests |
| `context_pack_compiler` | `routing/prompting.py` (frozen-prefix assembly) → Phase 3 budgeted packs |
| `code_context_engine` | node library + `TraceStore` + drawer files → Phase 3 index |
| `edit_transaction` | `_build_function_node` (`gateway/app.py:3258`) → Phase 4 build transaction |
| `memory.task_ledger` / `writeback_engine` | `durable/records.py`, `knowledge/client.py` → Phase 5 |
| `model_router.strategies` | `routing/matrix.py`, `ChatModelRouter._route` → Phase 6 |
| `observability` | `ModelCallMeter`, audit log → Phase 0 telemetry + Phase 3 pack traces |
| `authority_order` / verification | seats + verify-by-execution (`docs/model-seats.md`) — already law; unchanged |

## 5. Sequencing

Phase 0 and Phase 1 start immediately and are independent (measure while
unstarving). Phase 2 unblocks 3 and 4; 3 and 4 can proceed in parallel once 2
lands (4 needs 2's structured outputs; 3 needs 2's token counting). Phase 5
follows 3; Phase 6 follows 2 + 0. The expected shape of the payoff: Phase 1
alone should visibly change node quality (it removes the 1024-token ceiling
and the fast-tier default); Phase 4 is what makes creation *reliable*; Phase 3
is what makes it *right in context*; the rest compounds.

## 6. What we deliberately keep

The plan strengthens rather than replaces OoLu's three proven disciplines:
**seats** (scoped, audited, consented model call sites), **verify-by-execution**
(a model's output re-earns trust in the sandbox — extended to birth, not
relaxed), and **prefix-cache-safe assembly** (frozen prefix, volatile tail —
now also monetized on paid providers via cache markers). The type system still
disposes what the model proposes; the harness just finally lets the model
propose at full strength.
