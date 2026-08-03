"""G2 — gate edges in the contract vocabulary, compiler, and gateway surface.

A SubgraphBody contract carrying guard/loop edges and joins compiles to a
blueprint the DAG scheduler executes as gates; legacy contracts compile
unchanged (under id normalization); a sequential blueprint promotes to
graph when a gate edge lands.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oolu.orchestrator import (
    Blueprint,
    BlueprintEdge,
    DagRouteRunner,
    ReservedAction,
    RoutePlan,
    compile_with_owners,
    contract_to_blueprint,
    promote_sequential_for_gates,
)
from oolu.skills.contract import (
    ActionsBody,
    ContractEdge,
    NodeContract,
    Slot,
    SubgraphBody,
)
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    Postcondition,
)


class StubExecutor:
    """Per-operation verdicts and evidence — the gate scheduler's stand-in."""

    name = "stub"

    def __init__(self, evidence=None, fail=None):
        self._evidence = dict(evidence or {})
        self._fail = set(fail or ())
        self.order = []

    def capabilities(self):
        return frozenset(
            {"a", "b", "c", "d", "work", "after", "x", "y", "z"}
        )

    def execute(self, action, *, idempotency_key):
        self.order.append(action.operation)
        ok = action.operation not in self._fail
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED if ok else ExecutionStatus.FAILED,
            evidence=self._evidence.get(action.operation, {}),
            error=None if ok else f"{action.operation} failed",
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key):
        return None


def _child(name: str, *, consumes=None, produces=None) -> NodeContract:
    return NodeContract(
        name=name,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="stub", operation=name)]
        ),
    )


def _guard(name, pointer, op, value=None) -> Postcondition:
    return Postcondition(name=name, pointer=pointer, op=op, value=value)


def _subgraph(children, edges=None, joins=None) -> NodeContract:
    return NodeContract(
        name="composed",
        provenance="synthesized",
        body=SubgraphBody(
            nodes=children, edges=list(edges or []), joins=dict(joins or {})
        ),
    )


def _run(contract):
    blueprint = contract_to_blueprint(contract)
    executor = StubExecutor(evidence={"a": {"rows": 3}})
    return blueprint, executor


# --------------------------------------------------------------------------- #
# ContractEdge validators (mirror BlueprintEdge).                              #
# --------------------------------------------------------------------------- #
def test_contract_edge_gate_validators():
    import pytest

    with pytest.raises(ValueError, match="declares no predicate"):
        ContractEdge(source="a", target="b", relation="guard")
    with pytest.raises(ValueError, match="rides only relation"):
        ContractEdge(source="a", target="b", guard=_guard("g", "rows", ">", 0))
    with pytest.raises(ValueError, match="unbounded loop"):
        ContractEdge(source="b", target="a", relation="loop")
    with pytest.raises(ValueError, match="must be >= 1"):
        ContractEdge(source="b", target="a", relation="loop", max_iterations=0)


