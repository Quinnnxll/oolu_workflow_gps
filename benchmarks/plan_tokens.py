"""The node-token planning model, demonstrated end to end.

The mission: stop asking a frontier model to reason each node out in words,
and instead learn a small model whose TOKENS ARE NODES AND ROUTES, so a whole
mission is planned the way a sentence is generated — cheaply, within a compute
budget, and scaling to 3B/8B/30B as more users, more nodes, and more routes
accumulate.

This harness shows the three pieces that make that real today, with no
framework and no training run:

1. VOCABULARY — a node/route database becomes a token vocabulary. Each node
   key is one stable token; goals condition the plan through a bounded band;
   the vocabulary grows with the marketplace, not with the sentences users
   type.
2. GENERATION — a pure-Python autoregressive planner, trained on verified
   runs, GENERATES a whole plan for a goal by rolling out node tokens. This
   is "route like a sentence", running in the base install.
3. SCALE — the SAME architecture (planner/config.py) grows from a runnable
   ``tiny`` reference to 3B/8B/30B by changing four numbers. The sizes are
   arithmetic, printed here and pinned in tests.

Run it:  python benchmarks/plan_tokens.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oolu.knowledge.traces import NodeObservation, TraceStore, route_node_key
from oolu.planner import (
    PLANNER_PRESETS,
    MarkovPlanner,
    build_vocabulary,
    encode_run,
    human_size,
    parameter_count,
)


def _seed_corpus() -> TraceStore:
    store = TraceStore()

    def record(goal, names, ok=True):
        steps = [NodeObservation(route_node_key(n), ok=ok) for n in names]
        store.record_run(goal=goal, steps=steps, success=ok, context="")

    # Two missions the user runs repeatedly, each with a reliable chain and a
    # rival opener that keeps failing — the honest, mixed history a trace
    # store accumulates.
    for _ in range(20):
        record("month-end invoice run", ["export_ledger", "validate_totals", "publish_report"])
        record("onboard a new supplier", ["collect_kyc", "risk_screen", "create_vendor"])
    record("month-end invoice run", ["export_ledger", "corrupt_step"], ok=False)
    record("onboard a new supplier", ["skip_kyc", "create_vendor"], ok=False)
    return store


def main() -> None:
    store = _seed_corpus()
    runs = store.runs(limit=1000)

    print("== VOCABULARY: the node/route database becomes tokens ==")
    vocab = build_vocabulary(runs)
    print(f"  {len(runs)} recorded runs -> {vocab.node_count} node tokens "
          f"(vocab length {len(vocab)}, incl. reserved + goal bands)")
    example = encode_run(runs[0], vocab)
    decoded = [vocab.token_of(t) for t in example.token_ids]
    print(f"  one plan as tokens: {decoded}")

    print("\n== GENERATION: a plan rolled out node by node (pure Python) ==")
    planner = MarkovPlanner.from_store(store)
    print(f"  trained on {planner.runs_seen} verified runs")
    for goal in ("month-end invoice run", "onboard a new supplier"):
        plan = [k.removeprefix("route:") for k in planner.plan(goal)]
        print(f"  {goal!r} -> {plan}")

    print("\n== SCALE: one architecture, four rungs (arithmetic, not aspiration) ==")
    for name, cfg in PLANNER_PRESETS.items():
        print(f"  {name:5} d_model={cfg.d_model:>5} layers={cfg.n_layers:>3} "
              f"heads={cfg.n_heads:>3} -> {human_size(parameter_count(cfg)):>6}")

    print("\nThe frontier model still does what it is best at — turning words")
    print("into a typed goal. From there, the plan is generated in nodes.")


if __name__ == "__main__":
    main()
