"""W5 — gate-aware paving: the Paver speaks gates.

A paved web may carry ``guard`` and ``loop`` edges: candidates enter at
pave time (the human's SOP guards projected onto the web's children —
never model advice), ride the composed SubgraphBody through the SAME
rehearsal machinery every web earns promotion by, and are promoted only
on a clean pass. A guarded web rehearses both branches under scripted
evidence; a loop that exhausts its budget FAILS the rehearsal loudly and
the web is refused promotion.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oolu.orchestrator import DagRouteRunner, RoutePlan, contract_to_blueprint
from oolu.paver import PaverAgent, RehearsalResult, derive_gate_candidates
from oolu.paver.discovery import SurveyNode
from oolu.skills import parse_sop
from oolu.skills.contract import ContractEdge, NodeContract, ScriptBody, Slot
from oolu.skills.models import (
    ExecutionOutcome,
    ExecutionStatus,
    Postcondition,
)

INVOICE_SOP = parse_sop(
    "sop: invoice-discipline\n"
    "require_guard:\n"
    "  - operation: 'send invoice*'\n"
    "    source: 'compute total*'\n"
    "    when: {pointer: total, op: '>', value: 0}\n"
)


def _child(name, *, consumes=None, produces=None) -> NodeContract:
    return NodeContract(
        name=name,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ScriptBody(goal=f"do {name}"),
    )


# --------------------------------------------------------------------------- #
# The derivation — pure, deterministic, name-matched.                          #
# --------------------------------------------------------------------------- #
def test_sop_guards_project_onto_web_children_by_name():
    compute = _child("Compute Total")  # case-folded fnmatch finds it
    send = _child("send invoice")
    edges, problem = derive_gate_candidates([INVOICE_SOP], [compute, send])
    assert problem == ""
    (edge,) = edges
    assert edge.relation == "guard"
    assert edge.source == compute.id and edge.target == send.id
    assert edge.guard is not None and edge.guard.name == "total > 0"


def test_a_web_that_cannot_express_an_applicable_rule_is_refused():
    # The gated child is present WITHOUT its evidence source: paving it
    # would run the operation unguarded — refused, with the rule named.
    send = _child("send invoice")
    other = _child("archive")
    edges, problem = derive_gate_candidates([INVOICE_SOP], [send, other])
    assert edges == [] and "cannot express" in problem
    # The gated operation absent: the rule simply does not apply.
    edges, problem = derive_gate_candidates([INVOICE_SOP], [other])
    assert edges == [] and problem == ""


# --------------------------------------------------------------------------- #
# Rehearsal semantics: both branches, and the loud loop exhaustion.            #
# --------------------------------------------------------------------------- #
class ScriptedExecutor:
    """Per-operation scripted evidence — the rehearsal's stand-in."""

    name = "stub"

    def __init__(self, evidence=None):
        self._evidence = dict(evidence or {})
        self.order: list[str] = []

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        goal = str(action.parameters.get("goal") or action.operation)
        self.order.append(goal)
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED,
            evidence=self._evidence.get(goal, {}),
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key):
        return None


def _rehearse(contract, evidence):
    """The rehearsal machinery in miniature: the same compiler and the
    same gate scheduler the gateway's ``_rehearse_web`` drives. Script-
    bodied children compile to ``script`` actions, so the scripted
    executor answers for that adapter."""
    blueprint = contract_to_blueprint(contract)
    executor = ScriptedExecutor(evidence=evidence)
    record = DagRouteRunner({"script": executor}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k",
        attempt=1,
    )
    return record, executor


def _gated_web_contract():
    compute = _child("compute total")
    send = _child("send invoice")
    edges, problem = derive_gate_candidates([INVOICE_SOP], [compute, send])
    assert problem == ""
    return NodeContract(
        name="web:invoicing",
        provenance="synthesized",
        body=__import__("oolu.skills.contract", fromlist=["SubgraphBody"]).SubgraphBody(
            nodes=[compute, send], edges=edges
        ),
    )


def test_a_guarded_web_rehearses_both_branches_under_scripted_evidence():
    contract = _gated_web_contract()
    # Branch 1: the guard ADMITS — the invoice goes out.
    record, executor = _rehearse(contract, {"do compute total": {"total": 250}})
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["do compute total", "do send invoice"]
    # Branch 2: the guard DECLINES — an honest SKIP, still a clean pass.
    record, executor = _rehearse(contract, {"do compute total": {"total": 0}})
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["do compute total"]


def test_agent_promotes_a_gated_web_with_its_gates_intact():
    compute = _child(
        "compute total", produces=[Slot(name="total_csv", value_type="str")]
    )
    send = _child(
        "send invoice", consumes=[Slot(name="total_csv", value_type="str")]
    )
    nodes = [
        SurveyNode(key="compute", contract=compute, anchor_kind="webhook"),
        SurveyNode(key="send", contract=send),
    ]
    promoted: dict = {}

    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: promoted.update(c=contract)
        or "node-1",
        gate_candidates=lambda tenant, web, children: derive_gate_candidates(
            [INVOICE_SOP], children
        ),
    )
    report = agent.tick("t", nodes)
    assert report.paved  # the gated web paved
    body = promoted["c"].body
    guard_edges = [e for e in body.edges if e.relation == "guard"]
    assert len(guard_edges) == 1  # the gate RIDES the promoted contract
    assert guard_edges[0].guard.name == "total > 0"


