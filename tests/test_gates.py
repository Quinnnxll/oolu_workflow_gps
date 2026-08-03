"""Gate semantics: admissions, joins, verdicts — pure, no runner, no threads."""

from __future__ import annotations

import pytest

from oolu.orchestrator import Blueprint, BlueprintEdge, ReservedAction
from oolu.orchestrator.gates import (
    Admission,
    admit,
    dependency_edges,
    readiness,
    route_verdict,
)
from oolu.skills.models import ActionEvent, ExecutionStatus, Postcondition


def _action(operation: str, join: str = "all") -> ReservedAction:
    return ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation=operation),
        join=join,
    )


def _guard(pointer: str, op: str, value=None, name: str = "gate") -> Postcondition:
    return Postcondition(name=name, pointer=pointer, op=op, value=value)


def _before(source: str, target: str) -> BlueprintEdge:
    return BlueprintEdge(source=source, target=target)


def _guarded(source: str, target: str, guard: Postcondition) -> BlueprintEdge:
    return BlueprintEdge(source=source, target=target, relation="guard", guard=guard)


# --------------------------------------------------------------------------- #
# Edge construction refuses malformed gates loudly.                            #
# --------------------------------------------------------------------------- #
def test_guard_relation_without_predicate_refuses_by_name():
    with pytest.raises(ValueError, match="declares no predicate"):
        BlueprintEdge(source="a", target="b", relation="guard")


def test_predicate_on_before_edge_refuses_by_name():
    with pytest.raises(ValueError, match="rides only relation='guard'"):
        BlueprintEdge(
            source="a", target="b", guard=_guard("rows", ">", 0)
        )


# --------------------------------------------------------------------------- #
# Admissions.                                                                  #
# --------------------------------------------------------------------------- #
def test_before_edge_admissions():
    edge = _before("a", "b")
    assert admit(edge, ExecutionStatus.SUCCEEDED, {}) is Admission.ADMIT
    assert admit(edge, ExecutionStatus.FAILED, {}) is Admission.VETO
    assert admit(edge, ExecutionStatus.BLOCKED, {}) is Admission.VETO
    assert admit(edge, ExecutionStatus.CANCELLED, {}) is Admission.VETO
    assert admit(edge, ExecutionStatus.SKIPPED, {}) is Admission.DECLINE
    assert admit(edge, ExecutionStatus.PLANNED, {}) is Admission.WAIT
    assert admit(edge, None, None) is Admission.WAIT
    # A before edge needs no evidence — success admits even without a record.
    assert admit(edge, ExecutionStatus.SUCCEEDED, None) is Admission.ADMIT


def test_guard_edge_admissions_read_evidence():
    edge = _guarded("a", "b", _guard("rows", ">", 0, name="has-rows"))
    assert admit(edge, ExecutionStatus.SUCCEEDED, {"rows": 3}) is Admission.ADMIT
    assert admit(edge, ExecutionStatus.SUCCEEDED, {"rows": 0}) is Admission.DECLINE
    assert admit(edge, ExecutionStatus.SUCCEEDED, {}) is Admission.DECLINE
    assert admit(edge, ExecutionStatus.FAILED, {"rows": 3}) is Admission.VETO
    assert admit(edge, ExecutionStatus.SKIPPED, {"rows": 3}) is Admission.DECLINE
    assert admit(edge, None, None) is Admission.WAIT


def test_guard_requires_recorded_evidence_by_named_rule():
    # SUCCEEDED with no recorded outcome (a retired fallback): the guard
    # declines — a predicate over evidence nobody wrote is not a decision.
    edge = _guarded("a", "b", _guard("anything", "exists", name="ran"))
    assert admit(edge, ExecutionStatus.SUCCEEDED, None) is Admission.DECLINE


# --------------------------------------------------------------------------- #
# Join combination — deterministic under races.                                #
# --------------------------------------------------------------------------- #
def test_all_join_veto_cancels_immediately_even_while_waiting():
    edges = [_before("a", "d"), _before("b", "d")]
    verdict = readiness(
        "d", edges, "all", {"a": ExecutionStatus.FAILED}, {"a": {}}
    )
    assert verdict.verdict == "cancel"
    assert "dependency failed: a" in verdict.reason


