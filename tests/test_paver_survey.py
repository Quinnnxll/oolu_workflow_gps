"""W1 — the Paver's map and heartbeat: survey, schedule, store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oolu.durable import DurableConnection
from oolu.paver import (
    PaverScheduleStore,
    PaveStore,
    SurveyNode,
    WebSurveyor,
)
from oolu.runtime.sweep import SweepScheduleStore
from oolu.skills.contract import ActionsBody, NodeContract, ScriptBody, Slot
from oolu.skills.models import ActionEvent


def _slot(name: str, value_type: str = "str", role: str | None = None) -> Slot:
    return Slot(name=name, value_type=value_type, role=role)


def _script_node(
    node_id: str,
    *,
    consumes=None,
    produces=None,
    anchor_kind: str | None = None,
) -> SurveyNode:
    contract = NodeContract(
        name=node_id,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ScriptBody(goal=f"goal:{node_id}"),
    )
    return SurveyNode(key=node_id, contract=contract, anchor_kind=anchor_kind)


def _actions_node(node_id: str, *, produces=None) -> SurveyNode:
    """A demonstrated cli node — produces, but files no port value."""
    contract = NodeContract(
        name=node_id,
        provenance="demonstrated",
        produces=list(produces or []),
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")]
        ),
    )
    return SurveyNode(key=node_id, contract=contract)


# --------------------------------------------------------------------------- #
# The survey — direct edges, near-misses, anchors, components.                 #
# --------------------------------------------------------------------------- #
def test_direct_edge_links_a_producer_to_a_consumer():
    producer = _script_node("exporter", produces=[_slot("rows_csv")])
    consumer = _script_node("cleaner", consumes=[_slot("rows_csv")])
    report = WebSurveyor().survey("t1", [producer, consumer])
    assert report.surveyed_nodes == 2
    assert len(report.webs) == 1
    web = report.webs[0]
    assert [(e.source, e.target, e.slot) for e in web.edges] == [
        ("exporter", "cleaner", "rows_csv")
    ]
    assert set(web.node_ids) == {"exporter", "cleaner"}


def test_actionsbody_producer_never_sources_an_edge():
    # A cli node produces rows_csv but files no port value — no candidate.
    producer = _actions_node("cli-exporter", produces=[_slot("rows_csv")])
    consumer = _script_node("cleaner", consumes=[_slot("rows_csv")])
    report = WebSurveyor().survey("t1", [producer, consumer])
    assert report.direct_edges == 0
    assert report.webs == []  # nothing connects


def test_near_miss_is_recorded_not_edged():
    # Same type + SHARED ROLE, different name: a near-miss, not an edge.
    producer = _script_node(
        "exporter", produces=[_slot("invoice_rows", role="path")]
    )
    consumer = _script_node(
        "cleaner", consumes=[_slot("rows_csv", role="path")]
    )
    report = WebSurveyor().survey("t1", [producer, consumer])
    assert report.direct_edges == 0
    assert report.near_misses == 1
    web = report.webs[0]
    assert set(web.node_ids) == {"exporter", "cleaner"}  # candidate bridge unites
    miss = web.near_misses[0]
    assert miss.produced_slot == "invoice_rows"
    assert miss.consumed_slot == "rows_csv"
    assert "different name" in miss.reason


def test_same_type_no_shared_role_is_not_a_near_miss():
    # Two unrelated str ports must NOT drown the map in noise.
    producer = _script_node("weather", produces=[_slot("summary")])
    consumer = _script_node("invoices", consumes=[_slot("rows_csv")])
    report = WebSurveyor().survey("t1", [producer, consumer])
    assert report.near_misses == 0
    assert report.webs == []


def test_convertible_type_near_miss():
    producer = _script_node("counter", produces=[_slot("total", "number")])
    consumer = _script_node("printer", consumes=[_slot("total", "str")])
    report = WebSurveyor().survey("t1", [producer, consumer])
    assert report.near_misses == 1
    assert "convertible type" in report.webs[0].near_misses[0].reason


def test_web_anchors_at_the_trigger_door_node():
    anchor = _script_node(
        "anchor", produces=[_slot("rows_csv")], anchor_kind="webhook"
    )
    consumer = _script_node("cleaner", consumes=[_slot("rows_csv")])
    report = WebSurveyor().survey("t1", [anchor, consumer])
    web = report.webs[0]
    assert web.anchored
    assert web.anchor == "anchor"
    assert web.anchor_kind == "webhook"


def test_disconnected_nodes_form_separate_webs_and_lone_nodes_drop():
    a = _script_node("a", produces=[_slot("x")])
    b = _script_node("b", consumes=[_slot("x")])
    c = _script_node("c", produces=[_slot("y")])
    d = _script_node("d", consumes=[_slot("y")])
    lone = _script_node("lone", produces=[_slot("unused")])
    report = WebSurveyor().survey("t1", [a, b, c, d, lone])
    assert len(report.webs) == 2  # {a,b} and {c,d}; lone connects to nothing
    members = sorted(tuple(sorted(w.node_ids)) for w in report.webs)
    assert members == [("a", "b"), ("c", "d")]


def test_web_id_is_stable_across_surveys():
    nodes = [
        _script_node("a", produces=[_slot("x")]),
        _script_node("b", consumes=[_slot("x")]),
    ]
    first = WebSurveyor().survey("t1", nodes)
    second = WebSurveyor().survey("t1", list(reversed(nodes)))
    assert first.webs[0].web_id == second.webs[0].web_id


def test_a_chain_is_one_web_with_two_edges():
    a = _script_node("a", produces=[_slot("x")], anchor_kind="pulse")
    b = _script_node("b", consumes=[_slot("x")], produces=[_slot("y")])
    c = _script_node("c", consumes=[_slot("y")])
    report = WebSurveyor().survey("t1", [a, b, c])
    assert len(report.webs) == 1
    web = report.webs[0]
    assert web.anchor == "a" and web.anchor_kind == "pulse"
    assert len(web.edges) == 2
    assert set(web.node_ids) == {"a", "b", "c"}


# --------------------------------------------------------------------------- #
# The store — durable, tenant-scoped, replace-wholesale.                        #
# --------------------------------------------------------------------------- #
def test_store_replaces_the_tenant_map_wholesale(tmp_path):
    conn = DurableConnection(tmp_path / "paver.db")
    store = PaveStore(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    report = WebSurveyor().survey(
        "t1",
        [
            _script_node("a", produces=[_slot("x")], anchor_kind="webhook"),
            _script_node("b", consumes=[_slot("x")]),
        ],
    )
    store.replace_tenant(report, now=now)
    assert len(store.webs("t1")) == 1
    assert store.webs("t1", anchor="a")  # the trigger-door view finds it
    assert store.webs("t1", anchor="nobody") == []

    # A later survey with no connections replaces the map — nothing lingers.
    empty = WebSurveyor().survey("t1", [_script_node("solo", produces=[_slot("z")])])
    store.replace_tenant(empty, now=now)
    assert store.webs("t1") == []
    conn.close()


def test_store_is_tenant_walled(tmp_path):
    conn = DurableConnection(tmp_path / "paver.db")
    store = PaveStore(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for tenant in ("t1", "t2"):
        store.replace_tenant(
            WebSurveyor().survey(
                tenant,
                [
                    _script_node("a", produces=[_slot("x")]),
                    _script_node("b", consumes=[_slot("x")]),
                ],
            ),
            now=now,
        )
    assert len(store.webs("t1")) == 1
    assert len(store.webs("t2")) == 1
    # t1's replace never touches t2's map.
    store.replace_tenant(WebSurveyor().survey("t1", []), now=now)
    assert store.webs("t1") == []
    assert len(store.webs("t2")) == 1
    conn.close()


# --------------------------------------------------------------------------- #
# The heartbeat — the sweep discipline on its own table.                        #
# --------------------------------------------------------------------------- #
def test_paver_schedule_is_a_separate_table_from_the_sweep(tmp_path):
    conn = DurableConnection(tmp_path / "sched.db")
    sweep = SweepScheduleStore(conn)
    paver = PaverScheduleStore(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    paver.enable(interval_hours=6, granted_by="quinn", tenant="t1", now=now)
    # Enabling the Paver leaves the sweep schedule untouched.
    assert paver.view()["enabled"] is True
    assert sweep.view() is None or sweep.view()["enabled"] is False


def test_paver_claim_fires_once_then_waits_the_interval(tmp_path):
    conn = DurableConnection(tmp_path / "sched.db")
    paver = PaverScheduleStore(conn)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    paver.enable(interval_hours=6, granted_by="quinn", tenant="t1", now=start)

    assert paver.claim_due(start) is True  # first firing wins
    assert paver.claim_due(start + timedelta(minutes=5)) is False  # too soon
    assert paver.claim_due(start + timedelta(hours=7)) is True  # interval elapsed


def test_paver_revoke_stops_the_next_firing(tmp_path):
    conn = DurableConnection(tmp_path / "sched.db")
    paver = PaverScheduleStore(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    paver.enable(interval_hours=6, granted_by="quinn", tenant="t1", now=now)
    assert paver.disable() is True
    assert paver.claim_due(now + timedelta(hours=7)) is False
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway wiring — the survey tick reads the registry, draws the map.      #
# --------------------------------------------------------------------------- #
def _paver_gateway(tmp_path):
    from test_http_gateway import _app

    from oolu.gateway import GatewayApp
    from oolu.nodeplace import NodeplaceService, RegistryStore
    from oolu.skills.contract import Slot as _Slot
    from oolu.skills.models import SkillSignature
    from oolu.skills.pack import ReusableSkill

    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    nodeplace = NodeplaceService(registry)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=nodeplace,
    )

    def contribute(name, *, consumes, produces):
        skill = ReusableSkill(
            name=name,
            description=f"the {name} node",
            signature=SkillSignature(application="script", adapter="script"),
            actions=[
                ActionEvent(
                    correlation_id="function",
                    adapter="script",
                    operation="run",
                    parameters={"goal": name, "script": "x", "node_key": name},
                )
            ],
        )
        result = nodeplace.contribute(
            noder_principal="noder",
            tenant_id="t1",
            skill=skill,
            semver="1.0.0",
            title=name,
            summary=name,
            consumes=[_Slot(**c) for c in consumes] or None,
            produces=[_Slot(**p) for p in produces] or None,
        )
        return result.node.node_id

    return app, conn, ident, contribute


def test_survey_tick_draws_the_map_from_the_registry(tmp_path):
    app, conn, ident, contribute = _paver_gateway(tmp_path)
    exporter = contribute(
        "exporter",
        consumes=[],
        produces=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
    )
    cleaner = contribute(
        "cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)

    webs = app._pave_store.webs("t1")
    assert len(webs) == 1
    edge = webs[0].edges[0]
    # The edge keys by canonical node id — the same key W0 files under.
    assert edge.source == exporter and edge.target == cleaner
    assert edge.slot == "rows_csv"
    conn.close()


def test_paver_schedule_enable_is_approve_gated(tmp_path):
    from test_contract_run import _grant_approver
    from test_http_gateway import _req

    app, conn, ident, contribute = _paver_gateway(tmp_path)
    # A plain user cannot enable the survey Routine.
    denied = app.handle(
        _req(
            "POST",
            "/v1/paver/schedule",
            token=ident.token("user", "t1"),
            body={"interval_hours": 6},
        )
    )
    assert denied.status == 403

    # An approver in the tenant can.
    _grant_approver(ident, "approver", "t1")
    ok = app.handle(
        _req(
            "POST",
            "/v1/paver/schedule",
            token=ident.token("approver", "t1"),
            body={"interval_hours": 6},
        )
    )
    assert ok.status == 200, ok.body
    assert ok.body["enabled"] is True
    events = [
        r for r in app._durable.audit.records() if r.event_type == "paver.scheduled"
    ]
    assert events and events[-1].payload["by"] == "approver"
    conn.close()


def test_paver_webs_endpoint_reads_the_stored_map(tmp_path):
    from test_http_gateway import _req

    app, conn, ident, contribute = _paver_gateway(tmp_path)
    contribute(
        "exporter",
        consumes=[],
        produces=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
    )
    contribute(
        "cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=now
    )
    app._scheduled_survey_tick(now)

    resp = app.handle(
        _req("GET", "/v1/paver/webs", token=ident.token("noder", "t1"))
    )
    assert resp.status == 200
    assert resp.body["count"] == 1
    assert resp.body["webs"][0]["edges"][0]["slot"] == "rows_csv"
    conn.close()
