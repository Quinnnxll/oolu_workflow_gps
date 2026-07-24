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

- [ ] **Node-authoring benchmark** (`benchmarks/node_authoring.py`): a fixed
      suite of ~30 build goals spanning easy (slugify) to hard (multi-input
      transforms, brokered `http_request` use, downstream-shape matching).
      Each goal runs the real build door, then verifies by sandbox execution
      (`_author_verifier` machinery, `gateway/app.py:5336-5386`). Report per
      goal: first-pass validity, published-node run success, truncation
      (`finish_reason=length`) rate, IO-degradation rate, tokens in/out, cost,
      wall time, repair rounds. Print a FIT line like the Level B audition.
- [ ] **Per-seat effort telemetry**: the `ModelCallMeter` already books tokens
      by purpose (`billing/model_calls.py`); add finish_reason, tool-round
      count, and context-size to the booked record so "how much effort did
      `node.build` actually spend" is answerable from storage.
- [ ] **Failure taxonomy**: classify benchmark failures (truncated, mocked,
      wrong interface, runtime error, wrong shape vs upstream) — these
      categories become the acceptance axes for Phases 1–4.

**Deliverable:** a reproducible baseline scoreboard checked into
`benchmarks/`, run against at least one Anthropic and one OpenAI keyed model.

---

### Phase 1 — Unstarve the model (days, not weeks — highest leverage per line)

*Objective: remove the artificial ceilings. No architecture changes; only
parameters, defaults, and retry hygiene.*

- [ ] **Per-seat generation budgets.** Give `ChatModelRouter` a per-purpose
      generation profile (max_tokens, temperature, thinking budget) instead of
      one constructor default (`providers/chatmodel.py:135`). Defaults:
      `node.build` / `plan.rebuild` / `node.repair` → 16k output tokens;
      `chat.turn` → 4k; `plan.intake` / `plan.route` → 2k. Fix the asymmetry
      where the OpenAI adapter sends no `max_tokens`/`temperature` at all
      (`providers/apikey.py:165-190`).
- [ ] **Reasoning effort.** Plumb extended thinking through both adapters —
      Anthropic `thinking: {type: enabled, budget_tokens}` and OpenAI
      `reasoning_effort` — as a seat-profile field. Default `node.build` to a
      real thinking budget (≥4k tokens) on models that support it.
- [ ] **Default authoring to the reasoning tier.** Change `model.build_tier`'s
      effective default from `inherit`(→fast) to `reasoning`
      (`gateway/app.py:5261-5269`, `settings_node.py:302-309`). Code authoring
      is precisely the task the reasoning tier exists for; the meter and
      budget caps (`chatmodel.py:422-442`) already keep spend honest.
