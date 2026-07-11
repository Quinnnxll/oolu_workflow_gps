# Can this architecture actually find the nodes and the routes? — the proof

Issue 7 raised the right doubt, so it deserves a straight answer with
measurements, not reassurance:

> "It's not just Google search plus Google Maps. It's multi-dimensional
> search, map nodes with infinite allowable connections to choose from,
> and a reusable-cache problem. It seems there must be some
> transformer-type algorithm to navigate this, with an execution-result
> scoring machine as the feedback training loop — one with far fewer
> parameters than an LLM. Current LLMs are not trained on node IDs, so
> they cannot build the route like a sentence. If you still think the
> current algorithm + LLMs can handle it, prove it."

Short answer: **yes — and the premise about LLMs is correct, which is
exactly why the architecture never asks an LLM to build a route.** The
"transformer-type algorithm with execution-result feedback and far fewer
parameters than an LLM" the issue calls for **already exists in the
engine** — it just isn't a transformer, because the problem decomposes
into two parts, and each part has a solver that is *provably* efficient
where a transformer would only be *plausibly* efficient:

| Sub-problem | What actually solves it | Parameters |
|---|---|---|
| WHICH routes are possible (search) | Typed backward-chaining over `consumes`/`produces` slots (`ContractAssembler`) | 0 — the type system is the map |
| WHICH possible route to take (choice) | Beta posterior per node + Thompson sampling, fed by platform-verified execution outcomes | **2 per node** (success/failure counts) |
| WHAT the user means (semantics) | The LLM — intake to a typed brief, and picking from a **numbered menu** of already-enumerated routes | external, advisory |
| Reusable cache | An assembled route **is** a `NodeContract`: it re-enters the library as one node carrying the posterior it earned | inherited |

Everything below is measured by `benchmarks/route_scale.py` and pinned by
`tests/test_route_finding_proof.py`, on a synthetic marketplace built to
be hostile: a hidden production chain of depth 6, **8 rival providers at
every step** (8⁶ = 262,144 distinct end-to-end routes), thousands of
distractor nodes, and hidden reliabilities revealed only by executing.

## 1. Search: the type system prunes what a transformer would have to learn

The doubt pictures route-finding as navigating a graph with "infinite
allowable connections". But connections here are **not** infinite-and-
unlabeled the way road segments or web links are: every node declares
typed slots, and a connection is *allowable* exactly when a produced slot
unifies with a consumed slot (`Slot.matches`: name + value type + role).
Finding a route is therefore backward-chaining: want `s6`, find producers
of `s6`, their inputs become the new wants, repeat. Cost is
O(depth × library), never O(routes):

```
== SEARCH: depth 6, 8 rivals/step (route space 8**6 = 262,144) ==
      48 nodes -> route of 6 picks in   1.2 ms
   1,048 nodes -> route of 6 picks in   3.3 ms
   5,048 nodes -> route of 6 picks in  17.9 ms
```

The 262,144-route space is **never walked** — six slot resolutions close
the plan. Growth is linear in library size (and the linear factor is an
un-indexed scan; a slot→producers index makes it near-constant when the
library reaches six digits). This is the classical planning result the
issue's "Google Maps" analogy points at: maps need A\* because road
graphs have no types; typed dataflow graphs come with their own
admissible pruning built in.

## 2. Choice: the "scoring machine as feedback training loop" is installed — with 2 parameters per node

Among 8 rivals per step, which to bind? This is not a search problem, it
is a **contextual bandit**, and the engine already runs the textbook
solution: every node carries a Beta posterior over success
(`NodeStats.successes/failures` — two numbers), updated **only by
platform-verified execution outcomes** (the metering/attribution
pipeline; self-reported quality never enters), and assembly Thompson-
samples that posterior (`ContractAssembler(rng=...)`, the gateway's
explore mode). Measured, with the reliable provider deliberately placed
LAST in every deterministic tie-break so nothing but learning can find
it:

```
rounds   0-10 : best-pick rate 0.27   (chance = 0.20)
rounds  10-30 : best-pick rate 0.73
rounds  30-60 : best-pick rate 0.92
rounds  60-100: best-pick rate 0.98
rounds 300-400: best-pick rate 1.00
```

