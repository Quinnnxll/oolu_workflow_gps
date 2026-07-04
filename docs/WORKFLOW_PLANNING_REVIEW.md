# Workflow planning review — current system vs. the "Node Sorting" proposal

Status: Review / brainstorm. Scope: how OoLu should be trained to execute CLIs,
insert file locations, enforce human-defined SOPs and task dependencies,
construct nodes, and plan/navigate across nodes for personalized automation.

This document (1) reviews the planning machinery that exists in the repo today,
(2) reviews the uploaded `Node_Sorting.txt` prototype, and (3) proposes a more
general training architecture that unifies both, with a concrete build order.

---

## 1. What the repo has today: three planning subsystems, three vocabularies

### 1.1 `graph/` — the navigation loop (LangGraph)

`WorkflowGPS` runs a **fixed 7-node topology**: plan → synthesize → execute →
classify → recalculate → finalize/halt. "Planning" here is not decomposition —
the LLM synthesizes **one monolithic Python script** per intent, and navigation
is failure-class routing (dep-heal re-run vs. re-synthesize vs. halt) plus tier
escalation.

Learning signals today:
- `ScriptCache`, keyed on the **exact intent string** (+ prompt/model/backend
  fingerprint). A cache hit requires the same sentence to be asked again.
- Dependency hints (`knowledge/`): learned import → package mappings, recorded
  on verified success. This is the healthiest learning loop in the repo —
  deterministic re-derivation, success-gated, small keyspace.

Limits: a failure anywhere re-synthesizes the **whole** script; nothing learned
about sub-task structure transfers between intents; there is no notion of two
steps being independent (no parallelism), no fallback routes.

### 1.2 `orchestrator/` — the governance phase machine

`WorkflowOrchestrator` is an 11-phase deterministic, resumable machine
(intake → clarification → grounding → route optimization → human control →
confirmation → approval → execution → monitoring → recovery → finalization).
Its strengths are exactly what `Node_Sorting.txt` lacks: typed pause points,
re-derived preflight gates, approval quotas, idempotency keys, an auditable
`RunState`.

But its **planner is a stub**: `SkillRegistryPlanner` emits one `Blueprint`
per registered skill — a **linear** action list with `cost = len(actions)` —
and `RegistryGrounder` produces identity edges. There is no dependency graph,
no parallel scheduling, no alternative routes, no learned route choice.
`grep` confirms: no topological sort or DAG scheduler exists anywhere in `src/`.

### 1.3 `skills/` — demonstration learning

The learning spine (scrub → compile → sandbox-verify → register) is genuinely
good: verification-gated registration, secret scrubbing before anything is
stored, stable skill ids so re-learning versions rather than duplicates.

But the compiler is **exact-only**: `compile_exact` freezes the demonstrated
action list, pins the workspace *before*-fingerprint as a hard precondition,
and emits `parameters=[]`. A learned skill therefore replays byte-for-byte in
the demonstrated workspace and generalizes to nothing — no slots, no file-path
templating, no cross-workspace reuse. `ReusableSkill` *has* `parameters`,
`preconditions`, `validators`, `recovery_actions` fields — the schema is ready
for generalization; the compiler just never populates them.

### 1.4 The integration gap

The three subsystems use **three different node vocabularies**
(`graph/` GraphState+script, `orchestrator/` Blueprint+ReservedAction,
`skills/` ReusableSkill+ActionEvent) and don't feed each other: the graph
engine's script cache doesn't know about skills; the orchestrator's planner
can't call the graph engine to synthesize a missing step; demonstration
learning can't produce anything the graph engine can navigate *through*.

---

## 2. Review of `Node_Sorting.txt`

What it gets right (and the repo lacks):

1. **Typed node contract** — `SlotSpec` (name, type, required, validator) +
   `build/extract/verify/reward` per node. This is the missing generalization
   unit: the compiler's frozen action lists become parameterizable.
2. **DAG execution with a readiness scheduler** — `SuperNode.execute` runs the
   ready set in a thread pool, cascades failure to dependents, and supports
   `before/parallel/fallback/alternative` edge relations (in the schema).
3. **Kernel-enforced budgets** — allow-listed commands + per-command `risk`
   debited against a `risk_budget`, and path sandboxing. The repo has the
   allowlist and the jail (`CliExecutionPolicy`) but not the budget.
4. **Learning from execution traces**, not only from demonstrations.

Where it is weak or buggy — worth knowing before adopting any of it:

- **Sequence memorization, not generalization.** `LearnedWorkflowModel` keeps a
  `Counter` of *entire verified sequences* per goal label and replays the mode.
  Unseen goal → `ValueError`. A one-step variation → a brand-new sequence with
  count 1. Nothing transfers between goals that share sub-structure.
- **Edge learning can't recover DAGs.** `predict_edges` counts pairwise
  *adjacent* relations in linear traces and drops non-`before` pairs, so a
  fan-out/fan-in workflow collapses to a chain — which is why the
  `engineering_optimize` edges are **hardcoded** right below the learned path.
  The learned component demonstrably doesn't carry the interesting case.
