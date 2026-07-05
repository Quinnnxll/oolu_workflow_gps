"""NodeContract — the one node schema the three vocabularies unify into.

Item 6 of the planning review: ``ReusableSkill`` (skills), ``ReservedAction``/
``Blueprint`` (orchestrator), and the marketplace node economics all describe
the same thing — a unit of automation with typed inputs, typed effects,
verification, risk, and history. ``NodeContract`` is that thing said once:

- **consumes / produces** are typed ``Slot``s. This is what makes ordering
  *derivable*: node B depends on node A iff B consumes a slot A produces
  (``derive_data_edges``) — dependencies emerge from slot unification, and
  unrelated nodes are parallel by default.
- **body** is one of the three kinds the runtime already executes:
  ``ActionsBody`` (replayed adapter actions), ``ScriptBody`` (a synthesized,
  node-cached script for ``NodeScriptRunner``), or ``SubgraphBody`` (nested
  contracts — a learned super-node).
- **preconditions / validators** are the existing ``ConstraintSpec``s;
  verification stays binary and authoritative.
- **risk** uses the same verb taxonomy the orchestrator's human-control
  machinery gates on (``classify_risk`` is canonical here; the orchestrator
  re-exports it).
- **fallback** is a contract, not a flag — it compiles to the scheduler's
  fallback edges, so every contract can carry its own repair branch.
- **stats** is a snapshot of the trace/metering posteriors, so planners and
  the marketplace rank contracts by the same verified history.

The compilation to an executable ``Blueprint`` lives orchestrator-side
(``orchestrator.contract.contract_to_blueprint``), following the same
layering as SOPs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    SKILL_SCHEMA_VERSION,
    ActionEvent,
    ConstraintSpec,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)

_READ_VERBS = frozenset(
    {"get", "list", "read", "search", "fetch", "describe", "show", "query", "view"}
)
_IRREVERSIBLE_VERBS = frozenset(
    {"delete", "remove", "drop", "destroy", "purge", "revoke", "wipe"}
)


def classify_risk(operation: str) -> str:
    """Verb-based risk class: ``read`` | ``write`` | ``irreversible``.

    Canonical home of the taxonomy every layer gates on (the orchestrator's
    planner re-exports this function).
    """
    verb = operation.split(".")[-1].split("_")[0].split("-")[0].lower()
    if verb in _IRREVERSIBLE_VERBS:
        return "irreversible"
    if verb in _READ_VERBS:
        return "read"
    return "write"


def _id() -> str:
    return uuid4().hex


class Slot(BaseModel):
    """One typed input (consumes) or effect (produces) of a node."""

    model_config = ConfigDict(frozen=True)

    name: str
    value_type: str = "str"
    role: str | None = None  # e.g. "path"
    required: bool = True
    description: str = ""

    def matches(self, other: "Slot") -> bool:
        """Type-unification: a produced slot satisfies a consumed slot when
        name and value type agree (role, when both declare one, must too)."""
        if self.name != other.name or self.value_type != other.value_type:
            return False
        if self.role and other.role and self.role != other.role:
            return False
        return True


class ValueInput(BaseModel):
    """One declared creative input: the hole a value patcher may fill.

    The deterministic scaffolding of a workflow (open the app, open the
    file, select the tool) is parameterized actions; the *creative* part
    is values — dimensions, coordinates, text. A node declares those
    values here instead of hardcoding them: name, type, an honest
    default, and the bounds within which ANY filler (a user, an LLM
    patcher, a default) must stay. The node adapts the model — via the
    description, the default, and the bounds — never the other way
    around, and the verification predicate stays sovereign because
    admissible values are boxed in at declaration time.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    value_type: Literal["number", "string", "choice"] = "number"
    default: float | str | None = None
    # Numbers clamp into [minimum, maximum]; a filler cannot leave the box.
    minimum: float | None = None
    maximum: float | None = None
    # For value_type="choice": the closed set of admissible strings.
    choices: list[str] | None = None
    required: bool = True


class BodyKind(str, Enum):
    ACTIONS = "actions"
    SCRIPT = "script"
    SUBGRAPH = "subgraph"


class ActionsBody(BaseModel):
    """Replayed adapter actions — the demonstrated/learned skill body."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[BodyKind.ACTIONS] = BodyKind.ACTIONS
    actions: list[ActionEvent]


class ScriptBody(BaseModel):
    """A synthesized, node-cached script (executed by ``NodeScriptRunner``)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[BodyKind.SCRIPT] = BodyKind.SCRIPT
    goal: str
    node_key: str = ""  # defaults to the contract id at compile time
    bindings: dict[str, Any] = Field(default_factory=dict)


class ContractEdge(BaseModel):
    """An explicit dependency between two child contracts of a subgraph."""

    model_config = ConfigDict(frozen=True)

    source: str  # child contract id
    target: str
    relation: Literal["before", "fallback"] = "before"
    provenance: Literal["sop", "learned", "data"] = "learned"


