"""Turning recorded runs into node-token sequences the planner trains on.

``knowledge.corpus`` already exports (goal, prefix → next node) examples in
*string* space — the shape a next-node model trains on. This module lifts the
same corpus into *token-id* space using a :class:`NodeVocabulary`, which is
what an actual transformer ingests:

    plan sequence = [BOS] [goal-token] node1 node2 … nodeN [EOS]

The goal token conditions the plan; the node tokens are the plan. Training is
plain autoregressive next-token prediction over these ids — identical to
language modelling, with a vocabulary of nodes instead of words.

``export_token_jsonl`` writes the token-id sequences to a portable file for an
external training job (the 3B/8B/30B runs happen elsewhere, on data this
exporter makes portable), alongside the vocabulary snapshot that pins the ids.
Only verified runs are exported by default: a plan the platform could not
verify is not an example of how to plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..knowledge.traces import RecordedRun, TraceStore
from .vocab import NodeVocabulary


@dataclass(frozen=True, slots=True)
class PlanSequence:
    """One recorded run as a token-id sequence, plus its provenance."""

    goal: str
    context: str
    token_ids: tuple[int, ...]
    success: bool


def encode_run(
    run: RecordedRun, vocab: NodeVocabulary, *, grow: bool = False
) -> PlanSequence:
    """Encode one run as ``[BOS] [goal] steps… [EOS]`` token ids.

    ``grow=True`` registers unseen node keys (training/vocab-building);
    ``grow=False`` maps them to ``UNK`` (inference against a frozen vocab).
    """
    ids = [vocab.bos_id, vocab.goal_id(run.goal)]
    for step in run.steps:
        ids.append(vocab.id_of(step.node_key, add=grow))
    ids.append(vocab.eos_id)
    return PlanSequence(
        goal=run.goal,
        context=run.context,
        token_ids=tuple(ids),
        success=run.success,
    )


def build_vocabulary(
    runs: Sequence[RecordedRun],
    *,
    goal_buckets: int | None = None,
    only_successful: bool = False,
) -> NodeVocabulary:
    """Grow a vocabulary covering every node key in the given runs.

    Ordered oldest-first-independent (insertion order follows the runs as
    given); the caller controls run order, and ids stay stable across reloads
    because :meth:`NodeVocabulary.add` is append-only.
    """
    vocab = (
        NodeVocabulary(goal_buckets=goal_buckets)
        if goal_buckets is not None
        else NodeVocabulary()
    )
    for run in runs:
        if only_successful and not run.success:
            continue
        for step in run.steps:
            vocab.add(step.node_key)
    return vocab


def encode_runs(
    runs: Sequence[RecordedRun],
    vocab: NodeVocabulary,
    *,
    grow: bool = False,
    only_successful: bool = False,
) -> list[PlanSequence]:
    return [
        encode_run(run, vocab, grow=grow)
        for run in runs
        if not (only_successful and not run.success)
    ]


def export_token_jsonl(
    store: TraceStore,
    path: str | Path,
    *,
    vocab: NodeVocabulary | None = None,
    context: str | None = None,
    limit: int = 100_000,
    only_successful: bool = True,
) -> tuple[int, NodeVocabulary]:
    """Write the corpus as token-id sequences; return ``(count, vocabulary)``.

    Runs are written oldest-first so an external trainer can honour time
    (train on the past, validate on the recent). If no vocabulary is supplied
    one is grown from the corpus and written next to the data file as
    ``<path>.vocab.json`` — the two files together are a complete, portable
    training input for a job that never touches this repo.
    """
    runs = list(reversed(store.runs(context=context, limit=limit)))
    if only_successful:
        runs = [run for run in runs if run.success]
    grow = vocab is None
    if vocab is None:
        vocab = build_vocabulary(runs)
    target = Path(path)
    written = 0
    with target.open("w", encoding="utf-8") as sink:
        for run in runs:
            sequence = encode_run(run, vocab, grow=grow)
            sink.write(
                json.dumps(
                    {
                        "goal": sequence.goal,
                        "context": sequence.context,
                        "token_ids": list(sequence.token_ids),
                        "run_success": sequence.success,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    vocab.dump(target.with_suffix(target.suffix + ".vocab.json"))
    return written, vocab
