"""W2 — pave direct webs: the PaverAgent orchestration and the gateway
sandbox rehearsal → body-preserving promotion."""

from __future__ import annotations

from oolu.paver import PaverAgent, RehearsalResult, SurveyNode
from oolu.skills.contract import (
    ActionsBody,
    NodeContract,
    ScriptBody,
    Slot,
    SubgraphBody,
    contract_from_registered_skill,
)
from oolu.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus


def _slot(name: str, role: str | None = None) -> Slot:
    return Slot(name=name, value_type="str", role=role)


def _script_survey_node(
    node_id: str, *, consumes=None, produces=None, anchor_kind=None
) -> SurveyNode:
    # A published function node reconstructs as ActionsBody-with-script.
    contract = NodeContract(
        name=node_id,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="function",
                    adapter="script",
                    operation="run",
                    parameters={
                        "goal": node_id,
                        "script": f"# {node_id}",
                        "node_key": f"node:{node_id}",
                    },
                )
            ]
        ),
    )
    return SurveyNode(key=node_id, contract=contract, anchor_kind=anchor_kind)


def _direct_web_nodes():
    return [
        _script_survey_node(
            "exporter", produces=[_slot("rows_csv")], anchor_kind="webhook"
        ),
        _script_survey_node("cleaner", consumes=[_slot("rows_csv")]),
    ]


# --------------------------------------------------------------------------- #
# The agent orchestration — with stub ports.                                   #
# --------------------------------------------------------------------------- #
def test_direct_web_rehearses_then_promotes():
    promoted: list = []
    audits: list = []
    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=True, run_id="r1"),
        promote=lambda tenant, web, contract: (
            promoted.append((tenant, web.web_id, contract)) or "node-web-1"
        ),
        audit=lambda event, payload: audits.append((event, payload)),
    )
    report = agent.tick("t1", _direct_web_nodes())
    assert report.candidates == 1
    assert len(report.paved) == 1
    assert len(promoted) == 1
    # The promoted contract is the composed SubgraphBody of the web.
    _, _, contract = promoted[0]
    assert isinstance(contract.body, SubgraphBody)
    assert {n.name for n in contract.body.nodes} == {"exporter", "cleaner"}
    assert any(e == "paver.web_paved" for e, _ in audits)


def test_rehearsal_failure_files_negative_and_never_promotes():
    promoted: list = []
    negatives: list = []
    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=False, error="boom in the sandbox"),
        promote=lambda tenant, web, contract: promoted.append(1) or "x",
        negative=lambda tenant, web, reason: negatives.append((web.web_id, reason)),
    )
    report = agent.tick("t1", _direct_web_nodes())
    assert report.paved == []
    assert promoted == []
    assert negatives and "boom" in negatives[0][1]
    assert report.refused and "boom" in report.refused[0]["reason"]


def test_web_with_near_miss_is_skipped_for_w3():
    # exporter produces invoice_rows (path); cleaner consumes rows_csv
    # (path) — a shared-role near-miss, not a direct edge. Needs an
    # adapter (W3), so W2 does not pave it.
    nodes = [
        _script_survey_node(
            "exporter",
            produces=[_slot("invoice_rows", role="path")],
            anchor_kind="webhook",
        ),
        _script_survey_node(
            "cleaner", consumes=[_slot("rows_csv", role="path")]
        ),
    ]
    calls = {"rehearse": 0}

    def rehearse(contract):
        calls["rehearse"] += 1
        return RehearsalResult(ok=True)

    agent = PaverAgent(rehearse=rehearse, promote=lambda *a: "x")
    report = agent.tick("t1", nodes)
    assert report.candidates == 0
    assert calls["rehearse"] == 0  # never even rehearsed
    assert any("near-miss" in o.reason for o in report.outcomes)


def test_unanchored_web_is_not_paved():
    nodes = [
        _script_survey_node("a", produces=[_slot("x")]),  # no anchor door
        _script_survey_node("b", consumes=[_slot("x")]),
    ]
    agent = PaverAgent(rehearse=lambda c: RehearsalResult(ok=True), promote=lambda *a: "x")
    report = agent.tick("t1", nodes)
    assert report.candidates == 0
    assert any("no trigger door" in o.reason for o in report.outcomes)