class SubgraphBody(BaseModel):
    """Nested contracts — a composed super-node. Ordering = explicit edges
    plus the data-flow edges derived from the children's slots."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[BodyKind.SUBGRAPH] = BodyKind.SUBGRAPH
    nodes: list["NodeContract"]
    edges: list[ContractEdge] = Field(default_factory=list)


NodeBody = Union[ActionsBody, ScriptBody, SubgraphBody]


class NodeStats(BaseModel):
    """A snapshot of verified history (trace/metering posteriors).

    Counts are floats: recency-decayed trace posteriors weigh an old
    observation as a fraction of a fresh one. Marketplace-verified counts
    stay whole numbers.
    """

    model_config = ConfigDict(frozen=True)

    successes: float = 0
    failures: float = 0
    cost_ewma: float | None = None

    @property
    def success_mean(self) -> float:
        return (1.0 + self.successes) / (2.0 + self.successes + self.failures)


class NodeContract(BaseModel):
    """The unified node: typed I/O, one body, verification, risk, history."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    name: str
    version: str = "0.1.0"
    description: str = ""
    provenance: Literal["demonstrated", "synthesized", "human", "marketplace"] = (
        "demonstrated"
    )
    demonstration_ids: list[str] = Field(default_factory=list)

    consumes: list[Slot] = Field(default_factory=list)
    produces: list[Slot] = Field(default_factory=list)
    # Declared creative inputs: the values a patcher (user, LLM, default)
    # fills before execution. Actions reference them via {"$input": name}
    # / {"$template": "..."} placeholders in their parameters.
    inputs: list[ValueInput] = Field(default_factory=list)
    preconditions: list[ConstraintSpec] = Field(default_factory=list)
    validators: list[ConstraintSpec] = Field(default_factory=list)

    body: NodeBody = Field(discriminator="kind")
    fallback: "NodeContract | None" = None

    stats: NodeStats | None = None

    @property
    def risk(self) -> str:
        """The contract's risk class: the worst class among its operations."""
        order = {"read": 0, "write": 1, "irreversible": 2}
        worst = "read"
        for operation in self._operations():
            candidate = classify_risk(operation)
            if order[candidate] > order[worst]:
                worst = candidate
        return worst

    def _operations(self) -> list[str]:
        if isinstance(self.body, ActionsBody):
            return [action.operation for action in self.body.actions]
        if isinstance(self.body, ScriptBody):
            return ["run"]  # synthesized code is write-class by the verb rule
        return [op for node in self.body.nodes for op in node._operations()]

    # ------------------------------------------------------------------ #
    # Converters: the three vocabularies, losslessly.                     #
    # ------------------------------------------------------------------ #
    @classmethod
    def from_skill(
        cls,
        skill: ReusableSkill,
        *,
        provenance: Literal[
            "demonstrated", "synthesized", "human", "marketplace"
        ] = "demonstrated",
        produces: list[Slot] | None = None,
        stats: NodeStats | None = None,
    ) -> "NodeContract":
        """A learned/marketplace skill as a contract (actions body).

        ``consumes`` comes from the skill's induced parameters; ``produces``
        defaults to the artifact slots implied by an expected-artifacts
        validator, and may be overridden by a planner that knows better.
        """
        consumes = [
            Slot(
                name=p.name,
                value_type=p.value_type,
                role=p.domain.get("role") if isinstance(p.domain, dict) else None,
                required=p.required,
                description=p.description or "",
            )
            for p in skill.parameters
        ]
        if produces is None:
            produces = []
            for validator in skill.validators:
                for path in validator.evidence.get("expected_files", []) or []:
                    produces.append(
                        Slot(name=str(path), value_type="path", role="path")
                    )
        return cls(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            provenance=provenance,
            demonstration_ids=list(skill.demonstration_ids),
            consumes=consumes,
            produces=produces,
            preconditions=list(skill.preconditions),
            validators=list(skill.validators),
            body=ActionsBody(actions=list(skill.actions)),
            stats=stats,
        )

    def to_skill(self, *, signature: SkillSignature | None = None) -> ReusableSkill:
        """Round-trip an actions-bodied contract back to a ``ReusableSkill``."""
        if not isinstance(self.body, ActionsBody):
            raise ValueError(
                f"only actions-bodied contracts round-trip to skills; "
                f"this body is '{self.body.kind.value}'"
            )
        parameters = [
            SkillParameter(
                name=slot.name,
                value_type=slot.value_type,
                required=slot.required,
                description=slot.description or None,
                domain={"role": slot.role} if slot.role else {},
            )
            for slot in self.consumes
        ]
        return ReusableSkill(
            id=self.id,
            name=self.name,
            description=self.description,
            signature=signature or SkillSignature(application="learned", adapter="cli"),
            parameters=parameters,
            preconditions=list(self.preconditions),
            actions=list(self.body.actions),
            validators=list(self.validators),
            demonstration_ids=list(self.demonstration_ids),
        )


def derive_data_edges(nodes: list[NodeContract]) -> list[ContractEdge]:
    """Dependencies from slot unification: B depends on A iff B consumes a
    slot A produces. Nodes with no such relation stay parallel — the payoff
    of typed contracts. Mutual production yields both edges, which the
    blueprint's cycle preflight rejects loudly rather than guessing."""
    edges: list[ContractEdge] = []
    for producer in nodes:
        for consumer in nodes:
            if producer.id == consumer.id:
                continue
            if any(
                produced.matches(consumed)
                for produced in producer.produces
                for consumed in consumer.consumes
            ):
                edges.append(
                    ContractEdge(
                        source=producer.id,
                        target=consumer.id,
                        relation="before",
                        provenance="data",
                    )
                )
    return edges
