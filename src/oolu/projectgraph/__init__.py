"""The Global Project Graph: external project memory, transactionally kept.

The industrial vertical's spine (docs/industrial-vertical-plan.md,
steps 1–2): typed, revisioned truth objects with constraints and
evidence; path-scoped territory granted by the project's owner; and a
transaction kernel that is the ONLY door through which truth changes —
models propose, the kernel commits, every decision lands in the
hash-chained audit log either way.
"""

from .kernel import TransactionKernel
from .models import (
    FINDING_SEVERITIES,
    OBJECT_STATUSES,
    ConstraintSpec,
    GraphObject,
    GraphProposal,
    GraphScopes,
    PatchOp,
    ProposalResult,
    build_finding,
    evaluate_constraint,
    path_covered,
)
from .planner import (
    PLANNER_SYSTEM_PROMPT,
    ModelPlanner,
    parse_step,
)
from .store import ProjectGraphStore

__all__ = [
    "FINDING_SEVERITIES",
    "OBJECT_STATUSES",
    "ConstraintSpec",
    "GraphObject",
    "GraphProposal",
    "GraphScopes",
    "PatchOp",
    "ProposalResult",
    "PLANNER_SYSTEM_PROMPT",
    "ProjectGraphStore",
    "TransactionKernel",
    "ModelPlanner",
    "parse_step",
    "build_finding",
    "evaluate_constraint",
    "path_covered",
]
