# Changelog

All notable changes to Workflow-GPS are documented here.

## Unreleased

Memory-stack M4 — skill induction, the ignition:

- **`src/oolu/skillinduction.py`.** Repeated verified subgraphs become
  candidate skills: contiguous motifs (2–5 steps) mined over the trace
  corpus that already exists, one vote per run, failed runs teaching
  nothing — the corpus's own law. Candidates ride the spine with
  fresh-count supersession on re-induction; promotion holds the
  capability-web thresholds (support ≥ 5 verified runs, ≥ 3 distinct
  goals — generality, not habit) and supersedes candidacy into a
  ``verified`` skill record. ``skills_for`` is the reader: the
  promoted motifs found inside a proposed step sequence, closing the
  loop route assembly consults. Recorded remainders: replay +
  mutation as the final gate, and node-bundle publication.
- **Pinned** by ``tests/test_skill_induction.py``: promotion across
  distinct goals with candidacy superseded, failed runs and thin
  support never promoting, and re-induction serving fresh counts.

Memory-stack M3 — negative knowledge, first-class:

- **`src/oolu/negative.py`.** Failure records ride the spine,
  dual-scoped to their goal AND their mechanism, carrying
  applicability conditions, reproduction counts (recurrence supersedes
  with the count raised — evidence promotes), and reopen conditions.
- **The graduated pre-commitment check.** One failure never blocks —
  the lesson rides and the retry proceeds. A REPRODUCED failure blocks
  an identical retry at the build door BEFORE any authoring spend,
  naming the failure and what would make a retest material; a
  different model in the seat or a matched reopen condition allows the
  retest with the difference named. Never an unjustified universal
  prohibition. A publish resolves the goal's failure records the way
  it closes its lessons: superseded, never erased.
- **Pinned** by ``tests/test_negative_knowledge.py``: the graduation,
  block/allow/reopen verdicts, the mechanism twin, the gate refusing
  the third identical attempt with zero model calls, and
  publish-resolves.

Memory-stack M2 — episodic memory, summaries, overflow truth:

- **`src/oolu/episodes.py`.** Episodes are atomic memories on the M0
  spine — no new store, which is the point of a spine. Summaries are
  DERIVED and extractive (no model, no paraphrase, never global): the
  newest objective and outcome, every open unresolved item VERBATIM —
  commitments survive compaction — citing the episode rows compressed,
  superseding only prior summaries. Invalidation is read-side law:
  a summary older than its subject's newest episode never serves.
- **The build door writes episodes** for publishes and refusals alike,
  so a project interrupted for weeks restores its objective, latest
  outcome, and open problem from the stack — the capability-web exit
  test, landed for builds.
- **The window names its own truncation.** When chat history overflows
  the 20-turn window, the earliest dropped user asks ride into the
  context note verbatim instead of vanishing silently — the first step
  of server-side conversation truth, with the persisted-thread source
  still open (it touches the client contract).
- **Pinned** by ``tests/test_episodes.py``: citation and supersession,
  the never-serve-stale law, verbatim commitments, the interrupted-
  project restore through the real build door, and the overflow note
  present exactly when history overflows.

Memory-stack M1 — the temporal graph and state projections:

- **`src/oolu/temporalgraph.py`.** Relations get what facts got in M0:
  validity intervals, mandatory provenance (an edge nobody can trace
  is a rumor), supersession-not-deletion — adjacency rows on the same
  durable connection, no graph database until a measured bottleneck.
  Every read is time-scoped: "what depended on X when Y happened" is
  one query (``dependents_at``), and a closed edge never contributes
  proximity again (``neighborhood`` — the term retrieval ranks with).
- **Publishes land their relations.** Every published node connects
  ``satisfies`` to its goal and ``consumes``/``produces`` to its slots,
  the registry row as provenance — the edges route position and
  proximity ranking read as they accumulate.
- **State cards are projections.** ``_node_state_card`` derives a
  node's current truth (contract, valid relations, open lessons) from
  the stores on every call — never stored, so rebuild-equals-read
  holds by construction.
- **Pinned** by ``tests/test_temporal_graph.py``: provenance law,
  the as-of-then dependents query, closed-edge proximity silence,
  hop-bounded neighborhoods, and the publish-to-card loop through the
  real build door.

Memory-stack M0 — the atomic memory spine:

- **`src/oolu/memoryspine.py`.** The BuildLedger pattern promoted to
  the platform's memory contract: one table for every tier's records —
  type, statement, structured value, scope, validity, confidence,
  verification state, provenance, supersession. Three structural laws:
  admission is earned (``observed`` and better REQUIRE provenance;
  only ``proposed`` arrives bare), supersession is a WHERE clause (a
  corrected value cannot re-enter an executable context by oversight —
  the exclusion is the query's shape, not a caller's discipline), and
  history is never erased.
- **Writers bridge, never fork.** The BuildLedger dual-writes its
  lessons onto the spine with double provenance — the hash-chained
  audit event the gateway now appends on every refusal
  (``model.memory``) plus the attempt row — and a publish supersedes
  the goal's spine lessons exactly as it supersedes the ledger's own.
- **One reader.** The build context pack's lessons come spine-first
  (scope-walled, validity-filtered, ranked by the shared retrieval
  scorer), with the ledger as fallback. The loop-closure test drives
  the full circle through the real build door: refusal → audit event →
  spine lesson → retry's pack → publish closes the book everywhere.
- **Pinned** by ``tests/test_memory_spine.py``: earned admission,
  structural supersession, expiry, scope walls, ranked recall, the
  dual-write bridge, and the gateway loop.

The memory-stack build plan — phases for the atomic chain reaction:

- **`docs/memory-stack-plan.md`.** The building phases for the
  Adaptive Capability Web's memory substrate: the atomic memory spine
  (the BuildLedger pattern promoted to a platform contract), the
  temporal graph with validity intervals, episodic memory and the
  first hierarchical summaries, first-class scoped negative knowledge
  with the pre-commitment check, skill induction from the trace
  corpus, the reinforcement route-learning rungs (dataset → contextual
  bandit → bounded reranker → the node-token planner trained offline →
  constrained exploration), and multi-agent work over shared state.
  Every phase ships its writer, its reader, and a loop-closure test —
  records no seat consumes are deleted, not documented. Grounded
  seam-by-seam in the completed context-harness arc: the durable
  stores, TraceStore, ProposalModel containment, the Embedder seam,
  seats, and the earns_its_cost replay gate are the ignition already
  wired.

Context-harness epilogue — the arc measured, the seam filled:

- **The harness-delta scorecard** (``benchmarks/harness_delta.py``).
  The measurable before/after on the whole six-phase arc, computed
  live from the current machinery — output ceilings (1024 → 16384),
  reasoning budgets (0 → 4096), pushed context (0 → ~440 tokens avg
  per bench goal, budgeted and traced), upstream-shape coverage on
  route goals (0/2 → 2/2), gates before publish (mock-smells-on-one-
  path → six walls on every path), repair rounds (0 → 2), build
  memory (none → the durable ledger), backoff, caching, dispatch.
  Deterministic and offline; the model half (verified rate,
  $/verified) is two keyed ``node_authoring.py`` commands away, and
  ``--record`` turns those into the standing audition trend.
- **The embedding-model integration**
  (``src/oolu/providers/embeddings.py``). The retrieval seam Phase 5
  left open is filled: ``ModelEmbedder`` implements the Embedder
  protocol over the OpenAI-shaped ``/embeddings`` wire — the hosted
  key or any local OpenAI-compatible server (Ollama, LM Studio),
  through the same authenticated adapter pipeline every provider call
  rides. Cached per process (compiles repeat), fail-open to lexical
  (recall degrades, builds proceed), self-silencing after consecutive
  endpoint failures (a dead endpoint must not tax every compile).
  Opt in with ``OOLU_EMBEDDINGS=openai|local`` +
  ``OOLU_EMBEDDING_MODEL``; the context-pack compiler's ranking and
  the gateway's example retrieval obey the configured embedder and
  stay lexical the instant anything is missing or broken.
- **Pinned** by ``tests/test_embeddings.py``: dense vectors through
  the shared cosine, the embed-once cache, fail-open and the failure
  ceiling with recovery reset, the exact ``/embeddings`` wire body
  through the authenticated adapter, a contrarian embedder visibly
  steering the compiler's ranking, and the gateway's off/unkeyed
  paths staying None.

