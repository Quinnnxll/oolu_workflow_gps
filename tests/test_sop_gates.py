"""G3 — SOPs speak gates: ``require_guard`` compiles to sop-provenance
guard edges that drive REAL skips, riding the sequential-promotion rule.

"Only send the invoice when the total is positive" is a human-authored
gate — compiled into the plan (never pasted into a prompt), evaluated by
the same deterministic gate scheduler G0 landed, on a FRESH route with no
trace history. A route that cannot express the guard is excluded; a
guard that contradicts the demonstrated order surfaces as a cycle —
never a silent reorder, never an unguarded run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oolu.orchestrator import (
    Blueprint,
    DagRouteRunner,
    ReservedAction,
    RoutePlan,
    apply_sop_to_blueprint,
)
from oolu.skills import parse_sop
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
)

GUARD_SOP = """
sop: invoice-discipline
require_guard:
  - operation: send_invoice
    source: compute_total
    when: {pointer: total, op: ">", value: 0}
"""


def _blueprint(*operations: str) -> Blueprint:
    # The DEFAULT shape a fresh skill gets: sequential, no edges, no
    # trace history — exactly the blueprint the promotion rule must lift.
    return Blueprint(
        name="invoicing",
        actions=[
            ReservedAction(
                action=ActionEvent(
                    correlation_id="c", adapter="stub", operation=op
                ),
                required_capabilities=frozenset({op}),
                risk="write",
            )
            for op in operations
        ],
    )


class StubExecutor:
    """Scripted per-operation evidence — the gate scheduler's stand-in."""

    name = "stub"

    def __init__(self, evidence=None):
        self._evidence = dict(evidence or {})
        self.order: list[str] = []

    def capabilities(self):
        return frozenset({"compute_total", "send_invoice", "archive"})

    def execute(self, action, *, idempotency_key):
        self.order.append(action.operation)
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED,
            evidence=self._evidence.get(action.operation, {}),
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key):
        return None


def _run(blueprint, evidence):
    executor = StubExecutor(evidence=evidence)
    record = DagRouteRunner({"stub": executor}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k",
        attempt=1,
    )
    return record, executor


# --------------------------------------------------------------------------- #
# Parsing.                                                                     #
# --------------------------------------------------------------------------- #
def test_require_guard_parses_with_a_defaulted_predicate_name():
    sop = parse_sop(GUARD_SOP)
    (rule,) = sop.require_guard
    assert rule.operation == "send_invoice"
    assert rule.source == "compute_total"
    assert rule.when.pointer == "total" and rule.when.op == ">"
    assert rule.when.name == "total > 0"  # named for the decline reason


# --------------------------------------------------------------------------- #
# The loop-closure: a human gate drives a real skip on a fresh route.          #
# --------------------------------------------------------------------------- #
def test_sop_guard_skips_the_branch_on_a_fresh_sequential_route():
    sop = parse_sop(GUARD_SOP)
    blueprint = _blueprint("compute_total", "send_invoice")
    assert blueprint.ordering == "sequential"  # the default, no history

    compiled = apply_sop_to_blueprint(blueprint, sop)
    assert not compiled.excluded
    # The promotion rule lifted the chain: explicit graph, sop guard edge.
    assert compiled.ordering == "graph"
    guard_edges = [e for e in compiled.edges if e.relation == "guard"]
    assert len(guard_edges) == 1
    assert guard_edges[0].provenance == "sop"
    assert guard_edges[0].guard is not None
    assert guard_edges[0].guard.name == "total > 0"

    # A zero total: the guard DECLINES and send_invoice is skipped — a
    # real skip in a real run, not a prompt the model may ignore.
    record, executor = _run(compiled, {"compute_total": {"total": 0}})
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["compute_total"]  # the invoice never sent

    # A positive total: the same compiled plan sends it.
    record, executor = _run(compiled, {"compute_total": {"total": 250}})
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["compute_total", "send_invoice"]