def test_all_join_decline_defers_until_every_edge_settles():
    # One guard declined, one source still unsettled: the target must WAIT —
    # settling skip now would let a later veto lose the race it should win.
    edges = [
        _guarded("a", "d", _guard("rows", ">", 0, name="has-rows")),
        _before("b", "d"),
    ]
    statuses = {"a": ExecutionStatus.SUCCEEDED}
    evidence = {"a": {"rows": 0}}
    assert readiness("d", edges, "all", statuses, evidence).verdict == "wait"
    # The other source verifies: now every edge settled, and the skip lands.
    statuses["b"] = ExecutionStatus.SUCCEEDED
    evidence["b"] = {}
    verdict = readiness("d", edges, "all", statuses, evidence)
    assert verdict.verdict == "skip"
    assert "guard 'has-rows' declined" in verdict.reason
    assert "observed 0" in verdict.reason


def test_all_join_all_admit_is_ready_and_no_edges_is_ready():
    edges = [_before("a", "d"), _before("b", "d")]
    statuses = {
        "a": ExecutionStatus.SUCCEEDED,
        "b": ExecutionStatus.SUCCEEDED,
    }
    assert readiness("d", edges, "all", statuses, {"a": {}, "b": {}}).verdict == "ready"
    assert readiness("root", [], "all", {}, {}).verdict == "ready"


def test_any_join_first_admit_is_ready_even_while_others_wait():
    edges = [_before("a", "d"), _before("b", "d")]
    statuses = {"a": ExecutionStatus.SUCCEEDED}
    assert readiness("d", edges, "any", statuses, {"a": {}}).verdict == "ready"


def test_any_join_settled_without_admit_cancels_on_veto_else_skips():
    edges = [
        _guarded("a", "d", _guard("rows", ">", 0, name="has-rows")),
        _before("b", "d"),
    ]
    statuses = {"a": ExecutionStatus.SUCCEEDED, "b": ExecutionStatus.FAILED}
    evidence = {"a": {"rows": 0}, "b": {}}
    assert readiness("d", edges, "any", statuses, evidence).verdict == "cancel"

    declined = [
        _guarded("a", "d", _guard("rows", ">", 0, name="has-rows")),
        _guarded("a", "d", _guard("rows", "<", 0, name="negative")),
    ]
    only_declines = readiness(
        "d", declined, "any", {"a": ExecutionStatus.SUCCEEDED}, {"a": {"rows": 0}}
    )
    assert only_declines.verdict == "skip"


# --------------------------------------------------------------------------- #
# Dependency derivation and the route verdict.                                 #
# --------------------------------------------------------------------------- #
def test_sequential_ordering_synthesizes_chain_and_skips_fallback_targets():
    blueprint = Blueprint(
        name="seq", actions=[_action("a"), _action("repair"), _action("b")]
    )
    ids = {item.action.operation: item.action.id for item in blueprint.actions}
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"], target=ids["repair"], relation="fallback"
                )
            ]
        }
    )
    edges = dependency_edges(blueprint)
    # The chain skips the dormant repair: a -> b directly.
    assert [e.source for e in edges[ids["b"]]] == [ids["a"]]
    assert edges[ids["repair"]] == []  # fallback edges never join the ordering
    assert edges[ids["a"]] == []


def test_route_verdict_counts_skipped_as_ok_and_failures_as_not():
    ok = {
        "a": ExecutionStatus.SUCCEEDED,
        "b": ExecutionStatus.SKIPPED,
        "c": ExecutionStatus.SUCCEEDED,
    }
    assert route_verdict(ok, {}) is True
    assert route_verdict({**ok, "d": ExecutionStatus.FAILED}, {}) is False
    # A failure whose ENTIRE fallback branch verified still counts.
    repaired = {"a": ExecutionStatus.FAILED, "r": ExecutionStatus.SUCCEEDED}
    assert route_verdict(repaired, {"a": {"r"}}) is True
    half = {"a": ExecutionStatus.FAILED, "r": ExecutionStatus.FAILED}
    assert route_verdict(half, {"a": {"r"}}) is False
