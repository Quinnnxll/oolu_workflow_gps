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
from .taxonomy import TAXONOMY_VERSION, GENRES, Genre, taxonomy_items

__all__ = [
    "GENRES",
    "LICENSES",
    "MAX_BODY_CHARS",
    "MAX_MEDIA",
    "MAX_TITLE_CHARS",
    "SIMILARITY_FLAG",
    "TAXONOMY_VERSION",
    "ContentContribution",
    "ContributionStore",
    "Genre",
    "MediaRef",
    "PressDesk",
    "PressError",
    "leak_report",
    "taxonomy_items",
]
