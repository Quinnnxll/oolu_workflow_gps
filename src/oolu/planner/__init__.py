"""The node-token planning model — tokens that are nodes and routes.

The frontier model reasons a mission out in words; this package prepares the
model that plans it in nodes. Its vocabulary is the node/route database
(:mod:`planner.vocab`), its corpus is the trace store's verified runs lifted
into token ids (:mod:`planner.sequences`), its architecture scales from a
runnable ``tiny`` reference to 3B/8B/30B by config alone
(:mod:`planner.config`), and its first occupant is a pure-Python
autoregressive planner that already generates whole plans
(:mod:`planner.baseline`). The real transformer (:mod:`planner.torch_model`)
lives behind the ``workflow-plan`` extra and trains elsewhere on the portable
corpus this package exports.

Nothing here is wired into the running engine by default: the mission is to
*prepare* the model and its data pipeline, keeping the deterministic
type-system planner authoritative. The baseline plugs the existing
``ProposalModel`` seam (bounded, advisory, auditioned) whenever an operator
chooses to consult it.
"""

from __future__ import annotations

from .baseline import (
    MIN_TRAINING_RUNS,
    MarkovPlanner,
    PlannerProposalModel,
)
from .config import (
    DEFAULT_MAX_PLAN_LEN,
    DEFAULT_VOCAB_CAPACITY,
    PLANNER_PRESETS,
    PlannerConfig,
    human_size,
    parameter_count,
    preset,
)
from .sequences import (
    PlanSequence,
    build_vocabulary,
    encode_run,
    encode_runs,
    export_token_jsonl,
)
from .vocab import (
    DEFAULT_GOAL_BUCKETS,
    SPECIAL_TOKENS,
    NodeVocabulary,
    goal_token,
)

__all__ = [
    # vocabulary — the new primitive
    "NodeVocabulary",
    "goal_token",
    "SPECIAL_TOKENS",
    "DEFAULT_GOAL_BUCKETS",
    # sequences — corpus in token-id space
    "PlanSequence",
    "encode_run",
    "encode_runs",
    "build_vocabulary",
    "export_token_jsonl",
    # architecture — the scaling ladder
    "PlannerConfig",
    "PLANNER_PRESETS",
    "preset",
    "parameter_count",
    "human_size",
    "DEFAULT_VOCAB_CAPACITY",
    "DEFAULT_MAX_PLAN_LEN",
    # baseline — the pure-Python occupant of the seat
    "MarkovPlanner",
    "PlannerProposalModel",
    "MIN_TRAINING_RUNS",
]
