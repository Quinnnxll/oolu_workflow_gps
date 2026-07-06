"""Shared Pydantic vocabulary for OoLu.

Built first, on purpose: every other layer (runtime, routing, graph, knowledge)
imports its types from here, so this package depends on nothing but stdlib +
pydantic and can never participate in a circular import.

Import direction within the package:
    errors  ->  results  ->  knowledge / state
(errors is the root; nothing it touches imports back up.)
"""

from __future__ import annotations

from .errors import (
    ErrorClass,
    ErrorRecord,
    compute_signature,
    normalise_message,
)
from .knowledge import (
    DependencyHint,
    ErrorPattern,
    KnowledgeSource,
    RecalcStrategy,
)
from .results import ExecutionResult, Phase
from .state import (
    ExecutionPlan,
    GraphState,
    GraphStatus,
    ModelTier,
)

__all__ = [
    # errors
    "ErrorClass",
    "ErrorRecord",
    "compute_signature",
    "normalise_message",
    # results
    "ExecutionResult",
    "Phase",
    # knowledge
    "DependencyHint",
    "ErrorPattern",
    "KnowledgeSource",
    "RecalcStrategy",
    # state
    "ExecutionPlan",
    "GraphState",
    "GraphStatus",
    "ModelTier",
]