def test_write_class_hop_is_deferred_not_rehearsed():
    # A consumer that is a cli node (real side effects) — the web is not
    # rehearsed end-to-end; it is deferred to a real run.
    cli_consumer = SurveyNode(
        key="printer",
        contract=NodeContract(
            name="printer",
            provenance="demonstrated",
            consumes=[_slot("rows_csv")],
            body=ActionsBody(
                actions=[
                    ActionEvent(correlation_id="c", adapter="cli", operation="run")
                ]
            ),
        ),
    )
    nodes = [
        _script_survey_node(
            "exporter", produces=[_slot("rows_csv")], anchor_kind="webhook"
        ),
        cli_consumer,
    ]
    rehearsed = {"n": 0}
    agent = PaverAgent(
        rehearse=lambda c: (rehearsed.__setitem__("n", rehearsed["n"] + 1)
                            or RehearsalResult(ok=True)),
        promote=lambda *a: "x",
    )
    report = agent.tick("t1", nodes)
    assert rehearsed["n"] == 0  # never rehearsed
    assert any("write-class" in o.reason for o in report.outcomes)


def test_budget_caps_paves_per_tick():
    # Two independent direct webs, budget of one.
    nodes = [
        _script_survey_node("a1", produces=[_slot("x")], anchor_kind="webhook"),
        _script_survey_node("a2", consumes=[_slot("x")]),
        _script_survey_node("b1", produces=[_slot("y")], anchor_kind="webhook"),
        _script_survey_node("b2", consumes=[_slot("y")]),
    ]
    agent = PaverAgent(
        rehearse=lambda c: RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: web.web_id,
    )
    report = agent.tick("t1", nodes, max_paves=1)
    assert len(report.paved) == 1
    assert any("budget" in o.reason for o in report.outcomes)


# --------------------------------------------------------------------------- #
# Body-preserving round-trip.                                                  #
# --------------------------------------------------------------------------- #
def test_subgraph_skill_round_trips_with_body_intact():
    child = NodeContract(
        name="cleaner",
        provenance="synthesized",
        consumes=[_slot("rows_csv")],
        body=ScriptBody(goal="clean"),
    )
    web = NodeContract(
        name="web:anchor",
        provenance="synthesized",
        body=SubgraphBody(nodes=[child]),
    )
    skill = web.subgraph_to_skill()
    # A skill with no script action passes the contribute screen; decode
    # restores the exact subgraph.
    restored = contract_from_registered_skill(skill)
    assert isinstance(restored.body, SubgraphBody)
    assert [n.name for n in restored.body.nodes] == ["cleaner"]
    assert restored.body.nodes[0].consumes[0].name == "rows_csv"


def test_non_subgraph_skill_takes_the_actionsbody_path():
    from oolu.skills.models import SkillSignature
    from oolu.skills.pack import ReusableSkill

    skill = ReusableSkill(
        name="plain",
        description="a cli node",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")],
    )
    restored = contract_from_registered_skill(skill)
    assert isinstance(restored.body, ActionsBody)


# --------------------------------------------------------------------------- #
# The gateway loop-closure: survey → rehearse in the sandbox → promote.        #
# --------------------------------------------------------------------------- #
from datetime import UTC, datetime  # noqa: E402


class _ResolverStub:
    """A rehearsal script executor: resolves output:// bindings through the
    real ValueStore (as NodeScriptRunner does) and emits a scripted payload
    keyed by the action's goal. No synthesizer — provided scripts replay."""

    name = "script"

    def __init__(self, values, payloads):
        self._values = values
        self._payloads = dict(payloads)
        self.synth_calls = 0

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        params = action.parameters
        goal = str(params.get("goal"))
        tenant = str(params.get("_value_tenant") or "")
        now = datetime.now(UTC)
        try:
            resolved, _ = self._values.resolve_bindings(
                dict(params.get("bindings") or {}), tenant=tenant
            )
        except Exception as exc:
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id="s",
                status=ExecutionStatus.BLOCKED,
                error=f"unresolved value reference: {exc}",
                started_at=now,
                completed_at=now,
            )
        payload = self._payloads.get(goal, {})
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED,
            evidence={"result": payload},
            started_at=now,
            completed_at=now,
        )

    def verify_function(self, goal, script, *, session_id, ports=None, files=None,
                        bundle=None):
        """The F0 birth-verify seam, stubbed (W3): a real host executes the
        candidate in the sandbox against the staged sample; here a screened,
        non-empty adapter is judged to pass, so the gateway wiring
        (negotiate → sample → synthesize → verify → land → splice) is
        exercised end-to-end without a live sandbox."""
        from oolu.nodeplace.screening import screen_script

        self.verify_calls = getattr(self, "verify_calls", 0) + 1
        if not (script or "").strip() or screen_script(script):
            return {"ok": False, "honest_error": False, "error": "screen",
                    "healed": []}
        return {"ok": True, "honest_error": False, "error": "", "healed": []}

    def cancel(self, idempotency_key):
        return None


