# The memory stack — building phases for the atomic chain reaction

Status: Proposed. Scope: the build plan for the Adaptive Capability Web's
memory substrate (`oolu_adaptive_capability_web_build_plan.md` §8–§9,
§17–§22) — the layer that turns every verified execution into atomic,
provenanced records that make the NEXT execution cheaper, safer, and
smarter. That compounding loop is the chain reaction; reinforcement-style
route learning and the node-token reasoning model are what it fuels.

Companion reading: `docs/context-harness-plan.md` (the completed
six-phase arc this plan stands on), `docs/route-finding-proof.md` and
`docs/node-token-planner.md` (the containment law and the reasoning
model this stack feeds), `docs/model-seats.md` (the governance every new
writer must start from).

---

## 1. The chain reaction, named

The capability-web doc describes one flywheel:

```text
event → atomic memory → retrieval into a seat → better execution
      → verified trace → learned route / induced skill
      → cheaper, safer next execution → more (and better) events → …
```

Each phase below adds ONE stage of that loop and closes it before the
next phase opens. A stage that writes without a reader is inventory;
a stage that reads without a verified writer is hallucination fuel —
so every phase ships its writer, its reader, and the test that the loop
actually turned once.

Two standing laws carry over unchanged from the completed arc:

- **The model proposes; the kernel disposes.** Learned components enter
  only through contained ports (`ProposalModel`, `Embedder`, seat
  profiles) with bounded influence and independent rollback — the
  containment already proven in `route-finding-proof.md` §5 and
  `planner/baseline.py`.
- **Only verified outcomes teach.** The trace store already refuses
  unverified runs as training data (`knowledge/corpus`); every memory
  tier below keeps that admission bar.

## 2. What already exists (the arc left the ignition wired)

| Capability-web concept | Standing OoLu machinery |
|---|---|
| Immutable event fabric (§8) | Hash-linked audit log, transactional outbox, idempotency ledger (`oolu.durable`) |
| Atomic memory w/ supersession (§17.2) | `BuildLedger` (attempts, lessons, supersede-on-publish) — builds only |
| Negative knowledge (§18) | Bench failure taxonomy; ledger lessons; `knowledge` error patterns — narrow scopes |
| Hybrid retrieval (§17) | `retrieval.py` one scorer + `Embedder` seam + model embeddings; `contextpack.py` budgeted packs |
| Typed route search (§12) | Typed backward-chaining assembler + Thompson sampling over `TraceStore` posteriors |
| Route learning dataset (§20) | `trace_runs` (verbatim run log), `route_observation`-shaped audition scoreboard (`--record`) |
| Offline policy learning (§20) | `planner/` node-token vocabulary, corpus exporter, `MarkovPlanner` baseline, 3B/8B/30B ladder, `earns_its_cost` replay gate |
| Task leases / shared state (§21) | Worker control plane's signed single-use leases; resumable `RunState` (agents resume from state, not transcripts) |
| Verification as evaluator (§15) | Verify-by-execution, birth gate, `node.review` seat, `_answer_gap` port contracts |
| Model gateway manifest (§25) | `providers/registry.py` manifests + canonical chat interface + seat profiles |

The gaps are exactly the memory tiers: no general atomic-memory spine,
no temporal graph with validity intervals, no episodic summaries, no
first-class scoped failure records, no subgraph→skill induction, and
route learning stops at context-free posteriors.

---

## 3. The phases

### Phase M0 — the atomic memory spine (generalize what Phase 5 proved)

**Status: LANDED** — `src/oolu/memoryspine.py`; the BuildLedger dual-writes
lessons with audit-chain + attempt-row provenance; the gateway reads packs
spine-first; the loop-closure test drives refusal → spine → retry pack →
publish-supersedes through the real build door
(`tests/test_memory_spine.py`).

*The `BuildLedger` pattern — durable rows, provenance, lessons,
supersede-on-correction — promoted from one seat's memory to the
platform's memory contract.*

- `src/oolu/memoryspine.py`: one `memories` table on the same
  `DurableConnection` discipline, carrying the capability-web record
  verbatim: `memory_type`, `statement`, `structured_value`,
  `scope_ids`, `valid_from/valid_until`, `confidence`,
  `verification_state` (proposed→observed→reproduced→verified→rejected),
  `provenance_event_ids`, `supports/contradicts/supersedes`.
