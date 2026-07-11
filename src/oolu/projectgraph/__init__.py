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
    OBJECT_STATUSES,
    ConstraintSpec,
    GraphObject,
    GraphProposal,
    GraphScopes,
    PatchOp,
    ProposalResult,
    evaluate_constraint,
    path_covered,
)
from .store import ProjectGraphStore

__all__ = [
    "OBJECT_STATUSES",
    "ConstraintSpec",
    "GraphObject",
    "GraphProposal",
    "GraphScopes",
    "PatchOp",
    "ProposalResult",
    "ProjectGraphStore",
    "TransactionKernel",
    "evaluate_constraint",
    "path_covered",
]
