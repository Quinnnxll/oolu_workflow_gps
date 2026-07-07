"""Issue 7's proof harness: can the shipped planner actually find routes?

The doubt, stated fairly: node routing is a multi-dimensional search over
a graph with an enormous number of allowable connections, plus a reusable
cache problem — surely that takes a transformer navigating node IDs, with
execution scores as its training signal?

This harness measures what the shipped architecture actually does on a
synthetic marketplace built to be hostile:

- a hidden production CHAIN of depth D (the route that must be found),
- K competing providers at EVERY step (route space = K^D distinct
  end-to-end routes — the "infinite allowable connections" axis),
- M distractor nodes producing unrelated slots (the "search" axis),
- providers with hidden true reliabilities, revealed only by executing
  (the "scoring machine as feedback" axis),
- and route reuse (the "reusable cache" axis).

Run it:  python benchmarks/route_scale.py

The claims it measures:
1. SEARCH — backward-chaining over typed slots resolves D slots with
   O(D x library) work; it never enumerates the K^D route space. The
   type system is the map; search cost grows linearly, not exponentially.
2. LEARNING — a Beta posterior per node (TWO numbers) updated by
   verified outcomes, sampled by Thompson, converges onto the reliable
   providers. That is the "much fewer parameters than an LLM" learner —
   it is already installed, and its regret is O(log T) by construction.
3. CACHE — an assembled route is itself a NodeContract: registered back,
   it wins the next assembly as ONE pick, with the posterior it earned.
4. CONTAINMENT — model advice (any ProposalModel, LLM or tiny
   transformer) enters as bounded pseudo-observations in the SAME
   posterior. Garbage advice changes nothing; good advice decides
   thin-history ties; evidence always wins in the end.
"""

from __future__ import annotations

import logging
import random
import time

from oolu.orchestrator.assembler import ContractAssembler, GoalSpec
from oolu.skills.contract import (
    ActionsBody,
    NodeContract,
    NodeStats,
    Slot,
)
from oolu.skills.models import ActionEvent


def slot(name: str) -> Slot:
    return Slot(name=name, value_type="str")


def _body(name: str) -> ActionsBody:
    return ActionsBody(
        actions=[
            ActionEvent(
                correlation_id=name, adapter="http", operation="get"
            )
        ]
    )


def provider(step: int, k: int, stats: NodeStats | None = None) -> NodeContract:
    """One of K interchangeable providers turning s{step-1} into s{step}."""
    name = f"step{step}-provider{k}"
    return NodeContract(
        id=name,
        name=name,
        provenance="marketplace",
        consumes=[slot(f"s{step - 1}")],
        produces=[slot(f"s{step}")],
        body=_body(name),
        stats=stats,
    )


def distractor(i: int) -> NodeContract:
    name = f"noise{i}"
    return NodeContract(
        id=name,
        name=name,
        provenance="marketplace",
        consumes=[slot(f"nx{i}")],
        produces=[slot(f"ny{i}")],
        body=_body(name),
    )


def marketplace(depth: int, width: int, noise: int) -> list[NodeContract]:
    library = [
        provider(step, k)
        for step in range(1, depth + 1)
        for k in range(width)
    ]
    library += [distractor(i) for i in range(noise)]
    return library


def chain_goal(depth: int) -> GoalSpec:
    return GoalSpec(name="the-chain", want=[slot(f"s{depth}")], have=[slot("s0")])


# --------------------------------------------------------------------------- #
# Claim 1: search scales linearly while the route space explodes.              #
# --------------------------------------------------------------------------- #
def measure_search(depth: int = 6, width: int = 8, noises=(0, 1_000, 5_000)):
    print(f"\n== SEARCH: depth {depth}, {width} rivals/step "
          f"(route space {width}**{depth} = {width**depth:,}) ==")
    for noise in noises:
        library = marketplace(depth, width, noise)
        assembler = ContractAssembler(library)
        started = time.perf_counter()
        result = assembler.assemble(chain_goal(depth))
        elapsed = (time.perf_counter() - started) * 1000
        assert result.complete and len(result.selected) == depth
        print(
            f"  {len(library):>6,} nodes -> route of {len(result.selected)} "
            f"picks in {elapsed:7.1f} ms (complete: {result.complete})"
        )


