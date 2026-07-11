"""The Global Project Graph's vocabulary — typed, revisioned truth.

The graph is the only source of APPROVED project truth (the industrial
vertical's external project memory): every object carries its revision,
its owner, its constraints, and the evidence behind it; every change
arrives as a structured PROPOSAL of patch operations against declared
base revisions, and only the transaction kernel may commit one.

Deliberately small at birth: objects, constraints, patch ops, proposals,
scopes, and the result shape. What a field MEANS for suspension arms or
render scenes lives in the objects' own parameters — the graph stores
truth, it does not model domains.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..predicates import check, pointer_root


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


# The proposal lifecycle an object's ``status`` may hold — model output
# is not project truth until it climbs this ladder.
OBJECT_STATUSES = (
    "draft",
    "proposed",
    "verified",
    "approved",
    "released",
    "superseded",
)

# Where a ``set`` pointer may reach. ``parameters`` and
# ``native_references`` are nested (slash pointers walk into them);
# ``relations``, ``evidence``, and ``status`` replace whole.
_NESTED_ROOTS = ("parameters", "native_references")
_WHOLE_ROOTS = ("relations", "evidence", "status")


class ConstraintSpec(BaseModel):
    """One declarative check an object must honor.

    ``hard`` constraints are walls: once one PASSES at a committed
    revision, the kernel refuses any patch that would regress it.
    ``soft`` constraints only warn. The check is a comparison against a
    pointer into the object's own payload — deterministic, evaluable
    without any model."""

    model_config = ConfigDict(frozen=True)

    name: str
    severity: Literal["hard", "soft"] = "hard"
    pointer: str  # e.g. "parameters/front/y_mm"
    op: Literal["<=", ">=", "<", ">", "==", "!=", "exists"]
    value: Any = None


class GraphObject(BaseModel):
    """One node of project truth, exactly the build spec's shape."""

    model_config = ConfigDict(frozen=True)

    # Stamped by the kernel from the proposal — a create op's payload
    # never needs to repeat (or gets to lie about) its project.
    project_id: str = ""
    object_id: str = Field(default_factory=_id)
    # The object's place in the project tree ("subsystems/suspension/
    # front") — what read/write scopes are granted against.
    path: str
    type: str
    revision: int = 1
    status: str = "draft"
    # The principal answering for this object.
    owner: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    native_references: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_now)


class PatchOp(BaseModel):
    """One structured change. Never a rewrite — an exact, checkable step.

    - ``create``: a brand-new object (carried whole in ``object``).
    - ``set``: change ONE pointed-at value on an existing object; the
      declared ``base_revision`` and ``old_value`` must both match what
      the graph actually holds — a stale idea of the world is rejected,
      never merged.
    - ``append``: add ONE entry to ``evidence`` or ``relations`` — how
      an observation (a verified run's postconditions, a critic's
      finding) is FILED onto truth without racing a whole-list replace.
    - ``supersede``: retire an object (status -> superseded). Truth is
      append-only: nothing is ever erased, history keeps every revision.
    """

    model_config = ConfigDict(frozen=True)

    op: Literal["create", "set", "append", "supersede"]
    object_id: str = ""
    base_revision: int | None = None
    pointer: str = ""
    old_value: Any = None
    new_value: Any = None
    object: GraphObject | None = None


class GraphProposal(BaseModel):
    """A model's (or human's) offered change — words and ops, no writes."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(default_factory=_id)
    project_id: str
    # The principal submitting; the kernel checks THEIR scopes.
    owner: str = ""
    # The node whose work produced this proposal, when one did.
    node_id: str | None = None
    # Every change includes a reason — the core rule, enforced.
    reason: str
    patch: list[PatchOp]
    expected_effects: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None


class GraphScopes(BaseModel):
    """A principal's read/write territory inside one project.

    Path-prefix semantics, the same shape as the machine allowlist and
    the egress grants: a scope names a subtree; ``forbidden`` always
    wins. The project's owner needs no scopes — everyone else starts
    with NOTHING until the owner grants territory. Fail closed."""

    model_config = ConfigDict(frozen=True)

    principal: str
    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)


class ProposalResult(BaseModel):
    """What the kernel decided, in words — commit or reject, never silence."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    status: Literal["committed", "rejected"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # object_id -> the revision this commit produced.
    revisions: dict[str, int] = Field(default_factory=dict)


# A finding's weight: blocking findings stop an object's status from
# advancing to approved/released while open; the rest inform.
FINDING_SEVERITIES = ("blocking", "major", "minor")


def build_finding(
    *,
    target: "GraphObject",
    critic: str,
    severity: str,
    finding: str,
    evidence: dict[str, Any],
    recommended_action: str,
    affected_requirement: str | None = None,
) -> "GraphObject":
    """A critic's finding, shaped as a graph object of type ``finding``.

    It lives under ``issues/{target path}`` — the territory an owner
    grants critics WITHOUT opening the design itself, so "critics
    submit findings, not rewrites" is enforced by scopes, not
    etiquette. Evidence is required by the door: a finding without
    evidence is an opinion."""
    return GraphObject(
        path=f"issues/{target.path}",
        type="finding",
        owner=critic,
        parameters={
            "target": target.object_id,
            "severity": severity,
            "finding": finding,
            "recommended_action": recommended_action,
            "affected_requirement": affected_requirement,
            "state": "open",
        },
        evidence=[evidence],
        provenance={"critic": critic},
    )


def path_covered(path: str, scopes: list[str]) -> bool:
    """Prefix-subtree matching: scope "a/b" covers "a/b" and "a/b/c",
    never "a/bc" — the same wall shape as the host allowlist."""
    for scope in scopes:
        scope = scope.strip("/")
        if not scope:
            continue
        if path == scope or path.startswith(scope + "/"):
            return True
    return False


def valid_set_pointer(pointer: str) -> bool:
    """A ``set`` may reach nested values under parameters/
    native_references, or replace relations/evidence/status whole."""
    root = pointer_root(pointer)
    if root in _NESTED_ROOTS:
        return True
    return pointer.strip("/") in _WHOLE_ROOTS


def evaluate_constraint(
    payload: dict[str, Any], constraint: ConstraintSpec
) -> bool:
    """Deterministic verdict for one constraint against one payload —
    the shared predicate language, so an object's walls and an action's
    postconditions are judged by the same code."""
    return check(payload, constraint.pointer, constraint.op, constraint.value)
