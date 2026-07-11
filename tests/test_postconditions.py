"""Postconditions and observation — step 3 of the industrial vertical.

The production loop's evaluator: the model proposes, the runtime
executes, and the EVALUATOR verifies — an action declares what the
observed state must show, the executor reports what it saw, and a run
that succeeds by the API but breaks a promise is demoted to a failure
with the exact broken promise in words. Both route runners judge every
hand the same way, and a verified observation can be FILED onto the
project graph as appended evidence through the kernel.
"""

from __future__ import annotations

import httpx

from oolu.durable import DurableConnection
from oolu.orchestrator import ActionExecutorRouteRunner
from oolu.orchestrator.state import Blueprint, ReservedAction, RoutePlan
from oolu.projectgraph import (
    GraphObject,
    GraphProposal,
    PatchOp,
    ProjectGraphStore,
    TransactionKernel,
)
from oolu.skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    Postcondition,
    verify_postconditions,
)

MASS_OK = Postcondition(
    name="mass-budget", pointer="result/mass_kg", op="<=", value=3.5
)
NO_CLASH = Postcondition(
    name="no-interference", pointer="result/interference_count", op="==", value=0
)


def _action(*promises: Postcondition) -> ActionEvent:
    return ActionEvent(
        correlation_id="c1",
        adapter="cad",
        operation="run",
        postconditions=list(promises),
    )


def _outcome(evidence: dict, status=ExecutionStatus.SUCCEEDED) -> ExecutionOutcome:
    return ExecutionOutcome(
        idempotency_key="k1", skill_id="s1", status=status, evidence=evidence
    )


# --------------------------------------------------------------------------- #
# The evaluator itself.                                                        #
# --------------------------------------------------------------------------- #
def test_kept_promises_stay_succeeded_with_the_verdict_on_record():
    verdict = verify_postconditions(
        _action(MASS_OK, NO_CLASH),
        _outcome({"result": {"mass_kg": 3.42, "interference_count": 0}}),
    )
    assert verdict.status is ExecutionStatus.SUCCEEDED
    assert verdict.evidence["postconditions"] == {
        "checked": 2,
        "verified": True,
        "observed": {"mass-budget": 3.42, "no-interference": 0},
    }


def test_a_broken_promise_demotes_success_to_failure_in_words():
    verdict = verify_postconditions(
        _action(MASS_OK, NO_CLASH),
        _outcome({"result": {"mass_kg": 4.8, "interference_count": 2}}),
    )
    assert verdict.status is ExecutionStatus.FAILED
    # EVERY miss is named, not just the first.
    assert "mass-budget" in verdict.error and "no-interference" in verdict.error
    assert "observed 4.8" in verdict.error
    assert verdict.evidence["postconditions"]["verified"] is False


def test_missing_observations_and_type_junk_fail_never_raise():
    silent = verify_postconditions(_action(MASS_OK), _outcome({}))
    assert silent.status is ExecutionStatus.FAILED
    assert "observed nothing" in silent.error
    junk = verify_postconditions(
        _action(MASS_OK), _outcome({"result": {"mass_kg": "heavy"}})
    )
    assert junk.status is ExecutionStatus.FAILED


def test_only_success_is_judged_and_no_promises_means_no_verdict():
    failed = _outcome({"result": {}}, status=ExecutionStatus.FAILED)
    assert verify_postconditions(_action(MASS_OK), failed) is failed
    plain = _outcome({"anything": 1})
    assert verify_postconditions(_action(), plain) is plain


# --------------------------------------------------------------------------- #
# The runners judge every hand.                                                #
# --------------------------------------------------------------------------- #
class _Hand:
    """An executor that succeeds and reports what it 'measured'."""

    name = "cad"

    def __init__(self, evidence):
        self._evidence = evidence

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
            evidence=self._evidence,
        )

    def cancel(self, idempotency_key):
        return None


