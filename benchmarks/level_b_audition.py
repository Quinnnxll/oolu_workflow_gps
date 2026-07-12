"""Audition a LIVE model in the Level B seat — through OoLu's own router.

The bench and the gate already exist (benchmarks/level_b.py); this rig
puts a real brain in the seat using the SAME provider stack the desktop
chat uses — adapters, secret vault, retries, and the call meter, so the
report's cost column is the money the audition actually spent (§22's
"inference cost", measured, not estimated).

Configure exactly one brain and run it:

    ANTHROPIC_API_KEY=sk-...   python benchmarks/level_b_audition.py
    OPENAI_API_KEY=sk-...      python benchmarks/level_b_audition.py
    OOLU_LOCAL_URL=http://localhost:11434/v1 OOLU_LOCAL_MODEL=llama3.2 \\
                               python benchmarks/level_b_audition.py

    --tier reasoning   the provider's reasoning tier (default: fast)

The scripted careful-engineer runs alongside as the incumbent: the
model is FIT only if it finishes the same subsystem change, under the
same counted budget, past the same gate — and the table then shows
what that fitness cost. No key, no cloud, no seat: the rig refuses in
words rather than pretending.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from level_b import (
    DEFAULT_BUDGET,
    careful_engineer,
    fit_for_the_seat,
    model_planner,
    run,
)

from oolu.billing import ModelCallMeter
from oolu.durable.connection import DurableConnection
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring

AUDITION_PURPOSE = "bench.level_b"


def build_brain(workdir: Path, *, tier: str) -> tuple[ChatModelRouter, ModelCallMeter] | None:
    """The desktop's own model stack, fed from the environment.

    Own-API keys win; a local OpenAI-compatible server is the no-cloud
    door. None = nothing configured — the caller says so in words."""
    meter = ModelCallMeter()
    local_url = os.environ.get("OOLU_LOCAL_URL", "").strip()
    local_model = os.environ.get("OOLU_LOCAL_MODEL", "").strip()
    keys = {
        provider: os.environ.get(f"{provider.upper()}_API_KEY", "").strip()
        for provider in ("anthropic", "openai")
    }
    keyed = [provider for provider, key in keys.items() if key]
    if not keyed and not (local_url and local_model):
        return None
    conn = DurableConnection(workdir / "audition.db")
    keyring = ModelKeyring(conn, key_path=workdir / "machine.key")
    for provider in keyed:
        keyring.store("bench", provider, keys[provider])
    router = ChatModelRouter(
        keyring,
        "bench",
        meter=meter,
        tier=lambda: tier,
        source=(lambda: "own-api") if keyed else (lambda: "local"),
        local_url=lambda: local_url,
        local_model=lambda: local_model,
        max_tokens=2048,
        purpose=AUDITION_PURPOSE,
    )
    return router, meter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="fast", choices=("fast", "reasoning"))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as workdir:
        brain = build_brain(Path(workdir), tier=args.tier)
        if brain is None:
            print(
                "No brain configured — set ANTHROPIC_API_KEY or "
                "OPENAI_API_KEY (or OOLU_LOCAL_URL + OOLU_LOCAL_MODEL) "
                "and run again. The seat is not pretended into.",
                file=sys.stderr,
            )
            return 2
        router, meter = brain

        print(
            f"Level B audition · tier {args.tier} · budget "
            f"{DEFAULT_BUDGET} counted steps\n"
        )
        incumbent = run(careful_engineer, name="careful-engineer")
        challenger = run(model_planner(router), name=f"model ({args.tier})")

        spend = sum(c.cost for c in meter.charges(AUDITION_PURPOSE))
        calls = len(meter.charges(AUDITION_PURPOSE))
        print(
            f"{'contender':<20} {'steps':>5} {'done':>5} {'gate':>8} "
            f"{'model calls':>11} {'inference $':>11}"
        )
        for report, cost, count in (
            (incumbent, 0.0, 0),
            (challenger, spend, calls),
        ):
            verdict = "FIT" if fit_for_the_seat(report) else "not fit"
            print(
                f"{report.name:<20} {report.steps_used:>5} "
                f"{str(report.completed):>5} {verdict:>8} {count:>11} "
                f"{cost:>11.4f}"
            )
        print()
        for check, passed in challenger.acceptance.items():
            print(f"  {'✓' if passed else '✗'} {check}")
        if fit_for_the_seat(challenger):
            print(
                f"\nThe model EARNS the seat — at ${spend:.4f} for what the "
                "scripted engineer does for free. Whether that is worth it "
                "is exactly the question the spec says to answer with "
                "numbers like these."
            )
        else:
            print(
                "\nThe model does NOT earn the seat on this run — the "
                "scripted incumbent stays."
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
