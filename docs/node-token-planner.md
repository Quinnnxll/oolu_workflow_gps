# The node-token planning model — where a token stops being a word

Status: Prepared (not trained). Scope: the vocabulary, corpus, architecture,
and baseline that let OoLu plan a mission by *generating a sequence of node
tokens*, and that scale from a runnable reference to 3B/8B/30B as the node and
route database grows.

This doc is the map of `src/oolu/planner/`. Read `docs/route-finding-proof.md`
first — it establishes why routing needs no language model, and reserves the
socket this package prepares to fill at scale. Read `docs/adr/0006-node-token-
planning-model.md` for the decision and the containment argument in brief.

---

## 1. The idea in one line

An LLM's vocabulary is words; **this model's vocabulary is nodes and routes.**
Because the global project graph already exists and every run is
platform-verified, the structure of good plans is data we own. Tokenize the
node and its place in a workflow, and planning a large mission becomes
next-token generation — cheap, bounded in compute, and improving with every
recorded run instead of re-reasoned from language each time.

```
plan for a goal  =  [BOS] [goal-token] node₁ node₂ … nodeₙ [EOS]
```

The frontier model still does what it is best at — turning a user's words into
a typed goal. From that goal, the plan is generated in nodes.

## 2. The vocabulary (`planner/vocab.py`) — the new primitive

`NodeVocabulary` is a growable, stable bijection between node/route keys and
token ids.

- **A token is a node or a route.** Each key is `route:{name}` — the exact key
  the trace store grades outcomes under. A composed route re-enters the
  library as a single `NodeContract` (route-finding-proof §3), so the same key
  space already covers routes: *one vocabulary tokenizes both.*
- **Goals are not nodes.** A goal is free user text; it conditions the plan but
  never appears in it. Goals hash (crc32, deterministic across machines) into a
  bounded band of `<goal:N>` tokens. So vocabulary growth is driven by the
  node/route database — the thing we want to scale with — not by the unbounded
  space of sentences users type.
- **Ids are stable; growth is append-only.** A new node appends at the next id
  and nothing renumbers. A frozen vocabulary pins a checkpoint's tokenization;
  an unknown node at inference maps to `<unk>` rather than corrupting the other
  tokens in a sequence. Identity is the key — the same discipline the trace
  store keeps ("rename a node and it starts life over").

The reserved layout, frozen before any node is seen:

```
id 0..3     <pad> <bos> <eos> <unk>          (special)
id 4..4+G   <goal:0> … <goal:G-1>            (goal band, size = goal_buckets)
id 4+G..    route:… route:…                  (nodes and routes, append-only)
```

The whole thing serializes to portable JSON (only the node keys are stored;
the reserved and goal bands are implied by `goal_buckets`), so one file pins
tokenization for the pure-Python baseline today and a 30B checkpoint tomorrow.

## 3. The corpus (`planner/sequences.py`)

`knowledge.corpus` already turns the trace store's run log into (goal, prefix →
next node) examples in string space. `planner.sequences` lifts the same corpus
into token-id space:

- `encode_run(run, vocab)` → `[BOS] [goal] steps… [EOS]` as ids.
- `build_vocabulary(runs)` → a vocabulary covering every node key in a corpus.
- `export_token_jsonl(store, path)` → writes token-id sequences oldest-first,
  plus `path.vocab.json`. The two files together are a complete, portable
  training input for a job that never touches this repo.

Only verified runs export by default: a plan the platform could not verify is
not an example of how to plan.

## 4. The architecture (`planner/config.py`) — a ladder, not a rewrite

`PlannerConfig` describes a standard pre-norm decoder-only transformer. The
only unusual thing about it is the vocabulary — the tokens are nodes, not
word-pieces — which is a *data* decision, so the scaling behaviour is the
ordinary, well-understood kind.