def _pave_app(tmp_path, payloads):
    from test_http_gateway import _app

    from oolu.gateway import GatewayApp
    from oolu.metering.attribution import AttributionStore
    from oolu.metering.store import MeteringLedger
    from oolu.nodeplace import (
        CandidateAssembler,
        LiveVersionStats,
        NodeplaceService,
        PriceBook,
        RatingService,
        RatingStore,
        RegistryStore,
    )
    from oolu.values import ValueStore

    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    ratings = RatingService(RatingStore(conn), verified_run=metering.verified_run)
    assembler = CandidateAssembler(
        registry=registry,
        stats=LiveVersionStats(
            metering=metering, audit=base._durable.audit, attribution=attribution
        ),
        ratings=ratings,
    )
    values = ValueStore(conn)
    executor = _ResolverStub(values, payloads)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        ratings=ratings,
        market=assembler,
        price_book=PriceBook(tmp_path / "prices.db"),
        attribution=attribution,
        contract_executors={"script": executor},
        values=values,
    )
    return app, conn, ident, registry, executor


def _publish_script_node(app, registry, *, name, consumes, produces, hook=False):
    from oolu.skills.models import SkillSignature
    from oolu.skills.pack import ReusableSkill

    skill = ReusableSkill(
        name=name,
        description=name,
        signature=SkillSignature(application="script", adapter="script"),
        actions=[
            ActionEvent(
                correlation_id="function",
                adapter="script",
                operation="run",
                parameters={"goal": name, "script": f"# {name}", "node_key": name},
            )
        ],
    )
    result = app._nodeplace.contribute(
        noder_principal="noder",
        tenant_id="t1",
        skill=skill,
        semver="1.0.0",
        title=name,
        summary=name,
        consumes=[Slot(**c) for c in consumes] or None,
        produces=[Slot(**p) for p in produces] or None,
    )
    node_id = result.node.node_id
    if hook:
        app._node_hooks.mint(node_id, tenant="t1", principal="noder")
    return node_id


