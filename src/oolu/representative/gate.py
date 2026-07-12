"""The confidence gate: pure functions deciding what a draft may become.

Phase 0 spends none of this — every draft waits for the user — but the
verdict is computed and stored from day one so Phase 2's auto-send can be
tuned against real history. Two checks, in order of authority:

1. Commitments always draft. A reply that agrees to money, a meeting, or
   a promise never auto-sends, whatever its similarity score.
2. Grounding. A reply to something unlike anything the user has ever
   discussed is a guess wearing their voice.
"""

from __future__ import annotations

from ..replies.engine import normalize_message
from .models import GateVerdict, RecallHit

# Matched against the NORMALIZED generated reply (casefold, punctuation
# stripped to spaces) — so "I'll pay" is "i ll pay" here. Narrow phrases
# only: the cost of a miss is a pending draft, the cost of a false pass
# (in Phase 2) is a sent commitment.
_RAW_COMMITMENT_MARKERS: tuple[str, ...] = (
    "i'll pay",
    "i will pay",
    "i'll send the money",
    "i'll transfer",
    "i'll buy",
    "i'll order",
    "i'll book",
    # NOTE: markers are normalized (punctuation falls away), so a bare
    # "deal!" would match "the deal fell through" — phrases only.
    "you have a deal",
    "it's a deal",
    "i agree to",
    "agreed, let's",
    "i confirm",
    "i promise",
    "i'll be there",
    "i will be there",
    "see you at",
    "see you then",
    "see you tomorrow",
    "let's meet",
    "i'll schedule",
    "i'll sign",
    "count me in",
)
COMMITMENT_MARKERS: tuple[str, ...] = tuple(
    normalize_message(marker) for marker in _RAW_COMMITMENT_MARKERS
)

# Below this best-recall score the reply is ungrounded: nothing the user
# ever said resembles the message being answered.
DEFAULT_SIMILARITY_MIN = 0.35


def commitment_marker(text: str) -> str | None:
    """The first commitment phrase the text contains, word-aligned."""
    padded = f" {normalize_message(text)} "
    for marker in COMMITMENT_MARKERS:
        if f" {marker} " in padded:
            return marker
    return None


def judge(
    generated: str,
    hits: list[RecallHit],
    *,
    similarity_min: float = DEFAULT_SIMILARITY_MIN,
) -> GateVerdict:
    score = max((hit.score for hit in hits), default=0.0)
    reasons: list[str] = []
    marker = commitment_marker(generated)
    if marker is not None:
        reasons.append(f"sounds like a commitment ({marker!r}) — always drafts")
    if score < similarity_min:
        reasons.append(
            "ungrounded: nothing similar in what the user has said before"
        )
    return GateVerdict(
        score=score,
        commitment=marker is not None,
        auto_ok=marker is None and score >= similarity_min,
        reasons=reasons,
    )