# --------------------------------------------------------------------------- #
# Claim 2: the two-parameters-per-node learner converges on reliability.       #
# --------------------------------------------------------------------------- #
def learning_loop(
    depth: int = 3,
    width: int = 5,
    rounds: int = 400,
    seed: int = 7,
):
    """Thompson-sampled assembly against hidden reliabilities, with every
    execution outcome written back into the posterior — the feedback
    training loop, exactly as the runtime wires it (verified stats in,
    Beta posterior out)."""
    rng = random.Random(seed)
    world = random.Random(seed + 1)
    # Hidden truth: the LAST provider of each step (worst placed for
    # every deterministic tie-break) is the reliable one.
    best = width - 1
    true_rate = {
        f"step{step}-provider{k}": 0.95 if k == best else 0.30 + 0.05 * k
        for step in range(1, depth + 1)
        for k in range(width)
    }
    counts: dict[str, list[float]] = {name: [0.0, 0.0] for name in true_rate}

    def library() -> list[NodeContract]:
        return [
            provider(
                step,
                k,
                stats=NodeStats(
                    successes=counts[f"step{step}-provider{k}"][0],
                    failures=counts[f"step{step}-provider{k}"][1],
                ),
            )
            for step in range(1, depth + 1)
            for k in range(width)
        ]

    assembler = ContractAssembler(library, rng=rng)
    best_picks: list[int] = []
    for _ in range(rounds):
        result = assembler.assemble(chain_goal(depth))
        assert result.complete
        hits = sum(
            1 for name in result.selected if name.endswith(f"provider{best}")
        )
        best_picks.append(hits)
        for name in result.selected:
            outcome = world.random() < true_rate[name]
            counts[name][0 if outcome else 1] += 1.0
    quarter = rounds // 4
    early = sum(best_picks[:quarter]) / (quarter * depth)
    late = sum(best_picks[-quarter:]) / (quarter * depth)
    return early, late


def measure_learning():
    print("\n== LEARNING: hidden reliabilities, outcomes fed back "
          "(2 parameters per node) ==")
    early, late = learning_loop()
    print(f"  best-provider pick rate: first quarter {early:0.2f} "
          f"-> last quarter {late:0.2f}")
    return early, late


# --------------------------------------------------------------------------- #
# Claim 3: an assembled route is itself the reusable cache entry.              #
# --------------------------------------------------------------------------- #
def measure_cache(depth: int = 6, width: int = 8, noise: int = 1_000):
    print("\n== CACHE: the assembled route re-registered as one node ==")
    library = marketplace(depth, width, noise)
    first = ContractAssembler(library).assemble(chain_goal(depth))
    assert first.complete
    # The route ran and verified: it re-enters the library as ONE contract
    # carrying the posterior it earned. Composition memoized.
    cached = first.contract.model_copy(
        update={"stats": NodeStats(successes=3.0, failures=0.0)}
    )
    second = ContractAssembler(library + [cached]).assemble(chain_goal(depth))
    assert second.complete
    print(f"  first assembly:  {len(first.selected)} picks")
    print(f"  after reuse:     {len(second.selected)} pick(s) "
          f"({second.selected})")
    return len(first.selected), len(second.selected)


# --------------------------------------------------------------------------- #
# Claim 4: advice is contained — garbage changes nothing, evidence wins.       #
# --------------------------------------------------------------------------- #
class _GarbageAdvice:
    def propose(self, **kwargs):
        raise RuntimeError("the model is down / hallucinating node ids")


class _WrongAdvice:
    """Insists (endorsement 1.0) on an unreliable provider, every pick."""

    def propose(self, *, candidates, **kwargs):
        from oolu.orchestrator.assembler import Proposal

        worst = min(candidates, key=lambda c: c.name)
        return Proposal(weights={worst.id: 1.0})


def measure_containment(depth: int = 3, width: int = 5, rounds: int = 400):
    print("\n== CONTAINMENT: a wrong model only delays the posterior ==")
    rng = random.Random(11)
    world = random.Random(12)
    best = width - 1
    true_rate = {
        f"step{step}-provider{k}": 0.95 if k == best else 0.30
        for step in range(1, depth + 1)
        for k in range(width)
    }
    counts: dict[str, list[float]] = {name: [0.0, 0.0] for name in true_rate}

    def library() -> list[NodeContract]:
        return [
            provider(
                step,
                k,
                stats=NodeStats(
                    successes=counts[f"step{step}-provider{k}"][0],
                    failures=counts[f"step{step}-provider{k}"][1],
                ),
            )
            for step in range(1, depth + 1)
            for k in range(width)
        ]

    # A model that always endorses the worst candidate, at full strength.
    assembler = ContractAssembler(
        library, rng=rng, proposal_model=_WrongAdvice()
    )
    best_picks: list[int] = []
    for _ in range(rounds):
        result = assembler.assemble(chain_goal(depth))
        hits = sum(
            1 for n in result.selected if n.endswith(f"provider{best}")
        )
        best_picks.append(hits)
        for name in result.selected:
            outcome = world.random() < true_rate[name]
            counts[name][0 if outcome else 1] += 1.0
    quarter = rounds // 4
    late = sum(best_picks[-quarter:]) / (quarter * depth)
    print(f"  with a model endorsing the WORST provider every pick: "
          f"last-quarter best-pick rate {late:0.2f}")

    # And a dead model changes nothing at all (its expected warnings
    # silenced: raising IS this model's job here).
    logging.getLogger("oolu.orchestrator.assembler").setLevel(
        logging.CRITICAL
    )
    library_static = marketplace(6, 8, 0)
    plain = ContractAssembler(library_static).assemble(chain_goal(6))
    guarded = ContractAssembler(
        library_static, proposal_model=_GarbageAdvice()
    ).assemble(chain_goal(6))
    same = plain.selected == guarded.selected
    print(f"  with a model that only raises: identical route ({same})")
    return late, same


if __name__ == "__main__":
    print("Route-finding proof harness (Issue 7)")
    measure_search()
    measure_learning()
    measure_cache()
    measure_containment()
    print("\nDone.")