def test_gateway_paves_a_direct_web_end_to_end(tmp_path):
    app, conn, ident, registry, executor = _pave_app(
        tmp_path,
        payloads={
            "exporter": {"rows_csv": "the rows"},
            "cleaner": {"tidy_csv": "tidy"},
        },
    )
    exporter = _publish_script_node(
        app,
        registry,
        name="exporter",
        consumes=[],
        produces=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        hook=True,  # the webhook door makes it an anchor
    )
    cleaner = _publish_script_node(
        app,
        registry,
        name="cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)

    # The web paved: a web_paved audit fired and the contract is stored.
    paved_events = [
        r for r in app._durable.audit.records() if r.event_type == "paver.web_paved"
    ]
    assert paved_events, "the direct web should have paved"
    web_id = paved_events[0].payload["web_id"]
    stored = app._pave_store.paved("t1", web_id)
    assert stored is not None
    paved_node_id = stored["node_id"]

    # The promoted node round-trips storage with its SubgraphBody INTACT.
    version = app._nodeplace.latest_version(paved_node_id)
    skill = __import__(
        "oolu.skills.pack", fromlist=["ReusableSkill"]
    ).ReusableSkill.model_validate_json(version.sanitized_skill_json)
    restored = contract_from_registered_skill(skill)
    assert isinstance(restored.body, SubgraphBody)
    assert {n.name for n in restored.body.nodes} == {"exporter", "cleaner"}
    conn.close()


def test_gateway_paves_a_near_miss_web_via_a_rename_adapter(tmp_path):
    # W3 loop-closure: a producer's 'folder' near-misses a consumer's
    # 'path' (same type+role, different name). The tick negotiates the
    # junction, samples the producer's REAL filed value, pours the rename
    # template (no model), verifies it, lands it as a citizen, splices it
    # in, and paves the now-direct web.
    app, conn, ident, registry, executor = _pave_app(
        tmp_path,
        payloads={
            "exporter": {"folder": "/data/in"},
            "adapt folder to path": {"path": "/data/in"},
            "cleaner": {},
        },
    )
    exporter = _publish_script_node(
        app, registry, name="exporter", consumes=[],
        produces=[{"name": "folder", "value_type": "path", "role": "path"}],
        hook=True,
    )
    _publish_script_node(
        app, registry, name="cleaner",
        consumes=[{"name": "path", "value_type": "path", "role": "path"}],
        produces=[],
    )
    # The producer must have FILED its port for the adapter to be sampled
    # against a real value — seed the live tenant's port index.
    app._values.snapshot_outputs(
        "t1", {"folder": "/data/in"}, producer=exporter, label="exporter"
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)

    records = app._durable.audit.records()
    built = [r for r in records if r.event_type == "paver.adapter_built"]
    assert built, "the rename adapter should have been built"
    assert built[0].payload["verdict"] == "mappable"
    assert built[0].payload["templated"] is True

    paved = [r for r in records if r.event_type == "paver.web_paved"]
    assert paved and paved[0].payload["adapters"] == 1
    stored = app._pave_store.paved("t1", paved[0].payload["web_id"])
    assert stored is not None

    # The promoted web carries three children — producer, consumer, adapter.
    version = app._nodeplace.latest_version(stored["node_id"])
    from oolu.skills.pack import ReusableSkill

    skill = ReusableSkill.model_validate_json(version.sanitized_skill_json)
    restored = contract_from_registered_skill(skill)
    assert isinstance(restored.body, SubgraphBody)
    assert len(restored.body.nodes) == 3
    conn.close()


def test_adapted_web_is_not_repaved_on_a_second_tick(tmp_path):
    # W3.1: after an adapted web paves, a SECOND tick must NOT re-grind it.
    # The paver-authored adapter citizen is EXCLUDED from the survey, so it
    # never folds itself into a fresh-web_id the idempotence gate misses —
    # no duplicate adapter, no duplicate paved-web node, no re-paid bill.
    app, conn, ident, registry, executor = _pave_app(
        tmp_path,
        payloads={
            "exporter": {"folder": "/data/in"},
            "adapt folder to path": {"path": "/data/in"},
            "cleaner": {},
        },
    )
    exporter = _publish_script_node(
        app, registry, name="exporter", consumes=[],
        produces=[{"name": "folder", "value_type": "path", "role": "path"}],
        hook=True,
    )
    _publish_script_node(
        app, registry, name="cleaner",
        consumes=[{"name": "path", "value_type": "path", "role": "path"}],
        produces=[],
    )
    app._values.snapshot_outputs(
        "t1", {"folder": "/data/in"}, producer=exporter, label="exporter"
    )
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    def _counts():
        recs = app._durable.audit.records()
        built = len([r for r in recs if r.event_type == "paver.adapter_built"])
        paved = len([r for r in recs if r.event_type == "paver.web_paved"])
        nodes = len([n for n in app._nodeplace.all_nodes() if n.tenant_id == "t1"])
        return built, paved, nodes

    app._scheduled_survey_tick(datetime(2026, 1, 1, tzinfo=UTC))
    first = _counts()
    assert first[0] == 1 and first[1] == 1  # one adapter, one paved web

    # A second (and third) tick: no NEW adapter, no NEW pave, no new nodes.
    app._scheduled_survey_tick(datetime(2026, 1, 1, 6, tzinfo=UTC))
    app._scheduled_survey_tick(datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert _counts() == first, "the adapted web must not re-grind across ticks"
    conn.close()


def test_gateway_rehearsal_failure_leaves_no_promotion(tmp_path):
    # The consumer's script BLOCKS (its binding can't resolve because the
    # producer emits the wrong key), so the web fails rehearsal.
    app, conn, ident, registry, executor = _pave_app(
        tmp_path,
        payloads={
            "exporter": {"wrong_key": "x"},  # never fills rows_csv's port
            "cleaner": {"tidy_csv": "tidy"},
        },
    )
    _publish_script_node(
        app, registry, name="exporter", consumes=[],
        produces=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        hook=True,
    )
    _publish_script_node(
        app, registry, name="cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)

    assert not [
        r for r in app._durable.audit.records() if r.event_type == "paver.web_paved"
    ]
    # A rehearsal-failed audit was recorded instead.
    assert [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.rehearsal_failed"
    ]
    conn.close()


# --------------------------------------------------------------------------- #
# W2.1 — review fixes.                                                         #
# --------------------------------------------------------------------------- #
def test_already_paved_web_is_not_repaved():
    paved_ids: set = set()

    def promote(tenant, web, contract):
        paved_ids.add(web.web_id)
        return "node-1"

    agent = PaverAgent(
        rehearse=lambda c: RehearsalResult(ok=True),
        promote=promote,
        is_paved=lambda tenant, web: web.web_id in paved_ids,
    )
    nodes = _direct_web_nodes()
    first = agent.tick("t1", nodes)
    assert len(first.paved) == 1
    # Second tick: the same web is already paved — no re-rehearse, no
    # re-promote, no duplicate node.
    rehearsed = {"n": 0}
    agent2 = PaverAgent(
        rehearse=lambda c: (rehearsed.__setitem__("n", rehearsed["n"] + 1)
                            or RehearsalResult(ok=True)),
        promote=promote,
        is_paved=lambda tenant, web: web.web_id in paved_ids,
    )
    second = agent2.tick("t1", nodes)
    assert second.paved == []
    assert rehearsed["n"] == 0  # never re-rehearsed
    assert any(o.reason == "already paved" for o in second.outcomes)


def test_blocked_web_is_not_reground():
    rehearsed = {"n": 0}
    agent = PaverAgent(
        rehearse=lambda c: (rehearsed.__setitem__("n", rehearsed["n"] + 1)
                            or RehearsalResult(ok=False, error="x")),
        promote=lambda *a: "x",
        is_blocked=lambda tenant, web: True,
    )
    report = agent.tick("t1", _direct_web_nodes())
    assert rehearsed["n"] == 0  # a blocked web never rehearses
    assert any("blocked by a prior" in o.reason for o in report.outcomes)


def test_budget_caps_rehearsals_not_just_promotions():
    # Two failing webs, budget of one: only ONE rehearses (the expensive
    # step), the other is budget-deferred though neither ever "paves".
    nodes = [
        _script_survey_node("a1", produces=[_slot("x")], anchor_kind="webhook"),
        _script_survey_node("a2", consumes=[_slot("x")]),
        _script_survey_node("b1", produces=[_slot("y")], anchor_kind="webhook"),
        _script_survey_node("b2", consumes=[_slot("y")]),
    ]
    rehearsed = {"n": 0}
    agent = PaverAgent(
        rehearse=lambda c: (rehearsed.__setitem__("n", rehearsed["n"] + 1)
                            or RehearsalResult(ok=False, error="boom")),
        promote=lambda *a: "x",
    )
    report = agent.tick("t1", nodes, max_paves=1)
    assert rehearsed["n"] == 1  # the budget capped rehearsals, not paves
    assert any("budget" in o.reason for o in report.outcomes)


def test_web_contract_advertises_boundary_slots():
    from oolu.paver.agent import PaverAgent as _PA

    nodes = [
        _script_survey_node(
            "exporter", produces=[_slot("rows_csv")], anchor_kind="webhook"
        ),
        _script_survey_node(
            "cleaner", consumes=[_slot("rows_csv")], produces=[_slot("tidy_csv")]
        ),
    ]
    captured = {}
    agent = _PA(
        rehearse=lambda c: RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: captured.setdefault("c", contract)
        or "node-1",
    )
    agent.tick("t1", nodes)
    contract = captured["c"]
    # rows_csv is produced AND consumed inside → internal, not on the face.
    # tidy_csv is a terminal output; the web consumes nothing external.
    assert [p.name for p in contract.produces] == ["tidy_csv"]
    assert [c.name for c in contract.consumes] == []


def test_web_signature_changes_when_shape_moves():
    from oolu.paver.discovery import WebSurveyor

    a = WebSurveyor().survey("t1", _direct_web_nodes()).webs[0]
    # Same web, surveyed again → same signature.
    b = WebSurveyor().survey("t1", _direct_web_nodes()).webs[0]
    assert a.signature() == b.signature()
    # A third node joins the web → different membership → new signature.
    more = _direct_web_nodes() + [
        _script_survey_node("cleaner2", consumes=[_slot("rows_csv")])
    ]
    c = WebSurveyor().survey("t1", more).webs[0]
    assert c.signature() != a.signature()


def test_contribute_screens_child_scripts_inside_a_subgraph():
    from oolu.nodeplace.service import _screenable_scripts
    from oolu.skills.contract import NodeContract, ScriptBody, SubgraphBody

    # A subgraph whose child carries a hostile script: the nested script
    # must be surfaced to the screen, not buried in the encoded contract.
    child = NodeContract(
        name="evil",
        provenance="synthesized",
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="c",
                    adapter="script",
                    operation="run",
                    parameters={"script": "import socket\nsocket.socket()"},
                )
            ]
        ),
    )
    web = NodeContract(
        name="web", provenance="synthesized", body=SubgraphBody(nodes=[child])
    )
    skill = web.subgraph_to_skill()
    scripts = _screenable_scripts(skill.actions[0])
    assert any("socket" in s for s in scripts)