def test_agent_refuses_a_web_that_cannot_express_the_rule():
    send = _child("send invoice", produces=[Slot(name="receipt", value_type="str")])
    sink = _child("archive", consumes=[Slot(name="receipt", value_type="str")])
    nodes = [
        SurveyNode(key="send", contract=send, anchor_kind="webhook"),
        SurveyNode(key="sink", contract=sink),
    ]
    negatives: list = []
    rehearsed: list = []

    agent = PaverAgent(
        rehearse=lambda contract: rehearsed.append(1) or RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: "never",
        negative=lambda tenant, web, reason: negatives.append(reason),
        gate_candidates=lambda tenant, web, children: derive_gate_candidates(
            [INVOICE_SOP], children
        ),
    )
    report = agent.tick("t", nodes)
    assert report.paved == []
    assert rehearsed == []  # refused BEFORE any sandbox spend
    refused = [o for o in report.outcomes if o.status == "gate-refused"]
    assert len(refused) == 1 and "cannot express" in refused[0].reason
    assert negatives and "cannot express" in negatives[0]


def test_a_loop_that_exhausts_its_budget_is_refused_promotion():
    # A loop candidate whose exit guard never satisfies: the rehearsal
    # exhausts max_iterations and FAILS loudly — the agent files the
    # negative note and never promotes. The pipeline carries loop edges;
    # the budget is the wall.
    work = _child("poll status")
    loop_edge = ContractEdge(
        source=work.id,
        target=work.id,
        relation="loop",
        max_iterations=2,
        guard=Postcondition(name="more", pointer="remaining", op=">", value=0),
    )
    contract = NodeContract(
        name="web:poller",
        provenance="synthesized",
        body=__import__("oolu.skills.contract", fromlist=["SubgraphBody"]).SubgraphBody(
            nodes=[work], edges=[loop_edge]
        ),
    )
    # The rehearsal in miniature: evidence keeps the loop going forever.
    record, executor = _rehearse(contract, {"do poll status": {"remaining": 9}})
    assert record.status is ExecutionStatus.FAILED
    assert "budget" in (record.error or "").lower() or "exhaust" in (
        record.error or ""
    ).lower()

    # Through the agent: that failure is a rehearsal failure — negative
    # note filed, no promotion. A producer/consumer pair so the survey
    # draws a web; the loop candidate rides the gate port.
    producer = _child(
        "poll status", produces=[Slot(name="status", value_type="str")]
    )
    partner = _child(
        "collect", consumes=[Slot(name="status", value_type="str")]
    )
    loop_on_producer = ContractEdge(
        source=producer.id, target=producer.id, relation="loop",
        max_iterations=2,
        guard=Postcondition(name="more", pointer="remaining", op=">", value=0),
    )
    negatives: list = []

    def rehearse(composed):
        rec, _ = _rehearse(composed, {"do poll status": {"remaining": 9}})
        ok = rec.status is ExecutionStatus.SUCCEEDED
        return RehearsalResult(ok=ok, error="" if ok else (rec.error or "failed"))

    agent = PaverAgent(
        rehearse=rehearse,
        promote=lambda tenant, web, contract: "never",
        negative=lambda tenant, web, reason: negatives.append(reason),
        gate_candidates=lambda tenant, web, children: ([loop_on_producer], ""),
    )
    report = agent.tick(
        "t",
        [
            SurveyNode(key="p", contract=producer, anchor_kind="webhook"),
            SurveyNode(key="c", contract=partner),
        ],
    )
    assert report.paved == []
    failed = [o for o in report.outcomes if o.status == "rehearsal-failed"]
    assert len(failed) == 1
    assert negatives  # the M3 note — never grind the same failing loop


# --------------------------------------------------------------------------- #
# The map renders its gates.                                                   #
# --------------------------------------------------------------------------- #
def test_paver_webs_renders_the_paved_contracts_gates(tmp_path):
    from test_http_gateway import _req
    from test_paver_pave import _pave_app

    app, conn, ident, registry, executor = _pave_app(tmp_path, payloads={})
    try:
        contract = _gated_web_contract()
        # Survey map entry + its paved contract, stored as promotion would.
        from oolu.paver import RouteWeb, SurveyReport

        web = RouteWeb(
            web_id="w-gated", tenant="t1", anchor="a", anchor_kind="webhook",
            node_ids=["a", "b"],
            edges=[],
            near_misses=[
                # a non-empty web (the store keeps only connectable webs)
                __import__("oolu.paver", fromlist=["NearMiss"]).NearMiss(
                    source="a", target="b",
                    produced_slot="x", consumed_slot="y",
                    reason="same type+role, different name",
                )
            ],
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        app._pave_store.replace_tenant(
            SurveyReport(tenant="t1", surveyed_nodes=2, webs=[web]), now=now
        )
        app._pave_store.record_paved(
            "t1", "w-gated", node_id="n1", version_id="v1",
            contract_json=contract.model_dump_json(),
            signature=web.signature(), now=now,
        )
        response = app.handle(
            _req("GET", "/v1/paver/webs", token=ident.token("user-1", "t1"))
        )
        assert response.status == 200
        (web_view,) = response.body["webs"]
        (gate,) = web_view["gates"]
        assert gate["relation"] == "guard"
        assert gate["source"] == "compute total"
        assert gate["target"] == "send invoice"
        assert gate["guard"]["name"] == "total > 0"
    finally:
        conn.close()