- **Implicit data flow defeats reordering.** Nodes read upstream output via
  `state.memory["scan_files"]` — a hardcoded producer name inside the
  consumer. The dependency structure the scheduler needs is hidden in string
  literals the scheduler can't see.
- **Transitive failure cascade deadlocks.** When A fails, only *direct*
  dependents are marked skipped; a grandchild stays pending with a parent that
  will never be "verified", so the loop hits `raise RuntimeError("deadlock…")`
  instead of cascading cleanly.
- **Concurrency hazards.** `state.memory` and the shared sqlite connection are
  mutated from pool threads without locks; several `verify` lambdas compare
  DB row counts that another node may be writing. The 0.05s `wait` timeout is
  a busy poll.
- **`fallback` / `alternative` relations are declared but never executed.**
- **Handcrafted rewards per node** (`len(output)/10`), and the objective is an
  unweighted mean — no credit assignment, and reward magnitude is meaningless
  across nodes.

Conclusion: adopt its **shapes** (slot contract, DAG scheduler, risk budget,
trace learning), not its **algorithms** (sequence counters, adjacency edges)
or its executor as-is.

---

## 3. Proposal: learn a typed capability graph, not sequences

The core reframe: **stop learning orderings; learn node contracts, and derive
orderings.** If every node declares what it *consumes* and *produces* (typed
slots + state predicates), then:

- **Task dependencies become derivable**: B depends on A iff B consumes a slot
  type A produces. Edges emerge from slot-type unification, not from counting
  which step happened to follow which in traces. Pairs with no data or SOP
  relation are **parallel by default** — the fan-out `Node_Sorting` had to
  hardcode falls out for free.
- **Human SOPs become constraints layered on top** of the derived graph, not
  the only source of structure.
- **Planning becomes retrieval + backward chaining**, which generalizes to
  goals never seen verbatim — the thing sequence counters can't do.

### 3.1 One node schema (unify the three vocabularies)

Merge `Node` (Node_Sorting), `ReusableSkill` (skills/) and `ReservedAction`
(orchestrator/) into a single contract; `ReusableSkill` is ~80% there already:

```
NodeContract:
  id, version, provenance (demo ids / synthesized / human-authored)
  consumes:  [Slot(name, type, role)]        # typed inputs, incl. file roles
  produces:  [Slot(name, type, role)]        # typed effects/artifacts
  preconditions: [ConstraintSpec]            # state predicates (exists today)
  body:      actions | script | sub-graph    # CLI argv, sandbox script, or nested nodes
  verify:    [ConstraintSpec]                # deterministic, authoritative
  risk:      float + classify_risk() class   # debited against plan budget
  stats:     Beta(successes, failures) per context bucket, cost EMA
```

Three body kinds matter: **actions** (replayed CLI, cheap, deterministic),
**script** (the graph/ engine synthesizes one node's script — not the whole
workflow), and **sub-graph** (a learned super-node). This lets the expensive
LLM path shrink over time: every verified synthesized node becomes a cached,
replayable node.

### 3.2 Training channels — three sources, one artifact

**(a) Demonstrations → slot induction (the generalization step the compiler
refuses today).** Keep exact-mode as the safe default, and add: when the user
demonstrates the *same named task* 2–3 times, diff the aligned action streams —
values that **vary** across demos become slots (with types inferred from shape:
path, date, email, number), values that stay **constant** become defaults.
This is classic programming-by-demonstration generalization and it directly
answers "inserting file locations": a path that varies across demos becomes a
`Slot(role=input_file)` rather than a frozen literal.

**(b) Execution traces → statistics, not sequences.** Replace the
whole-sequence `Counter` with per-node and per-pair statistics:

- per node: Beta posterior of success given a coarse context bucket
  (application, workspace fingerprint class, param shapes), cost/latency EMA;
- per pair (a, b): a **precedence matrix** p(a before b | both ran) across all
  traces — keep a hard edge only when the order is consistent (>~0.95) *and*
  there is a data-flow or SOP justification; consistently-ordered-but-
  unjustified pairs become soft (advisory) edges; everything else is parallel.

This fixes both Node_Sorting failure modes: DAG recovery from linear traces,
and transfer between goals that share nodes.

- selection among alternatives: **Thompson sampling** over the Beta posteriors
  instead of `Counter.most_common(1)` — same cheap bookkeeping, but it explores,
  handles drift, and is per-user by construction (the counts are the user's own
  history). That *is* the personalization mechanism; no model training needed.

**(c) Human SOPs → compiled constraints.** Give SOPs a first-class declarative
form (YAML/Markdown front-matter) and compile them into structures the engine
already enforces:

```yaml
sop: month-end-report
applies_to: {tags: [reporting]}
require_order: [export_data, validate_totals, publish]   # → hard edges
forbid: [{operation: "*.delete", unless_approved: true}] # → reserved actions
require_verify: [{node: publish, check: totals_match}]   # → validators
approval: {actions: [publish], approvers: 1}             # → HumanControlDecision
risk_budget: 0.5                                          # → kernel budget
```

