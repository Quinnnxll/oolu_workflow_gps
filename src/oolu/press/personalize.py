"""Semantic taste — the member's own words become their ranking input.

The rubric's factor breakdown stays recorded (durable, versioned, on the
audit trail) but it no longer renders to the member: the scoring is the
house's own working, and the order speaks for itself. What bends the
order instead is the member's own semantics, read from two consented
places:

1. **Their taps.** A 👍 or ⏭ on a story stores the story's own words
   next to the signal; liked texts pull similar stories up, skipped
   texts push them down. One tap adjusts the profile immediately.
2. **Their words.** What the member says in their OoLu and News threads
   is the plainest statement of what they care about. Only the member's
   OWN turns count — never the assistant's — and they only attract:
   talking about a subject is interest, never a skip.

Both live under the one law genre affinity already obeys: consent shapes
ranking; its absence shapes nothing (``press.personalize`` off = the
neutral edition, and nothing is gathered). Similarity is
``retrieval.score`` — the one scorer every recall site shares,
deterministic and model-free, so the same member and the same shelf
always rank the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..retrieval import score

# How strongly semantic taste bends the neutral order. At 1.0 a story
# the member liked VERBATIM doubles its standing and one they skipped
# verbatim loses it entirely — a tap speaks decisively — while mere
# chat interest lands in between (cosines under 1), so the rubric still
# matters everywhere the member hasn't spoken.
SEMANTIC_PULL = 1.0
# A taste corpus is bounded: the most recent texts speak.
TASTE_TEXTS = 50
SPOKEN_TEXTS = 100
# The snippet of a story a tap stores — enough to carry the subject,
# never the archive re-copied next to the signal.
TASTE_SNIPPET_CHARS = 600


@dataclass(frozen=True)
class Taste:
    """The member's semantic leaning, as texts: what they liked, what
    they skipped, what they said. Empty everywhere is no taste at all —
    ``bool(taste)`` says whether it can bend anything."""

    liked: tuple[str, ...] = field(default=())
    skipped: tuple[str, ...] = field(default=())
    spoken: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return bool(self.liked or self.skipped or self.spoken)


def taste_snippet(headline: str, prose: str) -> str:
    """The text a tap stores — the story's own words, bounded."""
    return f"{headline}\n{prose}"[:TASTE_SNIPPET_CHARS]


def _closest(text: str, corpus: tuple[str, ...]) -> float:
    return max((score(text, other) for other in corpus), default=0.0)


def semantic_affinity(text: str, taste: Taste | None) -> float:
    """How the member's taste leans on THIS text, bounded to [-1, 1].

    The closest liked or spoken text attracts; the closest skipped text
    repels; the two pull against each other in the open — a story the
    member liked verbatim scores 1.0, one they skipped verbatim -1.0,
    and a story their chats merely circle lands in between.
    """
    if not taste:
        return 0.0
    drawn = max(_closest(text, taste.liked), _closest(text, taste.spoken))
    pushed = _closest(text, taste.skipped)
    return round(max(-1.0, min(1.0, drawn - pushed)), 4)
