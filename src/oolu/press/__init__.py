"""The press — member-contributed content, attributed and closed-loop.

A1 of the agents-expansion plan (docs/agents-expansion-plan.md): the
contribution spine. Members publish material into the app under a stated
license; every contribution is scrubbed, genre-keyed, author-attributed,
durable, and revocable-forward. Later phases build on exactly these
records: the newsroom composes them into stories (A2) and the ad
dividend splits over their attribution weights (A5). The poll floor
(A3) has been REMOVED — the member's pairwise preference book it wrote
into survives in :mod:`pairwise`, fed by story feedback and future
survey instruments.

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
from .demand import (
    DEMAND_READER_FLOOR,
    DEMAND_VERSION,
    GenreDemand,
    GenreDemandStore,
    GenreEvidence,
    demand_line,
    rank_demand,
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
from .metrics import METRICS_K_FLOOR, StoryMetricsStore
from .newsroom import LineageShare, Newsroom, Story, StoryStore
from .pairwise import PairwiseStore
from .personalize import (
    SEMANTIC_PULL,
    Taste,
    semantic_affinity,
    taste_snippet,
)
from .standards import RUBRIC_VERSION, RubricBreakdown, score, select
from .taxonomy import GENRES, TAXONOMY_VERSION, Genre, taxonomy_items
from .topics import (
    TOPIC_VERSION,
    TOPICS_PER_RUN,
    BeatRow,
    ClusterPiece,
    MarketFact,
    TopicBrief,
    TopicBriefStore,
    TopicCandidate,
    mine_clusters,
    mine_measured_gaps,
    mine_price_moves,
    mine_trust_bands,
    select_topics,
    topics_line,
)

__all__ = [
    "DEMAND_READER_FLOOR",
    "DEMAND_VERSION",
    "DROPPED",
    "EDITION_LABEL",
    "EDITION_PULSE_GOAL",
    "EDITION_SIZE",
    "GATHER_ASK",
    "GATHER_ASK_WORDS",
    "GENRES",
    "GenreDemand",
    "GenreDemandStore",
    "GenreEvidence",
    "INTAKE_LICENSE",
    "LICENSES",
    "MAX_BODY_CHARS",
    "MAX_MEDIA",
    "MAX_TITLE_CHARS",
    "METRICS_K_FLOOR",
    "RUBRIC_VERSION",
    "SEMANTIC_PULL",
    "SIMILARITY_FLAG",
    "TAXONOMY_VERSION",
    "TOPIC_VERSION",
    "TOPICS_PER_RUN",
    "Taste",
    "BeatRow",
    "ClusterPiece",
    "ContentContribution",
    "ContributionStore",
    "Genre",
    "IntakeDraft",
    "IntakeStore",
    "LineageShare",
    "MarketFact",
    "MediaRef",
    "Newsroom",
    "PairwiseStore",
    "PreferenceStore",
    "PressDesk",
    "PressError",
    "RubricBreakdown",
    "Story",
    "StoryMetricsStore",
    "StoryStore",
    "TopicBrief",
    "TopicBriefStore",
    "TopicCandidate",
    "demand_line",
    "draft_from_material",
    "edition_message",
    "fold_answer",
    "rank_demand",
    "leak_report",
    "looks_like_material",
    "mine_clusters",
    "mine_measured_gaps",
    "mine_price_moves",
    "mine_trust_bands",
    "rank_edition",
    "review",
    "score",
    "select",
    "select_topics",
    "semantic_affinity",
    "taste_snippet",
    "taxonomy_items",
    "topics_line",
]
