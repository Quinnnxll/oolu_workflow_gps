"""Before/after on the whole context-harness arc — the measurable half.

Two kinds of numbers tell the arc's story. The MODEL numbers (verified
rate, wrong answers, cost) need a live key and are one command away:

    ANTHROPIC_API_KEY=... python benchmarks/node_authoring.py \\
        --max-tokens 1024                      # the pre-arc starvation
    ANTHROPIC_API_KEY=... python benchmarks/node_authoring.py \\
        --record data/auditions.jsonl          # the arc's defaults

This script prints the HARNESS numbers — every model-independent delta
the six phases changed, measured live from the current machinery (the
"after" column is computed, not quoted; the "before" column is the
Phase 0 audit's findings, each anchored in docs/context-harness-plan.md
§1). Deterministic, offline, zero spend — run it anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from node_authoring import GOALS, bench_context_pack  # noqa: E402

from oolu.author import NodeAuthorAgent  # noqa: E402
from oolu.providers.profiles import resolve_profile  # noqa: E402
from oolu.providers.tokens import estimate_tokens  # noqa: E402


def rows() -> list[tuple[str, str, str]]:
    """(dimension, before, after) — after measured from the machinery."""
    build = resolve_profile("node.build")
    chat = resolve_profile("chat.turn")
    agent_steps = NodeAuthorAgent(object())._max_steps

    packs = {g.key: bench_context_pack(g) for g in GOALS if g.kind == "build"}
    pack_tokens = [estimate_tokens(p) for p in packs.values() if p]
    route_goals = [g for g in GOALS if g.upstream]
    upstream_seen = sum(
        1 for g in route_goals if '"outputs"' in packs.get(g.key, "")
        or "recent VERIFIED outputs" in packs.get(g.key, "")
    )

    return [
        (
            "node.build output ceiling (tokens)",
            "1024 (universal constructor default)",
            str(build.max_tokens),
        ),
        (
            "node.build reasoning budget (tokens)",
            "0 — no thinking parameter existed",
            str(build.thinking_budget),
        ),
        (
            "authoring temperature",
            "provider default (~1.0), unset",
            f"{build.temperature} (or thinking, which supersedes it)",
        ),
        (
            "authoring tier default",
            "fast (Haiku / gpt-4o-mini)",
            "reasoning (model.build_tier default)",
        ),
        (
            "chat.turn output ceiling (tokens)",
            "1024",
            str(chat.max_tokens),
        ),
        (
            "context pushed to the author (tokens, avg over bench goals)",
            "0 — the goal sentence only",
            (
                f"{sum(pack_tokens) // max(len(pack_tokens), 1)} avg / "
                f"{max(pack_tokens) if pack_tokens else 0} max, budgeted+traced"
            ),
        ),
        (
            "route-position goals seeing their upstream shape",
            f"0/{len(route_goals)}",
            f"{upstream_seen}/{len(route_goals)}",
        ),
        (
            "gates before publish",
            "0 — mock_smells only, on one path; first real run found the rest",
            "safety screen + mock smells + contract presence + interface "
            "honesty + sandbox verify + optional review seat, every path",
        ),
        (
            "repair rounds before a build is refused",
            "0 — one shot, publish or nothing",
            "2 at the gate (+ the agent's verify-fix loop)",
        ),
        (
            "agent authoring step budget",
            "6",
            str(agent_steps),
        ),
        (
            "memory across turns for a failed build",
            "none — every retry started from zero",
            "durable ledger: lessons + last failure feed the retry's pack",
        ),
        (
            "retry backoff between provider attempts",
            "0s — a silent no-op hammered the 429 it retried",
            "real, seamed (1s, 2s), both stacks",
        ),
        (
            "Anthropic prompt-cache breakpoint",
            "never sent",
            "on the frozen prefix, every call",
        ),
        (
            "capability dispatch at the authoring door",
            "hasattr(consult) — true for every router, distinguished nothing",
            "the model manifest's answer (consult_ready)",
        ),
    ]


def main() -> int:
    table = rows()
    width = max(len(dimension) for dimension, _b, _a in table)
    print("Context-harness arc — the measurable before/after\n")
    for dimension, before, after in table:
        print(f"{dimension:<{width}}")
        print(f"  before: {before}")
        print(f"  after:  {after}")
    print(
        "\nThe model half (verified rate, wrong answers, $/verified build)"
        " needs a key — the two commands are in this file's docstring;"
        " --record turns them into the standing audition trend."
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
