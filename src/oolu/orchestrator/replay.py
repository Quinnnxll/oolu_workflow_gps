"""The ProposalModel replay harness — a learned ranker auditions here.

route-finding-proof.md §5 (and ``TraceProposalModel``'s own docstring)
made a promise: anything smarter dropped into the ``ProposalModel`` seat
"must beat this baseline in the replay harness to earn its inference
cost." This module IS that harness.

The evaluation is prequential — test, THEN train — because that is how
these models actually live: both the counting baseline and the tiny
transformer read the trace store live, so the honest question is "given
everything recorded so far, how well do you predict the NEXT run?"
Replaying a corpus oldest-first, each run past a warmup is first scored
(the model predicts each step's success from the history it has) and
then revealed (recorded into the model's store). Nothing is ever
predicted from its own future.

Scoring is the Brier score — mean squared error between the predicted
weight and what actually happened — with one deliberate convention: a
model with NO opinion on a step is scored at the neutral prior 0.5
(Brier 0.25). Abstaining is honest, but it is not free: the counting
baseline abstains on every node it never saw, and exactly there is the
cold-start territory the proof reserved the learned seat for. The
report splits cold (first-ever-seen nodes) from warm so the win, if
any, is visible where it was promised.

``earns_its_cost`` is the gate in code. ``synthetic_semantic_runs``
builds the seeded audition world: providers whose NAMES carry their
technology ("…-pandas-…" works, "…-regex-…" doesn't), then a cold phase
of brand-new provider names sharing those tokens — counts must abstain,
semantics can answer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ..knowledge.traces import (
    NodeObservation,
    RecordedRun,
    TraceStore,
    route_node_key,
)
from ..skills.contract import ActionsBody, NodeContract, Slot
from ..skills.models import ActionEvent
from .assembler import GoalSpec

# The neutral prior an abstention is scored at: no opinion = a coin.
NEUTRAL = 0.5

_SLOT = Slot(name="result", value_type="str", role="result")


@dataclass(frozen=True)
class ReplayReport:
    """One model's audition, summarized.

    ``brier`` is over every scored step (lower is better; 0.25 is what
    always-abstaining earns). ``cold_brier``/``warm_brier`` split the
    steps by whether the node had ANY history at prediction time —
    cold is the territory §5 reserved for a learned model."""

    name: str
    predictions: int
    opinions: int  # steps where the model actually spoke
    brier: float
    warm_predictions: int
    warm_brier: float | None
    cold_predictions: int
    cold_brier: float | None


def earns_its_cost(
    challenger: ReplayReport, baseline: ReplayReport, *, margin: float = 0.0
) -> bool:
    """The §5 gate: strictly better overall Brier than the baseline on
    the SAME replay, by at least ``margin``. A challenger that merely
    matches the free baseline has not earned an inference bill."""
    return challenger.brier < baseline.brier - margin


def _step_labels(run: RecordedRun) -> dict[str, bool]:
    """One label per distinct node key: a key that appears several times
    in a run counts as ok only if EVERY appearance was ok — the same
    all-actions-verified rule the contract trace recorder uses."""
    labels: dict[str, bool] = {}
    for step in run.steps:
        labels[step.node_key] = labels.get(step.node_key, True) and step.ok
    return labels


def _stub(node_key: str) -> NodeContract:
    """A candidate as the assembler would present it: the contract's id
    is the node key itself, and its NAME is the key without the
    ``route:`` prefix — so ``route_node_key(name)`` round-trips and the
    counting baseline's evidence lookup works unchanged."""
    name = node_key[6:] if node_key.startswith("route:") else node_key
    return NodeContract(
        id=node_key,
        name=name,
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="run")
            ]
        ),
    )


