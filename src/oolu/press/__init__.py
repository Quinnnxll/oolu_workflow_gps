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
from .newsroom import LineageShare, Newsroom, Story, StoryStore
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
    "EDITION_LABEL",
    "EDITION_PULSE_GOAL",
    "EDITION_SIZE",
    "GENRES",
    "K_FLOOR",
    "LICENSES",
    "MAX_BODY_CHARS",
    "MAX_MEDIA",
    "MAX_TITLE_CHARS",
    "RUBRIC_VERSION",
    "SIMILARITY_FLAG",
    "TAXONOMY_VERSION",
    "ContentContribution",
    "ContributionStore",
    "Genre",
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
    "edition_message",
    "leak_report",
    "rank_edition",
    "score",
    "select",
    "taxonomy_items",
]