`parameter_count(cfg)` is exact arithmetic (token + positional embeddings;
per layer four attention projections, a two-matrix MLP, two LayerNorms; a
final norm; a tied or untied head). That makes the rungs checkable:

| preset | d_model | layers | heads | parameters |
| ------ | ------- | ------ | ----- | ---------- |
| tiny   | 256     | 4      | 4     | ~5.3M (runnable reference) |
| s3b    | 2560    | 32     | 20    | ~2.9B |
| s8b    | 4096    | 36     | 32    | ~7.8B |
| s30b   | 7168    | 48     | 56    | ~30.5B |

Moving up a rung is a four-number change. `test_planner_config.py` asserts
each preset lands in its named band, so "start at 3B, then 8B, then 30B, as
more users, more nodes, and more routes accumulate" is a real ladder.

The real module, `planner/torch_model.py`, reads the same config and lives
behind the `workflow-plan` extra (`pip install 'oolu[workflow-plan]'`),
imported lazily exactly like `representative/trainer/run_sft.py`. Nothing in
CI instantiates a billion-parameter module; `parameter_count` is the check,
and `torch_model.num_parameters` asserts the arithmetic and the built module
never drift.

## 5. The baseline (`planner/baseline.py`) — generation, today, in pure Python

A 30B transformer is not what proves the idea; a working autoregressive
planner over node tokens is. `MarkovPlanner` is a goal-conditioned back-off
bigram over node tokens, trained only on verified runs. Two capabilities, one
model:

- `plan(goal)` — the new one. Roll out node tokens until `<eos>`: whole-mission
  planning in one cheap pass. On a corpus where a goal is reliably done by a
  three-node chain, `plan` regenerates that chain (`test_planner_baseline.py`,
  `benchmarks/plan_tokens.py`).
- `PlannerProposalModel` — the safe one. It adapts the same next-node
  distribution to the assembler's `ProposalModel` protocol, so the generative
  planner enters the marketplace as **bounded advice**: endorsements fold into
  the Beta posterior at `DEFAULT_PROPOSAL_STRENGTH`, unknown ids drop,
  exceptions downgrade to evidence-only. It plugs the real `ContractAssembler`
  and assembly still completes on verified history alone.

The baseline is deliberately humble — the modest end of the ladder. A trained
transformer trains on the same exported corpus and must beat it in the replay
harness (`orchestrator/replay.py`, gate `earns_its_cost`) before it may bill an
inference. Same audition, one rung up.

## 6. Why a generative planner is safe here

`route-finding-proof.md` argues a model must never *author* a route as a raw
token sequence. This package keeps that invariant: the planner's plan is a
**prior, not a commitment.** The type system still enumerates routes by typed
backward-chaining; verified outcomes still choose among them via Thompson
sampling; the planner only proposes, through a port whose containment is
already proven. The model proposes; the type system disposes.

What the sequence model adds is leverage the counts and posteriors cannot
give: a cheap whole-mission proposal for **cold start** (a fresh goal with no
matching history) and **long-horizon "vision-level" missions** (where per-slot
ranking is myopic and re-asking a frontier model per step is expensive). That
is exactly the territory §5 reserved a learned seat for — now reachable by a
model that plans in nodes.

## 7. What is prepared, and what is not

Prepared and tested, in the base install:

- the node/route vocabulary and its persistence;
- the token-id corpus and portable exporter;
- the scaling-ladder config and exact parameter-count math;
- a pure-Python generative baseline that both plans and plugs the seam.

Not done, deliberately:

- no training run — the 3B/8B/30B checkpoints train off-box on the exported
  corpus when it reaches scale;
- nothing wired into the running engine by default — the planner is consulted
  only when an operator chooses to, through the contained port.

The mission was to *prepare* the model whose tokens are nodes and routes, so
that planning large missions becomes achievable within a compute budget as the
database grows. The seam, the vocabulary, the data pipeline, and a working
baseline are in place; scale is a corpus and a config away.