Compilation targets all exist: `ConstraintSpec`, `ReservedAction.reserved`,
`HumanControlDecision`, the approval/confirmation pauses in the orchestrator.
Crucially, SOP edges and learned edges are **kept distinguishable**: learned
structure may be pruned by new evidence; SOP structure may only be changed by
the human who owns it. The preflight gate should re-derive SOP satisfaction
from the plan on every execution, exactly like `assert_execution_preflight`
does today.

### 3.3 File locations as typed, role-tagged slots

- Learn path **templates**, never literals: canonicalize demo paths against
  the workspace root (machinery exists in `skills/workspace.py` +
  fingerprints), extract naming patterns (`reports/{YYYY-MM}/summary.csv`).
- At plan time, a path slot resolves by: template expansion → glob candidates
  → rank by recency/fingerprint match → if ambiguous, emit a
  `ClarificationQuestion` (the `RequirementConstraintCompiler` flow already
  renders these) rather than guessing.
- Every resolved path is validated by the existing `CliExecutionPolicy`
  workspace jail before it reaches an argv. Extend `knowledge/scrubbing.py`
  to canonicalize home-directory prefixes so absolute user paths never enter
  stored skills.

### 3.4 Planner: retrieve → assemble → repair

1. **Retrieve** candidate nodes/sub-graphs for the goal (tag/embedding match
   over the registry), scored by posterior success in the current context.
2. **Assemble** by backward chaining from the goal's required artifacts
   through `produces`/`consumes` types; layer SOP edges; unresolved required
   slots become clarification questions; the result is a `Blueprint` that is
   a real DAG (`actions: list` → `nodes + edges`).
3. **Execute** with a readiness scheduler (port `SuperNode.execute`, fixed):
   `concurrent.futures.wait` without the busy-poll, transitive skip cascade,
   per-node timeout, per-node result checkpointed into `RunState` so pause/
   resume works mid-graph.
4. **Repair at node granularity** — this is the biggest efficiency win over
   today's whole-script recalculation loop: failed node → `fallback` edge if
   one exists → re-synthesize *that node only* via the graph/ engine →
   tier-escalate → halt per `EdgePolicy`. The existing error-class taxonomy
   (recalculable vs. halting) carries over unchanged.
5. **Memoize per node**, not per intent: cache key = node id + slot-binding
   fingerprint + environment fingerprint. Sub-tasks recur across different
   intents, so hit rates rise where the current intent-string `ScriptCache`
   almost never hits.

### 3.5 Verification and reward: verify decides, reward only ranks

Keep verification **binary, deterministic, and authoritative** (exit codes,
artifact existence/hashes, SOP checks) — it gates registration, caching, and
earnings (the Nodeplace roadmap already requires platform-verified success).
Use scalar reward **only to rank alternatives that all verified**: cost,
latency, and — the strongest personalization signal available — the
**human-edit distance** (how much the user corrected the output afterward,
observable via the recorder). Never let a learned reward override a failed
verify; that separation is what keeps "training" from drifting into
plausible-but-wrong automation.

### 3.6 Trust ladder for learned nodes

Promotion gates, aligned with the learner's verification gating and the
risk model:

1. **shadow** — learned, never auto-selected; visible in plans as a suggestion.
2. **suggested** — planner may propose it; runs only after user confirmation
   (existing confirmation pause).
3. **trusted** — auto-runs within the SOP risk budget; `reserved` actions
   still require approval regardless of trust.

Promotion requires N verified successes in context; any verify failure demotes
one level. This gives users a legible answer to "why did it do that on its
own?" — because they promoted it.

---

## 4. Recommended build order

| # | Change | Where | Why first |
| - | ------ | ----- | --------- |
| 1 | DAG `Blueprint` (nodes + edges) + readiness scheduler with transitive skip, timeouts, checkpointed per-node results | `orchestrator/state.py`, new `orchestrator/scheduler.py` | Unblocks everything; port of `SuperNode.execute` with its bugs fixed |
| 2 | Slot induction in the compiler (multi-demo diff → parameters; path templating) | `skills/compiler.py`, `skills/workspace.py` | Turns exact replays into reusable nodes; delivers "insert file locations" |
| 3 | SOP compiler: YAML → ConstraintSpecs + hard edges + approval gates + risk budget | new `skills/sop.py`, `orchestrator/planner.py` | Human dependencies enforced by the machinery that already gates execution |
| 4 | Node-granular script cache + single-node re-synthesis via the graph engine | `cache/`, `graph/`, planner seam | Replaces whole-script recalculation; big cost win |
| 5 | Trace statistics: Beta posteriors + precedence matrix; Thompson selection among alternatives | new `knowledge/traces.py` | Replaces sequence memorization with transferable, personalized learning |
| 6 | Unify `Node`/`ReusableSkill`/`ReservedAction` into one `NodeContract` | `skills/models.py` outward | Do last, mechanically, once 1–5 prove the shape |

Items 1–3 are pure-Python, deterministic, and testable with the existing
stub executors; none requires a model in the loop.
