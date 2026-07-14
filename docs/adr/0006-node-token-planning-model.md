# ADR-0006: A node-token planning model — tokens that are nodes and routes

- Status: Accepted
- Date: 2026-07-14

## Context

Today a frontier model reasons out, in words, which node to build and what
code goes inside it. That is expensive per mission, it does not improve with
use, and it scales badly to large missions: the model re-derives structure it
has seen a hundred times. We already have a **global project graph** and a
verified execution history, so the structure of good plans is *data we own* —
not something to re-reason from language every time.

`route-finding-proof.md` established how routing works without a language
model: typed backward-chaining enumerates routes (0 parameters), a Beta
posterior per node plus Thompson sampling chooses among them (2 parameters per
node), and an assembled route re-enters the library as one `NodeContract`
(the cache). §5 of that proof reserved a socket — the `ProposalModel` port —
for a small learned model to help with what counts cannot see: cold start,
cross-goal generalization, and context-conditioned choice. A pure-Python
one-head ranker (`orchestrator/ranker.py`) sits there now, tokenizing the
*words inside* node names.

The doubt this ADR answers is sharper than "add a learned ranker": *make the
tokens the nodes themselves.* An LLM's vocabulary is words; if a planning
model's vocabulary is the node/route database, then planning a mission is
generating a sequence of node tokens — "route like a sentence" — and the model
scales like a language model does (3B → 8B → 30B) as the vocabulary and the
corpus grow. This is the preparation, not the training run.

## Decision

Prepare a **node-token planning model** as a new, self-contained package
(`oolu.planner`), keeping the deterministic type-system planner authoritative
and every model output advisory.

- **Vocabulary (`planner/vocab.py`) — the new primitive.** A `NodeVocabulary`
  maps each node/route key (`route:{name}`, the same key the trace store
  grades) to one stable token id. Because a composed route re-enters the
  library as a single `NodeContract`, one vocabulary tokenizes both nodes and
  routes. Goals are free text, so they never enter the vocabulary as tokens;
  they condition a plan through a **bounded band** of hashed goal tokens. Ids
  are append-only and freezable, so a checkpoint keeps its meaning as the
  marketplace grows and an unknown node degrades to `<unk>` rather than
  corrupting a sequence.

- **Corpus (`planner/sequences.py`).** The trace store's verified runs lift
  into token-id sequences `[BOS] [goal] node… [EOS]`; `export_token_jsonl`
  writes them plus the paired vocabulary as a portable training input. Model
  training happens *elsewhere*, on data this exporter makes portable — the
  same "build the seam and the pipeline here, train off-box" stance as the
  representative trainer.

- **Architecture (`planner/config.py`) — the scaling ladder.** A plain,
  JSON-serializable `PlannerConfig` describes a standard pre-norm decoder-only
  transformer. `parameter_count` is exact arithmetic, so the rungs
  `tiny`/`s3b`/`s8b`/`s30b` (2.9B / 7.8B / 30.5B) are checkable in a unit
  test. Scaling is a four-number change, never a rewrite. The real module
  (`planner/torch_model.py`) reads the same config and lives behind the
  `workflow-plan` extra; nothing in CI instantiates it.

- **Baseline (`planner/baseline.py`).** A pure-Python autoregressive
  back-off planner over node tokens, trained on verified runs, that
  *generates* a whole plan (`MarkovPlanner.plan`). It is the modest occupant
  a trained transformer must beat before billing an inference — the same role
  the counting baseline plays for the ranker. It also adapts to the
  `ProposalModel` port (`PlannerProposalModel`), so the generative planner
  enters the marketplace as bounded advice.

## Why this does not violate "a model must never author a route"

`route-finding-proof.md` argues, correctly, that a model must not *author* a
route as a raw token sequence — unknown ids, hallucinated steps, and
untyped transitions would corrupt execution. This ADR keeps that invariant:

- The planner's output is a **prior, not a commitment.** It plugs the
  existing `ProposalModel` containment unchanged: endorsements fold into the
  Beta posterior at `DEFAULT_PROPOSAL_STRENGTH` (worth three verified runs),
  unknown ids are dropped, and any exception downgrades assembly to
  verified-history-only. A generated plan is a *suggestion the type system
  then enumerates, verifies, and can overrule.*
- The type system still finds the routes; verified outcomes still choose
  among them. What the sequence model adds is a **cheap, whole-mission
  proposal in one pass** — the leverage for cold start and long-horizon
  ("vision level") missions that per-slot ranking, which is myopic, cannot
  give.

So the model proposes; the type system disposes. That is the same bargain the
ranker already lives under, extended from "score one candidate" to "sketch a
whole plan".

## Consequences

- The node/route vocabulary, corpus exporter, scaling-ladder math, and a
  working generative baseline all run in the **base install**, dependency-free
  and tested. Only the real transformer needs torch, and only off-box.
- The training curriculum named in the mission — 3B, then 8B, then 30B — is
  now a config lookup with a verified size, waiting on corpus scale.
- Adoption risk is zero today: nothing is wired into the running engine by
  default. When an operator consults the planner, it enters through a port
  whose containment is already proven.
- If a trained checkpoint never beats the baseline in the replay harness, the
  seam is inert and the baseline keeps generating plans for free.