def replay(
    runs: Sequence[RecordedRun],
    models: Mapping[str, Callable[[TraceStore], object]],
    *,
    warmup: int = 10,
) -> dict[str, ReplayReport]:
    """Audition every model on the same corpus, prequentially.

    ``runs`` must be oldest-first — the corpus is replayed as time.
    ``models`` maps a display name to a factory taking the replay's own
    (fresh, in-memory) ``TraceStore``; the factory shape is what lets
    live-reading models (the baseline, the transformer, the stack) see
    exactly the history that exists at each prediction moment. The first
    ``warmup`` runs are recorded without being scored — grading a model
    on an empty world measures nothing.
    """
    reports: dict[str, ReplayReport] = {}
    for name, factory in models.items():
        store = TraceStore()
        try:
            model = factory(store)
            seen: set[str] = set()
            squared: list[float] = []
            cold_squared: list[float] = []
            warm_squared: list[float] = []
            opinions = 0
            for index, run in enumerate(runs):
                labels = _step_labels(run)
                if index >= warmup:
                    candidates = [_stub(key) for key in sorted(labels)]
                    proposal = model.propose(
                        goal=GoalSpec(name=run.goal, want=[]),
                        slot=_SLOT,
                        selected=[],
                        candidates=candidates,
                    )
                    for key in sorted(labels):
                        spoke = key in proposal.weights
                        p = proposal.weights.get(key, NEUTRAL)
                        error = (p - (1.0 if labels[key] else 0.0)) ** 2
                        squared.append(error)
                        (warm_squared if key in seen else cold_squared).append(
                            error
                        )
                        opinions += 1 if spoke else 0
                # Reveal: the run becomes history for the NEXT prediction.
                store.record_run(
                    goal=run.goal,
                    steps=run.steps,
                    success=run.success,
                    context=run.context,
                )
                seen.update(labels)
            reports[name] = ReplayReport(
                name=name,
                predictions=len(squared),
                opinions=opinions,
                brier=(sum(squared) / len(squared)) if squared else 0.0,
                warm_predictions=len(warm_squared),
                warm_brier=(
                    sum(warm_squared) / len(warm_squared)
                    if warm_squared
                    else None
                ),
                cold_predictions=len(cold_squared),
                cold_brier=(
                    sum(cold_squared) / len(cold_squared)
                    if cold_squared
                    else None
                ),
            )
        finally:
            store.close()
    return reports


# --------------------------------------------------------------------------- #
# The audition world: name semantics carry the truth, cold names arrive late.  #
# --------------------------------------------------------------------------- #
# Hidden truth: a provider whose name is KIN to the goal's family mostly
# works; a stranger — a name from nowhere near the goal's vocabulary —
# mostly fails. This is exactly the "name/slot semantics ('tax-form-pdf'
# ≈ 'tax filing document') predict fit" signal §5 says a learned ranker
# should read, scaled up and given a cold-start cliff.
FIT_RATE = 0.93
STRANGER_RATE = 0.10

_FAMILIES = ("invoice csv", "report pdf", "photo archive")
# Warm-phase strangers: distractor vocabulary, kin to nothing.
_STRANGERS = (
    "mahogany sprocket tumbler",
    "velvet gramophone mill",
    "quartz pendulum loom",
)
# Cold-phase strangers: NEW distractor names, so the cold set holds both
# kinds of unseen candidate — kin the model should endorse, strangers it
# should doubt.
_COLD_STRANGERS = (
    "copper zeppelin whisk",
    "amber telescope churn",
    "walnut accordion press",
)


def synthetic_semantic_runs(
    *,
    warm_rounds: int = 120,
    cold_generations: Sequence[str] = ("v2", "mk3", "mk4"),
    seed: int = 7,
) -> list[RecordedRun]:
    """A seeded corpus with a cold-start cliff, oldest-first.

    Warm phase: each goal family alternates between its own kin provider
    (mostly succeeds) and a rotating stranger (mostly fails) — the mixed,
    honest history a real trace store accumulates. Cold phase: brand-new
    provider GENERATIONS appear ("invoice-csv-v2-worker" — zero recorded
    history, names still carrying their family words) alongside brand-new
    strangers. Counting abstains on every one of them by construction;
    a semantic model has everything it needs to tell them apart."""
    rng = random.Random(seed)
    when = 0

    def one(goal_family: str, provider: str, rate: float) -> RecordedRun:
        nonlocal when
        when += 1
        ok = rng.random() < rate
        return RecordedRun(
            goal=f"process {goal_family} files",
            context="",
            success=ok,
            steps=(NodeObservation(node_key=route_node_key(provider), ok=ok),),
            recorded_at=f"t{when:06d}",
        )

    def worker(stem: str, generation: str = "") -> str:
        tail = f"-{generation}" if generation else ""
        return stem.replace(" ", "-") + tail + "-worker"

    runs: list[RecordedRun] = []
    for i in range(warm_rounds):
        family = _FAMILIES[i % len(_FAMILIES)]
        if i % 2 == 0:
            runs.append(one(family, worker(family), FIT_RATE))
        else:
            stranger = _STRANGERS[(i // 2) % len(_STRANGERS)]
            runs.append(one(family, worker(stranger), STRANGER_RATE))
    for generation in cold_generations:
        for i, family in enumerate(_FAMILIES):
            runs.append(one(family, worker(family, generation), FIT_RATE))
            runs.append(
                one(
                    family,
                    worker(_COLD_STRANGERS[i], generation),
                    STRANGER_RATE,
                )
            )
    return runs