Context-harness Phase 6 — multi-model strategies and continuous
evaluation (the plan's final phase):

- **Draft → review for publishing** (``src/oolu/reviewer.py``, the
  ``node.review`` seat). Birth verification proves a function
  executes; it cannot prove the function is the right citizen — that
  the declared interface is the code's truth, that the answer is
  computed rather than smuggled past the mock screen, that the slot
  vocabulary is reused instead of forked. A seated reviewer now judges
  the verified function before it lists, under its own purpose in the
  meter and the books, possibly a different provider than the author.
  Availability is advisory — no reviewer, or an unreachable one,
  publishes exactly as before — but a seated reviewer's block is
  final: the build refuses, the reason becomes the goal's next lesson
  on the ledger, and the transaction records reviewed/review-blocked.
  The seat is declared in the registry (reads, never writes, no
  hands: a reviewer that can edit is an author with extra steps).
- **Performance-fed routing, the evidence half.** Build attempts
  record WHO sat in the seat (the router's answering model), and
  ``BuildLedger.seat_performance`` ranks models by published/refused
  outcomes — the per-seat history a demotion policy will read; the
  board makes the case visible before any rule acts on it.
- **The continuous-audition scoreboard.** ``--record`` appends every
  benchmark run to a JSONL ledger — model, ceiling, rates, the failure
  taxonomy, and the new cost-per-verified trend line — so one cron or
  Routine invocation per configured model turns the Phase 0 bench into
  a standing quality trend: a provider drifting is visible the day it
  drifts.
- **Deliberately open, on the record:** the planner→executor split
  waits for scoreboard evidence that a cheaper drafter earns it, and
  automatic seat demotion waits for volume and an operator-agreed
  rule.
- **Pinned** by ``tests/test_multi_model.py``: the verdict protocol
  (pass, block-with-reason, unreachable-never-blocks, structured
  preferred), the block-becomes-lesson flow at the real build door,
  the reviewed publish on the audit transaction, the performance
  board, and the audition scoreboard.

Context-harness Phase 5 — memory and continuity:

- **The build ledger** (``src/oolu/buildledger.py``). A build that
  fails birth verification used to be forgotten the moment the turn
  ended; the retry started from zero, free to repeat the exact mistake
  that sank it. Every build outcome is now a durable row — goal,
  script, problem, transaction states — on the same connection every
  other promise rides, surviving unrelated turns, restarts, and
  processes. Refusals admit LESSONS citing their attempt row (the
  provenance); the lessons enter the next attempt's context pack
  through the Phase 3 lessons port, now wired; and a publish
  SUPERSEDES the goal's open lessons — corrections beat stale
  warnings, and the ledger never forgets, it only supersedes.
- **One retrieval scorer, seamed** (``src/oolu/retrieval.py``). The
  context pack's example ranking and the representative's voice recall
  now share one scorer — words plus character trigrams, so
  "normalizing invoices" recalls "normalize invoice csv" with no
  stemmer and no dependency — behind the ``Embedder`` protocol a
  model-backed index implements to upgrade every consumer at once. The
  representative keeps its stricter silence gate: no shared words, no
  memory, whatever the trigrams think.
- **Focus, shaped around a consent invariant.** The growth offer still
  lives for exactly one message — consent detached from its question
  is not consent, and that wall stands. What survives an interruption
  is the WORK: the spec's "daily chat interrupts coding" scenario now
  passes end to end — a failing build, unrelated turns between, and a
  retry whose context pack names exactly what already failed.
- **Pinned** by ``tests/test_memory_continuity.py``: the scorer's
  properties and seam, lesson provenance, supersession-on-publish,
  ledger durability across connections, tenant walls, and both
  gateway-level acceptance scenarios.

Context-harness Phase 4 — verify at birth:

- **The birth-verify primitive**
  (``NodeScriptRunner.verify_function``). One candidate function
  against the runtime contract: dependency healing in place, but NO
  model repair and NO resynthesis — the function under test is the
  function judged, where ``execute``'s recovery ladder would let a
  substitute script pass for the authored one. Declared output ports
  are held against the emitted payload (a run that skips its ports is
  a mocked answer), and an honest structured ``emit_error`` PASSES the
  contract — at birth, with no real bindings staged, a function naming
  its missing data has proven it executes and speaks the protocol.
  This closes the Phase 0 finding that an honest input-reading
  function could never pass the author's verify hand.
- **The build door is a transaction.** ``proposed → generated →
  (repair:…) → validated → published``, recorded on the hash-chained
  audit log inside the ``model.seat`` publish event. The gate runs the
  safety screen, mock smells, the emit_result presence check, and
  interface honesty on EVERY authoring path before a node is created
  (the one-shot path used to skip the screen entirely — validation was
  deferred to the node's first real run, which is exactly what "node
  creation is unstable" felt like); sandbox verification runs wherever
  the host carries a script runtime, degrading to ``validated-static``
  where it doesn't. The agent's finish-gate verification is trusted
  for the exact script it delivered — no double-paying the sandbox —
  and any repaired script re-verifies.
- **Repair at birth.** A gate failure buys two bounded repair rounds
  (``repair_node_function`` — the runtime's edit-don't-rewrite
  discipline under the same REPAIR prompt, before publish instead of
  after), then an honest refusal: an unpublished node beats an
  unstable one. The agent's step budget doubles to 12 — a verify-fix
  cycle costs two steps, and the seat's spend cap is the real budget.
- **Interface honesty, the sneaky half.** A script that reads
  ``./bindings.json`` while declaring no inputs is held at the door,
  named, and repaired or refused — never published input-less to break
  quietly on every route it joins.
- **Pinned** by ``tests/test_verify_at_birth.py``: the primitive's
  verdicts (clean, honest error, crash, skipped ports, no
  substitution), repair-at-birth publishing with the transaction on
  the audit log, the unrepairable build refusing to publish, and the
  doubled agent budget.

Context-harness Phase 3 — the build seat gets its context pushed,
budgeted, and traced:

- **The build context pack** (`src/oolu/contextpack.py`). The node
  author used to write nearly blind — the one-shot path saw only the
  goal sentence; the agentic path had to think to pull. Every
  ``node.build`` call now compiles a pack the way the frontier
  harnesses do: the slot vocabulary in circulation (routes chain on
  exact names), the route position — recent VERIFIED output shapes of
  the upstream nodes the goal names, closing the author's #1 silent
  failure (code written against an imagined shape) — similar node
  contracts, and 2–3 verified example functions read seat-scoped from
  their drawers. Pushed on BOTH authoring paths ahead of the request
  (``compose_build_request``); the frozen system contract keeps its
  prompt-cache breakpoint.
- **Budgeted, compacted, traced.** The pack takes at most 30% of the
  answering model's window, measured with the Phase 2 token seam and
  compacted in the spec's order — the verbatim classes (vocabulary,
  upstream shapes) survive whole; examples drop first, then extra
  contracts, then lessons — and every drop lands in the pack's
  included/excluded trace, logged per call. A silently truncated pack
  reads as complete; this one says what it left out.
- **The full error ledger reaches the model.** The synthesis prompt
  now renders the DISTINCT earlier failures, not just the latest
  (deduped, capped, inside the volatile action message — the
  cache-safe prefix fingerprint is pinned unchanged), and the runner's
  second repair round carries round one's failure inside the error
  text, with no synthesizer signature change.
- **Retrieval is a seam.** Similarity is token-overlap cosine today —
  deterministic, offline, dependency-free; Phase 5's embedding index
  replaces the scorer, not the pack. The bench mirrors the gateway
  (``bench_context_pack``), so route-position goals now see their
  upstream shapes on the one-shot path too.
- **Pinned** by `tests/test_context_pack.py`: pack contents, the
  compaction order with recorded drops, both authoring paths carrying
  the pack, the bench mirror, the prompt ledger, the pinned prefix
  fingerprint, and the repair ledger through the real runner loop.

Context-harness Phase 2 — one canonical model interface:

- **Model manifests** (`providers/registry.py`). What each model can
  do is declared once — tool calling, structured output, extended
  thinking, the reasoning-effort dial, prompt caching, window sizes —
  with conservative family inference for unknown ids (an unrecognized
  local tag is assumed to speak NO reliable native tool calling; the
  fenced-code path exists for exactly those models) and an
  `OOLU_MODEL_MANIFESTS` JSON overlay for operators. The adapter's
  capability predicates moved here: one table serves routing and wire
  construction alike.
- **Routing asks the manifest, not the object shape.**
  `ChatModelRouter.answering_model()` names the (provider, model) that
  would answer next; `consult_ready()` answers whether it reliably
  speaks tools. The authoring door dispatches on that — the old
  `hasattr(consult)` probe never distinguished models (every router
  has `consult`), so small local models were being trusted with tool
  JSON they emit badly; they now route honestly to the one-shot path.
- **One construction path for every chat request.** `reply`,
  `consult`, and the new `structured` all flow through `_execute` into
  `_call_provider`/`_call_local`; the Anthropic and OpenAI wire
  branches exist exactly once. The neutral transcript gained a
  provider annex — thinking blocks carried verbatim in
  `ToolReply.thinking_blocks`, re-attached by the Anthropic renderer,
  shed cleanly by the OpenAI dialect — which lifts Phase 1's
  hold-back: the seat's reasoning budget now rides tool consultations
  too, and a provider switch mid-task keeps the task while shedding
  the thoughts the new provider never issued.
- **Structured output, schema-forced.**
  `ChatModelRouter.structured(messages, schema=...)` forces a
  synthetic delivery tool, validates the arguments against the schema,
  gives the model one correction round with the violation named, and
  raises `StructuredOutputError` rather than defaulting silently.
- **The prose IO channel is honest.** A present-but-broken `IO:` line
  now REFUSES the build with the problem named
  (`parse_node_io_checked`); only a genuinely absent line keeps the
  lenient default. The bench classifies the new refusal under
  `bad_interface`.
- **Token accounting exists** (`providers/tokens.py`): a
  deterministic, deliberately conservative estimate by default, exact
  tiktoken counts for OpenAI-family ids via the new `tokens` extra —
  the seam Phase 3's context-pack budgeter compiles against.
- **Pinned** by `tests/test_canonical_interface.py`, including the
  plan's two acceptance scenarios: a provider switch mid-task keeps
  the task from canonical state, and an undeclared tool call is
  refused before any handler runs.

Context-harness Phase 1 — the model is unstarved:

- **Seat generation profiles** (`providers/profiles.py`). Effort now
  rides per PURPOSE, not per constructor default: the code-writing
  seats (`node.build`, `node.repair`, `plan.rebuild`) get 16k output
  tokens, temperature 0.2, and a 4k reasoning budget; `plan.synthesize`
  8k; `chat.turn` 4k; intake/route 2k; unknown purposes 4k — never the
  old universal 1024 that forced whole node functions through a
  keyhole (the bench's truncated bucket made the cost visible).
  `ChatModelRouter` resolves its seat's profile from its purpose;
  an explicit constructor ceiling remains for benches.
- **Reasoning effort, capability-gated.** Anthropic extended thinking
  (`thinking: enabled, budget_tokens 4096`) rides on reply calls for
  thinking-capable Claude models — budget floored and fitted under the
  ceiling, temperature correctly absent beside it; OpenAI reasoning
  models (o-series, gpt-5) take `reasoning_effort` +
  `max_completion_tokens` and no temperature; the classic OpenAI wire
  (and every local OpenAI-compatible server) now carries `max_tokens`
  + `temperature` — the per-provider asymmetry is gone. Thinking stays
  off on tool consultations until the canonical transcript can return
  thinking blocks across tool turns (Phase 2).
- **The author thinks by default.** `model.build_tier` defaults to
  `reasoning` — code authoring is the work that tier exists for; the
  spending cap still governs. `inherit` remains a choice.
- **Retries wait now.** The provider backoff is real (late-bound seam;
  offline tests neutralize it in one conftest fixture), and the
  LiteLLM synthesis gateway retries transient failures (rate limit,
  timeout, connection, 5xx — matched by exception name) with backoff
  through an injectable `completion_fn` seam before surfacing
  `GatewayError`.
- **Prompt caching on the paid path.** The Anthropic system prompt
  rides as a block carrying `cache_control: ephemeral` — the frozen
  prefix finally earns its cache discipline on hosted Claude, not just
  on local vLLM.
- **The chat model registry is configuration.**
  `OOLU_CHAT_MODEL_<PROVIDER>_<TIER>` overrides the (provider, tier)
  model ids at call time; `DEFAULT_MODELS` is the fallback, and a
  model rename is no longer a code change.
- **Pinned** by `tests/test_effort_unlock.py` — the wire bodies
  themselves asserted per provider, profile table, env overrides, both
  retry ladders, and the explicit-ceiling override.

Context-harness Phase 0 — the node-authoring seat, measured before it
is re-upholstered:

- **The node-authoring bench** (`benchmarks/node_authoring.py`). 24
  goals — easy transforms to route-position and brokered-web tasks,
  plus three conversation goals the author must decline — run through
  the REAL authoring paths (one-shot `author_node_function`, or the
  `NodeAuthorAgent` loop for tool-calling models), and every authored
  function is verified by executing it against the real runtime
  contract: the `sandbox_shim` staged as `_oolu_runtime`, the goal's
  `bindings.json`, the stdout envelope parsed by `runtime/contract`,
  a web broker that refuses every call with the taught status-0
  contract, and dependency healing through an injectable installer.
  The scoreboard reports verified/answer/interface/first-pass rates,
  a twelve-bucket failure taxonomy (truncated, mocked, bad_interface,
  built_conversation, …), and per-goal effort (calls, tokens, cost,
  wall time) sliced from the call meter. A scripted incumbent holds
  the reference FIT line offline; `main()` is the live audition in
  the Level B pattern, `--max-tokens` previewing the Phase 1 ceiling
  lift. Pinned end to end by `tests/test_node_authoring_bench.py`.
- **Effort books.** `ModelCallRecord` now carries `finish_reason` and
  `context_chars`, booked by `ChatModelRouter` on all four consult
  paths — so "did `node.build` hit its 1024-token ceiling?" is
  answerable from storage alone. Older telemetry shapes (the routing
  gateway's) meter unchanged.
- **Two Phase 4 findings** documented where the bench diverges from
  production on purpose: `_author_verifier` stages no `bindings.json`
  (an honest input-reading function cannot pass it today) and mounts
  no web exchange (`http_request` raises instead of answering the
  taught refusal).

The context-harness build plan — why node creation with external LLM
APIs trails the frontier coding harnesses, and the phases that close
the gap:

- **`docs/context-harness-plan.md`.** A source-anchored diagnosis (the
  1024-token authoring ceiling, fast-tier default, zero reasoning
  effort, goal-only context, no verification at birth, two divergent
  provider stacks) and six building phases: baseline benchmark →
  effort unlock → one canonical model interface → budgeted context
  packs → the verify-at-birth build transaction → memory and
  multi-model strategies. Maps the Continuity Context Harness
  specification onto OoLu's existing modules; keeps seats,
  verify-by-execution, and prefix-cache discipline as the foundation.

Investor panel Phase 3 — competitor intelligence, moat, scenarios,
the automated report:

- **Competitor intelligence.** An append-only observation ledger
  (competitor × the matrix's ten strategic dimensions), every entry
  carrying its evidence, source, confidence, and stamp — recorded
  through the same approved/audited door as manual metrics. ``GET
  /v1/platform/competitors`` answers the strategic comparison (newest
  observation per cell; unobserved dimensions absent, never guessed),
  and the panel renders it as a shaded position table.
- **Attention share.** Share of search, share of voice, category time
  share, and external tools eliminated join the catalog as manual
  metrics with weekly/monthly cadence contracts — external eyes enter
  through the recording door, never invented.
- **The moat, measured.** Node reuse rate (terminal runs routed
  through an existing node's own function, 30 days), reusable
  verified nodes (sealed releases on the provenance ledger), and
  proprietary event volume (the audit chain's row count) — all auto,
  all off stores this session's earlier work built. The moat and
  market scorecard pillars read them.
- **Scenario modeling.** ``POST /v1/platform/metrics/scenario``: the
  matrix's eight decision-support scenarios projected by DETERMINISTIC
  arithmetic over current actuals (revenue, cost, cash from the
  ledgers) and stated assumptions — revenue/cost/margin/cash impact,
  runway after, break-even months, and a confidence range that is
  exactly the stated uncertainty. No model touches a number.
- **Automated investor reporting.** ``GET
  /v1/platform/metrics/report``: one Markdown document assembled from
  the ledgers — executive summary table, scorecard, cohort retention,
  competitive position — every figure the runtime's own. The panel
  gains a Download-report button.
- **Tests.** The observation ledger (supersession, dimension and
  confidence walls), the scenario arithmetic to the cent, and the
  three doors end to end (walled, moat metrics live, the report
  carrying its sections).

Investor panel Phase 2 — unit economics, cohorts, AI quality,
marketplace health, customer health:

- **Unit economics.** ARPU (month earnings per monthly active), cost
  per successful workflow (model spend / completed runs, month to
  date), contribution margin (earnings after model cost) — all off the
  real books, blank when a store is absent, never a fake zero. CAC and
  LTV/CAC arrive through the manual door with their contracts riding.
- **Cohort analysis.** ``GET /v1/platform/metrics/cohorts``: each
  account joins the cohort of its FIRST activity month; every cohort
  answers how many members were active in each month since — computed
  from real run stamps, last 12 cohorts, M0 onward. The panel renders
  the classic retention triangle, shaded by retention band.
- **AI quality.** Task success rate over node-function runs (the AI's
  own code executing end to end, 30-day window), human intervention
  rate (terminal runs that needed a retry), and self-repairs promoted
  (the node.repair seat's audit count).
- **Marketplace and customer health.** Active listings, transactions
  today, average transaction value; activation rate (accounts that
  ever completed among accounts that ever started) and the at-risk
  watchlist (active this month, silent 7 days).
- **Scorecard follows.** The product pillar now reads AI task success
  and activation; economics reads contribution margin.
- **Tests.** The month-span walker, phase-2 readers against real runs
  (honest blanks where books are absent), the cohort door (M0 = 100%,
  columns walking to the current month, walled), and the scorecard's
  new inputs.

The investor panel learns the performance matrix — Phase 1: contract,
executive summary, scorecard:

- **The metric contract.** Every ``MetricSpec`` now carries the
  matrix's contract fields — section, formula (the one-line business
  definition), owner, update frequency, direction, target /
  warning / critical thresholds, version — and ``metric_status``
  applies them honoring direction (higher-is-better floors,
  lower-is-better ceilings). The catalog view exposes the contract, so
  every number answers "defined how, owned by whom, healthy when."
- **Phase-1 metrics, real readers.** Monthly active users and DAU/MAU
  stickiness; workflow success rate and first-attempt success rate
  (today's terminal runs); successful workflows today; net earnings
  today (the earnings ledger); model spend this month (the usage
  books); day-7 retention (the run-activity cohort); request success
  rate (the gateway's own counters — the availability proxy). Readers
  that have no basis raise honestly and leave the metric blank, never
  a fake zero.
- **Executive summary.** ``GET /v1/platform/metrics/summary``: each
  headline metric with the matrix's status components — actual,
  previous period, growth rate, target, threshold status, owner.
- **Investor scorecard.** ``GET /v1/platform/metrics/scorecard``: the
  matrix's weighted pillars (growth .20, retention .15, economics .15,
  product / technology / physical / market / moat .10 each), each
  scored from its inputs by threshold status or 7-day trend; pillars
  with no measurable input are EXCLUDED BY NAME and the weights
  renormalize — the score never averages in what this platform cannot
  measure yet. The panel page renders the executive strip
  (health-edged tiles with period deltas) and the scorecard bars.
- **Next phases** (planned, not built): Phase 2 — unit economics,
  cohorts, AI quality from the author/verify books, marketplace
  health; Phase 3 — competitor intelligence and attention share
  through the manual doors, moat measures, scenario modeling,
  automated reporting.
- **Tests.** Direction-aware status, the summary's status components
  and honest empties, scorecard renormalization with named
  exclusions, and both routes walled.

繁體中文 speaks everywhere — full coverage, 勿擾, and its own faces:

- **The gap.** The Traditional dictionary covered 412 of 533 strings;
  everything else fell back to Simplified — most visibly the ENTIRE
  Settings window (every label and description), the account
  descriptions, the Work tabs and node tags, the login/phone strings,
  the org-template block, and the pin/mute/delete margins.
- **Full coverage.** All 121 missing entries translated into
  Traditional with Taiwan vocabulary (登入, 簡訊, 帳戶, 設定, 檔案,
  程式碼, 使用者, 封鎖, 方案, 金鑰, 伺服器…), and Mute is 勿擾 /
  取消勿擾 with its descriptions translated alongside. A coverage
  GUARD now lives in the test suite: every key in the string table and
  every Settings label/description must have a Traditional entry, so a
  future string can never silently fall back to Simplified.
- **Its own faces.** ``applyLanguage`` already stamps the html lang
  attribute; the stylesheet now gives ``zh-hant`` its own font stack —
  Noto Sans TC first, Taipei Sans TC beside it, then the CJK-TC system
  faces (PingFang TC, Microsoft JhengHei) — across body, inputs, and
  buttons, so Traditional glyphs stop rendering through a
  Simplified-tuned font.
- **Tests.** The coverage guard, 勿擾 and the Settings window reading
  Traditional, and the lang attribute following the switch. Shell
  rebuilt.

Reset codes, e-mailed passwords, and phone sign-in become OFFERABLE —
the doors existed; the deployment could never open them:

- **The gap.** All three doors were fully built (hashed expiring
  codes, throttles, no-enumeration answers, texted passwords) but
  gated on a mail/SMS sender — and the docker-compose files never
  passed a single mail or SMS variable through, so a deployed server
  answered 404 "not offered" forever. Worse, the only real mail
  sender spoke an HTTP JSON API; the classic SMTP mailbox most
  operators actually have had no door at all.
- **`SmtpMailSender`.** Pure-stdlib SMTP (STARTTLS on 587 by default,
  ssl on 465, none for an in-network relay), fresh connection per
  send. `build_mail_sender` learns it: ``OOLU_SMTP_HOST`` +
  ``OOLU_MAIL_FROM`` (+ user/password/port/security) — SMTP outranks
  the HTTP door, and a HALF-configured SMTP refuses to boot with the
  missing name rather than silently leaving the doors shut.
- **Compose pass-through.** Both docker-compose files now carry every
  mail and SMS variable (`OOLU_SMTP_*`, `OOLU_MAIL_*`, `OOLU_SMS*`,
  `OOLU_TWILIO_*`) so a `.env` entry is all it takes; the deploy guide
  gains a section for each door with copy-paste configs (SMTP, Resend
  style, Twilio, generic JSON, console dry-run).
- **Tests.** The SMTP builder (defaults, ssl port flip, precedence
  over HTTP, loud half-configuration refusals) and the sender's exact
  SMTP conversation (starttls → login → send → quit) via an injected
  fake.

The interact agent gets hands — and the org wears its name:

- **New hands in the node window.** The interact agent can now DO the
  desk work it could only describe: ``write_file`` grew a ``folder``
  arm (upload straight into a folder of this node's drawer),
  ``create_folder`` makes a new one (held open by a ``.keep`` file
  until real files arrive), ``create_member`` mints a node on the
  org's access desk (this Supernode's, or the fleet a member serves
  under — unclaimed until someone onboards it), and ``grant_host`` /
  ``block_host`` / ``block_user`` move the node's egress consent and
  refusals. Every hand flows through the SAME real handlers as the
  Access desk's buttons — ownership walls, fixed-trait refusals,
  validation, and audit bind the model exactly as they bind a human.
  The operator charter teaches each tool with its exact JSON shape.
- **The org in words.** A member's card said "under 3f9a2c1d" to the
  person who onboarded it — the Supernode's title only resolved from
  the viewer's own desk. ``/v1/work/nodes`` now carries
  ``supernode_title`` resolved server-side, so the onboarder reads the
  org's NAME exactly as the owner does; the id remains the last
  resort.
- **Tests.** Folders and uploads landing in the node's drawer (with
  honest answers from unwired hands), access hands flowing through the
  real account door (grant/block land, idempotent in words), member
  minting from the org window (standalone nodes refuse; dispatch
  reaches the hand by tool name), and the member card naming its
  Supernode. Shell rebuilt.

Node deletion is REAL — everywhere at once, with a 7-day undo:

- **The gap.** "Delete" on a Work node was a list margin: the node
  left YOUR sidebar but kept living everywhere else — still on its
  Supernode's Access roster, still resolving goals, still runnable.
- **Tombstone now.** ``DELETE /v1/work/nodes/{id}`` (desk-walled)
  stamps ``deleted_at`` on the node's account: it leaves the Work
  desk, its Supernode's member roster, run resolution (chat, API, and
  webhook fire alike), the twin guard, and the build dedupe in the
  same moment — a deleted node never blocks rebuilding its goal, and
  a rebuilt twin resolves past the tombstone. The marketplace listing
  is revoked best-effort with it.
- **Revive within 7 days.** The accidental-delete safety window:
  ``POST /v1/work/nodes/{id}/revive`` restores the node whole — the
  node's own responsible/admin or its SUPERNODE's may do it; 410 once
  the window closes. The Access tab shows a "Recently deleted" list
  under the member roster with a Revive button and the deadline, fed
  by ``GET /v1/work/nodes/{id}/deleted-members``.
- **Purge after.** The hourly retention tick removes accounts whose
  window has passed — the account row, the node's whole drawer, and
  its webhook go together, audited as ``node.purged``. The delete
  becomes exactly what it said.
- **Tests.** Both stacks: gone-everywhere + revive-restores, the
  walls (stranger delete 404, stranger revive 403, double delete),
  the closed window (410) and the final purge (account + files), the
  tombstone never blocking a rebuild; the UI confirming into the real
  delete door with the 7-day hint. Shell rebuilt.

The conversation window tidies up — one fold control, drafts that
survive, the representative IN the thread:

- **One fold control.** The Work page's conversation window carried a
  duplicate fold/unfold button next to Back; it's gone. The My-nodes
  column's toggle is the one control, and it survives folding as the
  thin rail's own button — same as Life.
- **Unsent words survive.** The OoLu chat and every node's interact
  window now keep their typed-but-unsent message when the user leaves
  for another conversation and comes back — the same per-account
  compose store friend threads already used (sending clears it;
  sign-out purges it).
- **The representative lives in the conversation.** Waiting replies no
  longer pop as a separate window pinned above the chat: the drafts
  inbox rides IN the thread as an execution-style block at the
  conversation's end — bordered like a run card, scrolling with the
  messages — through the new ``inlineBlock`` seam on ``Chat``.
- **Tests.** Draft persistence across unmount for both composers, the
  inline block rendering inside the thread, and Work carrying exactly
  one fold control. Shell rebuilt.

Failed runs revive in place — no more phantom siblings in the Noder —
and retention finally applies:

- **The pile-up.** Asking a goal OoLu had no working node for failed —
  and asking again minted a WHOLE NEW run, so the Noder list filled
  with dead threads of one goal (each looking like a node that never
  syncs to Work, because it never was one). Worse, a failing execution
  parks on the incident door (awaiting retry/abort) — and the re-ask
  ignored the waiting run entirely.
- **Revive, don't recreate.** The orchestrator gains ``restart``: a
  FAILED run re-drives IN PLACE — same run_id, same thread, per-phase
  outputs reset, human gates (confirmation, approval) re-earned, retry
  counted, history kept. The chat surface and ``POST /v1/runs`` now
  revive the caller's own dead-or-stuck run of the same goal: a
  terminal failure restarts; an incident-paused run takes the re-ask
  AS the retry answer. The revived attempt re-resolves the node fresh
  — a node built or revised since the failure now carries the route —
  and the thread RISES (its moment moves), which is exactly the
  latest-executed-first order both the Noder and Work lists already
  sort by. One goal, one thread, however many attempts.
- **Retention applies for real.** ``prune_retention`` existed but
  nothing ever called it. It now covers terminal runs (the dead
  threads nobody revives), finished queue tasks, delivered outbox
  rows, AND the audit chain's oldest prefix — pruned as a PREFIX only,
  with the cut attested in-chain (an ``audit.retention`` entry names
  the hash the surviving chain resumes from), so ``verify`` still
  passes while a SILENT prefix deletion still fails. The gateway runs
  it on ordinary traffic, hourly, under the new
  ``retention_days`` config (default 45; 0 turns it off); live and
  paused work is never touched. The activity log stops growing without
  bound.
- **Tests.** The revive loop end to end (fail → re-ask same thread →
  heal → same thread completes; a different goal is a new thread; a
  stranger's same-goal run is never reused), retention trimming runs
  and attesting the audit cut, the hourly tick from config, and
  retention never touching live work.

Per-user API draw — every account gets its own gauge on the shared
platform key:

- **The gap.** On the global service every self-registered user shares
  one tenant, and model usage was booked per TENANT — so the admin
  Finance monitor collapsed everyone into a single "main" row. Whether
  each user's draw was recorded could not even be checked.
- **Who-drew books.** `model_usage_accounts` sits beside the tenant
  books: the SAME consultation is booked on the tenant line (the
  quota's basis) and on the acting user's own line, in one
  transaction. The router now carries an actor channel (`act_as`) —
  routers are cached per (tenant, purpose), so the gateway names the
  drawing principal before every use: the speaking user in chat, the
  node author's builder seat, the reviser, the rebuild offer — and a
  fleet interact names the SUPERNODE OWNER, the account those books
  were already charging in name.
- **Independent gauges.** `GET /v1/platform/finance` now lists every
  user under their tenant row — calls, tokens, and drawn USD each —
  and the operator SPA renders them as sub-rows with their own
  checkboxes. `GET /v1/usage/model` answers each user with `mine`:
  their OWN line, not the tenant's collapsed total.
- **Per-user give-back.** The giveback door accepts
  `users: [{tenant, account}]`: one user's booked spend is erased and
  the shared tenant line decreases by exactly that amount (a negative
  adjustment, current month) — the shared quota refills by what that
  user drew, and everyone else's gauge stands. A whole-tenant reset
  clears its user lines with it; audited by name and amount as before.
- **Tests.** The double-entry booking, the exact per-user refill, the
  router naming the actor, the seat helper tolerating stub brains, the
  monitor's per-user gauges, the user-level giveback door, and the
  personal usage view.

Node provenance — immutable commits, sealed releases, honest
revocation (the build policy: a draft is a laboratory, a verified node
is a sealed artifact):

- **`oolu/nodeplace/provenance.py`.** Two append-only, per-tenant,
  content-addressed ledgers. COMMITS: every write to a node's function
  — build, revision, repair promotion, hand edit in the Code tab —
  files an immutable commit chained to its parent, carrying the tree
  hash, per-file hashes, the bytes (bounded), the instruction, and the
  author; the drawer's current tree is just the HEAD of a chain that
  preserves every attempt (the same tree twice is the same commit — no
  empty history). RELEASES: a verified run seals the EXACT tree it
  executed as a release; re-verifying the same tree is the same
  release. The module can only INSERT OR IGNORE artifact rows — no
  update, no delete, by construction.
- **Revocation over modification.** A release's operational status
  lives in a separate CONTROL row (active | revoked). Revoking names
  the reason and refuses new runs of that exact tree at every door —
  chat, `/v1/runs`, the public webhook — with `release_revoked` in
  words; a REVISED function is a new draft (different tree) that runs
  to earn a new seal, and a revoked artifact cannot be laundered by
  re-sealing it. The chat surface answers the refusal conversationally.
- **The release stamp.** Every resolved node function is stamped with
  what the policy says about the exact tree about to run: sealed (it
  IS the latest verified release), a draft (edited since the seal), or
  revoked. Advisory where it can be, a wall where it must be.
- **Doors.** `GET /v1/work/nodes/{id}/commits` (the function's history,
  read like a repo log), `GET /v1/work/nodes/{id}/releases` (each with
  live status), `POST …/releases/{rid}/revoke` (reason required,
  idempotent, first reason stands) — all desk-walled. Reuse decisions
  now land on the audit log too: running the node that already answers
  files `reuse_directly`; building past the twin guard with the user's
  explicit "this is different work" files
  `create_new_node_with_justification` naming the node considered.
- **Tests.** The chain (parents, dedupe, preserved attempts, tenant
  wall), tree-hash identity, idempotent sealing, revocation standing
  through re-seals and lifting on revision, the stamp and the
  production guard, drawer-tree commits through the files store, and
  the desk-walled doors end to end.

Typed output ports, port edges, and lineage — the typed-workflow
contract binds every run:

- **The dropped stamps (bug).** The node-function route copied only the
  egress keys and inline files onto the run's action — `bundle`,
  `bindings`, and `_value_tenant` were silently DROPPED. A node whose
  `src/` tree had been frozen into a content-addressed bundle ran
  without its own files, and the exact-value binder had no tenant wall
  on node-function runs. The engine now carries every stamped key
  (`bundle`, `bindings`, `_value_tenant`, `_output_ports`, egress,
  files), so what the gateway resolved is what the sandbox runs.
- **Output ports enforced.** A node has always DECLARED what it
  produces (its `produces` slots) — now the runtime holds every
  successful payload against that declaration before trusting or
  caching it. `output_port_problems` (runtime/contract.py) is the
  deterministic output validator: a missing declared port or a
  mistyped value fails the run with the gap named — the exact shape a
  mocked answer takes ("executed once, nothing real computed") is now
  a failure, not a success. The gateway stamps `_output_ports` from
  the node's own contract; the script runner validates on every path —
  cached (contract drift stales the cache), provided (the repair model
  hears `output_contract_violation: …` in correctable words),
  repaired, and synthesized (never cached on violation). Legacy nodes
  with no declaration validate nothing and keep working.
- **Port edges: `output://{node}/{port}`.** The edge form of a
  reference — "whatever the named producer last filed on that port."
  A per-tenant PORT INDEX points each (producer, port) at the newest
  snapshot (history stays append-only; retries never overwrite, they
  move the pointer), and the binder resolves the edge to the exact
  stored value at run time — an empty port is an honest miss, and the
  provenance keeps the edge next to the value it resolved to. This is
  how a downstream node consumes an upstream answer without any value
  being retyped through a model.
- **Lineage.** `value_lineage` records which stored values went INTO
  producing which stored values, and through whom — append-only,
  idempotent, walled per tenant, both directions
  (`ValueStore.lineage`). When a node-function run COMPLETES, the
  gateway files its payload per port (filling the port index) and
  records lineage from the run's resolved input references — so every
  execution can be reconstructed from stored state. `GET
  /v1/runs/{id}/lineage` answers each output field's ref with the
  inputs it was computed from and the work later computed from it,
  submitter-walled like every run read.
- **Tests.** The validator naming every gap; the runner refusing a
  success that skips declared ports (and the repair loop hearing the
  gap); the route carrying every stamped key; port edges resolving
  through the index with honest misses and the tenant wall; lineage
  both directions and idempotent; completion filing ports + lineage;
  the endpoint walled.

The exact-value reference layer: refs in, exact values out:

- **`oolu/values.py`.** The architectural form of the exact-value rule.
  Every authoritative value is stored ONCE — immutable, typed,
  tenant-owned, content-addressed (`value://{tenant}/{id}`; the same
  typed value is always the same reference) — and everything upstream
  of execution speaks by reference. Decimals and identifiers ride as
  strings, so scale, leading zeros, and case survive verbatim.
- **The deterministic binder.** `resolve_bindings` turns every
  `value://` reference among a run's bindings into its exact stored
  value — tenant wall, type check, honest lookup failure, one
  provenance line per resolution — and the script runner applies it
  just before the sandbox: the cache keys on real values, and
  `bindings.json` stages what the runtime holds, never what a model
  retyped. An unhonorable reference BLOCKS the run with the reason
  named; a missing authoritative value is never filled from memory.
- **Result snapshots and the renderer.** `GET /v1/runs/{id}/values`
  files a run's result outputs as immutable refs (submitter-walled,
  audited); `POST /v1/values/render` is the deterministic renderer —
  the model shapes the sentence segments, the store supplies every
  number, identifier, and date through registered formatters only
  (raw, decimal_exact, currency_code, date_iso, identifier). A missing
  reference refuses with 422; the renderer never fabricates the value
  it exists to guarantee.
- **Tests.** Exactness (scale, leading zeros, case), the tenant wall
  and honest misses, the binder's provenance and named refusals, the
  runner staging resolved values and blocking bad refs, the renderer
  end to end, and the gateway routes walled like every run read.

The exact-value rules: real computation only, values from the runtime:

- **The gap.** A mocked function RUNS: it emits a baked-in answer and
  every gate that only checks "did it execute" passes — one successful
  execution, nothing real computed. And the authored function had no
  channel for its exact inputs, so the model retyped (or invented)
  values as literals in the code.
- **The mock screen.** `mock_smells` in the screening module: an
  AST-level check that refuses an authored function whose
  `emit_result` is handed a constant the model wrote (plain, dict,
  f-string of constants — a constant in any costume), or whose code
  names its own pretending (mock/placeholder/dummy/sample data).
  Enforced at BOTH authoring doors — the one-shot
  `author_node_function` and the agent's `finish_node` gate — as a
  correctable refusal naming exactly what to fix, so the model
  rewrites instead of shipping a fabricated success.
- **The exact-value channel.** A node's resolved bindings now ride
  into the sandbox as `./bindings.json` on EVERY run — cached,
  provided, and resynthesized alike — so the function reads the exact
  values the runtime bound, never literals the model retyped. The
  prompt's new REAL COMPUTATION ONLY block teaches all of it: read
  inputs from bindings, compute from real sources, and emit_error
  naming what is missing rather than fabricate — an honest failure
  outranks a fabricated success every time.
- **Tests.** Every costume of a constant refused and real computation
  passing clean; both authoring doors refusing in words; the agent
  correcting on the named refusal; and bindings staged verbatim into
  the backend request. Authored-script fixtures across the suite now
  compute their answers.

OoLu gets a face: the mark, the favicon, the login lockup:

- **The mark.** Two nodes joined by a route — the big O and the small
  o of the name, read as a workflow edge. One stroke-only inline SVG
  (`OoLuMark`), wearing the accent in both palettes, scaling anywhere
  without an asset path to break.
- **The login page.** The card opens with the lockup — the mark beside
  the wordmark — instead of the bare word.
- **The favicon.** The same mark as a data-URI SVG icon on the desktop
  app (riding the committed shell) and the operator UI alike — no
  file to 404, one identity in every tab.

The investor metrics tracker: one catalog, one daily ledger, live:

- **`telemetry/investor.py`.** A declarative metric CATALOG (engagement,
  nodes, executions, model, capital, code, SEO — one line to register
  the hundredth metric), a daily snapshot ledger (`metric_snapshots`,
  one honest point per metric per day), and a collection service whose
  readers are closures over the app's REAL stores: DAU/WAU and average
  daily use time off the run books, node totals off the registry,
  executions daily/all-time, token/call/spend totals off the model
  usage books, capital-in-app off the earnings balances. A broken
  reader is skipped, never a blanked panel; an unwired metric shows
  honest absence, never a fake zero.
- **The routes.** `GET /v1/platform/metrics` (the grouped catalog view),
  `GET /v1/platform/metrics/history` (the charted series), `POST
  /v1/platform/metrics/snapshot` (the daily tick a Routine can drive) —
  all behind `metrics:view` like the other operator screens — and `PUT
  /v1/platform/metrics/{key}`, the approved, audited manual door for
  sources the app cannot see: GitHub commits, SEO, capital raises.
- **The panel seed.** `deploy/investor-panel.html`: a single
  self-contained page for the investors domain — server + token
  configured on the page, grouped stat tiles refreshed each minute,
  manual sources badged. The history endpoint is the substrate the
  full monitor panel and analysis reports build on.
- **Tests.** One point per day in the ledger; the broken-reader and
  honest-absence rules; and the gateway walls, real-book reads, manual
  recording with audit, the snapshot tick, and the history read.

Work reads like Life: the fold, the tags, the order, the margins:

- **The fold, top-left.** The Work sidebar gains the same fold/unfold
  button Life wears at its top left — one shared choice, one layout.
- **Each trait its own tag.** The parenthesized regime sentence
  ("(Supernode, Audit, Auto-growing)") gives way to independent tags —
  Supernode, L{n}, Audit, Auto-growing — on the node card's header and
  on every Access-desk member row alike.
- **Newest upper.** Each work node now carries when it last moved (its
  newest run), and the sidebar orders pinned-first-then-newest with the
  same helper Life uses — hidden nodes leave the list and return when
  the node moves again.
- **Pin, mute, delete.** The node card's header row carries the
  owner's margins: pin and mute (marked 📌/🔕 in the sidebar), and
  delete-from-list behind a one-step confirm — the node's record
  survives, and the thread returns on new activity. One prefs store
  serves friends, runs, and nodes; the route is walled to the caller's
  own desk.
- **Tests.** Margins and last-activity on the list (walled to the
  desk), the tag split asserted trait by trait, and the seat/staffing
  flows unchanged.

The seat block, the branch trigger, and the fleet that stays a fleet:

- **One block per seat.** The member roster's execution-order dial and
  its word annotations are gone; each row wears ONE block — theme-
  colored saying "onboard" when a human answers for the seat, blue
  saying "on demand" when it runs unstaffed. The blue block is also
  the org's staffing hand: clicking it assigns a user to the seat
  (`POST /v1/work/nodes/{id}/assign` — the Supernode's responsible
  only, refused in words on a claimed seat, audited by name).
- **The structure re-reasons when the code says so.** Code size is the
  branch trigger: a member whose src/ outgrew the threshold (24 KB)
  marks the template preview `needs_branch`, and the operator's
  "Re-reason structure" button applies with `re_reason: true` —
  dropping the recorded verdict and thinking again, never a silent
  re-plan.
- **A fleet stays a fleet.** Whatever a member node builds from its
  interact window now lands under the member's own Supernode — never
  a stray standalone.
- **The org pays for its own interact.** Model consultations inside a
  fleet member's interact window ride the `node.interact` purpose, so
  the usage books carry the Supernode owner's line, separate from the
  visitor's chat.
- **Tests.** Growth pressure on the preview and the re-reason door;
  assignment walled to the org's responsible, refused on claimed
  seats, audited; the seat block staffing an on-demand member end to
  end.

The sandbox image carries the polyglot toolchains:

- **`docker/sandbox.Dockerfile`.** The hostile-execution base now
  installs Node.js, gcc, and g++ (with libc headers), so JavaScript,
  C, and C++ node functions run out of the box — no operator action
  beyond rebuilding the image. Debian's gcc/g++ register the cc/c++
  alternatives the polyglot wrapper invokes; compiles land in the
  /sandbox tmpfs under the same read-only-rootfs, no-network walls.
  Rebuild with:
  `docker build -f docker/sandbox.Dockerfile -t oolu-sandbox:latest .`

The node read like a repo, speaking the mainstream languages:

- **The Code tab.** The node card grows a Code tab that reads like a
  repository: the description up top (what the node was built to do —
  now riding the work-nodes payload from the registry), then every
  drawer file with a language badge and its size, each opening
  read-only with the path and badge on its head.
- **Mainstream languages, one contract.** The sandbox still speaks one
  contract — a Python script calling emit_result — but the FUNCTION may
  now be JavaScript (main.js/main.mjs), C (main.c), C++ (main.cpp), or
  shell (main.sh): a generated Python wrapper drives the toolchain
  inside the same sandbox and speaks the contract on the program's
  behalf — stdout becomes the result; a non-zero exit or a missing
  toolchain is an honest emit_error, never a silent nothing. main.py
  stays native and wrapper-free, and always wins over siblings. JSON,
  HTML, Markdown, and React sources are assets a node creates and
  stages, not entry points.
- **Tests.** Python stays native; every foreign entry generates a
  wrapper that passes the same safety screen; data and markup stay
  assets; and the Code tab end to end — description, badges, content.

Access gets its own desk: one tab for who and what may reach a node:

- **The Access tab.** The node card grows a fourth tab holding
  everything about reach — KYC verification, the org template, the
  member roster, the block lists (hosts and users), and the egress
  grant — so none of it ever crowds the activity log again. The
  Activity tab is the execution feed and the holds desk, nothing else.
- **Members are doors, and minted here.** A member node's name in the
  roster is now a link that opens that node's own card. Creating a
  node under a Supernode happens on the Supernode's own Access desk
  (name, optional Supernode mark, authority level) — the sidebar's +
  makes standalone nodes only, and lost its under-Supernode selects.
- **The global service opens the web by default.** A signed-in global
  account needs no per-host grants: `open_egress` gains a
  `default_open` stance the gateway sets from `global_service`, so
  every node's web stands open minus the chain's block lists —
  verification or none. Edge and local installs keep the allow-grant
  regime unchanged, and the grant UI on a remote build now says the
  default is open and grants only narrow.
- **Tests.** The default-open stance (blocks still bind, Edge
  unchanged); the Access desk end to end — sections live there, a
  nested Supernode minted from the member form, the member link opens
  the node — and the + creating standalone nodes only.

The operator's two-sided ledger, and the give-back that refills it:

- **`GET /v1/platform/finance`.** One screen, both sides of the books,
  read straight off the stores the meters write: per ACCOUNT, the model
  API spend drawn from the platform — all-time totals (calls, tokens,
  dollars), this month's per-source rows, and the subscription quota's
  own standing (allowance, spent, remaining, trial marker); per NODER,
  the node-execution revenue balances (available, pending, reserved,
  lifetime paid). Permission-gated (`finance:view`) like every other
  operator read — 403 for everyone else.
- **`POST /v1/platform/usage/giveback`.** The experiment-cohort refill:
  erase the booked subscription spend of all (`"all": true`) or
  selected (`"tenants": [...]`) accounts, restoring their allowance —
  a trial is measured lifetime, so the give-back reaches the whole
  history. Own-api and local rows are the account's own money and
  machine and never move. An approved (`approve:usage.giveback`),
  audited platform move: the forgiven amounts land on the audit log by
  name; an empty ask is refused in words, never a silent no-op.
- **The Finance screen.** A new operator-UI tab between Earnings and
  Users: the account table (with trial badges and checkboxes), "Give
  back to selected" / "Give back to all" behind a confirm, the refill
  summary with a refresh link, and the noder revenue table beneath.
- **Tests.** The wall (403 both doors), both sides of the books in one
  view, the selected give-back (t2 refilled, t1 untouched, own-api
  spared, audited by name and amount), give-back-to-all, and the
  refused empty ask.

The list reads like a messenger, the code greets first, the margins
live behind the photo:

- **The reading order.** Friends and Noder threads now sort the way a
  messenger reads: pinned first, then the most recently spoken — the
  newer, the upper. The server orders the friends list (and stamps
  `updated_at` on every run summary); one shared frontend helper
  (`orderThreads`) applies the same order to both sidebar groups.
- **The code greets first.** The start-a-conversation pane shows the
  user's own QR code upper middle the moment it opens — no tap — and
  ONE button flips the same spot between showing and scanning, so the
  window keeps one symmetric centered shape either way. A successful
  scan (or a camera failure) flips back to the code by itself.
- **The margins, behind the photo.** Clicking the profile photo in a
  friend's or a node thread's header opens the profile: the name note
  (rename moved here from the sidebar), pin, mute, hide, and delete —
  every margin in one place, for people and nodes alike.
  - *Pin* lifts the thread to the top; *mute* silences the unread
    nagging (the words still arrive); *hide* stamps a moment — new
    words bring the thread back by themselves.
  - *Delete* on a friend unfriends without blocking: messages stay,
    the thread leaves the list, and they may ask again. On a node
    thread it removes the entry from the list; the run's audit record
    is preserved.
- **The wire.** `convo_prefs` in the friendship store (one table, both
  thread kinds), `PUT /v1/friends/{peer}/prefs`, `DELETE
  /v1/friends/{peer}`, `PUT /v1/runs/{run_id}/prefs` (walled to the
  run's own submitter), and pinned/muted/hidden flags on the friends
  and runs lists.
- **Tests.** Store margins move only the named fields; delete clears
  my margins without a block; the gateway list reads pinned-first-
  then-newest; hide returns on new words; run margins are walled to
  their submitter. Frontend: ordering unit tests, the QR-first flip,
  profile rename/pin/delete, and the sidebar's pinned/hidden reading.

The revision reaches the registry, and the author gets its own tier:

- **The registry follows the revision.** A revised function updated the
  drawer (what runs) but left the registry contract frozen at the
  original interface — the goal assembler kept planning over slots the
  code no longer spoke. Now `revise_node` also contributes a NEW
  version on the SAME node, derived (lineage) from the one it replaces,
  carrying the revised script and the revised consumes/produces —
  before the drawer write, so a version the safety screen or ownership
  refuses leaves the node exactly as it was. The reply names the new
  version; the semver moves one honest patch step.
- **`model.build_tier`.** The node author's consultations may ride the
  reasoning tier while the conversation stays fast: a new model
  setting, default "inherit" (follows `model.tier`, so nothing changes
  until the user asks), read at call time by the `node.build` router.
- **Tests.** The revise reply names the followed version; the new
  version derives from its parent and carries the revised script; the
  tier setting moves the author without moving the chat, and inherit
  follows the shared tier wherever it goes.

The author's finish gate becomes real: a sandbox dry-run before trust:

- **The gap.** The `NodeAuthorAgent` carried a `verify` seam since it
  was born, but nothing filled it — a finished script was trusted into
  the drawer on the strength of containing `emit_result`, and its first
  real run was its first execution.
- **`_author_verifier`.** The gateway now fills the seam with the SAME
  script hand contract runs use — safety screen, dependency healing,
  contract classification — run with NO web grant and NO staged files,
  so nothing leaves the box: a refused `http_request` answers status 0,
  exactly what the script contract already teaches the function to read
  and report honestly. `finish_node` refuses a failing script back to
  the model as words to fix; the model can also `verify_function` early
  and iterate before finishing (the prompt now says to). Build and
  revise both pass through it — one gate, both doors.
- **Honest absence.** A host without a script runtime (no isolation
  backend) wires no verify hand at all — the agent authors exactly as
  before, and no fake gate pretends otherwise. A crashed sandbox is
  answered in words, never a dead build.
- **Tests.** The dry-run refuses a failing candidate and lands the
  corrected one (both executions observed, the refusal in the
  transcript, the passing script in the drawer); a runtime-less host
  offers no verify hand; a crashed sandbox answers in words.

The interact window learns to revise: THIS node's code, on your ask:

- **The gap.** Asking a node's interact window to change its code went
  nowhere honest: `build_node` refuses to touch the current node (its
  public-safety rule mints a separate sibling), and any direct edit
  depended on the chat model volunteering `write_file` calls. The one
  thing the user actually asked — "change this node's function" — had
  no hand.
- **`revise_node`.** A new interact-window hand, typed (`revise …` /
  `recode …`) or model-called: the seated author rewrites THIS node's
  `src/main.py` — the function's runtime home, so the next run executes
  the updated code. The change request rides with the current function
  framed in the goal; a tool-calling model works as the
  `NodeAuthorAgent` with a seat-scoped drawer read added to its hands,
  a reply-only model takes the one-shot door with the same context.
- **The same walls.** Revision sits behind the auto-build consent
  exactly like building (the model cannot rewrite code mid-conversation
  uninvited), writes only through the `node.build` seat (scope-checked,
  attested), and lands a `model.seat` audit line marked `revision` with
  the files written. Conversation is refused in words; no model, no
  files, no store — every dead end names itself.
- **The charter tells the model.** The interact context note now
  distinguishes the two doors: `build_node` for a separate new node,
  `revise_node` for this node's own function.
- **Tests.** The full consent ladder in the window (off → named switch;
  conversation → refused; no model → named; reply-only author →
  revision lands, audited), and the agent path: `recode …` seats the
  author with the drawer read, the change and current function frame
  the goal, and the rewritten script lands through the seat.

The node author becomes an agent: the library in hand, seated apart:

- **The gap.** A node's function was written from one thing — the goal
  sentence — in one blind shot by the same brain that chats. No look at
  the slot names already in circulation, no look at what the upstream
  node actually produces, and the interface regexed out of an `IO:`
  line in prose.
- **`oolu.author.NodeAuthorAgent`.** For models that speak native
  tool-calling: a bounded authoring loop whose hands are `list_nodes`
  (the desk's contracts — reuse slot names, don't mint synonyms),
  `read_node_output` (a named node's recent run outputs — code written
  downstream parses the shape that ACTUALLY arrived), `read_file`, and
  an optional `verify_function`. Delivery only through `finish_node`
  with the script and its interface as schema-validated arguments; a
  script that never calls `emit_result` — or fails verification, when
  the seam is wired — is refused back to the model as a correctable
  answer, not an exception. Conversation is declined in words. The
  one-shot protocol still lands when it leaks through (same gates as
  `author_node_function`), and running out of steps is an honest
  refusal, never a silent nothing.
- **Seated apart.** The gateway routes the author's consultations under
  the `node.build` purpose — its own line in the meter, the usage
  books, and the audit trail, separate from `chat.turn` — and the
  `node.build` seat now declares the agent's hands. Model routers are
  cached per (tenant, purpose); a changed key drops every seat's
  router, not just the conversation's.
- **Nothing breaks where tool-calling hasn't arrived.** A model
  without `consult` keeps the exact one-shot path — every existing
  builder test passes untouched.
- **Tests.** The working loop end to end (library read, hard gates,
  decline, prose fallback, step ceiling, dead model), and the gateway
  seam: a tool-calling author builds a real node through `/v1/chat`
  with the desk-backed hands on the table and the script landing in
  the drawer; a reply-only author still takes the one-shot door.

Native tool-calling: one schema, both wires, validated before dispatch:

- **The gap.** Every hand the model could use rode as prose conventions —
  a fenced script here, an `IO:` line there — parsed back out of free
  text. The providers never sent a real tool schema, so the model was
  guessing at contracts the runtime enforces everywhere else.
- **`providers.tools`.** A `ToolSpec` is declared once with a JSON-schema
  parameter shape and rendered onto either wire (OpenAI
  `tools=[{"type":"function",…}]`, Anthropic `input_schema`); replies come
  back as a structured `ToolReply` whose `ToolCall`s carry parsed
  arguments. One neutral transcript shape converts to each dialect —
  including tool answers riding back as `tool` messages / `tool_result`
  blocks.
- **Nothing unvalidated reaches a handler.** The `ToolRouter` stands
  between model and handler: unknown names, malformed argument JSON, and
  schema violations (a stdlib validator: types, required, closed objects,
  enums, bounds, patterns, nesting) become error `ToolResult`s the model
  reads and retries on — a bad emission costs a turn, not a crash. A
  handler that blows up is likewise answered, never fatal.
- **`ChatModelRouter.consult()`.** `reply()`'s structured sibling on the
  same routing skeleton (now factored and shared): same budget gate, same
  provider order and failover, same books — but tools ride natively, on
  Anthropic, OpenAI, and any OpenAI-compatible local server alike, next
  to the existing server-side web-search tool.
- **The bounded loop.** `run_tool_loop` is the agent loop over that
  contract — consult, dispatch, feed answers back, stop on a final text
  or at `max_steps` — returning the full transcript for audit or
  follow-up. This is the floor the node-authoring agent builds on.
- **Tests.** A dedicated offline suite: both wire renders, the validator's
  refusals by name, dialect conversion round-trips, router discipline
  (nothing unvalidated through, everything answered), the loop's ceiling,
  and `consult()` end-to-end against a scripted transport for both
  providers.

The chat offers its hands: proactive node-building, on consent:

- **The gap.** The engine can build real automations — program files,
  guarded web/API/webhook hands, self-repair — but a user only found
  out if they happened to phrase a request as work. The chat knew how
  to accept building; it never *offered* it.
- **`BUILDER_OFFER_NOTE`, on every chat turn.** A standing system note
  (the `WEB_TASK_NOTE` idiom) that tells the model two things. What is
  true: a node carries its own `src/` tree the builder writes and keeps
  maintaining, installs the packages it needs, runs in a sandbox that
  cannot touch anything else, reaches APIs and the live web through the
  granted host-guarded hand, can be fired from outside by its webhook,
  and is repaired mid-run when its code trips. And what to do about it:
  when the user describes work that repeats or could run on its own,
  OFFER — one short sentence, in words — to build it. The offer
  discipline keeps the prompt's "never invent work" rule intact:
  `task` stays null until the user agrees; only their yes starts
  anything (the same one-message consent shape the engine's own growth
  offers use); a declined or ignored offer is dropped, one offer per
  chore, never a campaign.
- **The model-free door says it too.** The deterministic "what can you
  do" reply now names the building hands — automations with their own
  programs that call APIs, catch webhooks, crunch files, sandboxed and
  self-fixing — so even a keyless install stops hiding the light under
  the bushel.
- **Tests.** The note's facts and discipline are pinned; a gateway test
  proves every `/v1/chat` turn carries it; and an assistant-level test
  walks the shape end to end — the offering turn starts nothing, the
  user's yes is where the work begins.

The frozen trees themselves: a bundle inventory on the Storage tab:

- **`GET /v1/work/bundles`.** Every stored manifest with its file
  count, logical size, freeze time, and — the part that matters —
  whether a live node still freezes to it and which (by skill). The
  `live` flag is EXACTLY the sweep's reachability, computed by the same
  recomputation from each node's current drawer tree, so the inventory
  and the sweep can never disagree about what is dead: a bundle shown
  `unreferenced` is one the next sweep would reap once its blobs age
  past the grace. Same `hygiene:sweep` authority as the other read
  routes; totals come with an honest caveat (logical bytes — the
  content-addressed store dedups shared blobs).
- **A "frozen trees" card between the sweep report and the history.**
  One table: short bundle id, files, size, frozen-at, and a `live`
  badge naming the holding skills or an `unreferenced` badge for what
  the sweep will claim. The Storage tab now shows the whole lifecycle
  in one screen: what exists, what a sweep would do about it, the
  Routine that will do it unattended, and the audit trail of every time
  it happened.
- **Tested at both layers.** A gateway test freezes a stale tree next
  to a node's live one and asserts the route reports the stale one
  unreferenced, the live one held by the node's skill (with main.py
  correctly absent from the bundle), and the totals adding up — behind
  the permission wall. The browser flow checks the fresh host's honest
  empty state ("0 stored", no trees yet).

The sweep's history, read back off the audit chain:

- **`GET /v1/work/bundles/audit`.** The trail was already being written
  — `bundles.sweep_scheduled`, `bundles.swept` (manual or `scheduled:
  true` with the grantor's name), `bundles.sweep_unscheduled` — this
  route just reads it back: the records under the sweep's two audit
  run-ids, merged, newest first, capped at fifty. No new bookkeeping,
  and the same `hygiene:sweep` authority as the other read routes.
- **A history card on the Storage tab.** Under the Routine and the
  dry-run report, the whole story as one table: who granted the
  standing consent and at what interval, every firing (manual sweeps
  name the operator; scheduled firings name whose consent they ran
  under, with reclaimed bytes and counts), and every revocation.
- **Tested at both layers.** A gateway test replays the full arc —
  grant, manual sweep, scheduled firing, revocation — and asserts the
  route returns it newest-first behind the permission wall; the
  Playwright flow now also watches the history card fill in live as
  the admin grants, sweeps, and revokes from the browser.

The Routine gets a face: sweep reports in the operator UI:

- **A Storage screen on the operator page.** The gateway front-end grows
  a `Storage` tab (behind the same `hygiene:sweep` authority the API
  demands; members without it see the honest "no authority" message).
  Two cards, both against the existing endpoints — nothing new on the
  wire:
  - **sweep Routine** — the standing consent at a glance: enabled or
    not, the interval, who granted it and when, the last firing's
    finish time and its summary (reclaimed bytes, dead trees, orphan
    blobs, tier copies purged) or its recorded error. Enabling (or
    retuning the interval) submits `POST /v1/work/bundles/schedule` —
    the approve-gated act that IS the consent — and `Disable` revokes
    it via `DELETE`, same authority. The card is explicit that enabling
    consents to every unattended firing.
  - **sweep report (dry run)** — `GET /v1/work/bundles/sweep` rendered
    as honest numbers: dead frozen trees, orphan blobs, reclaimable
    bytes, what was kept (referenced or within the grace) and which
    reference sources were honored — the union-of-sources safety made
    visible. `Sweep now` applies it (`POST`, approve-gated) and the
    applied plan replaces the estimate.
- **Proven in a real browser.** New Playwright tests drive the whole
  loop end to end against the real host runtime: an admin enables the
  Routine at 6 h, reads the empty store's zeros, applies a sweep, sees
  the applied report, and revokes the consent; a freshly provisioned
  member opens Storage and is told plainly they lack the authority.
  The route-coverage test now pins both bundle endpoints to the page.

The sweep becomes a Routine: standing consent, fleet-safe firings:

- **The tension, resolved by moving consent up a level.** The manual
  sweep is approve-gated because it deletes; a schedule means unattended
  firings. So ENABLING the Routine is the approved act:
  `POST /v1/work/bundles/schedule` passes the same approve gate as a
  manual sweep, records who granted the standing consent and how often
  (min 1 h), and audits `bundles.sweep_scheduled`. `DELETE` revokes it
  (same authority, audited) and stops the next firing cold; `GET` shows
  the Routine — interval, grantor, last firing, last summary or error.
- **Lazy tick, atomic claim.** Firing uses the platform's own idiom
  (hold expiry): ordinary traffic advances the clock. Each request runs
  a due-check bounded to once a minute per host; the host that wins the
  one-conditional-`UPDATE` claim over the shared database performs the
  sweep — a whole fleet fires exactly once per due interval with no
  coordinator. Every scheduled firing audits as `bundles.swept` with
  `scheduled: true` and the grantor's name; a failed firing records its
  error on the Routine and waits for the next interval, never surfacing
  into a request. A quiet host fires late — the same honest trade the
  hold-expiry sweep makes.

Fleets share one materialized root — and the sweep is its one remover:

- **`OOLU_BUNDLE_MOUNT_DIR`: a network root every host mounts.** A
  multi-host fleet (many gateways/workers over one database and one
  object store) now shares the mounted bundle tier too: a bundle
  extracted by ANY host is instantly warm for all of them. The
  atomic-rename publish already made concurrent materialization
  race-safe (the loser discards its staging and uses the winner's tree),
  and freezing was already fleet-safe (idempotent CAS puts,
  `INSERT … DO NOTHING` manifests) — the shared root is the missing
  piece that makes the extraction cost fleet-wide-once.
- **Shared semantics: a host never evicts on its own judgement.** Naming
  a shared dir implies shared mode (override with
  `OOLU_BUNDLE_MOUNT_SHARED`), and shared mode turns per-host budget
  eviction OFF: one host cannot see the fleet's usage, and deleting a
  tree another host has bind-mounted read-only would pull it out from
  under a running container. Removal belongs to the sweep alone.
- **The sweep now purges the accelerator tiers with the dead.**
  `CasSweep` takes the tiers (warm tars, materialized trees) and, on
  apply, discards each dead bundle's copies — grace-checked (`discard`
  refuses a tree touched within the grace window, since `ensure`
  touches on every use on every host), counted in the plan as
  `tier_discards`, and dry-run-first like everything else about the
  sweep. `WarmBundleTier.discard` and `MaterializedBundleDir.discard`
  are the tier-side hands; a busy NFS dir simply waits for the next
  pass. Other hosts' private warm tars for dead bundles age out under
  their own budgets — bounded, and honestly second-order.

The sweep: reclaiming the shared bundle store, safely:

- **Idle growth, unbounded until now.** The bundle tiers made boot fast
  but nothing made idle lean: every edited node re-freezes to a new
  bundle and leaves the old manifest behind, and blobs for files no node
  references anymore sit forever. `oolu.runtime.sweep.CasSweep` reclaims
  those dead frozen trees.
- **Safe on a SHARED store — the whole point.** The CAS holds bundle
  blobs, the file drawer's blobs, and CAD exports as one content-
  addressed store, so identical bytes are one object. Two rules keep the
  sweep from corrupting a neighbor: (1) its authority is limited to blobs
  a now-dead bundle introduced — a CAD export or drawer-only upload is
  never even a candidate; (2) a candidate is deleted only if NO reference
  source holds it (the live bundles' blobs unioned with the drawer's
  `all_blob_refs()`), and a blob younger than the grace window, or whose
  age can't be read, is kept. Live-ness is recomputed from each node's
  CURRENT `src/` tree (idempotent re-freeze), so a bundle referenced by
  nothing is genuinely dead. The CAS stays the durable truth — a deletion
  costs only a re-freeze, so the rule errs toward keeping.
- **Dry-run first, platform-gated, audited.** `GET /v1/work/bundles/sweep`
  returns the exact plan (dead manifests, orphan blobs, reclaimable
  bytes, kept count) touching nothing; `POST` applies it under approve
  authority — the same gate the hygiene sweep uses — and records
  `bundles.swept` on the audit log. No CLI, by design: a destructive
  store operation stays behind the approval flow. A store adapter that
  can't cheaply enumerate its objects is reported unsweepable rather than
  swept on a guess.

Mounted bundle tier: mount the tree, stop extracting it per run:

- **The next lever after the warm tier.** The warm tier saved the
  *pack*; a large tree still cost one archive *extraction* per run when a
  backend unpacked its tar into the sandbox. The optional mounted tier
  (`MaterializedBundleDir`, opt-in via `OOLU_BUNDLE_MOUNT`, default off)
  removes that too: a bundle is extracted ONCE to a read-only,
  content-addressed host directory (`<data>/bundle-mounted/<bundle_id>/`,
  `0444` files / `0555` dirs, published by atomic rename) and then staged
  by *reference* — a read-only bind-mount in Docker, a symlink in the dev
  backend. A run copies no bytes at all, and the OS page cache keeps a
  hot bundle resident across ephemeral containers, so the boot cost of a
  professional-library node stops being paid per run entirely.
- **Docker: kernel-enforced and severance-safe.** The materialized dir
  bind-mounts read-only at `/opt/oolu/bundles/<bundle_id>`, and one
  `exec` symlinks its top-level entries into `/sandbox` so `import pkg`
  and `open('data.csv')` resolve transparently into the mount. The
  read-only mount is kernel-enforced (even root in the container cannot
  write back through a symlink — the tree is immutable), and a mounted
  directory is not a network, so the Phase-B network severance and its
  verification are untouched. `symlink_stage_cmd` is a pure function,
  unit-tested without a daemon.
- **A latent Docker bug fixed on the way.** `containers.run` was never
  passed its computed `volumes`, so the web-hand exchange bind-mount was
  silently dropped (undetected: the Docker backend has no daemon-backed
  tests here). Both the exchange and the new bundle mount now mount
  correctly.
- **Bounded and safe to evict.** The materialized dir is capped
  (`OOLU_BUNDLE_MOUNT_MB`, default 2048 MiB) and evicted
  least-recently-used, with a grace window (default 900 s, above the
  install + execute ceilings) that never evicts a directory that may
  back a live run. The CAS stays the durable truth: an eviction costs
  one re-extract, never correctness. The tier is default-off, so no
  existing deployment changes until an operator opts in.

Warm bundle tier: the packed tree survives a restart:

- **The in-memory prepared cache was forgetful.** Bundles pack once and
  reuse across runs — but only within one process. A deploy or restart
  lost every packed bundle, so the first run of each warm node after a
  bounce re-read its whole tree from the CAS and re-packed it, exactly
  the boot cost bundles exist to flatten.
- **`PreparedBundleCache` is now two-tier** (`docs/node-bundles.md`):
  a bounded in-memory LRU in front of a bounded on-disk **warm tier**
  (`WarmBundleTier`) of packed tars under the data dir. A miss in both
  reads the manifest and blobs and packs once, then writes back up both
  tiers; a node that ran before a restart stages warm on its very first
  run after it — no CAS re-read, no re-pack. Resolution is memory →
  disk → CAS, and the hit counters (`hits`, `warm_hits`, `misses`) tell
  the three apart.
- **The warm tier is safe by construction.** Each tar is self-verifying:
  its `bundle_id` fixes the tree it must contain, so a truncated or
  tampered file is detected on read and re-prepared, never trusted.
  Writes are atomic (a reader never sees a partial tar), and the
  directory is bounded and evicted least-recently-used, so it never
  grows without limit. The CAS is always the durable truth — a corrupt
  or evicted warm tar costs one re-pack, never correctness. Budget:
  `OOLU_BUNDLE_CACHE_MB` (default 1024 MiB; `0` disables the disk tier).

Node bundles: boot speed and idle efficiency at codebase scale:

- **The scaling problem.** A node grew from one `src/main.py` into a
  whole tree — a cloned professional library, hundreds of modules. The
  old staging path did not scale: every run re-read *every* `src/` row
  inline, carried the bytes through the durable run state, and copied
  them into the sandbox **one file at a time** (one container round-trip
  per file). Nothing was reused between runs, and each file's bytes
  lived inline in its own DB row with no dedup — two nodes cloned from
  the same software stored two full copies.
- **Content-addressed bundles (`oolu/runtime/bundle.py`,
  `docs/node-bundles.md`).** A node's `src/` tree (minus the `main.py`
  entry) now freezes ONCE into an immutable, content-addressed bundle:
  file bytes in the same CAS that backs the drawer's blobs (identical
  files across any nodes or versions store once), and a manifest —
  sorted `(path, sha256, size)` — hashed to a `bundle_id`. Freezing is
  idempotent; two identical trees are the same bundle.
- **Ship the reference, not the bytes.** A run now carries the 64-char
  `bundle_id` and its small manifest, never the tree's contents, so the
  durable `RunState` stays tiny no matter how large the node is. The
  gateway freezes at `_finalize_function` and ships the id; a single-file
  node ships nothing extra; an install with no bundle store keeps the
  tree inline (same bytes, same walls).
- **Pack once, reuse across runs, stage in one operation.** Materializing
  a bundle produces one deterministic tar, cached by `bundle_id` in a
  bounded, idle-evictable LRU (`PreparedBundleCache`) — the first run
  packs the tree once, every later run of the unchanged node reuses it
  with no CAS read and no re-pack. Staging extracts the whole tree in a
  SINGLE archive extraction instead of one round-trip per file; the same
  one-shot staging now also collapses small inline trees (N per-file
  execs became one tar).
- **Same trust, faster shape.** The bytes are the same `src/` files that
  already passed the drawer's walls; freezing re-checks every path and
  refuses an unsafe one; the tree still extracts into the
  network-severed sandbox and the script is still screened and verified
  by execution; the `bundle_id` joins the script cache key so an edited
  tree re-verifies. The CAS is the durable truth — the prepared cache is
  a pure accelerator, so an eviction costs one re-pack, never
  correctness. Ceilings (4096 files, 64 MiB/tree) are enforced at freeze.

The in-run repair loop closes its circle: healed code comes home:

- **A run that heals its own function now writes the fix to the
  drawer.** When a node's stored function fails at run time, the model
  edits it, the edit is verified by execution, and — as before — it is
  cached so the run still succeeds. What was missing: the healed code
  never reached `src/main.py`, so the drawer (the function's home since
  the model-seats work) drifted from what actually ran. Now, **after a
  COMPLETED run**, the gateway promotes the healed code into
  `src/main.py` through the `node.repair` seat — scope-checked and
  audited as a `model.seat` event, exactly once per run, and only for
  the node's OWN function (never some other script the route carried).
  A failed repair promotes nothing.
- **The run still never mutates files mid-flight.** The discipline
  holds: the repair loop touches no files while executing; the healed
  code rides the outcome evidence (`repaired_script`) and the gateway
  performs the explicit write afterwards. The runner also caches the
  heal under the healed code's own fingerprint, so the promoted file's
  very next run hits a warm cache instead of re-verifying — one heal,
  one execution.
- `node.repair` is now a **seated** call in `docs/model-seats.md`, not
  just a declared one; the migration table and the promotion flow are
  documented there.

The node's code becomes a file, and every model call gets a seat:

- **The bug: built nodes left no source file.** Building "succeeded" —
  the model planned the function, the node was created — but nothing
  ever appeared in the node's drawer, because no call site owned the
  duty of materializing the model's output: the function lived only
  inside the version's JSON snapshot, and the drawer's `src/` folder
  was a run-time input nobody wrote. Building now writes the authored
  function to **`src/main.py`** in the node's own drawer (the drawer
  speaks `.py` natively now), so the code is a real file a human can
  open, read, and change.
- **The drawer copy is the function's HOME.** Runs resolve the function
  drawer-first: `src/main.py`, when present, IS the script (the
  version's snapshot answers only for a deleted drawer copy), and the
  promoted file leaves the staged set — it becomes `user_script.py`
  itself, never also a sibling. The script cache now keys on the
  function's own fingerprint, closing the second half of "building
  keeps failing": an edited or re-authored function was previously
  SHADOWED by the cache-hit path replaying the old verified code until
  it failed twice — new code, new key, and the edit takes effect on its
  very next run while still re-earning trust by verified execution.
- **Model seats (`oolu/seats.py`, `docs/model-seats.md`).** Models are
  interchangeable — the tenant's Anthropic key today, OpenAI or a local
  model tomorrow — so everything that must NOT change with the model is
  now defined once, per call site, in a seat: the files it may read and
  write, the hands it holds, the charge it answers for, and the consent
  switch, meter purpose, and audit that govern it. The registry speaks
  the SAME purpose vocabulary the model router meters under
  (`chat.turn`, `plan.intake`, `plan.route`, `plan.synthesize`,
  `plan.rebuild`, `node.build`, `node.repair`, `rep.draft`), so
  accounting and governance agree on names. `DeskFiles` is the
  enforcement: one node's drawer held through one seat — writes outside
  the seat's scope are refused whatever the model asks for, a
  consent-gated seat will not open without the caller's attestation,
  and every seated write lands on the hash-chained audit log as a
  `model.seat` event (purpose, node, files written). The node-function
  author is the first fully seated call; the migration map for the rest
  is in the doc. A seat bounds what a call can REACH — verification
  (safety screen, severed sandbox, verified-by-execution, human
  confirmation) still decides what its output is WORTH.

The doors back in actually open: a Twilio SMS phone sign-up that
reaches a real provider, and a one-step forgot-password that e-mails a
fresh password:

- **Phone sign-up now reaches a real provider.** "Registration through
  phone with an SMS code is not functional" had one honest cause: the
  generic SMS sender speaks JSON + Bearer, and Twilio — the provider
  nearly every deployer reaches for — speaks form-encoded + HTTP Basic
  against a per-account message resource, and 401s the other shape. A
  first-class `TwilioSmsSender` sends exactly what Twilio expects
  (`To`/`Body`/`From` or `MessagingServiceSid`, Basic auth from the
  account SID + token, the derived `Messages.json` endpoint) and
  surfaces Twilio's own error message when a send is refused.
  `build_sms_sender` picks it whenever Twilio is configured
  (`OOLU_TWILIO_ACCOUNT_SID` + `OOLU_TWILIO_AUTH_TOKEN` +
  `OOLU_SMS_FROM`), asked for (`OOLU_SMS_PROVIDER=twilio`), or pointed
  at a twilio.com URL — and a half-configured Twilio now fails loudly at
  startup instead of silently falling back to a door it cannot speak
  through. The generic JSON door and `OOLU_SMS=console` (dev) stay.
- **Forgot password, one step: the server e-mails a new password.** A
  new `POST /v1/auth/reset/password` looks the address up, generates a
  secure password, sets it, and e-mails it — the user signs in with it
  and changes it in Settings, no code round-trip. It answers `202` for
  any address (no account enumeration), only ever resets a real
  e-mail-linked account, and receiving the new password counts as
  address verification, so a forgotten-password user is never also
  stuck behind the verification wall. The existing code-based reset
  (`/v1/auth/reset/request` → `/confirm`, user picks their own new
  password) stays alongside it.
- **The sign-in screen and docs follow.** The reset view gains an
  "e-mail me a new password" option next to the code flow; the phone
  door lights up the moment Twilio is configured; and
  `docs/going-online.md` documents the Twilio variables and both reset
  doors.
- **The one-step reset is hardened against griefing.** Setting a
  password the moment anyone asked handed strangers a lockout lever:
  knowing an address was enough to force-reset its account. Now the
  mailed password is **staged**, not set — it waits in a new
  `PendingPasswordStore` (hashed, 30-minute TTL) beside the real one,
  which keeps working untouched; the staged key becomes the account's
  password only on its first successful sign-in (which is also what
  proves inbox control and clears the verification wall), and a sign-in
  with the *current* password dispels any staged key. A stranger's
  reset now changes nothing the owner will notice, and the mail says so
  ("if you didn't ask, nothing has changed"). All three outbound doors —
  reset code, reset password, and the phone sign-in SMS — are paced per
  address/number by a new `SendThrottle` (a cooldown plus a daily cap),
  so none can be turned into a mail cannon or an SMS-billing lever, and
  the pacing never changes the `202`/`200` response, so it is not an
  enumeration oracle.

OoLu gets hands: web-capable nodes, files inside the node, and a
webhook that fires it — the sandbox stays severed:

- **The refusal this closes.** A task beyond the model's own reach — a
  web search, an API call, anything needing the live web — used to end
  in "the sandbox has no network, so a searching task can only fail":
  the conversation was literally instructed never to build such nodes.
  The boundary was real (and stays), but it was walling off the work
  instead of the risk.
- **The web hand: brokered, granted, severed.** A script node's function
  can now call `http_request(url, method=, headers=, body=)` from the
  same runtime module `emit_result` lives in. The sandbox NEVER gets a
  network — a run whose node carries an egress grant gets a bind-mounted
  file exchange (JSON request/response files; severance verification is
  untouched), and a host-side broker (`runtime/webhand.py`) answers each
  request through the SAME guarded HTTP executor http actions use: the
  machine allowlist, the node's `network_hosts` grant (or the open web
  minus the org's blocks for a verified Supernode fleet), and the
  always-on SSRF guard, re-checked on every redirect hop. Writes reach
  only granted hosts and never follow redirects; bodies and per-run call
  counts are capped; the broker keeps the honest record of every call.
  An ungranted run mounts nothing and the shim refuses in the words that
  fix it. The egress stamp now rides script actions exactly as it rides
  http actions — one consent, two hands — and the node-function route
  carries it too, so a chat-built node runs under its account's grants.
- **A node carries its own programs.** `ExecutionRequest.files` stages
  named files next to the script in both backends (relative paths only;
  escapes and harness shadowing are refused loudly; count and bytes are
  capped). The drawer's `src/` folder is the source: files there ride
  every run of the node's function, so a node is no longer one string of
  code but a small program with modules and data it can import and read.
- **The webhook door.** The node's owner mints ONE token-credentialed
  URL (`POST /v1/work/nodes/{id}/hook` → `/v1/hooks/nodes/{id}/{token}`);
  an outside system POSTing there fires the node's own function with the
  payload staged at `webhook_payload.json`. The token is stored as a
  digest and compared in constant time; re-minting rotates it; a wrong
  token and a hookless node answer the same 404. The fired run wears the
  MINTER's identity — quota, egress grants, and the model-written-code
  confirmation walls bind unchanged — and every mint/revoke/fire lands
  in the audit log.
- **The prompts stopped lying.** The function writer is taught the one
  honest door to the web (and that a web-needing task IS executable
  work); the search note now says one-off questions are answered inline
  while REPEATABLE web work becomes a task; and every chat turn carries
  the engine's web truth even when the conversation model itself cannot
  search — so no model refuses web work as beyond the machine. A node
  born reaching for the web says at birth that its hosts must be granted
  on its account before the calls pass.

The org runs like an SOP — optional audit under the root, and an
owner-set execution order:

- **Not every node in an org audits.** Only the org's ROOT Supernode
  (one with no parent) still always audits — humans in full control at
  the top. Everything created UNDER a Supernode — plain members and
  nested division Supernodes alike — now takes its creator's audit
  choice (default off): a division no longer needs a human
  countersigning every run just because of where it sits.
- **The execution order: the Supernode owner's SOP dial.** Each member
  node can carry an order number, set (and retuned — the order is
  MUTABLE, unlike the fixed trust regime) only by the parent Supernode's
  own humans, via the member list in the Supernode's window or
  `POST /v1/work/nodes/{id}/order`. Work flows in ascending numbers —
  an explicit hand-off to the next node, like an SOP; members sharing a
  number run in PARALLEL; a member with no number is called whenever
  needed.
- **The order binds at execution, not just on paper.** Every submitted
  contract passes the fleet stamp: ordered members present on the
  contract gain `provenance="sop"` edges the DAG scheduler honors —
  earlier groups finish before the next PRESENT group starts, ties stay
  parallel, different Supernodes' SOPs never entangle. Typed data flow
  outranks the SOP in either direction: a slot dependency is physics,
  and a contradiction surfaces as parallelism, never a cycle.

Imitate: the honest record button — guided lessons that build nodes:

- **The capability audit, answered in the tree.** The old vision (watch
  the user drive OTHER software) cannot be honest here: the desktop
  shell is capability-minimal by design (no input hooks, no screen
  recording, loopback-locked), mobile will never allow a backend screen
  recorder, and nothing reads other apps' logs. What the platform owns
  COMPLETELY is everything that runs through a node: the hash-chained
  audit of every execution, each node's daily log file, script
  stdout/stderr, and files of every type (text inline, binary blobs,
  CSV/PDF/image/office). The audit and the design it forces are recorded
  in `docs/imitation-learning.md`.
- **The Imitate button** rides the right edge of the
  Activity/Interact/Files row in Work → My nodes. Press it and teach:
  name the goal, describe each step in order, run the real work through
  the node while the lesson records — every run the window logged while
  recording pairs automatically with the demonstrated steps. Stop &
  build compiles the demonstration into ONE node through the same gated
  build path as every other door; the model is told the numbered steps
  ARE the plan and to imitate them exactly, never to re-plan. A refusal
  keeps the lesson recording (nothing demonstrated is lost); the built
  node lands on the desk needing verification, like every node.
- **Node creation as training data.** Every lesson persists verbatim —
  rows in the new `LessonStore` (goal, ordered say/run/file steps,
  timestamps, outcome; erased with the account) and a
  `lessons/lesson-<id>.json` data log in the built node's own drawer
  (goal, steps, paired executions, who taught it, where) — the solid,
  consent-gathered corpus later training rides on.

Friends the way people actually meet and remember — QR connect, a face
on the thread, name notes, and OoLu that recalls:

- **Side by side, physically: QR connect.** Start a new conversation now
  offers **My QR code** (the username as `oolu:friend:<name>` — nothing
  secret) and **Scan a code** (the camera + an in-page decoder; no image
  ever leaves the device). Scanning a friend's code looks them up and —
  being handed the code in person IS the invitation — sends the friend
  request without a second tap; if they already requested you, Accept is
  right there. A machine without a camera says so politely.
- **You always know who you're talking to.** The friend thread now wears
  a header just like OoLu's own: the avatar, the name (your note first),
  the real username underneath, and "friends since …" — the date the
  friendship was accepted.
- **Rename a friend the old way.** Click a friend's avatar in the list
  and leave a name note — "Anna from the conference" — the way phones
  stored prospects and first-met friends for decades. The note is the
  OWNER's alone (stored per account, ≤60 chars, empty clears it; a
  paragraph is refused: "a name note is a label, not a paragraph");
  the other side never sees it, and erasure takes the notes with it.
- **OoLu recalls people the way memory works.** A new `find_friend` chat
  tool searches the roster by username, by the user's own name note, by
  words from the conversation (with who said them), and by roughly when
  the friendship began (an ISO date or prefix) — reporting only what is
  actually stored, never guessing a name.

Personal settings split per account — with the tenant layer kept as the
safe shared base:

- **Working-style settings are the ACCOUNT's own.** The `app` and
  `account` groups — theme, language, notifications, voice, display
  name, currency, units, log retention, auto-build consent — now store
  per account (`tenant::principal`) and overlay the tenant layer: on a
  shared tenant (the Global service), one account's theme, units, or
  consent can never touch a neighbor's. Threaded end to end: the
  settings routes, the chat's get/set hands, the units directive on
  both surfaces (assistant AND representative drafts), the auto-build
  consent checks, and the LLM rebuild's consent chain (which now knows
  the submitting principal — an older one-argument resolver still
  works). The personal layer rides account erasure; the tenant layer
  survives, because it belongs to the tenant.
- **Shared where sharing is safe — by design, not accident.** The
  `subscription`, `model`, and `budget` groups stay tenant-scoped: they
  govern shared money and shared infrastructure, so the per-tenant
  caches built on them (the model router, the budget profile) remain
  valid across accounts — no personal value ever feeds them. And the
  tenant layer doubles as the shared BASE for personal groups: a value
  set there (an org-wide language, a default consent) reaches every
  account as its default until that account overrides — one place to
  configure an org, zero duplication.

The doors open: message non-friends, register freely, continue with phone:

- **Messaging no longer waits on friendship.** The backend always
  honoured the "receive messages from non-friends" setting (open by
  default) — but the UI offered strangers nothing except "send a friend
  request". Finding someone now offers **Message** alongside the
  request, whatever the relationship state; a friends-only recipient
  still turns the send into the friend-request nudge — their choice,
  enforced by the server, not preempted by the UI.
- **Registration is open by default.** "This server does not offer
  registration yet" was the closed-by-default config. A server exists
  to take accounts: `open_registration` now defaults ON (config and
  CLI), with `--no-open-registration` for closed installs; the
  global-service rule (open registration needs a mail sender) stands.
- **Continue with phone — on both doors.** A new SMS seam (`sms.py`:
  console/HTTP senders, `OOLU_SMS*` env) powers
  `POST /v1/auth/phone/start` + `/verify`: a texted one-time code (the
  same hashed, expiring, attempt-limited store the mail door uses)
  signs an existing number in and CREATES the account when the number
  is new — no enumeration either way. The sign-in and create-account
  views both carry the button; a fresh account flows into an optional
  choose-your-password page.
  - **Every login account is born with a real password.** A phone
    account's auto-generated password is texted to it; a Google
    first-arrival's is mailed to the proven address (hosts without a
    mail door keep the old unknowable-password behavior). Settings
    accordingly says **Change password** — never "set": there is
    always one to change.
  - **The account-creation rule.** Phone accounts live in the reserved
    `phone-…` username namespace, and manual registration can never
    mint names there (an e-mail local part that collides gets a `u-`
    prefix) — a number's account name can never have been taken.

OoLu is strictly personal, and a friendship exists from acceptance:

- **An accepted friend shows in the list, empty thread and all.** The
  friends list used to be the CONVERSATIONS list — an accepted friend
  who hadn't spoken yet was invisible. It now unions the roster
  (`FriendshipStore.friends_of`) with the conversations: both sides see
  the friendship the moment it is accepted, with a "New friend — say
  hello!" line where the last message would be; once words flow, the
  conversation entry takes over.
- **The OoLu conversation is gated per account — the fatal leak
  closed.** The thread cache lived under one device-wide key: a second
  account signing in on the same device READ (and then unknowingly
  re-uploaded) the first account's whole conversation. The cache is now
  keyed per signed-in account (`oolu_chat::<who>`), the compose stashes
  likewise, and SIGN-OUT PURGES every account-content cache on the
  device — a shared machine keeps nothing readable behind. Server-side
  history was already per account; now the device is too.
- **Memories are personal even on a shared tenant.** Life-drawer files
  gain an OWNER: on the Global service (where self-registrations share
  one tenant) each account's OoLu lists, reads, and edits only its own
  documents — by listing and by id alike (another account's file is
  indistinguishable from missing), through the routes and the chat's
  file hands both. Writing a name someone else used creates YOUR file,
  never edits theirs. Legacy unowned rows stay visible so nobody's
  drawer goes dark on upgrade; node drawers stay the node's own. The
  representative (QLoRA voice) was already scoped per account.

A message is delivered, never built for — and nodes get clean names:

- **"Reply to a friend" now has real hands, and never mints a node.**
  Message-shaped sentences — "tell bob I'll be late", "reply to alice
  that we're coming", "let kai know the meeting moved", "send X to Y" —
  are recognized DETERMINISTICALLY before any model: WHO resolves
  against the user's real friends and nodes (exact name → substring →
  habits break ties; a name matching nobody falls through, so "tell me
  a joke" stays chat), and WHAT is the user's own words, delivered
  marked as forwarded via OoLu. The same `messaging_intent` wall guards
  every node-minting door — the build refuses in words ("that's a
  message to send, not a node to build"), the growth trigger never
  offers, consented auto-build never fires — and the system prompt
  tells the model: messaging is never a task and never needs a node.
- **Node names are keywords, not transcripts.** `concise_name` now
  filters the trigger sentence's scaffolding — create/build/make/node,
  politeness, "automatically" — on top of the stopwords, so "please
  create a node that can reply to quinn on whatsapp" names the node
  “Reply Quinn Whatsapp”, not a slice of the whole ask. Machine ids
  (`keyword_slug`) deliberately keep the old filter: identities never
  shift under existing nodes. When only scaffolding survives ("build me
  a node"), the plain keywords still stand — a name is never empty.

The work actually completes: reminders are real, builds come first,
run-again reuses, and self-built code lands on the desk:

- **"Remind me" stops being a doomed workflow.** There was no scheduling
  capability at all — a reminder intent failed at route optimization
  every time. Now a reminder is what it really is: a ROW with a clock
  (`ReminderStore`, `/v1/reminders`). "remind me to X in 20 minutes /
  at 3pm" is DETERMINISTIC — parsed before any model, created through
  the store, confirmed from the STORED row in the user's local time
  (the client sends its timezone offset on every chat turn, and the
  model's context now carries the current clock). The model has the
  same door (`create_reminder` / `list_reminders` tools) for
  conversational phrasings — and is told its words alone never create
  one. Ripe reminders ring in the OoLu conversation via the client's
  poll, delivered exactly once even across devices. Reminders ride the
  account export and erasure like everything else.
- **The nodes and the route are built together, BEFORE the run.** With
  the standing "Auto-build nodes on my paths" consent on, a chat task
  whose route has no node no longer fires a doomed run and offers
  afterwards: the missing node is built FIRST — the model writes its
  execution function, the node lands on the desk — and the run that
  follows routes through that function. No consent, no silent build:
  the growth offer still asks, exactly as before.
- **Run again reuses the node — never recreates it.** Re-running a
  built goal routes through the existing node's stored function and
  mints nothing (proven by test: two runs, one node); a near-twin goal
  is a QUESTION (reuse or build distinct), never a silent second build.
- **Self-built code the user's credit paid for becomes a REAL node.**
  A completed run whose route the LLM rebuild wrote used to bury that
  proven script in one run's log — visible in the run list, absent from
  Work → My nodes. Now it is contributed as a function node WITH its
  script and given a desk account, so it appears in My nodes and the
  next run of that goal routes straight through it instead of
  rebuilding. One node per goal, refusals silent — persistence is a
  bonus on a succeeded run, never a new way for it to fail.

A settings reply is the real result, never the model's narration of one:

- **Explicit settings commands are deterministic again — model or not.**
  "set / change / switch / update … to …" and "turn … on/off" run
  straight through the settings node BEFORE any model is consulted (both
  the blocking and streaming paths), and the confirmation value is READ
  BACK from the store — "Done — Theme is now dark" states what the app
  actually holds. Soft verbs never hijack: an ambiguous or unmatched
  name falls through to the model instead of guessing.
- **The set_setting tool verifies at the boundary.** A successful tool
  result is now re-read from the store and reported as
  "set <key> to <stored value> — verified in the store"; a change that
  did not stick is an error. A set_setting action on a turn therefore
  PROVES the app is really configured — a lying layer is caught before
  the model ever sees a success.
- **An unbacked claim is corrected, not repeated.** If the model's final
  reply claims a settings change ("I've switched your units…", "your
  theme is now…", "Done — changed…") and no verified set_setting ran
  this turn, the reply is replaced with an honest correction telling the
  user nothing changed and how to apply it for real. The system prompt
  says so up front: words alone configure nothing, and the app checks.

The representative asks the USER what it's missing — never the peer:

- **Questions never leak into a draft.** When the model can't honestly
  write a reply ("what gathering? who's asking?"), it no longer files a
  draft full of meta-questions addressed at nobody. The persona prompt
  forbids asking the user inside the reply and gives the model an honest
  escape hatch — a single `NEED_INFO:` line — which files a WAITING
  draft (new status `needs_info`) whose text is the questions, kept out
  of the peer-facing inbox entirely.
- **OoLu gathers the information in conversation, one task at a time.**
  The sweep surfaces ONE waiting question per pass as OoLu's own
  assistant message in the user's conversation (appended to the server
  history and announced live) — no reply has to exist the moment the
  toggle flips. Three new chat hands close the loop: `rep_waiting`
  lists what waits, `rep_answer` redrafts with what the user just said
  (the fresh reviewable draft supersedes the question — status
  `answered`), and `rep_ignore` lays a message to rest. The chat turn's
  context says when drafts are waiting, so OoLu raises the oldest one
  when the moment fits.
- **Discard postpones; it never buries.** A discarded draft's words land
  in that friend's typing block (persisted per peer), ready to rework by
  hand — and the message earns a fresh draft when the peer writes again,
  when the representative is toggled back on, or after a day still
  unread (`has_draft_for` forgives a discard on those exact terms; a
  new `mode_on_at` column records the switch-on).
- **"Ignore it" means read.** A new `ignore` verdict (button on the
  draft card, or asked of OoLu in conversation) settles pending AND
  waiting drafts, marks the friend's thread read, blocks any redraft
  forever, and never counts toward the accept-rate — ignoring the
  message says nothing about the draft's quality.

The verified Supernode's web opens, and its org imports as a template:

- **Open web for a Supernode under the global account.** A Supernode
  verified as a legal entity (KYC on the Global service) is no longer
  limited to the 8-host egress grant: its whole fleet's web stands OPEN,
  flowing down the membership chain exactly like trust. The machine
  policy and the SSRF guard still stand; what changes is the node-consent
  wall — `KycService.open_egress` walks the chain, the execution stamp
  carries `_egress_open`/`_egress_blocked` instead of an allowlist, and
  the HTTP executor enforces the blocks on every redirect hop. An
  unverified Supernode (and every edge install) keeps the grant regime.
- **The org chooses its refusals, like a user.** Two mutable lists on
  the account, edited through the same door as the grant:
  `blocked_hosts` — hosts the org refuses (subdomains covered, binding
  every node down the chain, unioned never cancelled) — and
  `blocked_users` — principals the org will not hear from: their
  messages to the Supernode or any member are refused in words, exactly
  like a user blocking a user. The Work desk shows both editors on the
  Supernode; verified orgs see the open-web line in place of the grant.
- **The template button: a working structure, imported.** A Supernode's
  description resolves to a curated org template — member nodes with a
  NAME, one clear RESPONSIBILITY each, and an essential starting
  function — through `GET/POST /v1/work/nodes/{id}/template`.
  - **Deterministic plan first, reasoning last — the node execution
    concept applied to org design.** A RECORDED choice returns instantly
    and is never re-reasoned (the key sticks on the account after the
    first press); a keyword match over the description is pure
    arithmetic; only when evidence is thin is the model consulted — and
    then only to PICK a key from the catalog, never to invent an org
    chart. No model at all falls back to the lean generic shape.
  - **Lean beats large.** Every template — commerce, software, client
    services, public-service division, logistics, research, lean-org —
    seats at most 5 roles, because communication, coordination, trust,
    and clear responsibility are what limit mass-produced intelligence;
    a corporation or government gets a LEANER structure, not a bigger
    one. Scale comes from each role's node growing its function.
  - **Essential functions, deterministic.** Each imported node is born
    with a real script (no model writes it): it emits the role's
    structured work product — record fields and working checklist — so a
    route can chain on it today; imports are idempotent by role name,
    and members start unclaimed like any node minted under a Supernode.
- Tests: open egress takes a verified Supernode; blocks union down the
  chain and die at the redirect bounce; open beats the allow-grant; the
  account door edits blocks and still refuses fixed traits; a blocked
  user is refused in words; the catalog stays lean; all 28 role scripts
  execute deterministically and pass screening; resolution order
  (recorded > matched > model-picked > fallback) with the recorded
  choice never re-reasoning; the routes preview/record/import
  idempotently, owner-only. Backend 1215 passed; frontend 232 passed.

The chat frame settles: fold rail, aligned toggle, and hidden node IDs:

- **The list fold moves to its own rail.** The show/hide toggle for the
  friends-and-nodes column used to sit where the interaction window begins;
  now it lives at the upper-left of the list column itself. When folded, the
  column collapses to a slim 40px rail that keeps only the toggle visible, so
  reopening the list is always one click away and never steals the chat's
  space.
- **The representative toggle sits on OoLu's row.** The rep quick-toggle was
  a row above; it now rides in the chat header, on the same line as the OoLu
  name, threaded through a `headerAside` slot on `Chat` rather than living in
  the pane bar.
- **Node IDs are masked by default.** Every node ID renders as
  `***-<last six>` — a `NodeIdChip` that reveals the full value only when the
  user presses its eye button, and copies the full ID with its copy button.
  The reveal is per-chip and local; nothing is spoken.
  - **Ask OoLu to copy.** A new `copy` field on a chat turn carries a value
    OoLu is putting on the clipboard because the user asked ("copy that node's
    ID"). It flows model → `ChatTurn.copy` → gateway payload → the frontend,
    which writes it to the clipboard best-effort — so the ID reaches the
    clipboard without ever being printed in the reply. The system prompt tells
    OoLu to use `list_nodes` to find the ID and set `copy`, then say plainly
    that it copied it.

The reasoning streams live, end to end:

- **A streaming chat transport, strictly additive.** A new
  `POST /v1/chat/stream` endpoint sends the model's ⟨think⟩ reasoning as
  Server-Sent Events while it thinks, then a terminal `done` frame carrying
  the finished turn. The blocking `/v1/chat` is untouched; the frontend uses
  the stream when it's there and falls back to the blocking turn on a 404, so
  older hosts keep working. The whole path: model → router → assistant →
  gateway → ASGI → browser.
  - **Model layer.** `HttpxTransport.stream` opens the HTTP client in
    streaming mode; `OpenAiAdapter.chat_stream` sets `stream: true` (with
    usage in the terminal frame, so streamed calls still meter honestly); a
    pure, unit-tested `openai_sse_events` parser turns the wire into
    (text-delta, usage) pairs. `ChatModelRouter.reply_stream` streams for real
    on the local brain (the product default) and keyed OpenAI-shape providers;
    every other source (subscription, Anthropic) yields the finished reply in
    one chunk — same transport, coarser granularity.
  - **Assistant.** `respond_stream` / `respond_streaming` mirror `respond`'s
    decision order but emit each round's ⟨think⟩ content as it arrives, then
    finalize the authoritative turn from the complete text — so say/task/tool
    routing is unchanged and only the reasoning is revealed live. A model
    without `reply_stream` degrades to one blocking call per round.
  - **Gateway/ASGI.** `_chat_turn` gained an optional `emit` hook (blocking
    path passes `None`); the ASGI binding runs the turn in a worker thread and
    bridges its reasoning deltas onto the event loop as chunked SSE, with auth
    resolved before any stream headers so a bad token is a normal error.
  - **Frontend.** `api.chatStream` reads the SSE body with a stream reader and
    forwards reasoning deltas; the "thinking" bubble now shows the model's
    real reasoning growing token by token (dimmed, scrollable) instead of the
    canned phrase — the honest live picture the earlier fix promised. It falls
    back to `api.chat` when the endpoint isn't present.

Building a node: named, priced, real, and honest about the wait:

- **The build offer names the node it will build.** "I'll build a node for
  '<your whole sentence>'" became "I'll build the '<Node Name>' node for
  '<goal>'" — the name is `concise_name(goal)`, the exact title the node gets
  in My nodes, so the offer and the result match. Both build offers carry it;
  the reuse offer already named its node.
- **Building a node shows what it cost.** Writing a node's execution function
  is a real model call; it was metered but the figure was thrown away. The
  build reply now reports it — "Building it drew ≈1,240 tokens (about $0.0007
  of model compute)", or "free — written by your own local model" — captured
  by diffing the model-call meter across the authoring call. No figure is
  shown when nothing was metered, so the number is never invented.
- **"Build me a node" actually creates one — no more narrated builds.**
  General chat had no build tool, so the model could reply "Done, I built your
  node!" while nothing was persisted. Now an explicit "build me a node …"
  (the word "node" required, so "build me a report" stays ordinary work) is
  routed to the REAL builder before the model is ever consulted: it writes the
  function and persists the node to My nodes, or refuses in words. The system
  prompt also forbids the model from ever claiming it built a node — that is
  only ever done by the builder, which reports the result itself.
- **The "thinking" indicator stops pretending to show reasoning.** The live
  bubble said "Thinking — the reply lands when the reasoning is done", but it
  was a wall-clock boolean with a fixed animation — no reasoning, and often no
  reasoning model at all. It now says the honest thing ("Working on it — the
  reply lands when it's ready"). The genuinely real reasoning is what already
  ships: the model's own ⟨think⟩ trace, shown after the reply, and the run
  card's live phase/step timeline for work that becomes a run. (A live
  streaming reasoning view would need a streaming transport, which the chat
  route does not have yet.)

Units "auto" resolves the same way everywhere:

- **One stored signal decides `auto`, so every surface agrees.** The chat
  assistant used to read the region from the browser's transient
  `Accept-Language` while the representative (no browser request) fell back to
  SI — so the same account could get imperial in chat and metric in a draft.
  Both now resolve `auto` from the account's spending currency (`account.currency`,
  a per-tenant setting both already read): imperial for the US/Liberia/Myanmar
  currencies, SI otherwise. `units_directive(pref, currency=…)` replaces the
  header path; a metric account spending in USD can still choose `metric`
  outright.

The representative drafts in the user's units too:

- **The units preference now reaches the representative's drafts.** The same
  `account.units` directive the chat assistant honours is threaded into the
  representative persona prompt: a drafted reply expresses measurements the way
  the account chose. It rides UNDER the voice examples and above the output
  rule, so the user's own register still wins. The engine takes a
  `units_note_for(scope)` resolver (wired from settings in the host); the draft
  path has no browser locale, so `auto` resolves to SI while an explicit
  imperial/metric choice is honoured regardless — applied to both the
  gateway-requested draft and the auto-send path.

Three fixes: a fix that actually locks, units the user thinks in, a clean draft block:

- **Location now waits for the fix to sharpen instead of taking the first
  coarse one.** The previous change flipped `enableHighAccuracy` on, but a
  single `getCurrentPosition` still returns whatever fix exists *now* — on a
  phone that is the wifi/cell/IP estimate (tens of km off) because the GNSS
  chip hasn't locked, and on a laptop it is that estimate forever. `device.ts`
  now `watchPosition`s: it keeps the tightest reading, resolves the instant one
  lands within ~35 m, and if the window closes with only coarse fixes it hands
  back the best one *with its true ±radius* (never a stale cache) so the answer
  is honest about its own roughness. Coordinates print to six decimals (~0.1 m)
  so the string stops discarding precision the receiver did resolve.
- **A measurement-units preference — metric/SI or imperial — and the reply
  honours it.** New `account.units` setting (`auto`/`metric`/`imperial`,
  default `auto`) renders in Settings from the catalog with no new UI code.
  Each chat turn now carries a one-line units directive into the model's
  context (beside the mood and web-search notes): an explicit choice wins
  outright, and `auto` reads the user's region from the browser's
  Accept-Language — imperial only for the US, Liberia, and Myanmar, SI
  everywhere else. This is the first true user *preference* threaded into the
  LLM prompt, not just the interface.
- **The representative's drafted reply is now one clean OoLu message block,
  and the buttons no longer collide with it.** The draft containers
  (`.rep-suggestion`, `.draft-card`) had no styling at all: the inner accent
  bubble rendered full-width and the accent Send/Edit buttons sat flush against
  it, reading as one overlapping slab. They are now bordered, left-aligned
  cards — like an OoLu message — that hold the "drafted:" caption, the drafted
  text as an inset quote, and the actions clearly separated below by the card's
  own gap. Same treatment for the friend-thread suggestion and the OoLu-window
  drafts inbox.

A plain-language "buy me…" becomes a real, consent-gated order:

- **The intent→blueprint planner: a shopping ask now becomes a commerce
  route.** Nothing turned free text into a commerce blueprint — the order
  roads existed but only tests ever built them. `skills/commerce_intent.py`
  closes that: `parse_order_intent` reads a purchase ask ("buy me a stainless
  steel water bottle on Amazon for $24.99") into a typed `OrderIntent`, and
  `plan_commerce_blueprints` turns it into the candidate roads (Amazon when
  named, the general web road always). It is conservative about the one field
  that must never be guessed — the amount: an ask with only a budget ceiling
  ("under $30") or no price returns nothing, so a number the user would be
  asked to authorize is never invented.
- **A new seat in the optimizer, and the run stamped at the wrist.**
  `CommerceRouteOptimizer` sits in the `RouteOptimizer` port — the one seam
  that sees the brief — so a purchase ask yields commerce routes at plan time
  while every other ask passes straight through unchanged; it self-grounds its
  routes against the installed executors, so it needs no grounder changes.
  Because the plan is built before the run is known, `stamp_order_context`
  writes `run_id` and the account scope onto order actions at execution
  binding (in `_phase_execution`, beside `bind_brief_parameters`) — exactly
  what `PaymentAuthorizationResolver` needs to reconcile the order with the
  user's consent. `build_host_runtime` turns this on only where the
  order-placing hands are wired.
- **End to end, at last.** A single test drives the whole chain the engine
  runs — intent → commerce route → execution stamps run + scope → the resolver
  files the consent request and blocks, unspent → a real TOTP authorization
  releases it → the order runs — against the real consent store. The path that
  was structurally impossible three commits ago now works from a sentence.
- **Still honest about the edges:** fuzzy language beyond these patterns
  belongs behind an LLM intake port (it produces the same `OrderIntent`); the
  site-specific `browser_steps` that click a given storefront come from site
  profiles, not this parser; and reconciling the *observed* cart total against
  the *authorized* amount is a checkout verify step left named, not faked.

The consent finally reaches the order, and the Amazon road gets a hand:

- **A released authorization now flows into the order action — the missing
  wire.** The gateway could mint (`request`) and release (`authorize`) a
  payment authorization, and the executor could verify one, but nothing
  connected the two: an order action never actually carried an
  `authorization_id`, so it was structurally always blocked. The new
  `PaymentAuthorizationResolver` is that wire. An order action that declares
  its intent — payee, exact amount, the run it belongs to, the account scope —
  gets reconciled against the consent store: the resolver files the pending
  request the first time it sees the order (so it appears on the user's
  `/v1/payment-authorizations` list) and returns the released `auth_id` the
  instant the user has authorized it. The `run_id` the request always recorded
  is finally *read* (`PaymentAuthorizationStore.match`), not inert. It never
  authorizes anything — that still needs the user's 2FA and exact-amount
  re-confirmation — it only reconciles a plan's order with the consent the
  user gives. Tested end to end against the *real* store (no hand-supplied id):
  first attempt files and blocks, a real TOTP authorization releases it, the
  same action then executes; a quietly-grown amount is a different order that
  must be consented to afresh.
- **The Amazon road gets a real, honest hand.** `AmazonClient` had no
  production implementation — only test fakes. `BrowserAmazonClient` fills it,
  truthfully: Amazon offers no consumer order API, so `place_order` drives the
  same persistent, headed browser session as the general road through the
  cart → checkout steps the plan carries, pausing to the human for sign-in /
  OTP / CAPTCHA (it reuses `BrowserSiteDriver`). It plugs
  `build_commerce_executors(amazon_client=...)` unchanged, under the same two
  money gates. A per-site *specialisation* — Amazon-tuned selectors, priced as
  its own road — not a faster protocol that skips the browser.
- **Still needed, and deliberately not faked:** nothing yet turns a
  free-text shopping intent ("buy me X on Amazon") into a commerce blueprint
  whose order action carries that intent — the planner path that would stamp
  payee/amount/run/scope onto the action. The seam above is ready to receive
  it; that intent→blueprint planner is a separate, larger piece, left honest
  rather than half-built.

A master switch above every order — off until the operator says go:

- **Autonomous order placement now has an operator switch, and it defaults
  off.** Wiring a browser driver made the checkout road drivable — but a
  drivable road with a released consent + 2FA authorization would place a
  *real* order the moment both lined up, with no deployment-level control.
  The commerce executors (`AmazonExecutor`, `SiteDriverExecutor`) gained an
  `orders_enabled` gate that sits **above** the per-order consent gate: even a
  fully-authorized order is BLOCKED ("operator switch off") until the operator
  turns real ordering on. Browsing and searching are never gated — only the
  money step.
- **`ordering_enabled` on the host, `--ordering` on the CLI.**
  `build_host_runtime(ordering_enabled=...)` (default `False`) feeds the
  switch, and `oolu host --ordering` flips it. Deliberately distinct from
  `--transactions`: that opens the LaunchGuard so OoLu may charge its *own*
  prices; this permits OoLu to spend the *user's* money at a retailer through
  their released authorization. Two different kinds of money, two independent
  switches, both off by default. Omit the switch entirely (as tests and the
  in-process executors do) and the historical behaviour is unchanged.

The general road becomes drivable, and it knows when to hand you the wheel:

- **The checkout seam gets a real hand.** `skills/commerce.py` has always
  defined a `SiteDriver` port and gated the money step behind consent + 2FA,
  but no production driver ever implemented the port — so "buy this on any
  site" reached nothing. `skills/site_driver.py` fills it: `BrowserSiteDriver`
  maps a commerce step (open / search / add_to_cart / checkout) to the browser
  primitives the plan carries, running them through a `BrowserSession`. The
  production session, `PlaywrightSession`, is a *persistent, headed* Chromium
  profile — so a login survives between steps and the human can actually
  perform it — reusing the existing browser adapter's step vocabulary.
- **It pauses to the human for login, 2FA, and CAPTCHAs instead of trying to
  defeat them.** Before any step that needs the user's own session, the driver
  checks whether the browser is signed in (via a site-supplied probe) and, if
  not, stops and asks — through a `LoginGate`. The default `AssumeAuthenticated`
  never pauses; `CallbackLoginGate` hands off to the host UI and blocks until
  the user is done, and an abandoned login surfaces as an honest FAILED
  outcome. OoLu never types the user's password or code; it hands them the
  wheel for exactly the steps that are theirs, then resumes.
- **The composition root finally calls the wiring that was only ever
  defined.** `build_host_runtime` gained an opt-in `site_driver` (and
  `amazon_client`) injection: when present, the `web` (and `amazon`) commerce
  executor is registered and tied to the host's payment-consent + 2FA gate
  (`_payment_auth.is_authorized`). A signed-in session is not consent to spend
  and consent to spend is not a signed-in session — both gates hold,
  independently. The default stays `None` (a server host has no display to
  sign a storefront in), and **no money port is opened**: the LaunchGuard
  (`transactions_enabled`) and the checkout authorization are untouched.

A token stops being a word, and becomes a node:

- **The plan is now generated, not reasoned out.** A new package
  (`src/oolu/planner/`) prepares a model whose vocabulary is the
  node/route database instead of English. `NodeVocabulary` gives every
  node key (`route:{name}` — the same key the trace store grades) one
  stable token; because a composed route re-enters the library as a
  single node, one vocabulary tokenizes both nodes and routes. Goals
  never enter the vocabulary — they are free text, so they condition a
  plan through a bounded band of hashed goal tokens, and vocabulary
  growth follows the marketplace, not the sentences users type. Ids are
  append-only and freezable: a checkpoint keeps its meaning as nodes
  accumulate, and an unknown node degrades to `<unk>` rather than
  corrupting a sequence.
- **A plan reads like a sentence — and rolls out like one.** The trace
  store's verified runs lift into token-id sequences
  (`[BOS] [goal] node… [EOS]`), and `MarkovPlanner` — a pure-Python,
  dependency-free autoregressive back-off planner over those tokens —
  *generates a whole mission plan* by emitting node tokens until it
  decides the plan is done. Trained only on verified runs, it
  regenerates the reliable chain for a goal in one cheap pass;
  `benchmarks/plan_tokens.py` shows a month-end run and a supplier
  onboarding planned node by node with no framework in sight.
- **One architecture, four rungs, checked by arithmetic.**
  `PlannerConfig` describes a standard decoder-only transformer, and
  `parameter_count` is exact — so the curriculum the mission names is a
  four-number change with a verified size: `tiny` (~5.3M, a runnable
  reference), `s3b` (~2.9B), `s8b` (~7.8B), `s30b` (~30.5B). The real
  transformer (`planner/torch_model.py`) reads the same config and lives
  behind a new `workflow-plan` extra, imported lazily like the
  representative trainer; nothing in CI instantiates a billion
  parameters, and the corpus exports to portable JSONL so the training
  run happens off-box when the data reaches scale.
- **The model proposes; the type system still disposes.** The generative
  planner enters the marketplace only through the existing
  `ProposalModel` port (`PlannerProposalModel`): its plan is a prior, not
  a commitment — folded into the Beta posterior at the bounded proposal
  strength, unknown ids dropped, exceptions downgraded to
  verified-history-only. Typed backward-chaining still finds the routes
  and verified outcomes still choose among them; what the node-token
  model adds is a cheap whole-mission proposal for cold starts and
  long-horizon work. Design in `docs/node-token-planner.md` and
  `docs/adr/0006-node-token-planning-model.md`; nothing is wired into the
  running engine by default.

The hosted app signs people in:

- **The shell now knows when it is hosted.** The packaged desktop app
  learns "remote host, sign-in required" from its Tauri wrapper; a
  browser visiting the hosted app domain has no wrapper, so the shell
  believed it was the loopback desktop — Settings hit naked 401s
  ("missing bearer token") and the sign-in form aimed at a paired
  online server that doesn't exist ("Failed to fetch"). `oolu host`
  now serves the shell with an injected `window.__OOLU_REMOTE__`
  flag (`GatewayASGI(shell_remote=True)`): the sign-in gate appears,
  username/password posts to the app's own origin, and accounts
  created from the admin console sign straight in. No shell rebuild —
  the built bundle already honored the flag.
- **"Continue with Google", hosted.** The compose file now passes
  `OOLU_GOOGLE_CLIENT_ID` / `OOLU_GOOGLE_CLIENT_SECRET` through to the
  gateway, and the deploy guide gained the Google Cloud Console
  walkthrough (Web-application OAuth client; redirect URI
  `https://<app domain>/v1/auth/google/callback`). Leave the id empty
  and the button stays hidden.
- **The deploy workflow tells the truth about a bad key.** Both first
  runs failed at `ssh-add` with "error in libcrypto" — a
  `SSH_PRIVATE_KEY` secret whose line breaks didn't survive the paste.
  The workflow now strips CR characters and fails early with words
  ("re-create the secret with the entire key file; safest:
  `gh secret set SSH_PRIVATE_KEY < deploy_key`") instead of a
  libcrypto stack whisper.

Two doors and a deploy button:

- **The app domain shows users the app.** One public gateway now wears
  a different face per hostname: `GatewayASGI` gained `admin_hosts`,
  and requests whose Host header names an admin host get the operator
  console while every other hostname serves the product shell (the
  chat). `oolu host` reads `OOLU_ADMIN_HOST` (comma-separated
  hostnames) to turn this on; unset keeps the classic single-face
  host. The production stack wires it end to end: the Caddyfile serves
  `OOLU_DOMAIN` (the app — may list several names, e.g. `app.` and
  `www.app.`) and `OOLU_ADMIN_DOMAIN` (the console) to the same
  container, and the compose file hands the admin hostname to the
  gateway. Sign in and manage users at `admin.your-domain`; chat at
  `app.your-domain` — same accounts, same tenant walls.
- **Push to main, the droplet rebuilds.** `.github/workflows/deploy.yml`:
  every push to `main` SSHes into the droplet (secrets `DROPLET_IP`,
  `SSH_PRIVATE_KEY`, `SSH_PASSPHRASE` — the key is loaded into a
  transient ssh-agent, never written to disk unencrypted on the
  runner), resets the checkout to `origin/main`, rebuilds the compose
  stack, and fails loudly (with the gateway's log tail) if any
  container is not running afterwards. One deploy at a time by
  concurrency group; the setup walkthrough (mint the key, authorize
  it, store the three secrets) is in `docs/deploy-production.md` §7.

The production launch kit: Cloudflare + DigitalOcean + Docker + R2:

- **Blobs go to object storage with four variables.** The new
  `S3ArtifactStore` (behind the `s3` extra) speaks any S3-compatible
  bucket — Cloudflare R2 first — with the exact contract of the
  filesystem store: content-addressed `sha256:` refs, idempotent
  dedup, prune by age. `blob_store_from_env` selects it wherever
  `OOLU_BLOB_S3_BUCKET` is named (endpoint, key id, secret; optional
  prefix and region) and keeps the local filesystem otherwise — the
  file drawer's blobs and the CAD hand's exports move to R2 with no
  code changes above the port.
- **The stack in one compose file.** `docker-compose.prod.yml`: Caddy
  (automatic HTTPS) → OoLu (`oolu host`, tenant-walled multi-user
  gateway) → PostgreSQL (the production durable adapter via
  DATABASE_URL, never published), blobs on R2, platform model keys
  optional. `deploy/Caddyfile`, `.env.production.example`, and a
  Dockerfile extras build-arg (default `serve,http,oidc,postgres,s3`;
  add `,cad` for the geometry hand).
- **The walkthrough.** `docs/deploy-production.md`: Cloudflare DNS
  (proxied, SSL Full-strict), the DigitalOcean droplet, R2 bucket and
  scoped token, launch, real model APIs both ways (per-user own-api
  keys and platform subscription keys), what the tenant_id walls
  actually guarantee, backups, updates, hardening.

The live audition rig: a real brain, the real router, measured cost:

- **One command from key to verdict.**
  `python benchmarks/level_b_audition.py` puts a live model in the
  Level B seat through OoLu's OWN provider stack — ChatModelRouter,
  the Anthropic/OpenAI adapters, the secret vault, the call meter.
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` enter as own-api;
  `OOLU_LOCAL_URL` + `OOLU_LOCAL_MODEL` is the no-cloud door;
  `--tier reasoning` picks the heavier tier. The scripted
  careful-engineer runs alongside as the incumbent, and the table ends
  with the §22 number the spec says to decide by: what fitness COST
  (metered dollars per audition, measured, not estimated). No brain
  configured = a refusal in words and exit 2 — the seat is not
  pretended into.
- **Wire-true in CI.** The audition path is tested through the REAL
  adapter code with only the HTTP wire scripted: a provider-shaped
  brain earns FIT through the whole vertical, every model call enters
  the books (six charges, five steps plus the closing "done"), the
  planner's protocol prompt rides as Anthropic's system PARAMETER
  with the key in its one header, and the kernel's committed/rejected
  feedback demonstrably reached the model as conversation turns.

A model takes the planner's seat — proposing, never committing:

- **The seat speaks one protocol.** `projectgraph/planner.py`:
  `ModelPlanner` puts any `model.reply(messages)` brain — the
  desktop's configured router, a local server, a scripted stand-in —
  into the Level B seat. One fenced JSON step per turn (read /
  propose / run_cad / done); everything the model offers goes through
  the SAME doors as anyone else's work: the transaction kernel for
  truth, the judged CAD hand for geometry. It cannot commit, cannot
  skip the evaluator, cannot reach past the bench.
- **Failure is feedback.** Rejections return in words — "stale",
  the wall, the exact broken postcondition — and the tests prove a
  model that reads them REPAIRS: a stale-revision proposal is
  diagnosed and corrected mid-audition (fail → diagnose → repair →
  verify, the spec's most valuable trajectory, produced live).
  Babble is told the protocol once and then cut off — an honest
  "not fit", never a crash — and an out-of-protocol verb changes
  nothing at all.
- **The audition is two lines.** `level_b.model_planner(model)`
  enters the seat; the same §23 gate that passed the scripted
  careful-engineer decides. A scripted protocol-speaking brain earns
  FIT through the whole vertical (kernel patches, real geometry,
  filed evidence, advancement) in 5 counted steps — so when a live
  frontier model auditions, the bar and the bench are already set.

Level B: the whole vertical, benchmarked under one budget (step 6):

- **A subsystem change is the exam.** `benchmarks/level_b.py`: the
  suspension shaft grows 8mm -> 12mm, and a planner must propagate it
  through everything the vertical built — read the graph, grow the
  bracket's bore through the kernel (honestly, past the 20mm
  manufacturability wall), REBUILD AND MEASURE with the real geometry
  kernel, file the measurements as evidence citing the CURRENT shaft,
  and advance the status. Every contender gets the same Bench: reads
  free, proposals and CAD runs counted against an identical budget.
- **The finish line is the graph's, not the planner's.** Acceptance is
  recomputed from committed truth alone: clearance for the grown
  shaft, manufacturability, measured zero interference, measured mass
  in budget, approved status — evidence of yesterday's world verifies
  nothing. The scripted careful-engineer completes all five in 5
  counted steps and is FIT for the seat (§23); the reckless-intern —
  bores past the wall (caught by the kernel), never re-measures,
  ships anyway — fails the gate with its shortfall itemized. A
  model-backed planner auditions by implementing one function against
  the same Bench.
- **In CI, deterministically.** `tests/test_level_b.py` pins the
  claims: completion within budget, the caught violation, the refused
  pretender, determinism, and an honest failure on a starved budget.

The vertical gets physical: the CadQuery hand (step 5):

- **A real geometry kernel joins the hands.** `skills/cad_adapter.py`
  executes semantic CAD actions — never UI clicks — through
  CadQuery/OpenCascade: `build` turns a compact feature list (box,
  cylinder, hole, fillet, shell) into a B-rep solid and reports the
  spec's CAD observation honestly (validity, exact volume, mass under
  a declared density, center of mass, bounding box); `assemble` places
  parts and MEASURES interference by boolean intersection — a shared
  volume is a count, not a hope. STEP and STL exports land in the same
  content-addressed store the file drawer's blobs live in,
  self-verifying by sha256. Unbuildable features and a missing kernel
  fail in words; the hand is deterministic, host-side (like http), and
  the sandbox stays severed.
- **The whole loop, proven on metal (well, B-rep aluminium).** The
  evaluator judges the engineering hand like any other: a bracket
  whose measured 31 g breaks its declared 10 g mass budget is demoted
  by its own promise — naming only the broken half. And a marketplace
  NODE whose action is a cad build runs end to end through the
  contract path, its outcome carrying the kernel's measurements and
  the postcondition verdict — the "lightweight domain adapter,
  generalized as a node," demonstrated on real geometry.
- **Opt-in weight.** CadQuery ships behind the new `cad` extra
  (heavy binary wheels); the desktop hands pick it up automatically
  wherever it is installed (`OOLU_CAD=off` to refuse), CI installs it
  so the geometry tests run for real, and hosts without it skip the
  hand silently.

Critics: findings with evidence, and teeth (vertical step 4):

- **Findings, not rewrites.** A critic files a finding through
  `POST /v1/graph/{project}/findings`: target, severity
  (blocking/major/minor), the finding in words, a recommended next
  action, and EVIDENCE — required at the door, because a finding
  without evidence is an opinion (400). The finding lands as a graph
  object under `issues/{target path}` THROUGH the kernel, so the owner
  grants critics the issues subtree only — the design itself stays
  closed to them by territory, not etiquette (the spec's own §8 scope
  shape). Findings are revisioned truth like everything else:
  resolved on the record with a reason, listed open-first at
  `GET /v1/graph/{project}/findings`.
- **Blocking findings stop the climb.** The kernel gains an
  advancement gate: an object with an OPEN blocking finding cannot
  move to approved/released — the rejection names the finding and its
  recommended action. Fixing parameters stays allowed (that is HOW
  findings get resolved), minors and majors inform without blocking,
  and a resolved finding unblocks the very next proposal.

The evaluator: actions promise, observation decides (vertical step 3):

- **One predicate language.** `oolu/predicates.py` — a pointer walks
  into observed state, a comparison judges what it finds, and a check
  NEVER raises. The project graph's constraints and the new action
  postconditions are the same code: "the wall an object must honor"
  and "the state a run promised to produce" get one judge.
- **Actions declare promises.** `ActionEvent` gains `postconditions`
  — "result/mass_kg <= 3.5", "interference_count == 0" — checked by
  BOTH route runners against the outcome's observed evidence. A run
  that succeeds by the API but breaks a promise is DEMOTED to a
  failure with every broken promise in words ("succeeded by the API,
  failed by the evaluator"); what was observed rides the evidence
  either way. Only success is judged — a failure is already honest —
  and the demotion feeds the existing failure-evidence path untouched.
- **Observation lands on truth.** The kernel gains an `append` op —
  one entry onto an object's `evidence` or `relations`, base-revision
  honest, never a racing whole-list replace — the door through which
  a verified run's postcondition verdict (or, next, a critic's
  finding) is FILED onto the project graph.

The industrial vertical begins: the Project Graph and its kernel:

- **A second kind of truth.** Alongside runs and files, OoLu now keeps
  a Global Project Graph (`src/oolu/projectgraph/`): typed, revisioned
  objects — parameters, relations, constraints, evidence, provenance —
  each at a declared place in the project tree. Every committed
  revision is kept forever (`graph_history`); every past truth stays
  readable verbatim. Projects are tenant-walled and owned by whoever
  opens them (the same claim pattern as node onboarding).
- **Models propose, the kernel commits.** The transaction kernel is
  the ONLY door through which graph truth changes. Every proposal
  carries a required reason and structured ops (create / set /
  supersede) that declare the base revision AND the exact old value
  they believe they are replacing — stale or misremembered proposals
  are rejected in words, never merged. Territory is granted by the
  owner as path-scoped read/write grants (forbidden wins, fail
  closed — the egress-grant consent shape, applied to truth).
  Previously passed HARD constraints are protected: a patch that would
  regress one is refused; pre-existing violations persist as spoken
  warnings instead of wedging unrelated work; soft constraints only
  warn. Both verdicts land in the hash-chained audit log and the
  proposal ledger.
- **Doors on the gateway.** `POST /v1/graph/{project}/proposals`
  (identity stamped from the session, 409 verdict with reasons on
  rejection), object reads with per-revision history, owner-granted
  scopes, and the owner's proposal ledger. Invisible and nonexistent
  answer alike — a 404 never confirms what the asker may not see.
- **Why.** docs/industrial-vertical-plan.md maps the industrial
  build spec onto OoLu: this is steps 1–2, the spine that upgrades
  OoLu's state from "runs + files" to engineering truth. Next:
  postconditions and observation, critics, and the CadQuery hand.

Hot paths stop scanning, and the whole interface speaks your language:

- **Stats and earnings read through indexes now.** `LiveVersionStats`
  walked every metering event AND every audit record per version
  question, and the desk's earnings join materialized the whole ledger
  per page view — cost that grew with the machine's total history.
  Both now read only what the question touches: the version's bound
  runs from the attribution store's participation index
  (`version_run_ids`), exactly those runs' metering events
  (`events_for_version`) and executed audit records
  (`executed_statuses`), and a key lookup per billing entry
  (`get_by_event_id`). New indexes ride the schema: metering events by
  version and run, bindings by version, and the audit log by run and
  (event_type, run) — durable schema v2 on SQLite and Postgres alike.
  A brute-force reference test proves the indexed answers identical on
  a mixed world (direct events, contract participation, legacy
  bindings, local failures, audit failures).
- **i18n beyond Settings.** The Chat, Work, and Files chrome — the
  first-run card, quick starts, mood line, device asks, run cards and
  their status words, the noder's whole desk (create/onboard forms,
  regime tags, holds, network grants, KYC), the files drawer, tiles,
  and the open file view, the node interact window, and even the
  activity feed's function words (`humanizeEvent`/`statusSentence`) —
  now follows app.language live in all four languages (en/zh/es/fr),
  through the same dictionary Settings already used, with a `tf()`
  helper so templated sentences order their words per language. Quick
  starts translate their labels while keeping the deterministic
  English commands underneath. 208 vitest green, shell rebuilt.

The replay harness: a learned ranker auditions before it may bill:

- **The promised gate exists.** route-finding-proof.md §5 (and
  `TraceProposalModel`'s docstring) said any smarter occupant of the
  `ProposalModel` seat "must beat this baseline in the replay harness
  to earn its inference cost" — a harness that didn't exist.
  `orchestrator/replay.py` is that harness: it replays a trace corpus
  PREQUENTIALLY (test, then train — no model ever predicts from its
  own future), scores every step by Brier with abstentions at the
  neutral coin (0.25), and splits cold (never-seen nodes) from warm —
  so a cold-start win shows up exactly where §5 promised it.
- **The verdict is real, both ways.** On the seeded audition world
  (kin providers mostly work, strangers mostly fail, brand-new
  provider generations arrive with zero history), the shipped
  `LearnedProposalStack` EARNS its seat: cold ~0.20 vs the counting
  baseline's forced 0.25, warm identical (counts outrank inside the
  stack, so verified evidence is never degraded), overall strictly
  better. And the same `earns_its_cost` gate REJECTS a pretender that
  endorses everything — a gate that can only say yes gates nothing.
- **Runnable and in CI.** `python benchmarks/proposal_replay.py`
  prints the audition table and verdict; `tests/test_proposal_replay.py`
  runs the same claims in CI — prequential mechanics, cold/warm split,
  determinism, the stack's pass, the pretender's fail. Any future
  Mamba/SSM or bigger transformer auditions here first.

Two honesty gaps close: durable growth offers, and failure evidence:

- **The question OoLu asked survives the process that asked it.** The
  growth trigger's standing offers ("say yes and I'll build/run it")
  lived in process memory — a restart or a second gateway process lost
  the question between the ask and the answer. They are now one small
  durable row each (`GrowthOfferStore`, on the runtime's own
  connection): keyed to the person asked, newest question wins,
  consumed atomically so a consent is spent exactly once — whichever
  process serves the next message.
- **A node's health can dip from local use, not only climb.** Personal
  runs of a node's own function recorded only successes, so a locally
  broken node kept a spotless record. A run that ends FAILED now
  records a verified failure in the same metering ledger (one event
  per run, terminal phases only — a paused run is not evidence yet;
  the user's abort is what makes it terminal). `LiveVersionStats`
  reads both ways: failed events count as failures, successes are
  filtered by outcome, and the audit-side failure scan is unchanged —
  no double counting, because personal runs carry no run binding.
  A failure never promotes, never silently demotes, and never unlocks
  rating: `verified_run` now requires a SUCCESSFUL run explicitly.
- **Tested end to end.** The offer survives a "restart" (a fresh store
  over the same connection sees and spends it, exactly once); one
  offer per person, tenant-walled; a failing node's second run aborts
  into a verified failure — health reads 1-and-1 — while the account
  stays live and ratings stay locked.

Semantic goal dedup: the twin-node leak closes, reuse offered first:

- **A twin is called a twin.** Node identity used to hang on the exact
  goal sentence, so "normalize invoice csvs" and "normalize invoice csv
  files" minted two nodes with split histories. `naming.goal_similarity`
  now reads two sentences AS GOALS — token overlap over the stopword-
  filtered content words catches rewordings, character trigrams catch
  morphology ('csvs' vs 'csv files') — and at `NEAR_GOAL_SIMILARITY`
  the same work said twice is one goal.
- **Reuse first, always asked.** When a chat task fails for want of a
  function and a node already answers for NEARLY that goal, the growth
  trigger now offers to RUN that node — "the execution lands in its one
  log instead of minting a twin" — never to build. A "yes" routes the
  run through the existing node's own function; a "no" means different
  work, and rolls into the plain build offer with the twin guard stood
  down (the user answered it). Nothing reroutes silently: the guard
  only ever asks.
- **The build door names the near-match.** Building through the
  interact window (or any consented build) refuses a near-identical
  goal in words — which node already answers, built for what sentence,
  and how to proceed: run that goal, or say this one more distinctly.
  `allow_twin` exists solely for the user's explicit "this is
  different work" answer.
- **Tested at both layers.** Similarity thresholds (twins caught,
  different work kept apart, politeness words never split a goal) plus
  the full conversation: paraphrase → reuse offer → yes runs the
  existing node and mints nothing; no → distinct-build offer → yes
  mints the second node; and the build door's refusal + override.

Gated network egress: a node reaches only the hosts its human granted:

- **Consent on the account, not in code.** Every node account carries
  `network_hosts` — the exact public hosts its HTTP actions may reach,
  given and withdrawable by the humans who answer for the node through
  the same account door as status and admin (`POST
  /v1/work/nodes/{id}/account`). Bare hostnames only, at most 8:
  URLs, ports, wildcards, IP literals, and localhost are refused in
  words — the machine's own network is never grantable. Subdomains of
  a granted host are covered, matching the machine allowlist's
  semantics.
- **Stamped at execution, enforced on every hop.** When a contract is
  prepared to run, each REGISTERED child's http actions get the owning
  node's grant stamped on (held contracts are stamped at approval
  time, so a run honors the consent of the moment it is authorized —
  not the moment it was submitted). The host-side HTTP executor — the
  honest enforcement point while the sandbox stays severed — checks
  the grant on the first request and on every redirect: a granted URL
  that bounces toward an ungranted host dies at the bounce, exactly
  like the SSRF guard. An empty grant fails closed — a registered node
  reaches NOTHING until someone consents. Ad-hoc actions a user
  submits directly stay governed by the machine policy alone.
- **A place to say yes.** The Work node thread gains a "Network
  access" desk: the granted hosts listed with a Withdraw button each,
  one input to grant the next — visible only to an onboarded node's
  humans. `docs/THREAT_MODEL.md` gains the "Node network egress"
  section spelling out the wall.
- **Proven at every layer.** Executor tests (granted host + subdomain
  pass, ungranted blocked before the network, empty grant fails
  closed, redirect re-checked), grant validation refusals, stamping
  (registered children marked, ad-hoc spared, compile untouched), and
  the whole wall end to end: contribute an http node, run blocked in
  words, consent through the desk, run green, withdraw, blocked again.

The 1 MB ceiling breaks: blob-backed files, raw in and raw out:

- **Two shapes, one drawer.** Inline files (documents, sheets, small
  images) stay in the database row, person-editable, capped at 1 MB as
  before. BLOB files — the PDFs, decks, videos, and datasets the row
  cap could never hold — keep their bytes in the content-addressed
  artifact store on disk (up to 100 MB each, identical uploads
  deduplicated), with the row carrying only metadata and the
  self-verifying sha256 reference. The database never swallows a video.
- **Raw bytes travel raw.** `POST /v1/files/upload` takes the file as
  the request body — no base64, no JSON envelope — and
  `GET /v1/files/{id}/content` streams it back typed honestly, named
  for the device's save dialog, tenant-walled like every other read.
  Editing a binary's bytes as JSON text is refused in words (rename and
  move still work); deleting the last reference removes the blob from
  disk — no orphans.
- **The app upgrades uploads automatically.** Past the inline cap, the
  Files + menu and the chat's file grant switch to the blob door with
  the FULL original bytes — no downscaling, no refusal — and a
  blob-backed file opens as its honest card (or player) with the bytes
  fetched behind the scenes for viewing and download.
- **Proven end to end, no fakes.** The Chromium smoke now pushes a
  2 MiB binary (every byte value) through the + menu, sees its card,
  and downloads it back byte-identical — device → blob store → device.
  Plus store/route tests: the 2 MiB round-trip with type and
  disposition, dedup-safe deletes, the tenant wall on /content, and
  the refused text-edit. 1130 backend tests and 205 vitest green;
  shell rebuilt; ruff clean.

CI sees everything now — and one real browser walks the real app:

- **The frontend is in CI.** The `ci` workflow gains a frontend job:
  npm ci, the full vitest suite, the shell build (typecheck included),
  and a DRIFT CHECK — the committed shell bundle must match a fresh
  build of the source, so editing frontend code without rebuilding is a
  red build instead of a silently stale app. Until now, none of the 204
  frontend tests ever ran in CI.
- **A real end-to-end smoke, on every push.** The Python job installs
  Playwright + Chromium, which turns ON the browser tests that were
  silently skipping everywhere — and adds a new one: the COMMITTED
  shell served by the real GatewayASGI over the real host runtime,
  driven by a real browser through the desktop's own #auth bootstrap —
  chat answered, a REAL file uploaded from disk through the + menu, its
  words visible on the reading page, and downloaded back byte-for-byte.
  Device → drawer → device, closed loop, no fakes anywhere.
- **The smoke already paid for itself.** First run caught a real bug no
  unit test could see: the Files + menu opened UPWARD (styled for the
  chat composer at the bottom of the screen), sliding under the app
  header where no click could reach it — Upload and New folder were
  unreachable at the top of the page. The menu now opens downward in
  the Files head. Enabling the skipped browser tests also caught a
  stale assertion (a host's Earnings screen shows the empty ledger now
  that the money stack is always wired) — updated to today's truth.
- Entire backend suite green with the browser tests ON (1128 passed);
  204 vitest; shell rebuilt; ruff clean.

The small transformer takes its reserved seat:

- **route-finding-proof.md §5, implemented.** A pure-Python, dependency-
  free small transformer — hashed token embeddings (words + character
  trigrams), ONE cross-attention head pooling a candidate against the
  goal-side query, a logistic scoring head; ~10k parameters — now sits
  in the `ProposalModel` socket, trained online on the trace store's
  recorded outcomes (the exact execution-result signal the proof names).
- **The three wins it was scoped for, proven in tests.** Cold start: a
  brand-new node whose name shares semantics with what worked wins the
  tie for a goal the model never saw. Cross-goal generalization: the
  parameters are shared token embeddings, so every goal's outcomes
  teach every future goal. Context-conditioned choice: two tenants with
  opposite histories get opposite advice from their own rankers.
- **Containment unchanged — that's what makes it safe.** It ships as
  `LearnedProposalStack`: Beta counts outrank it wherever both have an
  opinion (it fills only what counts never saw); an untrained ranker
  answers "no opinion", never noise; propose never raises (a burning
  store downgrades to evidence-only assembly); and the port still
  clamps all advice to DEFAULT_PROPOSAL_STRENGTH pseudo-observations.
  The gateway's per-tenant default proposal model is now this stack.
- Tests: cold-start/cross-goal preference, per-tenant opposite advice,
  the untrained silence and the never-raise contract, and counts
  outranking with the transformer filling only the unseen. Entire
  backend suite green (1101 passed); ruff clean.

The drawer speaks real file types — and files come back OUT:

- **Real types, typed honestly.** The drawer's media map now covers the
  formats developers, creators, and engineers actually exchange: PDF,
  DOCX, XLSX, PPTX, JPG/JPEG, PNG, GIF, MP4, MP3 — files named that way
  (created by OoLu, uploaded, or delivered by a node) carry their true
  MIME type instead of defaulting to markdown, so viewers, players, and
  the download door all know what they are holding.
- **Shown where showable, honest where not.** A PDF renders in place;
  MP4 and MP3 get the app's own players; pictures were already
  pictures. Word/Excel/PowerPoint (and any other binary) get an honest
  card naming their kind and size with a download button — never a page
  of base64 masquerading as a document, and never an "Edit" that would
  corrupt real bytes.
- **Download to the device.** Every file's head now carries a download
  door: the drawer's stored shape (text, or a data URL) turns back into
  the REAL file — true bytes, true type — and lands through the
  device's own save flow. Cloud-side files reach the machine the user
  is sitting at.
- Tiles name their kind (PDF, Word, Excel, PowerPoint, picture, video,
  audio, sheet, document) instead of calling everything a document.
- Tests: the media map (case-proof, old floor intact), the Office card
  with no base64 prose and no Edit, the PDF frame and media players,
  the download door on every file, base64→true-bytes round-trip, and
  the device save flow. 204 vitest and the entire backend suite green
  (1097 passed); shell rebuilt; ruff clean.

The Noder view is a record, not a control panel:

- **The "Run again" button is gone.** A button-made rerun submitted the
  intent as a fresh interaction with a fresh id — a stray duplicate
  outside any conversation, which read as "run again created a new node".
  Life/Noder now offers NO actions: it is the raw record. Re-running is
  OoLu's job, asked in the chat ("run again <its name>" — the hint on
  every log says so): the same task re-fires through its own route and
  node, so every execution keeps accumulating in ONE history.
- Tests: no button, the ask-OoLu hint, and nothing submitted by merely
  viewing the record. 198 vitest and the entire backend suite green
  (1096 passed); shell rebuilt; ruff clean.

The interact window is an operator's desk, not a chatroom:

- **The node's job is stated, and OoLu works it.** The Work interact
  window's model context now opens with the node's ROUTE job: process
  what the previous node (or a user) delivered — incoming activity
  lands as held requests on its desk and as files/messages in its
  drawer — and pass the results onward exactly as the route plans
  (sign/allow moves a held request to the next node by id;
  send_message delivers a result to a Supernode sibling or a friend by
  name). OoLu is told it is the OPERATOR at this desk: prefer DOING —
  open what arrived, edit or produce the result, pass it on, and
  (with consent) build the execution nodes that automate the step —
  over purely chatting.
- **The window's file hands reach the node's OWN drawer.** list, read,
  and write in the interact window used to touch the Life drawer; they
  now operate on THIS node's files — the same drawer where the route's
  deliveries (folder messages/) land — so "open the file, edit it,
  save the result" happens where the work actually is. The Life drawer
  stays untouched from a node's window.
- Tests: the node window listing/reading/writing the node's own drawer
  while the Life drawer stays as it was, and the operator charter
  (job, pass-onward, operator-not-chatbot, own-drawer reach) riding
  the model's context. Entire backend suite green (1096 passed); ruff
  clean.

Uploads that carry the file, folders you can drop into, and a drawer
where OoLu writes the documents:

- **An upload can never again be a hollow file that "passed".** The
  reading path was entirely mock-tested; it is now proven with REAL
  files — a text file's actual words, a typeless .csv, a binary's true
  bytes as a data URL — and hardened: a non-empty file that reads back
  blank is REFUSED in words ("could not read <name> — nothing arrived
  from disk") instead of saved as an empty document, and images still
  upload on webviews without createImageBitmap (shipped as-is within
  the budget instead of failing). The assistant is told the other half:
  a file on THIS device is reachable only through the "file" device
  request — never as an engine task, whose sandbox cannot see the
  device and would only fabricate an empty stand-in.
- **Drag a file onto a folder to move it.** File tiles are draggable;
  folder tiles (and the ".." row) accept the drop — one honest PATCH
  moves the file, with the move named in words. Works in the Life
  drawer and every node's drawer alike.
- **"New document" is gone — documents are OoLu's to write.** In both
  the Life Files page and a node's Files tab, the toolbar is now
  Select plus ONE + menu holding what only a human can do here: Upload
  from device, and New folder. Ask OoLu for the document itself.
- Tests: the real reading path end to end (words, typeless text, binary
  bytes, the no-downscale image fallback, the blank-read refusal),
  drag-to-move landing the PATCH and the notice, uploads landing in the
  current folder and the node's drawer through the + menu, and the
  New-document button's absence in both drawers. 198 vitest and the
  entire backend suite green (1094 passed); shell rebuilt; ruff clean.

The Supernode sees its fleet, the Files tab stops repeating it, and the
device's senses are OoLu's to ask for:

- **A Supernode's activity carries its fleet's executions.** The
  activity feed used to show only the Supernode's own bound runs —
  members' work was invisible. Now every execution touching a member
  node appears in the Supernode's activity, tagged with the executing
  node's name; and verified personal runs (a node's own function,
  Issue 18) count as executions alongside paid marketplace runs — for
  every node, not only Supernodes. One chronological feed across the
  fleet.
- **The member roster shows once.** The Member-nodes list rendered on
  both the Activity and Files tabs of a Supernode; it now lives on
  Activity only — the Files tab is the drawer, not a second directory.
- **The + menu is gone; senses are requested, then granted.** Sharing
  the location, using the camera, or picking a device file is no longer
  a button to remember: the model asks — its reply can carry a device
  request ("location" | "camera" | "file", anything else is dropped) —
  and the chat renders it as grant/decline buttons. Only a grant runs
  the sense (the OS permission prompt appears then, never at startup);
  "Not now" reads nothing and sends nothing; a settled request keeps a
  quiet record instead of live buttons. The model-bound history never
  carries the request bubbles.
- Tests: the fleet feed tagged by executing node and a member's verified
  run in its own feed, the device-request parse (unknown senses
  dropped), grant → sensor → result sent, decline → nothing read or
  sent, refused OS permission → words not a dead button, and the + menu
  gone. 192 vitest and the entire backend suite green (1094 passed);
  shell rebuilt; ruff clean.

OoLu's outbox — messages to friends and nodes, attributed, never
impersonated:

- **OoLu sends messages now, in Life and Work alike.** "send lunch at
  noon? to bob", "message carol: see you at five", or just asking the
  model to let someone know — a new `send_message` hand delivers to
  friends, to the user's own nodes, and (from a node's interact window)
  to the nodes under the SAME Supernode: the org's members, not
  strangers.
- **The best compatible destination, never a guess.** The user names
  the target in their own words; resolution goes exact name → substring
  → every-word, and ties break on the user's own HABITS — who they
  actually talk to (conversation recency). Only a clear winner is
  chosen; equals make OoLu ask. A host is still never a directory: the
  candidate list is the people you talk to and your own nodes, but an
  EXACT username you already know always resolves.
- **The backend delivers to exact ids.** A friend gets a real server
  message through the same store the Friends surface uses (tenant
  walls, enabled-account and not-yourself checks intact); a node gets a
  document in its OWN drawer (folder `messages`), the same drawer the
  Files surface shows.
- **Every delivery says who sent it.** The message arrives marked
  "↪ forwarded via OoLu from <user>" — presented as carried by OoLu,
  showing WHO forwarded it, never as words the recipient's own side
  typed. OoLu carries; it does not impersonate.
- Tests: resolution (exact beats habit, habits break substring ties,
  equals stay ambiguous, exact-lookup fallback), marked deliveries to a
  friend and into a node's drawer, unreachable/disabled/self refusals,
  the deterministic "send … to …" command (a message containing "to"
  lands whole), fall-through to work when nothing matches, the model's
  tool path, and Supernode siblings reachable from the interact window.
  Entire backend suite green (1092 passed); ruff clean.

Polish that was overdue: silent emoji, settings that speak your
language, a console that comes back, and replies sized to yours:

- **Emoji are for the eye, never the ear.** Speak-replies-aloud no
  longer pronounces emoji ("rocket", "party popper"): pictographs and
  their plumbing (skin tones, flags, keycaps, ZWJ families) are
  stripped before the utterance — every real word and its punctuation
  stays, and an all-emoji reply is spoken as silence, not described.
- **The settings words follow the language.** Changing Language used to
  translate only the group headers; every setting's label and
  description stayed English (they come from the backend catalog). A
  translation dictionary for the whole catalog (zh/es/fr) now covers
  item labels, descriptions, accessible names, choice values (Fast /
  Reasoning / Own API key / …), units, the section notes, the manage-
  plan row, and the privacy rows — with the server's own English as the
  honest fallback, so a new knob is never blocked on the dictionary.
- **The account console links back.** The plan page now carries a
  "← Back to OoLu" link, says changes show up the moment you return —
  and the shell actually makes that true: Settings re-reads the catalog
  on window focus, so a plan changed in the console tab is visible the
  instant you're back.
- **Replies mirror your length.** The assistant's prompt now instructs:
  a short message earns a short reply — about as long as what you
  wrote — running longer only when the substance truly needs it. Never
  padding.
- Tests: emoji stripping (words and punctuation intact, all-emoji =
  silence, the engine receives the cleaned text), the catalog rows
  translating live with the unknown-knob fallback, and the accessible
  names following the visible words. 191 vitest green; shell rebuilt;
  entire backend suite green (1082 passed); ruff clean.

One reminder, not a storm — and the return earns the next one:

- **The reminder posts once per idle stretch.** The chat's pending-work
  reminder used to repeat every five minutes for as long as the user was
  away — a nag storm into an empty room. Now the one bubble posts after
  two idle minutes and, while it sits at the bottom of the thread
  unanswered, it is never repeated: saying it again adds nothing.
- **Fifteen minutes of absence puts the loop to sleep.** Past the
  dormancy line the user is not "about to look", so the reminder loop
  pauses entirely instead of reminding nobody — even when work turns
  pending while they're gone.
- **Coming back earns one fresh look.** The user's next message after a
  long absence is the event that surfaces the open work again: the reply
  lands first, then a single welcome-back reminder lists what still
  waits on them and what is still running — with the same jump-arrows
  straight back to each task's action window.
- Tests: one reminder per stretch (five minutes on, still one), dormancy
  past the line (nothing posted into the empty room), a new message
  opening a fresh stretch, and the welcome-back reminder following the
  reply exactly once. 191 vitest green; shell rebuilt; entire backend
  suite green (1082 passed).

Substance over names, and verification that actually happens:

- **Search reads what the node DOES.** Contributing a node now derives
  capability tokens from the function's own code — parsed, not guessed:
  imported modules, defined functions, called names, adapter/operation
  words, and the slot vocabulary — and stores them on the listing AND in
  the search index. Discovery matches them alongside the title, so a
  node is findable by `normalize_rows` or `csv` even when the author
  named it something else — and a flattering name over an empty shell
  adds nothing to the index. The node's semantic VALUE keeps accruing
  the way it always did: verified runs, which rank above any wording.
- **A name is not a capability.** A node with no executable function
  inside is never a candidate — the assembler skips it for routes,
  ranking, AND paid bindings even if its listing somehow went active —
  and it cannot be published at all: publish now refuses an actionless
  version in words.
- **The verification dead-end is fixed.** Verified stats only ever came
  from marketplace bindings, which personal runs never create — so every
  built node sat at needs-verification forever, unpublishable and
  unranked. Now a COMPLETED run through the node's own function IS a
  verified run: the gateway records it in the metering ledger
  (idempotent per run, keyed to the node's version) and promotes the
  account needs_verification → live — one honest transition; error and
  restricted states are never healed by a passing run. The event carries
  NO consumer principal, so a self-run never unlocks rating your own
  node. Runs that complete after a resume (the human confirming
  model-written code) verify too.
- **Publish is gated on proof.** A listing reaches the global nodeplace
  only after at least one verified run — a local, sandboxed run through
  the node's own function counts; that IS the safe test environment —
  so nothing the online community can find is running on reputation it
  never earned. The growth trigger's reply now closes the loop out
  loud: "That run also VERIFIED the node — it is live now."
- Tests: capabilities derived from code (never the name), discovery by
  the function's own words, the empty-active listing excluded from
  candidacy and paid binding, publish refusals (no function / no
  verified run) and the door opening once proof exists, the full
  build → run → verified → live → publishable loop at the gateway, and
  mark_verified touching only needs_verification. Entire backend suite
  green (1082 passed).

A failure that asks, instead of a wall that repeats — and a model that
knows it can search:

- **The growth trigger (borrowed from n8n's editor).** When a workflow
  is missing the node it needs, the answer is a proposal to ADD that
  node — never the same "I can't do that" again. A chat task the engine
  cannot execute (or that fails at a node) now ends with a consent
  question in the conversation itself: "want me to build a node for
  '<goal>' and run it?". The user's plain "yes" on the very next message
  IS the consent — scoped to that one goal, one build, no trip to
  Settings — and it builds the node through the SAME gated path as the
  interact window's build (executable-work judgement, the
  actually-written function, the contribute screen), then re-fires the
  task through the node's own function. A "no" — or any other message —
  withdraws the offer: consent detached from the question it answered is
  not consent. No model to write the function means no offer (the old
  Settings hint stays as the fallback), and model-written code still
  re-earns the human's confirmation before it runs. The global
  "Auto-build nodes on my paths" switch is unchanged for people who want
  no question at all.
- **The model now KNOWS it can search.** A keyed install carried the
  provider's server-side web-search tool since Issue 13 — but the chat
  prompt never said so, so the model answered "I can't browse the
  internet", or worse, handed the search to the engine, whose
  network-severed sandbox can only fail it (the "OoLu can't even do a
  basic web search" symptom). The router now reports whether the path
  that will answer really carries the tool (`web_search_ready`: the
  setting is on AND an Anthropic path answers — a local model never
  searches), and the chat turn injects a context note telling the model
  to answer current-facts questions directly in words and never make a
  web search a task.
- One shared node-building core behind both doors: the interact window's
  `build` and the growth trigger now call the same
  `_build_function_node` — one goal one node, the declared IO interface,
  the supernode placement, and every refusal in words, identical
  everywhere.
- Tests: the narrow consent matcher, a stuck task ending in an offer,
  "yes" building the node and re-firing the task through its own
  function on the script hand, "no" and subject-changes withdrawing the
  offer, no offer without a model, offers keyed to the person asked,
  `web_search_ready` across keyed/closed/local paths, and the web note
  reaching the model's context. Entire backend suite green (1075
  passed).

Developers bring their own functions — and the gate holds:

- **Upload a function when creating a node.** The Create-a-node form now
  takes a Python function (upload a .py or paste it): the node is born
  a script node carrying it, the way a developer prefers to work. Left
  empty, the node still starts as a draft. The gatekeeping is identical
  to OoLu's own functions, because it is the SAME path.
- **An antivirus screen at every gate.** A new `screen_script` refuses
  obviously hostile code — reverse shells, decode-into-exec, raw
  sockets, credential reach, container-boundary probes, miner markers —
  and names the reason. It runs at contribute (a hostile upload is
  refused BEFORE it is ever stored) AND inside the script runner (no
  script — uploaded, synthesized, repaired, or replayed — reaches the
  backend without passing it). This is a screen, not the wall: the
  sandbox (docker isolation on a public host, network-severed execution
  on the desktop) and verify-by-execution remain the real walls behind
  it, and a public host still keeps the script hand off entirely without
  real isolation (`require_isolation`).
- **Build never edits the node you're in.** Confirmed and made explicit:
  the interact-window build command always creates a SEPARATE new node
  that expands the current node's path — it never changes the existing
  node's code (a public-safety rule) — and, once proven, the two can be
  merged into one throughout solution. The build reply and the model's
  context both say so.
- Tests: the screen's pass/refuse verdicts, a clean upload contributing
  and a hostile one refused before storage, the runner refusing a
  hostile script without running it, and interact-build leaving the
  current node byte-for-byte unchanged. 185 vitest and the entire
  backend suite green; shell rebuilt.

One node per goal, its own function on every run, a model that repairs
its own code, and a declared interface:

- **Rebuilding a goal reuses its node.** The built node's skill id now
  derives from the goal itself, so "build" for a sentence that already
  has a node finds it instead of minting a twin (and spends no model
  call saying so). Every execution accumulates in ONE node's log.
- **A re-run executes the node's OWN function.** A run whose goal the
  user built a node for no longer re-plans onto whatever hand the
  generic planner finds (the "workflow.executed always fetches a URL"
  symptom): the gateway attaches the node's stored function to the
  contract and the orchestrator routes it straight through the script
  hand (`origin: node_function`). Stored model-written code still
  re-earns the human's confirmation before it runs.
- **The model edits the failing function.** When a node's script fails,
  the runner now hands the model the goal, the CURRENT code, and the
  exact failure, and asks for an edit — not a rewrite. Each edit is
  verified by execution before it is trusted; the loop is bounded (two
  rounds); a verified repair becomes the node's cached function, so
  every later run executes the healed code. Beyond the bound, the
  failure is honest: "repair could not close the gap".
- **Nodes declare what they consume and produce.** The function-writing
  prompt now requires an `IO:` line (inputs/outputs with str/path/number
  types); it lands as the skill's parameters and the listing's
  consumes/produces slots — the exact vocabulary the route assembler
  chains on — and the build reply spells the interface out. A missing
  declaration degrades to the honest default (no inputs, one string
  result).
- Tests: node reuse with a single model spend, the IO parse and its
  degradation, the interface on the listing, goal→function resolution
  (spacing/case-proof), the forced node-function route executing through
  the script hand with the generic hand never firing, and the repair
  loop's edit-verify-cache cycle plus its bound. Entire backend suite
  green (frontend untouched).

Files that arrive from the device, and act as a group:

- **Upload from the device.** The Files drawer's new Upload button opens
  the native picker (multi-select, same on phone/tablet/computer) and
  lands the picked files in the OPEN folder: text stays text, images are
  downscaled to fit the drawer's 1 MB budget, other binaries ride as
  data URLs — and anything too large is refused in words naming the
  file, next to the count of what did land. "New document" still exists
  for starting from nothing.
- **Select many, act once.** A Select mode turns the tiles into
  checkboxes; the action bar forwards the whole selection to one picked
  destination — a node's drawer, the Life drawer, or a friend (a real
  server delivery carrying the file) — or deletes it in one two-tap
  move (first tap arms, "Really delete N?" fires) — never a silent mass
  delete.
- Tests: the upload write with its media type, the refused-file notice,
  the two-tap bulk delete, and a two-file forward to one node. 184
  vitest and the entire backend suite green; shell rebuilt.

The web through the model's own hands, and the desktop's own disk:

- **Model web search.** The Anthropic adapter can now carry the
  provider's server-side web-search tool (`web_search_20250305`, max 3
  uses per turn): the search runs INSIDE the API call on Anthropic's
  servers, so any keyed OoLu — an own key on Edge, or the Global
  subscription brain — answers current-facts questions from any
  install, with no web access needed on the machine itself. The new
  `model.web_search` setting (default on) closes the door; a local
  model never searches (local means local), and a keyless install stays
  deterministic by design — that part was a feature, not a bug.
- **The desktop finds its own files.** The chat's new
  `find_local_files` tool searches the user's computer by name or glob
  — home-rooted, listing only (path + size, never content), bounded
  (hidden and bulky tool directories skipped, scan capped, 40 matches
  max). ONLY `oolu desktop` wires it; `oolu host` never does — a server
  has no business in anyone's home directory, and the tool says so in
  words when asked there.
- Tests: the web-search tool riding the Anthropic request (and the
  setting removing it), the catalog knob, bounded home-rooted disk
  search with hidden-directory privacy, the host wall, and the chat
  tool answering on desktop / refusing on hosts. 180 vitest and the
  entire backend suite green.

The device's senses on demand, and reminders that point back:

- **Microphone, camera, location — asked for exactly when needed.** A
  new ＋ button on the chat composer opens the device door: "Share my
  location" reads the device's position (the browser/app permission
  prompt appears at that tap, never at startup) and sends it into the
  conversation; "Take a photo" opens the native camera on a phone or
  tablet (file picker on a computer), downscales the shot to fit the
  drawer, saves it to Files (folder: camera), and tells OoLu. A refused
  permission lands as honest words in the thread. The microphone was
  already live (hold Send to talk). Images in Files now display as
  pictures, read-only, instead of opening in the text editor.
- **The reminder's arrow.** When the idle reminder lists ongoing or
  snagged tasks, each task now carries an arrow (↦ task name) pointing
  straight back to its ACTION window: the click scrolls to the task's
  run card if it is in the thread — flashing it — or brings the card
  into the conversation, Retry buttons and all.
- Tests: location success/refusal/absence, shot naming, the composer
  device menu sharing coordinates and surfacing refusals, and the
  reminder arrow summoning the live run card. 180 vitest and the entire
  backend suite green; shell rebuilt.

The interact window becomes what it is — a conversation:

- **Nothing but the thread and the composer.** The interact tab's
  button row, task chips, and the "Automation reliability…" banner are
  gone; the conversation now takes every pixel the tab has (the thread
  stretches to fill). One hint line inside the EMPTY thread teaches the
  typed commands — “pending”, “sign <task id> as <your name>”, “reply”,
  “build” — and disappears with the first message. All commands still
  answer deterministically.
- **The stewardship blocks step aside while you talk.** With the
  Interact tab open, the KYC block, the member-node fleet, and the
  Pending desk fold away (they live on the other tabs as before), so
  the conversation window is large and clean. The reliability line
  moved to the Activity tab, where telemetry belongs.
- Tests: the clean-window assertions (no buttons, no banner, hint
  present), typed commands still driving the desk with task ids in the
  listing. 173 vitest and the entire backend suite green; shell rebuilt.

The retry that wouldn't press, the button acceleration never needed,
and a desk that hands you the task id:

- **Retry presses now.** The run card's 2.5-second poll rebuilt the DOM
  under the user's finger on every tick (a new task object every poll),
  so a click could land on a button that no longer existed — and a
  refused decision vanished into an unhandled rejection. The poll now
  re-renders only on REAL change, the decision buttons disable and
  relabel while the call is out ("Retrying…"), a refusal lands in the
  card as words, and the incident card counts the retries ("2 retries so
  far — the next retry lets OoLu plan and rebuild the path").
- **Acceleration is automatic, not a button.** Whatever can move on a
  node's path already moved; the interact window now surfaces exactly
  the work that waits on a human, by itself: each waiting task appears
  as a clickable chip (name + task id) the moment the window opens.
  Typing "accelerate" still answers honestly.
- **Pending · Sign · Build, one row.** The interact quick actions are
  now three: "Pending" lists what waits (each line carries the task id),
  "Sign" pre-fills `sign <task id> as ` — the id auto-appends when
  exactly one task waits, or comes from tapping a task chip / the
  pending list — and signing passes the task to the next node; "Build"
  pre-fills `build `. The thread's held-request heading is "Pending".
  The assistant's pending reply teaches the same commands.
- Tests: the incident Retry's press feedback, decision post, retry
  count, and surfaced refusal; the one-row quick actions; Sign's id
  append (single task) and open-endedness (several); the task chips'
  click-to-fill. 174 vitest and the entire backend suite green; shell
  rebuilt.

The settings that lied, and the forward menu that wouldn't behave:

- **The theme actually changes.** The whole stylesheet now reads
  variables (the status chips included) and carries a complete light
  palette: choosing "light"/"dark" pins `data-theme` on the root,
  "system" removes the pin so the OS preference decides, and the choice
  is cached so the right look paints before settings load. Saving the
  setting applies it the same instant.
- **Languages by their formal names, and a UI that follows.** The
  language dropdown shows English / 中文（简体） / Español / Français —
  never raw codes (theme values get words too; stored values stay
  stable codes). A new chrome dictionary (`ui.ts`) translates the
  navigation, labels, placeholders, and buttons live when
  `app.language` changes — Life/Work tabs, the conversation list, the
  chat composer, Settings headings, the forward menu. The assistant's
  own words follow the model, not this table; per-setting labels still
  come from the settings node.
- **The forward menu behaves like a menu.** A click anywhere else — or
  Escape — closes it (it used to stay open until cancelled), and a
  search box narrows long friend/node lists as you type, with the
  save-to-file escape hatch always in reach.
- Tests: theme pin/unpin + persistence, the language dictionary and
  its change notifications, formal choice labels, the Settings
  instant-apply and live chrome switch, and the forward menu's search /
  outside-click / Escape behaviors. 170 vitest and the entire backend
  suite green; shell rebuilt.

Phase 4 of going public — ship and operate: the data-subject's rights,
the legal surface, backups, the operator's numbers, and releases:

- **Export and erasure, self-serve.** `GET /v1/account/export` returns
  everything the host holds about the caller as one JSON document —
  account, identity links, settings, the OoLu thread, friend messages,
  Life-drawer files, runs, model usage, earnings, payment metadata.
  `POST /v1/account/delete` demands the password (a stolen session must
  not destroy an account), erases the per-person stores (messages both
  sides — the store keeps one shared copy — the assistant thread,
  identity links, verification records, card metadata with provider-
  side detach), disables the account forever (the username is never
  reissued — a freed name would let a stranger inherit its trust),
  appends an `account.erased` audit record, and answers with exactly
  what was and was not removed. Settings grows a "Privacy & data"
  section: Download my data, Delete my account (password-confirmed),
  and the legal links.
- **The legal surface.** Three public, stable URLs: `/v1/legal/terms`
  and `/v1/legal/privacy` serve the operator's `<data_dir>/legal/*.md`
  verbatim when present, and until then built-in templates headed by an
  unmissable "TEMPLATE — NOT LEGAL ADVICE" notice; `/v1/legal/
  node-policy` serves the code-owned, hygiene-enforced Node Policy.
- **`oolu backup`.** One command, one timestamped folder with everything
  a restore needs: every SQLite database through the ONLINE backup API
  (safe against a live server mid-write) plus the keyring's
  `machine.key` — without which every stored model key is unreadable.
  Says when the durable store is PostgreSQL and pg_dump owns that half.
- **The operator's numbers.** `/v1/metrics` is now permission-gated
  (`metrics:read` — grant a monitoring role that can read nothing else)
  and carries `uptime_seconds`, so a prober can spot crash-loops.
- **Releases and the runbook.** A pushed `v*` tag now publishes a
  GitHub Release carrying every platform's smoke-tested shell binary
  (new `release` job in build-installers.yml). `docs/operations.md` is
  the ops runbook: backup schedule + restore drill, monitoring and what
  to alert on, ship order, staging, retention, the legal files, the
  rights routes, and the 3 a.m. incident list. `scripts/load_test.py`
  measures the run pipeline (req/s, p50/p95) against a host you own.
- Tests: export completeness, password-gated erasure with store-level
  verification and the audit record, template-vs-operator legal
  documents, the metrics permission wall with uptime, and live-database
  backup round-trips in `test_account_privacy.py`; the Settings privacy
  flows (download, delete with wrong-password refusal) in vitest. 159
  vitest and the entire backend suite green; shell rebuilt.

Phase 3 of going public: people talking to people, one conversation
across devices, and a first minute that lands:

- **Friends for real.** Person-to-person messages between accounts on
  the same host, in a new durable store (`DirectMessageStore`): ordered
  threads, read state (opening a thread reads it), unread counts on the
  peer list. Discovery is EXACT — `POST /v1/friends/lookup` resolves a
  full username or e-mail (through the identity links) and nothing else;
  there is no directory to browse, so on a public host nobody is
  findable unless they shared their name. The peer must be a real,
  enabled account in the caller's own tenant. The Life screen's Friends
  group goes live: conversations with unread badges, a start-a-
  conversation pane, a per-person thread with the same composer as the
  OoLu chat — and the forward menu now offers friends as destinations
  (a real server delivery, marked with where it came from, never a
  local-storage append). Hosts without a server keep the honest
  placeholder.
- **One conversation across devices.** The OoLu thread now lives server-
  side per account (`AssistantHistoryStore`, capped at 500 turns like a
  messenger): `/v1/chat` records each user turn, assistant reply, and
  run marker, and `GET /v1/chat/history` is what a fresh device loads —
  the desktop, the browser, and the phone show the SAME thread. The
  local cache stays as the offline story and hosts that keep no history
  (404) keep working exactly as before. Idle-reminder bubbles remain
  client-side by design — presence, not conversation. The node-interact
  window stays its own context and is not recorded into the main thread.
- **A first minute that lands.** A one-time first-run guide inside the
  chat's welcome state: say hi (one tap), try a first task (drops a
  ready-to-send task into the box — nothing fires unseen), and where to
  add a model key. Used once or dismissed, it never returns.
- Tests: the store and every wall in `test_friends.py` (order + read
  state, tenant scoping, exact-lookup-only discovery, disabled accounts
  stop receiving, 404 on storeless hosts, chat turns landing per
  account, the messenger cap), plus the Life friends list/thread/start
  flows, Chat's server-history sync and cache fallback, the first-run
  guide's once-only walk, and friend forwarding in vitest. 156 vitest
  and the entire backend suite green; shell rebuilt.

Phase 2 of going public: the subscription brain becomes real, the money
stack wakes up behind honest walls, and KYC reviews get an inbox:

- **The hosted subscription brain.** `model.source="subscription"` now
  has something behind it: the host operator sets
  `OOLU_PLATFORM_ANTHROPIC_KEY` / `OOLU_PLATFORM_OPENAI_KEY` and tenants
  on that source are answered through the PLATFORM's keys (Claude first,
  the plan's order) — no pasted key needed. Every consultation lands in
  durable per-tenant monthly books (`ModelUsageStore`), and the plan's
  allowance gates it: free includes none (the refusal names the paid
  plans, own keys, and local models as the ways out), plus/pro/
  enterprise include $5/$20/$100 a month, and a spent allowance says
  when it renews. Platform keys follow the environment on every boot
  (set → stored encrypted under a reserved keyring tenant, unset →
  removed). New `GET /v1/usage/model` shows a tenant their books and
  remaining allowance. Hosts without platform keys keep the honest
  "isn't live yet" message.
- **The money stack is wired.** `build_host_runtime` now constructs the
  earnings ledger, payout store, dispute service, and payment adapters
  it previously left dormant — so `/v1/earnings`, `/v1/payout-accounts`,
  and `/v1/disputes/{event}` answer on every host. With `OOLU_STRIPE_KEY`
  the card vault and payout adapter are the real Stripe ones (card
  numbers never transit our servers — SetupIntent only); without it the
  test doubles stay. New `POST /v1/webhooks/stripe` verifies Stripe's
  `Stripe-Signature` over the exact raw payload and matches events back
  to our books through the `oolu_event_id`/`oolu_batch_id` metadata the
  adapters now attach to charges and transfers — refunds and disputes
  claw back the right event, payout confirmations settle the right
  batch, and replays are idempotent by event id.
- **The transaction port has a key, and it refuses test doubles.**
  `oolu host --transactions` opens the launch guard's operator gate —
  and refuses to start without `OOLU_STRIPE_KEY`, so the port never
  opens onto fakes. Even open, each class of work still charges only
  after its prices settle and its function has verified successes, and
  `require_production_money` still demands PostgreSQL + production
  identity. The subscription console's `charging_open` now tells this
  truth instead of a hard-coded `false`.
- **The KYC reviewer inbox.** Reviewers (the `kyc:review` permission —
  the bootstrap admin's `*` covers it) get `GET /v1/kyc/reviews`:
  pending applications, fast-tracked first, oldest first. The Work
  screen shows the queue with Approve/Reject right on the row (the
  existing decide route, authority-checked and audited); a verdict
  clears the row. Everyone else gets a 403 and sees no inbox at all.
- Tests: the brain's whole ladder in `test_subscription_brain.py`
  (platform key answers, free-plan refusal, spent-allowance renewal
  message, Claude-first fallback, own-api isolation, monthly book
  rollover, the usage surface), the money half in `test_stripe_money.py`
  (Stripe-Signature round trip and refusals, webhook→books matching,
  idempotent replays, adapter wire shapes, assembly's test-vs-live
  choice, the `--transactions` wall, and one full charge → accrue →
  settle → confirm → claw-back cycle on the fake processor), and the
  inbox in `test_kyc_inbox.py` + `Work.test.tsx`. 150 vitest and the
  entire backend suite green; shell rebuilt; the going-online runbook
  documents the new environment variables and flags.

Phase 1 of going public: proven e-mail addresses, a way back in, and
walls a public host cannot serve without:

- **E-mail verification on registration.** A host with a mail sender
  configured no longer hands out a session at `POST /v1/auth/register`:
  the account is created, a 6-digit code is mailed (`MailCodeStore` —
  hashed at rest, 30-minute expiry, 5 attempts, strictly single-use),
  and the answer is `{"verification_required": true}` with no token.
  `POST /v1/auth/verify` takes e-mail + code + password and mints the
  first session — the code alone is never a session, so a leaked inbox
  is not a leaked account. Sign-in answers 403 `verification_required`
  for registered-but-unproven addresses (bootstrap/operator accounts,
  which never registered an e-mail, are exempt). The sign-in screen
  grows the matching code-entry step, and `/v1/client-config` advertises
  `verification` so clients know the step is coming.
- **Password reset.** `POST /v1/auth/reset/request` always answers 202
  ("sent") whether or not the address exists — nothing enumerates
  accounts — and mails a reset code to real ones. `POST
  /v1/auth/reset/confirm` (e-mail + code + new password) changes the
  password and counts as address verification, since inbox control was
  just proven. The sign-in screen gets the matching "Forgot password?"
  flow.
- **The outbound door.** `oolu.mail`: `HttpMailSender` speaks the
  Resend-style JSON API (`OOLU_MAIL_URL` + `OOLU_MAIL_KEY` +
  `OOLU_MAIL_FROM`), `OOLU_MAIL=console` logs mail for development, and
  an unconfigured host keeps the old immediate-token registration (for
  private/testing installs that opted in knowingly).
- **`--global-service` walls.** A public host refuses to start with
  `--open-registration` and no mail sender (strangers must prove their
  address), and never wires the script hand unless the backend is real
  isolation (docker) — synthesized code does not run unsandboxed on a
  public host (`require_isolation` in `build_host_runtime`).
- **An honest "subscription" dead-end message.** With `model.source`
  still "subscription" and no keys, the router now says the hosted OoLu
  brain isn't live yet and points at own-api keys or a local model,
  instead of the generic "no model key is configured".
- Tests: the whole flow in `test_mail_verification.py` (verification-
  first registration, 403-before-verify, wrong/burned/expired codes,
  no-enumeration reset, reset-counts-as-verification, the code store's
  clock/attempt behaviors, the Resend wire shape, both public-host
  walls) plus the Login code-step and forgot-password flows and the new
  api adapters in vitest. 148 vitest and the entire backend suite
  green; shell rebuilt.

The BYO key actually takes over, and OoLu talks like it means it:

- **An added model key becomes THE model — and proves it.** The root of
  "I set my OpenAI key, it's billed, but nothing works": the default
  `model.source` is "subscription" (the OoLu plan's hosted brain, which
  a self-hosted/desktop install does not have), so a key added while
  still on that default was only ever a silent fallback behind a
  provider that will never answer. Now `POST /v1/keys/model` flips
  `model.source` subscription→own-api and points `model.provider` at the
  key just added (a deliberate "local" choice is left alone), so the key
  the user pasted is the model the user gets. New `POST
  /v1/keys/model/test` makes one real call through the live router and
  reports the model that answered — or the exact reason it could not —
  turning "billed but is it working?" into a definitive yes/no; the
  Settings "Add" now auto-tests and a "Test connection" button re-checks
  any time.
- **Energetic, mood-aware voice and tone.** OoLu's persona is rewritten
  upbeat and lively (the system prompt, the greetings, the acks, the
  presence lines), and it now speaks in its current MOOD: the chat turn
  carries the avatar's mood so the model's words match its face
  (`mood_directive`), and speech synthesis varies rate and pitch by mood
  (`toneForMood` — brighter and quicker when excited, steady when
  worried; the default is livelier than the old flat 1.05). The system
  prompt is also more conservative about turning chat into work — when
  in doubt it TALKS and offers, instead of silently kicking off a task
  that fails on a fresh machine.
- Tests: the source-switch + `/keys/model/test` pass/fail
  (`test_gateway_model_keys.py`), the Settings add-then-auto-test and
  Test-connection button (`SettingsPane.test.tsx`), the mood-driven
  speech tone (`voice.test.ts`), and updated Chat presence lines.
  Verified live through `build_host_runtime`: add key → source flips to
  own-api → test route answers → a chat turn uses the model with mood
  threaded. 141 vitest and the entire backend suite green; shell
  rebuilt.

Forwarding without friction, real hands on the local device, and the
creative-app lesson learned from the source file:

- **Forward messages and files anywhere.** Every chat bubble (the OoLu
  conversation and a node's interact window) carries a hover ↪: pick a
  destination — OoLu, any node on your desk, or "New file in Files" —
  and the message lands in that thread's history marked "↪ forwarded
  from <who>" (or becomes a document under the Life drawer's
  `forwarded/` folder). Files forward too: FileView's "forward" copies
  the file into the picked drawer's `forwarded/` folder — a COPY, so
  originals never move. `forward.ts` owns the logic; `ForwardMenu` is
  the picker.
- **The execution-access review, answered honestly, then fixed.** The
  desktop wired ONLY the GET-only HTTP hand: OoLu could not command the
  local device's CLI at all (the CLI executor existed but nothing
  passed it in); scripts ran only through the script-node path added by
  the retry work. Now `wfgps desktop` gives the engine
  `build_desktop_hands`: HTTP + the LOCAL DEVICE's command line — the
  discovered tools (ffmpeg, pandoc, …), workspace-confined under the
  data directory, on by default (commanding this machine is what the
  desktop engine is for), `OOLU_CLI_TOOLS=off` to disable and
  `OOLU_CLI_ALLOWLIST` to widen.
- **Creative apps: the source file is the lesson.** New
  `skills/creative.py`: a registry of creative applications (Photoshop,
  Illustrator, GIMP, SolidWorks, Fusion, AutoCAD, Blender, Figma,
  Premiere, After Effects) with their source extensions;
  `plan_creative_capture` sorts a session's artifacts with SOURCE FILES
  FIRST (.psd/.sldprt/.blend — the model-training payload) and the
  screenshot/mouse/keyboard trace as ADVISORY path context
  (`replayable` is a constant False — no flag can promote a pixel trace
  into execution). The learner refuses to compile a creative-app
  demonstration into a replayable skill (`creative_source_needed`, with
  the reason in words: the trace explains the user's path but "will
  never execute the work reliably"); ordinary applications learn
  exactly as before.
- Tests: `forward.test.ts` (marked thread insertion, message→file,
  file copy with drawer/folder, target list) and
  `tests/test_creative_learning.py` (app recognition, capture
  priority, the learner's refusal, desktop hands incl. CLI + the off
  switch). 139 vitest and the entire backend suite green; shell
  rebuilt.

A node IS its function; the record is a file; the feed reads like words:

- **No more empty nodes.** Building a node through OoLu now takes two
  verified gates in ONE model consultation (`author_node_function` +
  `NODE_FUNCTION_PROMPT`): first the sentence must be judged EXECUTABLE
  WORK — a greeting, a question, or conversation answers `NO_TASK` and
  nothing is created (`obviously_chat` refuses the obvious cases before
  any model) — then the model must actually WRITE the node's execution
  function. The published node carries it as its own script action
  (`adapter="script"`, the verified-before-trusted runtime from the
  retry work), never a placeholder draft; no model, no code, no node —
  "an empty node is unnecessary." Contract runs now receive the same
  executor set as the orchestrator (script hand included), so a node's
  own function executes and routes locally instead of falling back to
  the global machinery. The Work UI's manual create form is unchanged —
  a human's deliberate draft stays a human's choice.
- **Daily execution logs, kept as files for legal use.** Every activity
  fetch materializes the node's daily log in its own Files drawer
  (`logs/execution-YYYY-MM-DD.log`): full fidelity — ISO timestamps,
  run ids, executing node, raw event types — merged idempotently so
  nothing duplicates, and pruned after the new
  `account.log_retention_days` setting (default 180 days, 7–3650; set
  it to your legal record-keeping requirement).
- **The Supernode's feed reads like words.** A Supernode's activity now
  aggregates its members' executions, and every item names the node
  that EXECUTED it. The display simplifies for humans: the executing
  node's NAME instead of a run id, the clock down to the second
  (10:00:02, not an ISO blob), and plan/status words instead of
  function calls ("Carried out the actions", never
  `workflow.executed`) — with the full detail one tooltip away and in
  the log files.
- Tests: the four creation gates + the function riding the published
  version (`test_node_interact.py`), log materialization/idempotence/
  retention pruning and the member-named Supernode feed
  (`test_execution_logs.py`), and the humanized feed
  (`Work.test.tsx`). 135 vitest and the entire backend suite green;
  shell rebuilt.

The node's interaction window — OoLu called out to act ON a node:

- **An Interact tab beside Activity and Files** on every Work node
  thread: a node-scoped conversation with OoLu (`POST /v1/chat` +
  `node_id`, tenant-guarded to the caller's desk). Quick actions:
  Pending requests, Accelerate, Sign all…, Build a node…
- **OoLu's hands on the node's desk** (`NodeChatTools`): list its held
  requests, allow/reject them, SIGN them — single or "sign all as
  <name>", the fast manual floor of final-result audit signing — and
  reply to requesters. Every hand goes through the gateway's own
  handlers, so tenant scope, approve authority (a submitter still can't
  approve their own ask), the budget re-check, and the audit trail
  apply unchanged. Deterministic commands work with no model
  (pending / accelerate / allow / reject / sign … as … / reply …: … /
  build …); a configured model gets the same tools plus a node-context
  system note and is told never to decide a hold unasked.
- **Building on the node's path**: "build <goal>" (consent-gated on
  'Auto-build nodes on my paths') contributes a keyword-named draft
  node to the registry — a citizen the planner can find and route to,
  becoming callable as its runs verify — created UNDER the node when
  it is a Supernode (unclaimed: the node id is the claim ticket) or
  standalone on the caller's desk otherwise.
- **The automation vision, made visible and honest**: the Interact tab
  leads with the node's automation reliability ("99.2% over 133
  verified runs — every verified run takes this node closer to
  hands-off"), computed from platform-verified health. And when a
  node's automation FAILS, the failure now carries a stable error
  code — `EXEC_NODE_FAILED`, `EXEC_BLOCKED`, `PLAN_NO_ROUTE` — shown
  as a chip on the run view and spoken by chat ("saved with the run so
  you can fix it later").
- Tests: `tests/test_node_interact.py` (pending/accelerate listing,
  sign-all landing the typed signature in the audit trail and emptying
  the queue, the authority wall answering in words, replies, consent-
  gated building standalone and under a Supernode, 404 off the desk)
  and `NodeInteract.test.tsx` (reliability line, node-scoped turns +
  action chips, quick actions send vs pre-fill). Error-code asserts in
  `test_execution_retry.py`. 134 vitest and the entire backend suite
  green; shell rebuilt.

Folders in the drawers, KYC only where it binds, and lists that fold:

- **Folders organize a file drawer.** `UserFile` gains a `folder` path
  ('/'-separated, normalized, bounded; '' = root) — folders are derived
  from the files that name them, organization rather than a separate
  object. The gateway accepts `folder` on create and update (moving a
  file is just updating its folder), and every drawer — a node's files
  in Work (Supernodes included) and the Life drawer — navigates them:
  folder tiles, a breadcrumb with "up one level", "New folder" (held
  client-side until a document lands in it), and "New document" creating
  in the current folder.
- **KYC binds only on the Global service.** New
  `GatewayConfig.global_service` (set by `oolu host --global-service`;
  the desktop and private-network hosts never set it): a Supernode
  created under a GLOBAL account serves the whole ecosystem with a
  higher trust score, so the KYC policy and its paying-plan gate are
  enforced there — and only there. On an Edge install the KYC status
  answers `required: false`, the Work UI shows **no KYC block at all**
  (no form, no subscription nag), and applying is refused as
  unnecessary (409, never a 402 plan nag). And once a review IS done,
  the block disappears everywhere: a verified Supernode shows one quiet
  "✓ KYC verified · global trust ×N" badge instead of the section.
- **Lists fold for a clear view.** The Life sidebar's Friends and Noder
  groups and a Supernode's Member nodes section are now collapsible
  headers (▾/▸ with a count); Life's choices survive restarts via
  localStorage, and everything defaults open.
- Tests: folder round-trip/move/refusal through the gateway
  (`test_user_files.py`), Edge-vs-Global KYC (`test_supernode_kyc.py`:
  `required` flag, 409 apply, nothing stored), FilesPane folder
  navigation + empty-folder creation, the hidden-on-Edge KYC block, the
  verified badge, and folding Member nodes / Friends / Noder (persisted).
  131 vitest and the entire backend suite green; verified live through
  `build_host_runtime` (folder create/move; Edge default). Shell
  rebuilt.

The endless conversation keeps its promises, and names become labels:

- **The chat reminds an idle user of unfinished work.** A conversation
  with OoLu never ends, so open work must not rely on scrolling back:
  once the user has been idle for two minutes, a dashed "reminder"
  bubble lists what is still WAITING ON THEM (needs an answer / a
  decision / an approval / hit a snag) and what is still working —
  capped at three each with an "and N more", repeated at most every
  five minutes, and reset the moment the user speaks. Reminders are the
  chat's own words: they never enter the history sent to the model.
  Logic lives in pure `reminders.ts` (`reminderDue`/`reminderText`),
  wired into `Chat` on a 30-second check.
- **Names are labels, not transcripts.** New keyword-naming helpers —
  frontend `naming.ts` (`conciseName`) and backend `oolu/naming.py`
  (`concise_name`/`keyword_slug`) — distill a task sentence into its
  first four distinct non-stopword keywords ("convert the quarterly
  report to pdf and email it" → "Convert Quarterly Report Pdf"), with
  the trimmed original as the all-stopwords fallback. Applied wherever
  the system names things itself, explicit names always honored:
  the Life Noder list and thread header (full request kept as tooltip
  and quoted line), the chat run card, `wfgps record` without `--name`
  (the learned skill's name — and therefore its `learned.…` id — is now
  keywords; the full intent stays as the description), the gateway's
  listing-title fallback for contributions without a title, and the
  desk title fallback (a bare `learned.…` skill id now reads "Convert
  Quarterly Report Pdf", never a dotted sentence).
- Tests: `reminders.test.ts` (idle window, five-minute cadence,
  activity reset, capped concise listing), a fake-timers Chat
  integration test (the bubble appears once and does not repeat inside
  the window), `naming.test.ts` + `tests/test_naming.py` (keyword
  order, dedup, stopword fallback, learned-id derivation, desk-title
  condensation). 126 vitest and the entire backend suite green; shell
  rebuilt.

The messenger straightened out — the list, the Edge doors, who answers
for a node, and money in the user's own currency:

- **Independent scrolls, Settings under Files.** The app frame is pinned
  to the viewport (`height: 100vh`; the page itself never scrolls): the
  conversation list and the open pane each own their overflow, so
  scrolling one never moves the other. The Settings entry moved from the
  pinned bottom of the sidebar to directly below Files — a long Friends
  or Noder list can no longer hide it below the fold (`convo-bottom` is
  gone).
- **Edge is two doors: this device, or a private network.** The sign-in
  screen's Edge tab now offers "This device" (the old passthrough to the
  loopback engine) and "Private network" — a private server a group runs
  on its own network (a static address), entered once and remembered
  separately from the Global server (`oolu_edge_server`). The private
  network still uses real accounts: the same username/password sign-in
  and registration form, pointed at the private host (`oolu host
  --open-registration`), because onboarding a node created under a
  Supernode has to name an actual person.
- **A node created under a Supernode starts with NO responsible.**
  `create_account` leaves `responsible` empty for non-Supernode children
  (the regime stays fixed as before; a Supernode itself always keeps its
  creator — humans in full control cannot mean nobody). Onboarding is
  the claim: the first user account that presents the node id becomes
  the responsible, shown on the node thread as their user ID; after
  that, takeovers are refused as before. The Work UI shows "not
  onboarded yet" instead of an empty responsible, and warns — on the
  thread, in the member list, and in the create-under-Supernode form —
  not to show the node id publicly before onboarding, because the id is
  the claim ticket.
- **Caps in the user's regional legal currency.** New `oolu.currency`
  module: a closed catalog of 18 currencies with symbols, decimals, and
  FIXED reference rates (a cap is a safety rail, not an FX position;
  unknown codes read as USD, which errs toward stopping earlier). New
  `account.currency` setting (choice, default USD); all money settings
  (`budget.model_cap`, `hard_cap`, `review_threshold`, `monthly_limit`)
  carry `unit="currency"`, resolved by `describe()` to the tenant's
  code and shown next to the input; bounds widened to give high-rate
  currencies (JPY, KRW, MWK) headroom. `ChatModelRouter` converts the
  cap into the meter's USD unit at the comparison and speaks the
  budget-exceeded message in the user's currency. The Settings pane
  suggests the region's currency from the browser locale ("Your region
  suggests MWK — Use MWK"), one click, never automatic.
- Tests: `test_currency.py` (conversion round-trips, unknown-code
  safety, unit stamping/resolution, the router refusing in yen), the
  unclaimed→claim desk flow in `test_work_desk.py`, private-network
  sign-in + no-address refusal in `Login.test.tsx`, unclaimed/onboarded
  node threads in `Work.test.tsx`, and the Settings-below-Files order in
  `Life.test.tsx`. Verified live through `build_host_runtime`: currency
  switch re-labels every money field and refuses bogus codes. Shell
  rebuilt.

Execution retry, diagnosed and escalated — when a run breaks, the user
sees the plan, the exact broken node, and after two retries the model is
called out to plan and write the code:

- **The exact failing node is labelled everywhere.** `ExecutionRecord`
  gains `failed_action_id`/`failed_action_label` (the FIRST action that
  failed — cascade-cancelled dependents are consequences, not causes),
  set by both runners (`DagRouteRunner` incl. capability-blocked
  preflight, `ActionExecutorRouteRunner`). The monitor's summary — and
  therefore the incident, the pause payload, the abort's terminal
  `failure_reason`, and the audit events — all name the node.
- **The plan is visible.** `GET /v1/runs/{id}` now carries `plan` (the
  chosen route as ordered steps with live per-node statuses, the culprit
  marked), `failure` (node, error, attempt, retry count), `no_route`
  (when planning failed before a viable route existed: the reason,
  unresolved grounding terms, and every excluded candidate with its
  reason), `autobuild` (the consent check, below) and `user_retries`.
  Timeline frames and `/audit` entries gain a human-readable `detail`
  line. The Task pane renders all of it: the step list with per-node
  glyphs, a "failed here" tag, the no-route explanation, and a retry
  button that counts down to the AI rebuild.
- **Retry twice, then the model plans and writes the code.**
  `RunState.user_retries` counts the operator's incident retries; after
  two of them fail, `_phase_recovery` calls the new `RouteRebuilder`
  seam instead of raising a third identical incident.
  `LLMRouteRebuilder` (metered under `plan.rebuild`) asks the tenant's
  model for a numbered plan plus one script, builds an honest route
  (`origin="llm_rebuild"`, the plan in `plan_notes`, `risk="write"` so
  model-written code re-earns the human's confirmation), and the run
  re-enters HUMAN_CONTROL. One rebuild per run (`rebuild_attempts`);
  every failure mode is a refusal carried on the incident
  (`rebuild_refusal`), never a crash. `NodeScriptRunner` accepts a
  planner-`provided` script as a proposal — executed and classified
  before it is trusted or cached, with bounded missing-dependency
  healing — and `ChatModelSynthesizer` gives the repair ladder a
  lightweight single-shot synthesizer.
- **Auto-build now checks on EXECUTION failure, not just planning.**
  Previously `account.autobuild_consent` was consulted only on the
  chat's planning-time `cannot_execute` refusal; a run that failed while
  executing never mentioned it. The consent check now gates the rebuild
  itself, the run view carries the hint on every failed/incident run,
  and the chat surface folds an execution failure (failing node +
  hint/refusal) into its reply. `build_host_runtime` wires the
  rebuilder plus a script hand (`build_script_executor`: the configured
  isolation backend, node script cache at `scripts.db`) into every host.
- Tests: `tests/test_execution_retry.py` (labelling in both runners +
  blocked + abort, the two-retries→rebuild flow incl. confirmation,
  consent/no-code/no-runtime/exploding-rebuilder refusals, the
  one-rebuild cap, provided-script verify-then-cache, the gateway
  views) and `TaskPane.test.tsx` (plan steps + culprit, retry
  countdown, autobuild hint, AI-rebuild badge, no-route panel).
  Verified live end-to-end through `build_host_runtime`: submit → node
  labelled → two retries → consent-off refusal → settings flip → model
  consulted → abort keeps the diagnosis; and a provided script executed
  for real through `SubprocessBackend` (`emit_result` → payload).

Value patching — the mechanical-design scenario: deterministic
scaffolding (open the app, open the file, select the tool) chains by
slots, and at the creative step the run pulls the node's declared input
list and lets a smart plugin fill the values:

- **`ValueInput` on `NodeContract.inputs`**: a node declares its creative
  values — name, description, type (`number` / `string` / `choice`), an
  honest default, hard `minimum`/`maximum` bounds or a closed choice set —
  instead of hardcoding them. Actions reference them with two placeholder
  forms inside parameters: `{"$input": name}` (whole-value) and
  `{"$template": "...{hole}..."}` (named holes in source text; **numbers
  and choices only** — free strings in templates are refused at bind time
  as an injection vector).
- **`skills/inputs.py`**: `inputs_manifest` (qualified `"<node>.<name>"`
  names across a subgraph; duplicate child names refused), `validate_value`
  (numbers clamp into bounds, hallucinated choices revert to the default),
  `resolve_values` (precedence **user > patcher > default**, strict
  unknown-key refusal, garbage degrades to the default), and `bind_inputs`
  (substitutes resolved values into every placeholder; identity when there
  are none).
- **`orchestrator/patchers.py`** — the smart plugin seam: `ValuePatcher`
  protocol, `DefaultValuePatcher` (defaults, free), and
  `GatewayValuePatcher`, which fills the WHOLE manifest with **one batched
  model call** (the node adapts the model via its declared descriptions,
  defaults, and bounds), meters it under `values.patch`, and boxes every
  returned value through `validate_value` — unknown names drop, unusable
  output means defaults. `patch_or_defaults` guards the run path: no
  patcher, a raising patcher, or a dead endpoint all mean the declared
  defaults run; a creative model can improve a run, never block one.
- **Gateway wiring**: listings carry `inputs` (`POST /v1/nodeplace`
  passthrough to the marketplace `NodeContract`), `/v1/market/assemble`
  previews now list the assembled plan's needed inputs with defaults and
  bounds, and `POST /v1/runs/contract` accepts `{"inputs": {...}}`,
  patches + binds **before** compilation (held reserved contracts store
  the concrete values an approver will actually judge), adds the metered
  `patch_cost` to the budget-gated estimate, and surfaces it on the run
  response. Contract-run `outcomes` now include each action's `evidence`,
  so callers see what verification measured.
- **CAD**: `parametric_plate_pack()` — a plate whose width, depth,
  thickness, and hole radius are declared bounded inputs feeding a
  `$template` OpenSCAD source, with a verification spec derived from the
  bounds so EVERY admissible fill verifies (volume brackets computed from
  `t·(w·d − A₆₄(r))`, genus 1 provable for the whole box); and
  `rect_plate_with_hole` — an exact watertight genus-1 reference solid
  matching the closed form to 1e-9, used as the test instrument.
- Tests: `tests/test_value_patching.py` proves the scenario end to end
  through the public gateway — five marketplace nodes assembled by slots
  alone, scaffolding executed in order before the creative step, an LLM
  patch (one metered call) clamped into bounds with invented parameters
  dropped, user values outranking the model, defaults outlasting a dead
  one, and the rendered geometry verified against the analytic spec.

## v0.7.0 — 2026-07-05

Release notes: `docs/releases/v0.7.0.md`.

Unified-surface migration, final step — the loopback surface is gone:

- **Removed** the `workflow_gps.desktop` package (`DesktopService`, the
  loopback app, its view-models and inline UI), `build_desktop_runtime`,
  the `--legacy-loopback` / `--unified` flags, and **`wfgps web`** (the
  shared-token mode built on the loopback shell — superseded by
  `wfgps host`, which is multi-user with real accounts). One surface
  remains: the multi-tenant gateway, with `wfgps desktop` (loopback,
  auto signed in) and `wfgps host` (network, accounts) as its two
  bindings. `wfgps desktop` keeps its flags, port, and data layout — the
  setup scripts and the packaged app work unchanged.
- The `Dockerfile` / `docker-compose.yml` now run `wfgps host`
  (`WFGPS_HOST_SECRET` + `WFGPS_ADMIN_PASSWORD` instead of
  `WFGPS_WEB_TOKEN`); the README's self-hosting section is rewritten
  around accounts.
- Tests moved with the code they prove: the desktop-runtime lifecycle
  tests (planning-only failure, model-driven clarification, injected
  planner end-to-end, CLI-executor confirmation, reopen persistence) are
  ported to the host runtime through real gateway routes; the
  planning-cost/expected-success/cost-weight/default-advising surface
  tests are ported to `/v1/market/assemble`; the loopback-only suites
  (~57 tests whose behaviors have gateway twins) are deleted; the shared
  browser harness moved to `tests/browser_harness.py`.

Unified-surface migration, step 3 — the flip:

- **`wfgps desktop` now serves the unified gateway surface by default**:
  same routes and front-end as `wfgps host`, loopback-only, auto signed
  in, with `--registry` / `--seed-starter` keeping their meaning (the
  starter pack seeds the registry and its skills plan `POST /v1/runs`
  intents) — the setup scripts and the packaged app work unchanged. The
  pre-migration surface stays available behind **`--legacy-loopback`**
  for the transition window; `--unified` remains as a no-op flag.
- Parity screens: the Earnings screen gained the **payout-account card**
  (KYC status when onboarded; a country/currency onboarding form when
  not — a host without a payout adapter answers with the same honest
  404), and Health gained **execution isolation** (a new gateway
  `GET /v1/worker-health` route rendering the enforced
  `IsolationPolicy` via a helper both shells now share — the labels are
  computed from the policy, never restated by hand).
- **First-run crash fixed** (the packaged app's field failure,
  reproduced by the new seeding test): `SkillRegistry`, `TraceStore`,
  `PriceBook`, and `LocalKnowledgeClient` did not create their parent
  directories, so a fresh machine died with sqlite's "unable to open
  database file" before any table logic ran. All path-owning stores now
  create their directories at construction, pinned by a test that
  builds each one under a deliberately nonexistent path.

Unified-surface migration, step 2 — task-flow parity:

- The unified front-end's run detail now makes **every pause kind
  actionable**: clarification questions render as a form (suggested
  values as placeholders) posting `/answers`; route confirmation shows
  the chosen blueprint, estimated cost, and reserved actions with
  Confirm/Decline posting `/confirmation`; approval shows
  granted-of-required with an Approve button (self-approval refusals
  surface as the server's own error); incidents list with Retry/Abort
  posting `/incidents`; and any non-terminal run has a Cancel button.
- Added the **Skills screen** (`/v1/listings?q=` search over published
  marketplace nodes — title, summary, status, tags), degrading honestly
  where nodeplace is not wired.
- A real-Chromium test drives a run that pauses twice — clarification,
  then route confirmation — to completion entirely from the browser,
  via the paste-a-token sign-in path an IdP-fronted host would use.
  Route pins cover every new fragment the page calls.
  With this, the unified surface covers the loopback shell's task flow;
  what remains before flipping the default is the payout-onboarding
  screen and worker health (the Health screen shows gateway metrics).

Unified-surface migration, step 1 — plus field fixes:

- Added **`wfgps desktop --unified`** (opt-in preview): the desktop shell
  served over the SAME multi-tenant gateway `wfgps host` uses — same
  routes, same front-end, same identity semantics — bound to loopback
  with a `local` user auto-provisioned and signed in. The browser opens
  straight into the shell via a `#auth=<token>` bootstrap (the token
  moves into sessionStorage and out of the URL immediately): zero
  ceremony locally, because the loopback bind — not a password — is the
  trust boundary on the user's own machine. Credentials are ephemeral
  per launch by design (fresh secret, rotated password); the data
  directory persists like any host. The default `wfgps desktop` is
  unchanged; the loopback surface remains until the remaining screens
  are ported (step 2).
- **GitHub Actions**: bumped `checkout` v4→v5, `setup-python` v5→v6,
  `upload-artifact` v4→v5, `setup-node` v4→v5 across all workflows
  (the Node 20 runtime deprecation), and the Tauri frontend toolchain
  Node 20→22 (Node 20 is end-of-life). Pinned by a test so a stale
  major cannot creep back.
- **Startup schema guarantee, pinned**: new tests prove a fresh data
  directory answers every read surface before any write (the packaged
  app's exact startup path), restarts reopen and migrate cleanly (the
  admin created before a host restart still signs in after), and every
  SQLite store creates its schema at CONSTRUCTION time — "no such
  table" is structurally impossible on a fresh install.

The multi-user gateway grows a face:

- Replaced the gateway front-end (served by `GatewayASGI` at `GET /`)
  with a **sign-in page + shell**: username/password → `POST
  /v1/auth/login`, the bearer token lives in `sessionStorage` for that
  tab (a 401 signs the tab out; sign-out drops it), and every fetch
  carries `Authorization: Bearer`. IdP-fronted hosts (no local accounts)
  get a paste-a-token fallback on the same page.
- Screens over the authenticated surface: **Runs** (start an intent,
  list, detail with audit timeline + live WebSocket frames via the
  bearer subprotocol), **Assemble** (goal → priced preview with
  planning cost, expected success, and budget verdicts → run the
  contract; a 202 hold links to the inbox), **Inbox** (approve/decline
  held reserved contracts), **Earnings**, **Users** (admin-only:
  create, disable/enable), **Health**. Screens degrade honestly: 404 →
  "not enabled on this host", 403 → "no authority for this screen".
  XSS-safe by construction (DOM building, no HTML templates, no
  `innerHTML`), pinned by tests along with every route the page calls.
- Real-Chromium end-to-end tours against a real host runtime: sign-in
  (wrong password says only "invalid credentials"), admin provisions
  and disables a user from the browser and the disabled account is
  locked out, and a member sees the Users screen refuse and the
  unwired Earnings screen say so — instead of breaking.

Multi-user web hosting — accounts, not a shared token:

- Added `identity.accounts`: **local user accounts** as the identity
  provider a self-hoster lacks. Passwords are scrypt-hashed (stdlib;
  per-user salt, cost parameters recorded next to the hash), login mints
  a short-lived HS256 token through the SAME `OidcValidator` path an
  external IdP would use, and **roles become stored grants** — a forged
  token claim still buys nothing. Login failures are uniform ("invalid
  credentials" for unknown / wrong-password / disabled alike — no account
  enumeration), unknown users cost the same scrypt work as wrong
  passwords (decoy verification), and repeated failures lock the username
  briefly.
- New gateway routes (answering only when accounts are configured —
  IdP-fronted installs keep their 404): public `POST /v1/auth/login`;
  `GET/POST /v1/auth/users` and `POST /v1/auth/users/{name}/disabled`
  behind stored `users:manage` authority, tenant-scoped (admins provision
  their own tenant; the tenant comes from the session, never the body).
- Added `build_host_runtime(data_dir=, secret=)`: the full multi-tenant
  gateway (runs, marketplace, ratings, pricing, traces, approvals) over
  one backupable data directory, wired with local accounts. Refuses
  secrets under 32 characters.
- Added **`wfgps host`**: serves it, bootstraps the first admin
  (idempotently — never resets an existing password) from
  `WFGPS_ADMIN_PASSWORD` or a generated password shown once, warns when
  the signing secret is ephemeral, and says loudly to put HTTPS in front.

The self-host runner for online web users:

- Added **`wfgps web`**: the desktop shell served over the network,
  wrapped in `desktop.web.TokenGuardedApp` — the one property that makes
  a non-loopback bind defensible: nobody without the access token gets
  anything. Browsers sign in once at `/login?token=…` (HttpOnly /
  SameSite=Lax session cookie; sessions are in-memory, so a restart signs
  everyone out), API clients send `Authorization: Bearer <token>`, and
  WebSocket upgrades ride the cookie (4401 without). Token comparison is
  constant-time; the 401 page is deliberately information-free. The token
  comes from `WFGPS_WEB_TOKEN` (or is generated and printed once), must be
  ≥16 characters, and the startup banner says loudly to put HTTPS in
  front. `wfgps desktop` stays loopback-only, unchanged.
- Added a **`Dockerfile` + `docker-compose.yml`**: the shell behind the
  token on one backupable `/data` volume; compose refuses to start
  without `WFGPS_WEB_TOKEN`. Both pinned by tests, documented in the
  README's "Self-hosting for online web users".

Onboarding hardening (from a field DX audit) — every install trap grows
directions:

- Added **`wfgps doctor`**: checks Python version, data-dir writability,
  each optional stack (with the exact `pip install "workflow-gps[…]"` to
  run), the configured model endpoints (a probe that treats any HTTP
  answer as alive — 401 is not "down"), and the API-key requirement.
  Missing *optional* stacks are guidance, not failure: a desktop-only
  machine reports healthy. Exit 1 only on real problems, each with its
  one-line fix.
- **`wfgps run` preflights** the three classic fresh-install traps before
  any engine machinery can produce a misleading traceback: missing
  `[engine]` extras, no model server answering at the configured
  `api_base` (the silent `localhost:8000` trap — the error now names
  vLLM/Ollama/LM Studio and `--config models.yaml`), and an unset
  `OPENAI_API_KEY` (any value works for vLLM). `--no-preflight` bypasses;
  injected builders (tests, embedders) are never preflighted.
- **Dead ends answer with directions**: running
  `python src/workflow_gps/cli.py` as a bare file now prints how to run
  it properly (setup scripts / `wfgps` / `python -m`) instead of a
  relative-import traceback, and `uvicorn workflow_gps.gateway.asgi:app`
  serves a 503 signpost explaining that `GatewayASGI` is a class needing
  a wired `GatewayApp` — with the real local commands — instead of
  uvicorn's "Attribute 'app' not found".
- **Setup scripts bootstrap pip**: a `.venv` created by a stripped-down
  Python (no pip) is repaired via `ensurepip` instead of failing later
  with "No module named pip".
- Added the **`ci` GitHub Actions workflow** (lint + full test suite on
  every push/PR and on demand via `workflow_dispatch`); all three
  workflows are hand-dispatchable, pinned by a test.
- Moved model-call pricing from `metering.model_calls` to
  **`billing.model_calls`** — the metering package's own tested invariant
  is that it exposes no money symbols (metering counts usage; billing
  prices it), and the meter violated the layering. Import paths change;
  behavior does not.

- The learned planner is now **wired in by default**: when a surface has
  a `trace_store` and no explicit `proposal_model`, producer picks are
  advised by `TraceProposalModel` over the caller's own recorded runs —
  free, evidence-only, and tenant-scoped (the gateway constructs the
  model per request with the calling tenant's context, so one tenant's
  history never enters another's evidence pool; the desktop uses its
  single-user bucket). An explicitly passed `proposal_model` always
  wins. Pinned in tests with run-level evidence per-node personalization
  cannot see: steps that succeeded inside runs that failed as wholes.

The first domain pack — CAD, with verification grounded in mathematics:

- Added `domains.cad.geometry`: exact mesh mathematics with stated
  hypotheses. Volume by the divergence theorem (Σ v0·(v1×v2)/6 — exact
  on closed, consistently oriented meshes; translation invariance and
  orientation antisymmetry are *asserted in tests*, not assumed),
  surface area, extents, and a combinatorial `ManifoldReport`
  (boundary / non-manifold / misoriented edges, degenerate triangles,
  connected components, Euler characteristic, and genus via χ = 2c − 2g).
  STL both directions, with binary detection by the exact length
  equation — never the header, which real files lie about.
- Added `domains.cad.verify`: `GeometrySpec` (watertightness, volume and
  area intervals, extent box-fit, exact genus) → `GeometryReport` with
  measured numbers behind every failure. Volume is *withheld* on open
  meshes — the formula's hypothesis failed, so no number beats a wrong
  number.
- Added `domains.cad.OpenSCADExecutor` (adapter `cad`): deterministic
  `render_stl` through the OpenSCAD CLI (binary configurable as an argv
  prefix — tests drive the real subprocess path via a stub renderer;
  a `skipif` test runs the true binary when installed) and pure-Python
  `verify_geometry`. A failed predicate fails the action → the run → the
  earnings, and the trace posterior records the failure honestly: the
  platform's money-on-verified-success promise, enforced by geometry.
- Added `domains.cad.cad_starter_pack()`: a parametric mounting plate and
  its verification node, slot-chained. The spec's bounds bracket
  closed-form values (inscribed-polygon hole area (n/2)r²sin(2π/n),
  perimeter 2nr·sin(π/n); volume ≈ 3087.08 mm³, area ≈ 2098.9 mm²,
  genus exactly 1) — tight enough to refute a hole-less or double-holed
  part outright, recomputed from the formulas in the tests. A gateway
  test contributes both nodes and goal-assembles them: CAD nodes are
  ordinary marketplace citizens.

The trace corpus and the first learned planner:

- `TraceStore` now logs every recorded run **verbatim** (`trace_runs`, a
  new migration existing databases adopt cleanly): the aggregates grade
  nodes; the log answers "what did whole successful plans look like".
  Read it with `runs(context=, goal=, limit=)` — newest first, `None`
  filters mean all, the empty string stays a real bucket.
- Added `knowledge.corpus`: `build_examples` turns runs into
  (goal, plan-prefix → next node) training examples — the shape a
  forward-generating sequence model trains on — and `export_jsonl`
  writes them oldest-first as a portable file for offline model
  training. Failed runs export flagged (`run_success: false`), never
  silently dropped.
- Added `orchestrator.TraceProposalModel`: the baseline learned planner
  behind the same `ProposalModel` seam a Mamba/SSM checkpoint later
  implements. It proposes live from the caller's own run log, judged
  against the most specific evidence pool available (runs of this goal →
  runs sharing an already-selected node → all runs; the budget layer's
  class-first shape), weights candidates by the Beta mean of the runs
  they appeared in, has no opinion where it has no evidence, and costs
  nothing. A future sequence checkpoint must beat it in the replay
  harness to earn its inference cost.

Thompson v2 — the learning loop gets honest about time, money, and proof:

- `TraceStore` gained `recency_decay` (default 1.0 = today's exact
  counting): every fresh observation of a node first discounts its
  existing counts, so the posterior tracks what the node has done
  *lately* — a node that regressed last month stops looking as good as
  ever, old glory fades into honest uncertainty, and Thompson sampling
  re-explores it. Posterior (and `NodeStats`) counts are floats now.
- `ContractAssembler` (and previews on both surfaces, via `cost_weight`
  in the request body) can rank picks by expected **utility** — quality
  minus weighted personal cost — instead of quality alone, so a
  slightly-less-proven cheap node can honestly beat a proven expensive
  one, by exactly the trade the caller declared. Default 0 keeps cost a
  tie-break, unchanged.
- Previews now report `expected_success`: the plan's chance of verified
  success in the caller's own hands (product of picked nodes' posterior
  means over the personalized library; gap nodes count at their uniform
  prior 0.5). Shown on the desktop assemble screen.
- Added `knowledge.replay`: an offline harness where planner strategies
  audition before they ship. `ReplayWorld`s (fittable from recorded
  history via `from_trace_store`) run in drift-modeling phases,
  `PosteriorStrategy` replays the assembler's exact pick math with its
  own private trace store, and every strategy sees the same seeded
  outcome stream — reports compare decisions, not luck. Pinned in tests:
  decay adapts to drift faster, cost-awareness buys success cheaper.

- Added the `ProposalModel` seam to `ContractAssembler`: a model may weigh
  in on contested producer picks, but only as a **prior** — its `[0, 1]`
  weights enter the same Beta posterior verified history feeds, as
  pseudo-observations (`proposal_strength`, default 3) that decide
  thin-history ties and wash out as real evidence accumulates. Advisory by
  construction: unknown ids are dropped, wild weights clamp, exceptions
  (including a dead model endpoint) downgrade to verified-history-only
  assembly, and a single-candidate pick never spends a model call.
- Added `billing.model_calls`: `ModelCallMeter` records every completion's
  token telemetry under a purpose tag and a `ModelPriceTable` (per-tier
  cost per million tokens; unknown tiers priced conservatively) turns it
  into money — model calls are never free.
- Added `orchestrator.proposals.GatewayProposalModel`: the seam implemented
  over the same routing `Gateway` the synthesis engine uses (frozen
  cache-safe system prompt, fast tier, small completion budget, strongest
  candidates shortlisted), with defensive weight parsing — unreadable
  advice is no advice — and every call metered.
- Assembly previews now surface `planning_cost` (what the advice cost,
  distinct from market gross since no noder earns it) on both surfaces,
  and the budget verdict judges **gross + planning cost**: a plan that
  needed advice is honestly dearer. New ctor knob `proposal_model` on
  `GatewayApp`, `DesktopService`, and `build_desktop_runtime`; the desktop
  assemble screen shows the planning line when it is nonzero.

## v0.6.0 — 2026-07-05

Release notes: `docs/releases/v0.6.0.md`.

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

- Native installer packaging (`packaging/`): `python packaging/build_installer.py`
  produces a single self-contained executable (`dist/WorkflowGPS-Shell`,
  `.exe` on Windows) via PyInstaller — copy it anywhere, double-click,
  the shell starts, the browser opens, data lives in `~/.workflow-gps`.
  The frozen launcher (`shell_launcher.py`) is a thin wrapper over the
  same `wfgps desktop` invocation the setup scripts use (one launch path
  to keep honest), with a free-port fallback so a busy 8765 never turns
  into an error dialog. The spec bundles the starter-pack data
  (importlib.resources inside the frozen app) and uvicorn's dynamic
  imports statically, and excludes every heavy optional stack.
  PyInstaller cannot cross-compile, so `.github/workflows/
  build-installers.yml` builds Windows/macOS/Linux binaries on every
  version tag. Validated live: the Linux binary built here serves the
  UI, seeded skills, and earnings standalone. `tests/test_packaging.py`
  pins the launcher argv against the real CLI, the port fallback, the
  spec's bundling, and the CI wiring.
- One-step setup for non-developers: download the repo ZIP, unzip, and
  run `setup.bat` (Windows, double-clickable) or `./setup.sh`
  (macOS/Linux). The scripts find Python 3.11+ (with a friendly pointer
  when it's missing), create a private `.venv` inside the folder,
  install only the `serve` extra (the shell never needs the heavy
  `engine`), and launch `wfgps desktop --seed-starter --open` — which
  now auto-opens the browser (new `--open` flag) and prints a
  human-readable startup message. Idempotent: re-running reuses the
  environment and just starts the shell; nothing lands outside the
  folder. The README opens with a "Quickstart — download → run" section,
  and `tests/test_setup_scripts.py` pins every link of the story (the
  scripts' install command and launch flags against the CLI parser, the
  README pointers) so the setup path can never silently rot.
- Browser-level end-to-end tests (`tests/test_browser_e2e.py`): a real
  Chromium drives the real front-end over a minimal in-test ASGI HTTP
  server (no external server dependency). The tour: assemble the seeded
  marketplace chain, watch the budget verdict, confirm the run through
  the shared money path, onboard a payout account (KYC pending blocks
  payouts), and render health; a second test proves the task screen
  degrades gracefully where the transport has no websockets. Skips
  cleanly wherever the `browser` extra (playwright) or a Chromium
  executable is unavailable; falls back to the host-installed
  `/opt/pw-browsers/chromium` when playwright's own download is absent.
- Payout-account onboarding in the shell: `DesktopService.payout_account`
  / `onboard_payout_account` (new `payout_adapter` ctor hook, also a
  `build_desktop_runtime` passthrough) over `GET`/`POST
  /v1/payout-account`. Not-onboarded is a rendered state (200), never an
  error; onboarding is idempotent (an account is an external resource,
  returned rather than minted twice) and audited (`payout.onboarded`);
  the KYC status is refreshed from the processor on every read and the
  refresh persisted — verification happens on THEIR side, the shell only
  mirrors it, and `payouts_enabled` flips only on `verified`. The
  Earnings screen gains a payout-account card: onboarding form when
  absent, status badges ("payouts blocked until KYC verifies") after.
- Earnings wired into `build_desktop_runtime`: shells get the earnings
  screen out of the box — the runtime creates an `EarningsLedger` and
  `PayoutStore` over its own durable connection (honest zeros until the
  user's contributions earn), passes them to the shell under the new
  `noder_principal` parameter (default `"local-noder"`; `None`
  disables), and exposes them on `DesktopRuntime.earnings` /
  `.payouts` — hand THOSE to a settlement job so the screen and the
  money pipeline share one truth.
- Desktop earnings screen: `DesktopService.earnings()` (new
  `earnings_ledger` / `payout_store` / `noder_principal` ctor wiring)
  projects the local noder's ledger into a secret-free `EarningsView` —
  available/pending/reserved/lifetime-paid balance tiles, the ledger
  lines (kind, amount, event, availability; most recent first), and
  payout batch history — served at `GET /v1/earnings` (404 when the
  shell has no earnings wiring). Amounts cross the loopback in currency
  units; the ledger keeps its integer micros, and the shell can show
  the money but never move it. The front-end gains an Earnings screen
  with color-coded entry kinds and an explicit negative-balance
  explainer (a clawback exceeded the reserve; new earnings repay first).
- Desktop front-end (replacing the scaffold screen by screen, still one
  self-contained page with no build step): a DOM-builder kernel (`h()`)
  replaces innerHTML templates — every dynamic value is a text node, so
  the page is XSS-safe by construction; a hash router gives each screen
  and each task a deep-linkable address (`#/task/{run_id}`). The screens
  now drive the WHOLE loopback surface: the new task-detail screen
  answers clarification questions, previews and approves/declines
  routes, resolves incidents (retry/abort), cancels, and streams the
  live timeline over the websocket; Assemble renders per-step clearing
  forces and keeps its form across navigation; Inbox links run pauses to
  their task screens; a new Skills screen searches the library. The
  wiring test now pins all of it.
- Desktop UI scaffolding (`desktop/ui.py`, served by the loopback at
  `GET /`): one self-contained page — plain HTML + vanilla JS, no build
  step — over the same loopback endpoints the tests drive, so the page
  can never do anything the API cannot. Four screens: **Assemble** (goal
  + slots + budget knobs + explore/fill-gaps, preview with per-step
  prices/payouts, learned orderings, and the budget verdict; confirm
  with review acknowledgement, rendering held-for-approval outcomes),
  **Tasks** (submit + session task table), **Inbox** (all pause kinds;
  contract-approval items get approve/decline buttons using a bearer
  token held in page memory only — every decision is verified
  server-side), and **Health**. Light/dark aware, XSS-escaped rendering.
  Tests pin the page's wiring to the real routes and syntax-check the
  inline script with node (skipped where node is absent).
- Hardening passes: property-style fuzzing of the money invariants and
  concurrency stress on the shared stores (no new dependencies — seeded
  `random`, explicit seeds, failures replay exactly). The money machine
  (12 seeds x 50 random ops: accruals, clock advances, settlement cycles
  with a flaky processor, upheld/rejected disputes) checks after every
  step that the reserve is never negative, lifetime payouts never exceed
  gross accruals (money is never minted), only upheld clawbacks can
  drive a balance negative, and the ledger's PAYOUT outflow equals what
  the processor actually paid — then jumps past the risk window for the
  eventually-100% endgame (gross == paid + available + reserved, residue
  below threshold). The concurrency suite races 16 barrier-synchronized
  threads at the primitives: idempotent `run` executes exactly once (and
  exactly once again after `release`), ledger dedup admits one row per
  unique key with no lost distinct writes, a hold is decided by exactly
  one contender (with sweeps racing adds), and trace statistics lose
  nothing across threads.
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