That is the requested "execution result scoring machine as feedback
training loop", with a parameter count of **2 per node** — not millions —
and it comes with something no transformer offers: Thompson sampling's
O(log T) regret guarantee, per-tenant personalization for free (each
tenant's runs feed their own posteriors), and updates that cost an
integer increment instead of a gradient step.

## 3. The reusable cache: routes become nodes

A route, once assembled, is not a transient plan: `AssemblyResult.contract`
is itself a `NodeContract` (`SubgraphBody`) with the goal's `have` as its
inputs and the goal's `want` as its outputs. Registered back into the
library with the verified history it earned, it competes as ONE node:

```
== CACHE: the assembled route re-registered as one node ==
  first assembly:  6 picks
  after reuse:     1 pick  (['the-chain'])
```

This is the issue's "route like a sentence" — except the sentence, once
spoken and verified, becomes a **word**. Composition memoizes. The same
mechanism runs at every scale: learned skills, gap scripts (memoized by
the script runner), contributed marketplace nodes, and the trace store
grading every run so reuse is evidence-ranked, not merely remembered.

## 4. Where LLMs sit — and why they never touch node IDs

The issue is right that LLMs are not trained on node IDs and cannot
reliably emit a route as a token sequence. The architecture was built
around exactly that limitation; there is **no code path in which a model
authors a route**:

- **Intake** (`RouterIntakeModel`): the LLM turns free text into a typed
  brief — semantics, its real strength. It proposes slot values, and only
  values the user *actually said* bind as user-sourced.
- **Route choice** (`ModelRouteOptimizer`): the deterministic planner
  enumerates the viable blueprints first; the model picks **a number from
  a menu**. An out-of-range answer, a timeout, or no key at all falls
  back to least-cost. It cannot resurrect an excluded route.
- **Producer advice** (`ProposalModel`): advice enters the SAME Beta
  posterior as pseudo-observations, clamped to a bounded strength
  (`DEFAULT_PROPOSAL_STRENGTH = 3` — worth three verified runs, no more).
  Measured containment:

```
== CONTAINMENT ==
  a model endorsing an UNRELIABLE provider at full strength, every pick:
      last-quarter best-pick rate still 0.99  (evidence out-votes advice)
  a model that only raises exceptions:
      the assembled route is bit-identical    (advice is optional)
```

## 5. Where the issue's small transformer would genuinely help — and its reserved socket

The doubt is not wrong about everything a learned model could add; it is
wrong about *where* the leverage is. A small learned ranker would help
with what counts and posteriors cannot see:

- **cold start** — a brand-new node with zero history, where slot-name
  semantics ("tax-form-pdf" ≈ "filing-document") predict fit;
- **cross-goal generalization** — transferring what worked for one goal
  shape to a structurally similar one;
- **context-conditioned choice** — the best provider differing by
  tenant, time, or payload characteristics.

The architecture already reserves its seat: the `ProposalModel` port.
`TraceProposalModel` sits in it today — a learned, non-LLM,
counting-based model over the caller's own run history (goal-first, then
co-selection, then global). A small transformer trained on trace-store
outcomes (the exact "execution result scoring" signal the issue names)
drops into the same port, and the containment property above is what
makes adopting it SAFE: bounded prior strength means it can only decide
thin-history ties and speed up cold starts — it can never override
verified evidence, hallucinate a node ID into a plan (unknown IDs are
dropped), or take the marketplace down by failing. Upgrade path, not
re-architecture.

## Verdict

- Finding routes: **proved** — linear-cost typed search over a
  quarter-million-route space, milliseconds at 5,000 nodes.
- Learning routes from execution results: **proved** — the 2-parameter-
  per-node bandit converges from chance (0.27) to 1.00 within ~100
  feedback rounds, with a regret guarantee.
- Reusable cache: **proved** — routes re-enter the library as single
  nodes and win reuse on earned evidence.
- LLM inability to build routes: **agreed, and designed for** — models
  only translate semantics and choose from menus; advice is bounded and
  optional, measured to be harmless even when adversarial.
- The small learned router: **now in the socket** —
  `TinyTransformerProposalModel` (orchestrator/ranker.py), a pure-Python
  one-head cross-attention ranker over hashed token embeddings (~10k
  parameters), trained online on the trace store's outcomes. It ships
  behind `LearnedProposalStack`: Beta counts outrank it wherever both
  have an opinion, so it decides exactly what the proof scoped for it —
  cold starts (new-node name/slot semantics), cross-goal generalization
  (shared token embeddings), and context-conditioned choice (one ranker
  per tenant's trace context). An untrained ranker answers "no opinion",
  and the port's containment (bounded prior strength, unknown ids
  dropped, exceptions downgrade to evidence-only) holds unchanged.
- The replay harness: **built, and the gate is real** —
  `orchestrator/replay.py` replays a corpus prequentially (test, then
  train — nothing predicts from its own future), scores every step by
  Brier with abstentions at the neutral coin, and splits cold
  (never-seen nodes) from warm. On the seeded audition world the
  shipped stack beats the counting baseline exactly where this section
  scoped it (cold ~0.20 vs the baseline's forced 0.25, warm identical),
  and the same gate — `earns_its_cost` — rejects a model that endorses
  everything. Any future occupant of the seat (Mamba/SSM, a bigger
  transformer) auditions here before it may bill an inference.

Reproduce everything: `python benchmarks/route_scale.py` and
`python benchmarks/proposal_replay.py`; the same claims run in CI as
`tests/test_route_finding_proof.py` and `tests/test_proposal_replay.py`.
