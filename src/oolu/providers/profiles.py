"""Seat generation profiles — how hard each model seat may work.

Phase 1 of the context-harness plan (docs/context-harness-plan.md). The
seat registry (``oolu/seats.py``) answers what a call site may TOUCH;
this table answers how much EFFORT it may spend: output ceiling,
sampling temperature, and reasoning budgets. The two share one
vocabulary — the purpose strings the meter books under — so governance,
accounting, and effort are read off the same key.

The numbers replace a single constructor default that starved every
seat equally: ``ChatModelRouter`` capped ALL purposes at 1024 output
tokens — the node author had to fit a plan, an interface declaration,
and a complete Python script inside it (the bench's truncation bucket
exists because it often couldn't) — while the OpenAI path sent no
ceiling and no temperature at all, so the "same" seat behaved
differently per provider.

Principles the table encodes:

- **Code-writing seats think hardest.** ``node.build``, ``node.repair``
  and ``plan.rebuild`` get the room a whole program needs (16k out) and
  a reasoning budget on models that support it. Code likes low, stable
  temperature; conversation keeps the provider's own default.
- **Short structured seats stay short.** Intake and route advice are a
  paragraph of JSON, not an essay.
- **A profile is a ceiling, not a target** — completion stops at the
  answer; the ceiling only decides when the provider CUTS it. Ceilings
  are cheap; truncation is not: an unfinished script wastes the whole
  call that produced it.
- **Provider capability decides what actually rides.** The adapters
  attach a reasoning budget only to models that speak one (Anthropic
  extended thinking, OpenAI reasoning-effort models) — a profile never
  breaks a model that predates the knob.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeatProfile:
    """Generation effort for one metered purpose, model-independent.

    ``max_tokens`` is the output ceiling. ``temperature`` rides where
    the model accepts one (None = the provider's own default).
    ``thinking_budget`` is Anthropic extended-thinking tokens;
    ``reasoning_effort`` is the OpenAI reasoning-model dial — the
    adapter attaches whichever the seated model actually speaks."""

    max_tokens: int = 4096
    temperature: float | None = None
    thinking_budget: int | None = None
    reasoning_effort: str | None = None


# Code-writing seats share one shape: a whole program must fit, and the
# model may think before it writes.
_CODE_SEAT = SeatProfile(
    max_tokens=16384,
    temperature=0.2,
    thinking_budget=4096,
    reasoning_effort="medium",
)

SEAT_PROFILES: dict[str, SeatProfile] = {
    # The author of a node's execution function — the seat the plan's
    # diagnosis found starved at 1024 tokens on the fast tier.
    "node.build": _CODE_SEAT,
    # Mid-run repair edits the failing function with the exact error in
    # hand — the same work, the same room.
    "node.repair": _CODE_SEAT,
    # The post-retry rebuilder plans fresh AND writes the script.
    "plan.rebuild": _CODE_SEAT,
    # The runtime script synthesizer writes one self-contained script;
    # generous output, no standing thinking budget (its recalc loop
    # already escalates tiers on repeated failure).
    "plan.synthesize": SeatProfile(max_tokens=8192, temperature=0.2),
    # Conversation: room to answer well, the provider's own sampling.
    "chat.turn": SeatProfile(max_tokens=4096),
    # Short structured advice: a brief or a route weight, not an essay.
    "plan.intake": SeatProfile(max_tokens=2048, temperature=0.2),
    "plan.route": SeatProfile(max_tokens=2048, temperature=0.2),
    # Drafts in the user's voice keep the provider's sampling default.
    "rep.draft": SeatProfile(max_tokens=2048),
    # The authoring bench audits the node.build seat — same effort, so
    # its scoreboard measures the seat, not a bench-only configuration.
    "bench.node_authoring": _CODE_SEAT,
}

# Purposes not in the table (new call sites, benches) get honest room —
# noticeably more than the old universal 1024, deliberately less than a
# code seat until someone declares otherwise here.
DEFAULT_PROFILE = SeatProfile()


def resolve_profile(purpose: str) -> SeatProfile:
    """The standing effort for a purpose. Unknown purposes get
    ``DEFAULT_PROFILE`` — never a crash, never silent starvation."""
    return SEAT_PROFILES.get(str(purpose or ""), DEFAULT_PROFILE)
