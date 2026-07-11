"""The ProposalModel audition: does the learned ranker earn its cost?

route-finding-proof.md §5 promised that anything smarter dropped into
the ``ProposalModel`` seat "must beat this baseline in the replay
harness to earn its inference cost." This is that audition, runnable:

    python benchmarks/proposal_replay.py

Three contenders replay the SAME seeded corpus, prequentially (test,
then train — no model ever predicts from its own future):

- ``counts``       — ``TraceProposalModel``, the free Beta-mean baseline;
- ``transformer``  — ``TinyTransformerProposalModel`` alone;
- ``stack``        — ``LearnedProposalStack``, counts first, the
                     transformer only where counts never saw the node.

The corpus (``synthetic_semantic_runs``) carries a cold-start cliff:
brand-new provider names, zero history, family words intact. Counting
abstains there by construction (scored at the neutral 0.5); a model
that reads name semantics can answer. The verdict line is the gate.
"""

from __future__ import annotations

from oolu.orchestrator import (
    LearnedProposalStack,
    TinyTransformerProposalModel,
    TraceProposalModel,
    earns_its_cost,
    replay,
    synthetic_semantic_runs,
)


def main() -> None:
    runs = synthetic_semantic_runs()
    reports = replay(
        runs,
        {
            "counts": lambda store: TraceProposalModel(store),
            "transformer": lambda store: TinyTransformerProposalModel(store),
            "stack": lambda store: LearnedProposalStack(
                TraceProposalModel(store),
                TinyTransformerProposalModel(store),
            ),
        },
    )

    print(f"corpus: {len(runs)} runs (Brier: lower is better; 0.25 = coin)")
    print(
        f"{'model':<12} {'brier':>8} {'warm':>8} {'cold':>8} "
        f"{'spoke':>10}"
    )
    for report in reports.values():
        warm = f"{report.warm_brier:.4f}" if report.warm_brier is not None else "-"
        cold = f"{report.cold_brier:.4f}" if report.cold_brier is not None else "-"
        print(
            f"{report.name:<12} {report.brier:>8.4f} {warm:>8} {cold:>8} "
            f"{report.opinions:>4}/{report.predictions:<5}"
        )

    baseline, stack = reports["counts"], reports["stack"]
    verdict = earns_its_cost(stack, baseline)
    print(
        f"\nverdict: the stack {'EARNS its seat' if verdict else 'does NOT earn its seat'} "
        f"({stack.brier:.4f} vs baseline {baseline.brier:.4f}; "
        f"cold {stack.cold_brier:.4f} vs {baseline.cold_brier:.4f})"
    )


if __name__ == "__main__":
    main()