- [ ] **Sampling control.** Send temperature on authoring calls (low, ~0.2,
      matching the synthesis stack's fast-tier 0.1) with the rut-driven bump
      ladder ported from `routing/matrix.py:207-213`.
- [ ] **Retry hygiene.** Give `ChatModelRouter` a real backoff sleep
      (`providers/base.py:126` currently a no-op → hammers 429s), and
      configure request-level retries on the LiteLLM gateway
      (`routing/gateway.py:166-171`).
- [ ] **Prompt caching on the paid path.** Emit Anthropic `cache_control`
      breakpoints on the frozen prefix (system prompt + tool specs). The
      prompts are already assembled frozen-prefix-first
      (`routing/prompting.py`); the marker is all that's missing. This cuts
      the cost of Phase 4's longer loops before they arrive.
- [ ] **Configurable chat-model registry.** Lift `DEFAULT_MODELS`
      (`providers/chatmodel.py:50-59`) into config + env overrides, exactly as
      the synthesis stack already does (`config.py:41-43`), so a model rename
      is a config change, not a code change.

**Acceptance:** benchmark truncation rate ≈ 0; first-pass validity up
materially with no other change; per-seat telemetry shows `node.build`
spending an order of magnitude more thinking+output tokens than today.

---

### Phase 2 — One canonical model interface

*Objective: the spec's `canonical_model_interface` + `model_registry`. Today's
two stacks mean every fix lands twice and every capability check is an
`hasattr` probe (`gateway/app.py:5324`).*

- [ ] **Canonical request.** One internal request type — messages, tools,
      `response_schema`, generation profile (max_output_tokens, temperature,
      reasoning_effort), execution policy (`allow_tools`, `max_tool_rounds`) —
      compiled to provider wire formats only inside adapters.
      `providers/tools.py` (canonical transcript + dual renderers) is the
      seed; extend it to cover the synthesis stack's `AssembledPrompt` so
      `routing/gateway.py` and `providers/chatmodel.py` consume one shape.
- [ ] **Model registry manifests.** Per-model capability flags — tool_calling,
      structured_output, prompt_caching, thinking, context_window,
      max_output_tokens — declared in config (seeded from
      `capabilities()` discovery, `providers/apikey.py:93-110`, today unused
      by routing). Routing consults the manifest, not `hasattr`.
- [ ] **Structured output as the only delivery channel for capable models.**
      The schema-validated `finish_node` path (`author.py:222-241`) becomes
      the contract; fence-scraping (`extract_script`) and the prose `IO:` line
      (`chat.py:1044-1076`) are demoted to a legacy fallback for local models
      whose manifest says no-tools. Silent IO degradation becomes a hard,
      model-visible error.
- [ ] **Token accounting.** Integrate real tokenizers (tiktoken + Anthropic
      count-tokens endpoint) behind one `count_tokens(request)` seam — the
      prerequisite for Phase 3's budgeter. Today nothing counts tokens before
      sending.

**Acceptance:** the spec's tests — "model provider changes mid-task → task
continues from canonical state"; "a model proposes an unsupported tool call →
the adapter rejects it before execution." One code path constructs every
provider request.

---

### Phase 3 — Context packs: push the right context into the seat

*Objective: the spec's `context_pack_compiler` + `code_context_engine`,
scoped to the two seats that write code (`node.build`, `plan.synthesize`).
Stop making the model guess what the workspace looks like.*

- [ ] **The node-library index.** Embeddings + metadata over every published
      node: goal, contract (consumes/produces slots), verified `src/main.py`,
      success stats from the `TraceStore`. This replaces exact-sentence cache
      keying (`cache/signature.py`) as the *retrieval* mechanism — the cache
      stays for replay; retrieval feeds prompts.
- [ ] **The build context pack.** For every `node.build` call, compile:
      1. the system contract (today's `NODE_FUNCTION_PROMPT` + a distilled
         `docs/node-generation.md`),
      2. the goal verbatim,
      3. **route position**: the actual contracts and last verified output
         shapes of the upstream/downstream nodes this node must sit between —
         today's #1 silent failure (authoring against an imagined shape),
      4. the slot vocabulary in use (so slot names are reused, which is what
         makes route-finding work),
      5. 2–3 *similar verified node functions* retrieved from the index as
         few-shot examples,
      6. applicable lessons and error patterns
         (`knowledge/client.py` `error_patterns`, today never prompted),
      7. the **full error ledger** on repair/rebuild turns, not just
         `latest_error` (`routing/prompting.py:161`).
- [ ] **The budgeter.** Allocate the pack against the model's context window
      using Phase 2's token counting, with the spec's compaction order:
      preserve verbatim (goal, contracts, current errors, exact values) →
      compress (older discussion, duplicate examples) → discard first
      (unrelated chatter). Assembly stays frozen-prefix-first so the
      cache discipline (`routing/prompting.py:1-26`) and Phase 1's
      `cache_control` markers keep paying.
- [ ] **Server-side conversation truth.** Feed chat turns from the persisted
      `AssistantHistoryStore` rather than trusting the client's last-20 window
      (`gateway/app.py:1661`, `1447-1454`), with a rolling episode summary
      once the window overflows — the first compaction in the codebase, done
      under principle 4 (commitments survive verbatim).

**Acceptance:** benchmark adds route-position goals (build a node between two
existing nodes); wrong-shape failures drop to near zero; context-pack traces
(what was included/excluded, token allocation) are logged per call — the
spec's observability starting set.

---

### Phase 4 — Verify at birth: the build transaction

*Objective: no node is published without its function having executed
successfully once. Creation gets the same recovery ladder the runtime already
has. This is the phase that kills "node creation is unstable."*

- [ ] **One authoring path.** Retire the one-shot/agentic fork
      (`gateway/app.py:5317-5334`). Every build runs the loop; for manifest
      no-tools models the *harness* drives the same loop (generate → validate
      → execute → feed errors back) with fenced-block parsing as the transport.
- [ ] **The build transaction.** Adopt the spec's edit-transaction states —
      `proposed → context_verified → generated → validated → published /
      failed` — recorded on the node's version history. `verify_function`
      (`gateway/app.py:5336-5386`) stops being an optional tool the model may
      call and becomes a **mandatory gate** the harness runs before
      `nodeplace.contribute`; `screen_script` and the `emit_result` check run
      at creation on every path (today one-shot skips both,
      `chat.py:1139-1158`).
- [ ] **Repair at birth.** On a failed verification, reuse the runtime's
      repair muscle (`ChatModelSynthesizer.repair`,
      `runtime/script_node.py:145-170`) inside the transaction: dependency
      heal → repair with exact error → escalate tier/thinking (Phase 1
      profiles) → decline honestly. Budgets: raise `max_steps` 6 → 12
      (`author.py:111`), bounded by the seat's spend cap rather than a small
      constant.
- [ ] **Interface honesty.** A build that cannot produce a schema-valid
      interface *fails*; it never silently publishes as
      `{inputs:[], outputs:[result:str]}` (`chat.py:1053-1060`).
- [ ] **Keep the walls.** Everything stays inside the existing governance:
      the `node.build` seat's DeskFiles scopes, consent doors, metering
      purposes, audit events (`docs/model-seats.md`) — the transaction adds
      states, not new authority.

**Acceptance:** every published node carries ≥1 verified execution at birth;
benchmark "published node first-run success" ≥95%; the failure taxonomy shows
truncated/mocked/wrong-interface classes eliminated (caught in-transaction,
not by users).

---

### Phase 5 — Memory and continuity

*Objective: the spec's memory layers and write-back, so effort compounds
across turns and runs instead of restarting from zero.*

- [ ] **Task ledger for builds.** A build interrupted by chat, a question, or
      a decline resumes with goal, acceptance criteria, partial script, and
      error ledger intact — the spec's `task_ledger`, persisted through the
      existing durable stores (`durable/records.py`), not the model's
      transcript.
- [ ] **Write-back with provenance.** After each verified build/repair,
      admit structured records: new error patterns, dependency hints
      (extending `knowledge/client.py`), and "lessons" scoped to the node —
      each carrying source event ids and confidence, per the spec's admission
      policy. Superseded values are excluded from future packs (corrections
      beat stale summaries).
- [ ] **Semantic recall upgrades.** Replace token-overlap cosine in
      representative recall (`representative/memory.py:57-88`) and power the
      Phase 3 node-library index with a real embedding index — one retrieval
      service, three consumers (authoring examples, chat recall,
      representative voice).
- [ ] **Focus discipline in chat.** A side question during a build creates a
      temporary episode and returns — the spec's interrupt stack — so the
      build context pack is not diluted by unrelated turns.

**Acceptance:** the spec's scenarios pass as tests: "daily chat interrupts
coding → build resumes with plan and unresolved error intact"; "user corrects
an earlier requirement → old value superseded and excluded from executable
context."

---

### Phase 6 — Multi-model strategies and continuous evaluation

*Objective: the spec's `model_router` strategies, now that one canonical
interface (Phase 2) makes models interchangeable mid-task.*

- [ ] **Draft → review for publishing.** A second seat (`node.review`)
      critiques the verified function — contract fit, exact-value rule,
      slot-vocabulary reuse — before listing. High-risk (write-risk) nodes
      require it; the reviewer may be a different provider.
- [ ] **Planner → executor split.** Let the reasoning tier plan the interface
      and approach, the fast tier draft the body, verification arbitrate —
      the routing matrix's ladder generalized across seats.
- [ ] **Performance-fed routing.** Per-seat, per-model outcome history (from
      Phase 0's telemetry) feeds tier choice: a model that keeps failing
      `node.build` verification loses the seat, the way `earns_its_cost`
      already gates the token planner (`docs/node-token-planner.md` §5).
- [ ] **Continuous audition.** The Phase 0 benchmark runs on a cadence per
      configured model; the scoreboard (context precision, validation
      success, cost per completed build) lands beside the investor-panel
      metrics so quality regressions are visible the day a provider drifts.

**Acceptance:** provider failover mid-build loses no state; per-model FIT
lines published; cost-per-verified-node trends down while success trends up.

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