- **Writers bridge, never fork:** build-ledger lessons, `knowledge`
  dependency hints and error patterns, and repair outcomes dual-write
  into the spine with their existing stores as provenance. New writers
  MUST name a seat and an admission rule (verified / observed /
  inferred-with-confidence) — the write-back policy the harness plan
  already enforces for builds.
- **One reader:** `contextpack`'s `lessons` port generalizes to
  `spine.recall(scope, goal, kinds=...)` — budgeted, superseded rows
  excluded structurally (a WHERE clause, not a convention).
- Acceptance: a corrected value never re-enters an executable pack
  (the harness plan's outstanding acceptance scenario, landed); every
  memory answers "where did you come from" with event ids that resolve
  on the audit chain.

### Phase M1 — the temporal graph and state projections

*Facts and relationships get validity intervals; "current state" becomes
a projection, never a transcript summary.*

- `graph_edges` (capability-web §9.3) as adjacency tables beside the
  spine — edge_type from the doc's vocabulary, `valid_from/until`,
  confidence, provenance. Sources: node contracts (consumes/produces),
  run outcomes (`attempted_by`, `verified_by`), supersessions, the
  trace store's precedence pairs (`composed_with`), expertise
  (`owned_by`). No graph database until PostgreSQL/SQLite adjacency is
  a *measured* bottleneck (the doc's own rule; the repo's own habit).
- **Graph term in retrieval:** `contextpack`'s scoring gains
  `graph_proximity` — one hop over valid edges — alongside the lexical/
  embedding score; the spec's `memory_score` shape, pragmatically.
- State projections: per-node and per-goal "state cards" derived from
  events (last verified run, current contract, open failures) — the
  cards the router and packs read instead of re-deriving.
- Acceptance: "what depended on X when Y happened" is one query;
  a superseded edge never contributes proximity; deleting projections
  and rebuilding from events yields identical cards.

### Phase M2 — episodic memory and hierarchical summaries

*The first real compaction in the codebase — under the standing law
that commitments survive verbatim.*

- Episode segmentation over the existing event streams (runs, builds,
  chat turns) → `episode` rows: objective, outcome, decisions,
  unresolved items, artifact refs — each claim carrying source event
  ids; source supersession invalidates the summary (recompute, never
  patch).
- Summary levels grow bottom-up only as volume demands: execution →
  task → project. No global summary, ever (the summarization
  prohibitions in the Continuity spec and this repo agree).
- **Server-side conversation truth lands here** (the item deferred
  through harness Phases 3 and 5): the chat window's overflow note
  reads from the episodic store — earliest standing instructions
  verbatim, capped — instead of trusting the client's last-20 window.
- Acceptance: the capability-web exit test — a project interrupted for
  weeks restores its objective, open questions, and last error from
  the stack, not from a transcript; invalidated summaries never serve.

### Phase M3 — negative knowledge, first-class

*Failures become scoped records with applicability conditions — useful
forever, universal prohibitions never.*

- `failure_record` (capability-web §18): intended transition, failure
  mode, root-cause state (unknown/suspected/verified), applicability
  (parameter ranges, environment versions), reproduction count, reopen
  conditions. Writers: the birth gate's refusals (today's ledger
  problems, enriched), runtime `ErrorRecord`s (already signatured),
  bench taxonomy rows, review blocks.
- **The negative check before commitment:** route assembly and the
  birth gate run the doc's `negative_knowledge_check` — retrieve
  goal-equivalent and mechanism-equivalent failures, compare
  applicability, then *reject duplicate or allow retest* with the
  material difference named. A blocked retry says which failure blocked
  it; an allowed retest says what changed.
- Acceptance: a prior failure is retrieved before execution (metric:
  `prior_failure_recall`); a materially different context is allowed
  to retest — both as gateway-level tests in the growth-rig pattern.

### Phase M4 — skill induction (ignition)

*Repeated verified subgraphs become parameterized skills — the step
that makes capability compound instead of repeat.*

- Pipeline over `trace_runs` (the corpus that already exists):
  episode segmentation → value abstraction (the `knowledge/scrubbing`
  discipline, reused) → frequent-subgraph mining over precedence pairs
  → parameter anti-unification → candidate skill contract
  (capability-web §19.2, which is a `NodeContract` composition — the
  vocabulary already tokenizes it).
- **Promotion through existing gates, not new trust:** replay against
  history (`orchestrator/replay.py`), mutation tests, then the doc's
  thresholds (≥5 replays, ≥3 distinct contexts, ≤5% unexplained
  failures) — and the skill enters the marketplace as a node bundle,
  verified-by-execution like every other citizen. Failed promotions
  write failure records (M3) — the chain reaction's exhaust is fuel.