# --------------------------------------------------------------------------- #
# Guard OR-split compiles and runs one branch.                                 #
# --------------------------------------------------------------------------- #
def test_guard_or_split_compiles_and_takes_one_branch():
    a, b, c, d = _child("a"), _child("b"), _child("c"), _child("d")
    contract = _subgraph(
        [a, b, c, d],
        edges=[
            ContractEdge(
                source=a.id, target=b.id, relation="guard",
                guard=_guard("has-rows", "rows", ">", 0),
            ),
            ContractEdge(
                source=a.id, target=c.id, relation="guard",
                guard=_guard("empty", "rows", "==", 0),
            ),
            ContractEdge(source=b.id, target=d.id),
            ContractEdge(source=c.id, target=d.id),
        ],
        joins={d.id: "any"},
    )
    blueprint = contract_to_blueprint(contract)
    # The guard predicate rode through to the blueprint edge.
    guard_edges = [e for e in blueprint.edges if e.relation == "guard"]
    assert len(guard_edges) == 2 and all(e.guard is not None for e in guard_edges)
    # The join landed on d's entry action.
    d_action = next(i for i in blueprint.actions if i.action.operation == "d")
    assert d_action.join == "any"

    executor = StubExecutor(evidence={"a": {"rows": 3}})
    record = DagRouteRunner({"stub": executor}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k", attempt=1,
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["a", "b", "d"]  # c's branch not taken


# --------------------------------------------------------------------------- #
# Loop edge compiles to one region; endpoint refusal.                          #
# --------------------------------------------------------------------------- #
def test_loop_edge_compiles_and_iterates():
    work = _child("work")
    contract = _subgraph(
        [work],
        edges=[
            ContractEdge(
                source=work.id, target=work.id, relation="loop",
                max_iterations=5,
                guard=_guard("more", "remaining", ">", 0),
            )
        ],
    )
    blueprint = contract_to_blueprint(contract)
    loop_edges = [e for e in blueprint.edges if e.relation == "loop"]
    assert len(loop_edges) == 1 and loop_edges[0].max_iterations == 5

    # Evidence counts down: two iterations then exits.
    class Counting(StubExecutor):
        def __init__(self):
            super().__init__()
            self._n = 3

        def execute(self, action, *, idempotency_key):
            self._n -= 1
            self._evidence = {"work": {"remaining": self._n}}
            return super().execute(action, idempotency_key=idempotency_key)

    executor = Counting()
    record = DagRouteRunner({"stub": executor}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k", attempt=1,
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["work", "work", "work"]  # 3rd emits remaining=0


def test_loop_endpoint_must_be_single_exit_single_entry():
    import pytest

    # A subgraph CHILD with two internal actions has two exits — a loop
    # onto it cannot map to one region, so the compiler refuses.
    multi = NodeContract(
        name="multi",
        provenance="synthesized",
        body=SubgraphBody(nodes=[_child("x"), _child("y")]),  # x,y parallel: 2 exits
    )
    head = _child("z")
    contract = _subgraph(
        [head, multi],
        edges=[
            ContractEdge(source=head.id, target=multi.id),
            ContractEdge(
                source=multi.id, target=head.id, relation="loop", max_iterations=2
            ),
        ],
    )
    with pytest.raises(ValueError, match="single-exit tail and a single-entry head"):
        contract_to_blueprint(contract)


# --------------------------------------------------------------------------- #
# Boundary derivation over structural edges (guard-entered child).             #
# --------------------------------------------------------------------------- #
def test_guard_entered_child_is_not_a_boundary_entry_when_nested():
    # inner: a --guard--> b. When inner is a child of an outer subgraph,
    # b must NOT be an outer entry (it is guard-entered inside), so an
    # outer edge into inner lands on a only.
    a, b = _child("a"), _child("b")
    inner = NodeContract(
        name="inner",
        provenance="synthesized",
        body=SubgraphBody(
            nodes=[a, b],
            edges=[
                ContractEdge(
                    source=a.id, target=b.id, relation="guard",
                    guard=_guard("g", "rows", ">", 0),
                )
            ],
        ),
    )
    seed = _child("seed")
    outer = _subgraph(
        [seed, inner],
        edges=[ContractEdge(source=seed.id, target=inner.id)],
    )
    blueprint = contract_to_blueprint(outer)
    # The seed→inner before-edge targets inner's entry (a), never b.
    before_into_inner = [
        e for e in blueprint.edges
        if e.relation == "before"
        and e.target in {
            i.action.id for i in blueprint.actions if i.action.operation == "a"
        }
    ]
    assert before_into_inner  # seed → a
    b_id = next(i.action.id for i in blueprint.actions if i.action.operation == "b")
    assert not any(e.target == b_id and e.relation == "before" for e in blueprint.edges)


# --------------------------------------------------------------------------- #
# Legacy contracts compile normalized-identical.                               #
# --------------------------------------------------------------------------- #
def test_legacy_contract_compiles_normalized_identical():
    # A plain before-chain contract compiles the same shape as before the
    # gate fields existed — action ids are fresh per compile, so compare
    # the NORMALIZED structure (operations + relations), not raw ids.
    def normalized(contract):
        bp, owners = compile_with_owners(contract)
        op = {i.action.id: i.action.operation for i in bp.actions}
        return (
            sorted(i.action.operation for i in bp.actions),
            sorted(i.join for i in bp.actions),
            sorted(
                (op[e.source], op[e.target], e.relation) for e in bp.edges
            ),
        )

    p = _child("p", produces=[Slot(name="s", value_type="str")])
    q = _child("q", consumes=[Slot(name="s", value_type="str")])
    contract = _subgraph([p, q])  # no explicit edges: data edge p→q derived
    first = normalized(contract)
    second = normalized(contract)
    assert first == second
    # Every action defaults to the "all" join (pre-gate behaviour).
    assert set(first[1]) == {"all"}
    assert ("p", "q", "before") in first[2]  # the derived data edge


# --------------------------------------------------------------------------- #
# Sequential promotion for gates.                                              #
# --------------------------------------------------------------------------- #
def test_sequential_blueprint_promotes_when_a_gate_edge_lands():
    a = ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation="a")
    )
    b = ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation="b")
    )
    # A sequential blueprint (implicit a→b chain) with a guard added.
    blueprint = Blueprint(
        name="seq",
        actions=[a, b],
        ordering="sequential",
        edges=[
            BlueprintEdge(
                source=a.action.id, target=b.action.id, relation="guard",
                guard=_guard("g", "rows", ">", 0),
            )
        ],
    )
    promoted = promote_sequential_for_gates(blueprint)
    assert promoted.ordering == "graph"
    # The implicit chain is now explicit (a before b), plus the guard.
    befores = [(e.source, e.target) for e in promoted.edges if e.relation == "before"]
    assert (a.action.id, b.action.id) in befores
    assert any(e.relation == "guard" for e in promoted.edges)


def test_promotion_is_a_noop_without_gate_edges():
    a = ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation="a")
    )
    blueprint = Blueprint(name="seq", actions=[a], ordering="sequential")
    assert promote_sequential_for_gates(blueprint) is blueprint