def test_a_route_that_cannot_express_the_guard_is_excluded():
    sop = parse_sop(GUARD_SOP)
    # No compute_total step: the guard has no evidence producer — the
    # route is excluded, never run unguarded.
    compiled = apply_sop_to_blueprint(_blueprint("send_invoice"), sop)
    assert compiled.excluded
    assert "cannot express" in (compiled.exclusion_reason or "")
    # And no gated step at all excludes the same way.
    compiled = apply_sop_to_blueprint(_blueprint("compute_total"), sop)
    assert compiled.excluded


def test_a_guard_against_the_chain_is_a_cycle_never_a_reorder():
    # The SOP guards compute_total on send_invoice's evidence — but the
    # demonstrated chain runs compute_total FIRST. The guard's structural
    # edge reverses the chain: the compiled plan carries a cycle, and the
    # run refuses LOUDLY at preflight. Nothing runs, nothing reorders.
    contradiction = parse_sop(
        "sop: backwards\n"
        "require_guard:\n"
        "  - operation: compute_total\n"
        "    source: send_invoice\n"
        "    when: {pointer: sent, op: ==, value: true}\n"
    )
    compiled = apply_sop_to_blueprint(
        _blueprint("compute_total", "send_invoice"), contradiction
    )
    assert not compiled.excluded  # both steps exist — the shape compiles
    record, executor = _run(compiled, {})
    # BLOCKED at preflight: the plan was refused BEFORE anything executed.
    assert record.status is ExecutionStatus.BLOCKED
    assert "cycle" in (record.error or "")
    assert executor.order == []  # nothing ran, nothing silently reordered


def test_sops_without_guards_keep_the_sequential_shape():
    # The byte-compat pin: promotion is a NO-OP for every existing SOP —
    # a require_order SOP on a sequential blueprint stays sequential.
    sop = parse_sop("sop: order-only\nrequire_order: [compute_total, archive]\n")
    compiled = apply_sop_to_blueprint(
        _blueprint("compute_total", "archive"), sop
    )
    assert not compiled.excluded
    assert compiled.ordering == "sequential"
    sop_edges = [e for e in compiled.edges if e.provenance == "sop"]
    assert len(sop_edges) == 1 and sop_edges[0].relation == "before"


def test_guard_pattern_matching_reaches_adapter_qualified_names():
    # The same fnmatch discipline as every SOP rule: 'stub/send_*' gates
    # by adapter-qualified name.
    sop = parse_sop(
        "sop: qualified\n"
        "require_guard:\n"
        "  - operation: 'stub/send_*'\n"
        "    source: 'compute_*'\n"
        "    when: {pointer: total, op: '>', value: 0}\n"
    )
    compiled = apply_sop_to_blueprint(
        _blueprint("compute_total", "send_invoice"), sop
    )
    assert not compiled.excluded
    assert any(e.relation == "guard" for e in compiled.edges)