# --------------------------------------------------------------------------- #
# W4 — trigger propagation: one trigger fires the whole paved web.            #
# --------------------------------------------------------------------------- #
def _paved_web_app(tmp_path):
    """Pave a direct exporter->cleaner web and return (app, conn, web_id,
    anchor node id) ready for a propagation trigger."""
    app, conn, ident, registry, executor = _pave_app(
        tmp_path,
        payloads={
            "exporter": {"rows_csv": "the rows"},
            "cleaner": {"tidy_csv": "tidy"},
        },
    )
    exporter = _publish_script_node(
        app, registry, name="exporter", consumes=[],
        produces=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        hook=True,
    )
    _publish_script_node(
        app, registry, name="cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)
    paved = [
        r for r in app._durable.audit.records() if r.event_type == "paver.web_paved"
    ]
    assert paved, "the direct web should have paved"
    return app, conn, paved[0].payload["web_id"], exporter


def test_trigger_fires_the_whole_paved_web(tmp_path):
    # The W4 loop-closure: consent stands, the anchor is triggered, the
    # propagation is enqueued and — on the drain — the whole paved web
    # fires as ONE contract run under the real tenant.
    app, conn, web_id, anchor = _paved_web_app(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._propagation_consent.grant("t1", web_id, granted_by="quinn", now=now)

    # Trigger: the anchor's completion enqueues propagation (deferred).
    enqueued = app._web_router(now).on_trigger("t1", anchor, "run-1")
    assert [e.web_id for e in enqueued] == [web_id]
    fired_before = [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    assert not fired_before  # nothing fired in-request — it is deferred

    # Drain: the web fires.
    app._drain_propagation(now)
    fired = [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    assert len(fired) == 1
    assert fired[0].payload["web_id"] == web_id
    assert fired[0].payload["fired"] == 2  # exporter + cleaner ran

    # Idempotent: a second drain (at-least-once outbox) re-fires nothing —
    # the (trigger_stamp, web_id) claim was taken.
    app._web_router(now).on_trigger("t1", anchor, "run-1")  # same run => same stamp
    app._drain_propagation(now)
    still = [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    assert len(still) == 1
    conn.close()


def test_revoking_consent_stops_the_next_propagation_cold(tmp_path):
    # Consent stands at enqueue, is revoked before the drain: delivery
    # re-checks consent and refuses — the web never fires.
    app, conn, web_id, anchor = _paved_web_app(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._propagation_consent.grant("t1", web_id, granted_by="quinn", now=now)
    app._web_router(now).on_trigger("t1", anchor, "run-1")  # enqueued while granted
    assert app._propagation_consent.revoke("t1", web_id, now=now) is True

    app._drain_propagation(now)
    fired = [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    assert not fired  # revoked between enqueue and delivery => never fires
    no_consent = [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_no_consent"
    ]
    assert no_consent
    conn.close()


def test_unconsented_web_never_enqueues(tmp_path):
    # No consent => a trigger on the anchor enqueues nothing (the POST just
    # returns; the web does not fan out).
    app, conn, web_id, anchor = _paved_web_app(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    enqueued = app._web_router(now).on_trigger("t1", anchor, "run-1")
    assert enqueued == []
    app._drain_propagation(now)
    assert not [
        r for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    conn.close()