def _route(action: ActionEvent) -> RoutePlan:
    return RoutePlan(
        chosen=Blueprint(
            name="cad-run", actions=[ReservedAction(action=action)]
        ),
        alternatives=[],
    )


def test_the_route_runner_demotes_an_api_success_that_broke_its_promise():
    runner = ActionExecutorRouteRunner(
        {"cad": _Hand({"result": {"mass_kg": 9.9}})}
    )
    record = runner.execute(
        _route(_action(MASS_OK)), idempotency_key="run-1", attempt=1
    )
    assert record.status is ExecutionStatus.FAILED
    assert "postconditions unmet" in record.error
    assert record.failed_action_label == "cad/run"

    kept = ActionExecutorRouteRunner(
        {"cad": _Hand({"result": {"mass_kg": 3.1}})}
    ).execute(_route(_action(MASS_OK)), idempotency_key="run-2", attempt=1)
    assert kept.status is ExecutionStatus.SUCCEEDED


def test_the_http_hand_is_judged_too():
    # The fetch succeeds (200), but the action promised JSON — the
    # evaluator, not the transport, is what calls that a failure.
    executor = HttpActionExecutor(
        HttpExecutionPolicy(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text="<html/>", headers={"content-type": "text/html"}
                )
            )
        ),
        resolver=lambda host: ["93.184.216.34"],
    )
    action = ActionEvent(
        correlation_id="c1",
        adapter="http",
        operation="get",
        parameters={"url": "https://api.example/report"},
        postconditions=[
            Postcondition(
                name="json-answer",
                pointer="content_type",
                op="==",
                value="application/json",
            )
        ],
    )
    runner = ActionExecutorRouteRunner({"http": executor})
    record = runner.execute(_route(action), idempotency_key="run-3", attempt=1)
    assert record.status is ExecutionStatus.FAILED
    assert "json-answer" in record.error


# --------------------------------------------------------------------------- #
# Observation lands on truth: appended evidence through the kernel.            #
# --------------------------------------------------------------------------- #
def test_a_verified_observation_is_filed_as_graph_evidence(tmp_path):
    conn = DurableConnection(tmp_path / "graph.db")
    try:
        store = ProjectGraphStore(conn)
        kernel = TransactionKernel(store)
        store.ensure_project("veh-1", tenant="t1", owner="alice")
        kernel.process(
            GraphProposal(
                project_id="veh-1",
                owner="alice",
                reason="initial drop",
                patch=[
                    PatchOp(
                        op="create",
                        object=GraphObject(
                            object_id="m1",
                            path="subsystems/suspension",
                            type="component",
                            parameters={"y_mm": 412},
                        ),
                    )
                ],
            ),
            tenant="t1",
        )
        filed = kernel.process(
            GraphProposal(
                project_id="veh-1",
                owner="alice",
                reason="file the verified run's observation",
                patch=[
                    PatchOp(
                        op="append",
                        object_id="m1",
                        base_revision=1,
                        pointer="evidence",
                        new_value={
                            "kind": "postconditions",
                            "run_id": "run-7",
                            "observed": {"mass-budget": 3.42},
                            "verified": True,
                        },
                    )
                ],
            ),
            tenant="t1",
        )
        assert filed.status == "committed", filed.reasons
        current = store.get("veh-1", "m1")
        assert current.revision == 2
        assert current.evidence[0]["run_id"] == "run-7"
        # Append reaches ONLY evidence and relations — parameters change
        # through set, with old_value honesty.
        sneak = kernel.process(
            GraphProposal(
                project_id="veh-1",
                owner="alice",
                reason="sneak a parameter in",
                patch=[
                    PatchOp(
                        op="append",
                        object_id="m1",
                        base_revision=2,
                        pointer="parameters",
                        new_value={"y_mm": 999},
                    )
                ],
            ),
            tenant="t1",
        )
        assert sneak.status == "rejected"
        assert any("append reaches only" in r for r in sneak.reasons)
    finally:
        conn.close()
