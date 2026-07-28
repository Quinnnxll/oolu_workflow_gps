"""The press — member-contributed content, attributed and closed-loop.

A1 of the agents-expansion plan (docs/agents-expansion-plan.md): the
contribution spine. Members publish material into the app under a stated
license; every contribution is scrubbed, genre-keyed, author-attributed,
durable, and revocable-forward. Later phases build on exactly these
records: the newsroom composes them into stories (A2), the poll floor
mines them for comparable pairs (A3), and the ad dividend splits over
their attribution weights (A5).

The package is closed-loop by law (plan invariant 1): nothing in here
reaches a web-search or external-content seam — pinned by the import
scan in ``tests/test_press.py``.
"""

from .contributions import (
    LICENSES,
    MAX_BODY_CHARS,
    MAX_MEDIA,
    MAX_TITLE_CHARS,
    SIMILARITY_FLAG,
    ContentContribution,
    ContributionStore,
    MediaRef,
    PressDesk,
    PressError,
    leak_report,
)
from .editions import (
    EDITION_LABEL,
    EDITION_PULSE_GOAL,
    EDITION_SIZE,
    PreferenceStore,
    edition_message,
    rank_edition,
)
from .intake import (
    DROPPED,
    GATHER_ASK,
    GATHER_ASK_WORDS,
    INTAKE_LICENSE,
    IntakeDraft,
    IntakeStore,
    draft_from_material,
    fold_answer,
    looks_like_material,
    review,
)
from .newsroom import LineageShare, Newsroom, Story, StoryStore
from .personalize import (
    SEMANTIC_PULL,
    Taste,
    semantic_affinity,
    taste_snippet,
)
from .polls import (
    K_FLOOR,
    PairwiseStore,
    PollDesk,
    PollPair,
    PollSide,
    PollStore,
    comparable,
)
from .standards import RUBRIC_VERSION, RubricBreakdown, score, select
from .taxonomy import GENRES, TAXONOMY_VERSION, Genre, taxonomy_items

__all__ = [
    "DROPPED",
    "EDITION_LABEL",
    "EDITION_PULSE_GOAL",
    "EDITION_SIZE",
    "GATHER_ASK",
    "GATHER_ASK_WORDS",
    "GENRES",
    "INTAKE_LICENSE",
    "K_FLOOR",
    "LICENSES",
    "MAX_BODY_CHARS",
    "MAX_MEDIA",
    "MAX_TITLE_CHARS",
    "RUBRIC_VERSION",
    "SEMANTIC_PULL",
    "SIMILARITY_FLAG",
    "TAXONOMY_VERSION",
    "Taste",
    "ContentContribution",
    "ContributionStore",
    "Genre",
    "IntakeDraft",
    "IntakeStore",
    "LineageShare",
    "MediaRef",
    "Newsroom",
    "PairwiseStore",
    "PollDesk",
    "PollPair",
    "PollSide",
    "PollStore",
    "PreferenceStore",
    "PressDesk",
    "PressError",
    "comparable",
    "RubricBreakdown",
    "Story",
    "StoryStore",
    "draft_from_material",
    "edition_message",
    "fold_answer",
    "leak_report",
    "looks_like_material",
    "rank_edition",
    "review",
    "score",
    "select",
    "semantic_affinity",
    "taste_snippet",
    "taxonomy_items",
]