# --------------------------------------------------------------------------- #
# G3.1 — the review's holes, closed.                                           #
# --------------------------------------------------------------------------- #
def test_two_predicates_on_one_pair_both_compile_and_both_gate():
    # The review's blocker: dedup keyed on (source, target) alone dropped
    # a second, differently-predicated rule. Both rules now compile; the
    # scheduler ANDs them, so failing EITHER gate skips the step.
    sop = parse_sop(
        "sop: double-gate\n"
        "require_guard:\n"
        "  - operation: send_invoice\n"
        "    source: compute_total\n"
        "    when: {pointer: total, op: '>', value: 0}\n"
        "  - operation: send_invoice\n"
        "    source: compute_total\n"
        "    when: {pointer: approved, op: '==', value: true}\n"
    )
    compiled = apply_sop_to_blueprint(
        _blueprint("compute_total", "send_invoice"), sop
    )
    assert not compiled.excluded
    guards = [e for e in compiled.edges if e.relation == "guard"]
    assert len(guards) == 2  # BOTH human rules stand

    # total ok, approval missing: the second gate declines — skipped.
    record, executor = _run(
        compiled, {"compute_total": {"total": 100, "approved": False}}
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["compute_total"]
    # Both satisfied: the invoice goes.
    record, executor = _run(
        compiled, {"compute_total": {"total": 100, "approved": True}}
    )
    assert executor.order == ["compute_total", "send_invoice"]


def test_apply_is_idempotent_for_guards():
    sop = parse_sop(GUARD_SOP)
    once = apply_sop_to_blueprint(
        _blueprint("compute_total", "send_invoice"), sop
    )
    twice = apply_sop_to_blueprint(once, sop)
    assert len([e for e in twice.edges if e.relation == "guard"]) == 1


def test_a_target_whose_only_source_is_itself_excludes_the_route():
    # The review's silent-narrowing hole: 'send_*' gates send_receipt AND
    # send_invoice, but send_receipt's only source match is ITSELF — it
    # would have run unguarded. The route now excludes, loudly.
    sop = parse_sop(
        "sop: self-source\n"
        "require_guard:\n"
        "  - operation: 'send_*'\n"
        "    source: send_receipt\n"
        "    when: {pointer: ok, op: '==', value: true}\n"
    )
    compiled = apply_sop_to_blueprint(
        _blueprint("send_receipt", "send_invoice"), sop
    )
    assert compiled.excluded
    assert "cannot express" in (compiled.exclusion_reason or "")


def test_an_ambiguous_multi_source_pattern_excludes_with_the_names():
    # A guard reads ONE producer's evidence. A pattern naming several is
    # ambiguous — under the all-join it would AND the predicate across
    # producers the author never meant — so the route refuses, candidates
    # named, instead of compiling a gate that means something else.
    sop = parse_sop(
        "sop: ambiguous\n"
        "require_guard:\n"
        "  - operation: send_invoice\n"
        "    source: 'compute_*'\n"
        "    when: {pointer: total, op: '>', value: 0}\n"
    )
    compiled = apply_sop_to_blueprint(
        _blueprint("compute_total", "compute_tax", "send_invoice"), sop
    )
    assert compiled.excluded
    reason = compiled.exclusion_reason or ""
    assert "ambiguous" in reason
    assert "compute_tax" in reason and "compute_total" in reason


def test_a_later_sops_order_rule_survives_promotion_as_sop_provenance():
    # The review's absorption hole: a guard-SOP promotes the chain to
    # explicit provenance="learned" edges; a LATER order-SOP's rule was
    # then deduped away — the human's ordering lived only as prunable
    # learned structure. The sop edge is now always added.
    guard_sop = parse_sop(GUARD_SOP)
    order_sop = parse_sop(
        "sop: order\nrequire_order: [compute_total, send_invoice]\n"
    )
    blueprint = _blueprint("compute_total", "send_invoice")
    for sops in ([guard_sop, order_sop], [order_sop, guard_sop]):
        compiled = blueprint
        for sop in sops:
            compiled = apply_sop_to_blueprint(compiled, sop)
        sop_befores = [
            e
            for e in compiled.edges
            if e.relation == "before" and e.provenance == "sop"
        ]
        assert len(sop_befores) == 1, (
            "the human's ordering must stand as sop provenance in EITHER "
            "application order"
        )


def test_unknown_keys_in_a_guard_refuse_loudly():
    import pytest

    with pytest.raises(Exception, match="unknown key"):
        parse_sop(
            "sop: typo\n"
            "require_guard:\n"
            "  - operation: send_invoice\n"
            "    source: compute_total\n"
            "    when: {pointer: total, op: '>', value: 0, unles: retry}\n"
        )
    with pytest.raises(Exception):
        parse_sop(
            "sop: typo2\n"
            "require_guard:\n"
            "  - operation: send_invoice\n"
            "    source: compute_total\n"
            "    excempt: true\n"
            "    when: {pointer: total, op: '>', value: 0}\n"
        )
