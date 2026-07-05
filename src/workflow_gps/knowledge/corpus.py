"""The trace corpus — recorded runs become training data for learned planners.

Workflow assembly is a sequence-modeling problem: given a goal and the plan
so far, predict the node that belongs next. The trace store already logs
every run verbatim (``TraceStore.runs``); this module turns that log into
the two artifacts a learned planner needs:

- ``build_examples``: (goal, plan-prefix → next node) examples, one per
  step boundary of each recorded run, in the completion order the run
  actually exhibited — exactly the shape a forward-generating sequence
  model (an n-gram baseline today, a Mamba/SSM checkpoint tomorrow)
  trains on.
- ``export_jsonl``: the same examples as a JSONL file for an *external*
  training job. This repo builds the seam and the data pipeline; model
  training happens elsewhere, on data this exporter makes portable.

Runs that failed still export (with ``run_success: false``) so a training
job can weigh or filter them — throwing away negatives silently would be
editorializing the user's own history.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .traces import RecordedRun, TraceStore


@dataclass(frozen=True, slots=True)
class TrainingExample:
    """One next-step prediction target from one recorded run."""

    goal: str
    context: str
    prefix: tuple[str, ...]  # node keys completed before this step
    next_node: str
    next_ok: bool  # did THIS step verify
    run_success: bool  # did the whole run verify (what money moves on)


def build_examples(
    runs: Sequence[RecordedRun], *, only_successful: bool = False
) -> list[TrainingExample]:
    """Every step boundary of every run, as (prefix → next) examples."""
    examples: list[TrainingExample] = []
    for run in runs:
        if only_successful and not run.success:
            continue
        keys = [step.node_key for step in run.steps]
        for index, step in enumerate(run.steps):
            examples.append(
                TrainingExample(
                    goal=run.goal,
                    context=run.context,
                    prefix=tuple(keys[:index]),
                    next_node=step.node_key,
                    next_ok=step.ok,
                    run_success=run.success,
                )
            )
    return examples


def export_jsonl(
    store: TraceStore,
    path: str | Path,
    *,
    context: str | None = None,
    limit: int = 100_000,
    only_successful: bool = False,
) -> int:
    """Write the corpus as JSONL (one example per line); returns the count.

    The file is self-describing and ordered oldest-run-first so a training
    job can honor time (train on the past, validate on the recent).
    """
    runs = list(reversed(store.runs(context=context, limit=limit)))
    examples = build_examples(runs, only_successful=only_successful)
    target = Path(path)
    with target.open("w", encoding="utf-8") as sink:
        for example in examples:
            record = asdict(example)
            record["prefix"] = list(example.prefix)
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(examples)