- Acceptance: a route repeated across three distinct contexts yields
  one parameterized skill that replays clean; a false promotion is
  caught by mutation testing and stored as negative knowledge.

### Phase M5 — reinforcement route learning (the RL rungs, in order)

*The doc's progression, each rung gated by the last — never skipping to
online RL.*

- **Rung 1 — the dataset:** `route_observation` rows on every
  execution (context features, chosen route, node versions, outcome
  score, actual cost/latency, interventions, reuse created) — extends
  `TraceStore` + the audition scoreboard; the reward is the doc's §20
  expression with the repo's verified-only bar.
- **Rung 2 — contextual bandit:** context features (goal class, desk
  shape, model manifest) enter the assembler's Thompson choice —
  posterior per (route, context bucket) instead of per route.
- **Rung 3 — learned reranker:** behind the existing `ProposalModel`
  port at `DEFAULT_PROPOSAL_STRENGTH` — bounded advice, unknown ids
  dropped, exceptions downgrade to evidence-only. Containment already
  proven; rollback is unplugging the port.
- **Rung 4 — offline policy = the node-token planner trained.** The
  corpus exporter ships today; M4's skills and M5's observations grow
  it. Train the 3B rung off-box; it auditions in the replay harness
  and bills nothing until `earns_its_cost` passes — *this is the
  scalable reasoning model*: reasoning as next-NODE-token generation
  over the capability web, compute-bounded, improving with every
  recorded run instead of re-reasoned from prose each time.
- **Rung 5 — constrained exploration:** only under risk budgets the
  policy kernel already enforces (spend caps, human-control gates,
  review thresholds); high-risk exploration is structurally blocked,
  and the learned layer can be rolled back independently at every rung.
- Acceptance: route choice beats the fixed heuristic on replay;
  policy violations remain zero by construction; each rung's OFF
  switch is a config change.

### Phase M6 — multi-agent work over shared state

*Agents cooperate through the stack, never through each other's
transcripts.*

- Typed handoffs (capability-web §21) over the existing durable queue
  and signed leases: current state refs, completed execution refs,
  evidence, unresolved bindings, acceptance evaluators — a `RunState`
  excerpt, not a chat log. Expertise records derive from
  `seat_performance` + trace outcomes and feed the doc's
  `expert_score` routing.
- Independent verifier roles generalize the `node.review` seat: the
  agent that produced a deliverable never scores it.
- Acceptance: the doc's exit tests — an agent resumes a colleague's
  task from events alone; leases prevent duplicate claims; conflicting
  proposals persist separately until an evaluator or human resolves.

---

## 4. Sequencing and the loop-closure rule

M0 → M1 → (M2 ∥ M3) → M4 → M5 → M6. M2 and M3 are independent readers
of the spine; M4 needs M3 (failed promotions must have somewhere honest
to go); M5's rungs 2–4 need M4's skills in the corpus to be worth
learning over; M6 rides everything but blocks nothing.

The loop-closure rule for every phase: before it ships, one test must
drive the full circle — an execution writes the phase's records, a
LATER execution demonstrably reads them through a seat, and the read
changes the outcome (a lesson avoided, a skill reused, a route
re-ranked). Inventory that no seat consumes is deleted, not documented.

## 5. Kernel / learned boundary (capability-web §3, applied here)

Stays deterministic kernel, forever: event append + provenance, schema
and type checks, idempotency, budgets, consent and review gates, node
isolation, cache integrity, supersession law. Stays learnable and
unpluggable: retrieval ranking (Embedder), route proposal
(ProposalModel), route policy (bandit → reranker → planner), context
selection weights, skill candidacy. Every learnable sits behind a port
with a lexical/heuristic floor — the system degrades to Phase-5-of-the-
arc behavior, never below it.

## 6. Metrics (the doc's §34, started from day one)

Per phase, into the audition/metrics surfaces that already exist:
`prior_failure_recall`, `stale_memory_rate`, `applicable_rule_recall`
(M0–M3); `reusable_skill_count`, `average_subgraph_reuse`,
`invalid_composition_rate` (M4); `route_regret` vs the frozen heuristic,
`cost_per_verified_state` (M5); `handoff_success_rate`,
`duplicate_work_rate` (M6). The chain reaction has one scalar health
check: **reuse per execution** — verified work consumed from memory per
new execution — which must trend up while cost-per-verified-state
trends down.
