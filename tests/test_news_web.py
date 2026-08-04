"""The web assembled (N7): the magazine company stands on the platform's own legs.

Exit gate (news-agent-benchmark-roadmap, phase N7): one trigger runs
genre→topic→research→survey→composition→publication end to end with
gates enforced; an editorial hold halts and RESUMES on release (P3);
replacing one desk re-paves and re-rehearses without touching the
others (P2); and the promoted web's gates render in words on
``/v1/paver/webs``. Underneath: the tenant SOP store is real (P1 —
webs no longer pave gate-free where a law applies), the web signature
covers child CONTENT so an implementation edit re-paves and the
promotion retires its predecessor (P2), and the magazine's webs are
the News agent's standing property (P4). The observer seat (doctrine
3) files proposals the owner decides — models propose, the owner
disposes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_contract_run import _grant_approver
from test_http_gateway import _req
from test_paver_pave import _pave_app, _publish_script_node

from oolu.durable.connection import DurableConnection
from oolu.press import DESK_NODES, EDITORIAL_SOP, DeskProposalStore, observe_desk
from oolu.press.desknodes import (
    COMPOSITION_DESK,
    GENRE_DESK,
    PUBLICATION_DESK,
    RESEARCH_DESK,
    SURVEY_DESK,
    TOPIC_DESK,
)
from oolu.skills.contract import Slot
from oolu.skills.sop_store import TenantSopStore

NOW = datetime(2026, 8, 4, 7, 0, tzinfo=UTC)

NEWS_PAYLOADS = {
    GENRE_DESK: {
        "press_genre_demand": {
            "chosen": "products",
            "ranked": ["products", "local"],
            "draw_seed": 7,
            "beat": [],
            "sample": ["carol", "dave"],
        }
    },
    TOPIC_DESK: {
        "press_topic_brief": {
            "topic_key": "gap:listing-42",
            "subject": "The steel kettle, measured",
            "facts": [
                {"kind": "listing", "ref": "listing-42", "summary": "kettle"},
                {"kind": "lab", "ref": "listing-42", "summary": "lab mean 88"},
                {"kind": "feedback", "ref": "listing-42", "summary": "mean 5"},
            ],
            "disclosure": "",
            "sample": ["carol", "dave"],
        }
    },
    RESEARCH_DESK: {
        "press_research_bundle": {
            "topic_key": "gap:listing-42",
            "subject": "The steel kettle, measured",
            "facts": [
                {"kind": "lab", "ref": "listing-42", "summary": "lab mean 88"},
                {"kind": "feedback", "ref": "listing-42", "summary": "mean 5"},
            ],
            "sources_resolved": 2,
            "open_questions": 1,
            "questions": ["do the measured results match the lived ones?"],
            "disclosure": "",
            "sample": ["carol", "dave"],
        }
    },
    SURVEY_DESK: {
        "press_survey_result": {
            "topic_key": "gap:listing-42",
            "question": "do the measured results match the lived ones?",
            "options": ["worth", "not_worth", "more_evidence"],
            "sample": ["carol", "dave"],
            "brief": {"subject": "The steel kettle, measured"},
        }
    },
    COMPOSITION_DESK: {
        "press_post_draft": {
            "topic_key": "gap:listing-42",
            "headline": "The steel kettle, measured",
            "prose": "Lab mean 88; verified feedback mean 5.",
            "sources": [{"kind": "lab", "ref": "listing-42", "summary": "88"}],
            "sources_resolved": 3,
            "disclosure": "",
        }
    },
    PUBLICATION_DESK: {
        "press_post_published": {
            "topic_key": "gap:listing-42",
            "headline": "The steel kettle, measured",
            "publish": True,
        }
    },
}


def _spy(executor):
    """Record which desk each execution served, in order."""
    calls: list[str] = []
    original = executor.execute

    def execute(action, *, idempotency_key):
        calls.append(str(action.parameters.get("goal")))
        return original(action, idempotency_key=idempotency_key)

    executor.execute = execute
    return calls


# --------------------------------------------------------------------------- #
# P1 — the tenant SOP store: authored strictly, read by the Paver.             #
# --------------------------------------------------------------------------- #
def test_the_sop_store_authors_strictly_and_replaces_whole(tmp_path):
    conn = DurableConnection(tmp_path / "sops.db")
    store = TenantSopStore(conn)
    sop = store.put(
        tenant="t1",
        text=EDITORIAL_SOP,
        authored_by="quinn",
    )
    assert sop.name == "editorial-law" and len(sop.require_guard) == 2
    # A typo'd rule refuses loudly — nothing half-valid is stored.
    with pytest.raises(Exception):
        store.put(
            tenant="t1",
            text="sop: broken\nrequire_guard:\n  - operation: 'x'\n"
            "    source: 'y'\n    when: {pointer: p, op: '>', value: 1, "
            "extra: nope}\n",
        )
    assert [row["name"] for row in store.rows("t1")] == ["editorial-law"]
    # Re-authoring the same name replaces WHOLE — never a merge.
    store.put(tenant="t1", text="sop: editorial-law\n")
    assert store.list("t1")[0].require_guard == []
    assert store.delete(tenant="t1", name="editorial-law") is True
    assert store.list("t1") == []
    conn.close()


def test_the_sop_doors_author_list_and_delete(tmp_path):
    app, conn, ident, registry, executor = _pave_app(tmp_path, payloads={})
    token = ident.token("user", "t1")
    refused = app.handle(
        _req("POST", "/v1/paver/sops", token=token, body={"sop": "not: [a"})
    )
    assert refused.status == 400
    assert "does not parse" in refused.body["error"]["message"]
    created = app.handle(
        _req("POST", "/v1/paver/sops", token=token, body={"sop": EDITORIAL_SOP})
    )
    assert created.status == 201
    assert created.body == {"name": "editorial-law", "guards": 2}
    listed = app.handle(_req("GET", "/v1/paver/sops", token=token))
    assert [row["name"] for row in listed.body["sops"]] == ["editorial-law"]
    # The Paver's projection seam reads the SAME store — P1's whole point.
    assert [sop.name for sop in app._paver_sops("t1")] == ["editorial-law"]
    removed = app.handle(
        _req("DELETE", "/v1/paver/sops/editorial-law", token=token)
    )
    assert removed.status == 200 and app._paver_sops("t1") == []
    conn.close()


# --------------------------------------------------------------------------- #
# P2 — a same-id implementation edit re-paves; the promotion retires           #
# its predecessor. The other citizens are untouched.                           #
# --------------------------------------------------------------------------- #
def _script_skill(name, script):
    from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature

    return ReusableSkill(
        name=name,
        description=name,
        signature=SkillSignature(application="script", adapter="script"),
        actions=[
            ActionEvent(
                correlation_id="function",
                adapter="script",
                operation="run",
                parameters={"goal": name, "script": script, "node_key": name},
            )
        ],
    )


def test_an_implementation_edit_repaves_and_retires_the_predecessor(tmp_path):
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
        hook=True,
    )
    cleaner = _publish_script_node(
        app,
        registry,
        name="cleaner",
        consumes=[{"name": "rows_csv", "value_type": "path", "role": "path"}],
        produces=[],
    )
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=NOW
    )
    app._scheduled_survey_tick(NOW)
    paved_events = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.web_paved"
    ]
    assert len(paved_events) == 1
    web_id = paved_events[0].payload["web_id"]
    first_node = app._pave_store.paved("t1", web_id)["node_id"]

    # The exporter's IMPLEMENTATION changes under the SAME node id — the
    # web's membership and edges are identical, only the content moved.
    app._nodeplace.contribute(
        noder_principal="noder",
        tenant_id="t1",
        skill=_script_skill("exporter", "# exporter v2 — a better telling"),
        semver="1.1.0",
        title="exporter",
        summary="exporter",
        node_id=exporter,
        consumes=[],
        produces=[Slot(name="rows_csv", value_type="path", role="path")],
    )
    app._scheduled_survey_tick(NOW + timedelta(hours=7))

    paved_events = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.web_paved"
    ]
    # A content-blind signature would have skipped ("already paved") —
    # the P2 signature re-paves the same web_id with a new promotion.
    assert len(paved_events) == 2
    assert paved_events[1].payload["web_id"] == web_id
    second_node = app._pave_store.paved("t1", web_id)["node_id"]
    assert second_node != first_node
    # The promotion retired its predecessor: revoked, audited, never
    # left limping beside its successor.
    by_id = {node.node_id: node for node in app._nodeplace.all_nodes()}
    assert by_id[first_node].revoked_at is not None
    assert by_id[second_node].revoked_at is None
    retired = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.web_retired"
    ]
    assert len(retired) == 1
    assert retired[0].payload["node_id"] == first_node
    assert retired[0].payload["successor"] == second_node
    # The web's OTHER citizens are untouched.
    assert by_id[exporter].revoked_at is None
    assert by_id[cleaner].revoked_at is None
    conn.close()


# --------------------------------------------------------------------------- #
# P3 — the hold book: idempotent holds, one decision, a scoped release.        #
# --------------------------------------------------------------------------- #
def test_the_hold_book_holds_once_and_decides_once(tmp_path):
    from oolu.paver import WebHoldStore

    conn = DurableConnection(tmp_path / "holds.db")
    holds = WebHoldStore(conn)
    assert holds.hold(
        "t1", "w1", "anchor:run-1", hop=1, reserved=["publication"], now=NOW
    )
    # A re-delivered halt re-records nothing.
    assert not holds.hold(
        "t1", "w1", "anchor:run-1", hop=1, reserved=["publication"], now=NOW
    )
    assert [h["web_id"] for h in holds.open_holds("t1")] == ["w1"]
    assert holds.released("t1", "w1", "anchor:run-1") is False
    row = holds.decide(
        "t1", "w1", "anchor:run-1", approved=True, by="approver", now=NOW
    )
    assert row["status"] == "released" and row["reserved"] == ["publication"]
    assert holds.released("t1", "w1", "anchor:run-1") is True
    # The release is scoped to exactly that (web, trigger) — a different
    # trigger's fire still holds.
    assert holds.released("t1", "w1", "anchor:run-2") is False
    # A decided hold is never re-decided.
    assert (
        holds.decide(
            "t1", "w1", "anchor:run-1", approved=False, by="x", now=NOW
        )
        is None
    )
    conn.close()


# --------------------------------------------------------------------------- #
# N7 — the magazine web: paved gated, fired by one trigger, held and           #
# resumed, one desk replaced without touching the others.                      #
# --------------------------------------------------------------------------- #
def _news_app(tmp_path):
    app, conn, ident, registry, executor = _pave_app(
        tmp_path, payloads=dict(NEWS_PAYLOADS)
    )
    calls = _spy(executor)
    token = ident.token("user", "t1")
    stood = app.handle(
        _req(
            "POST",
            "/v1/press/desks",
            token=token,
            body={"schedule": True, "at_minute": 7 * 60},
        )
    )
    assert stood.status == 201, stood.body
    assert stood.body["owner"] == "oolu-agent-news"
    assert stood.body["sop"] == "editorial-law"
    assert stood.body["schedule"] is not None
    desks = {d["name"]: d["node_id"] for d in stood.body["desks"]}
    assert set(desks) == {d.name for d in DESK_NODES}
    app._paver_schedule.enable(
        interval_hours=6, granted_by="quinn", tenant="t1", now=NOW
    )
    app._scheduled_survey_tick(NOW)
    paved = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.web_paved"
    ]
    assert paved, "the magazine web should have paved"
    web_id = paved[-1].payload["web_id"]
    return app, conn, ident, token, executor, calls, desks, web_id


def test_the_magazine_web_paves_gated_under_the_news_principal(tmp_path):
    app, conn, ident, token, executor, calls, desks, web_id = _news_app(
        tmp_path
    )
    stored = app._pave_store.paved("t1", web_id)
    # P4: the web is the News agent's standing property.
    assert stored["owner_principal"] == "oolu-agent-news"
    assert stored["anchor"] == desks[GENRE_DESK]
    by_id = {node.node_id: node for node in app._nodeplace.all_nodes()}
    assert by_id[stored["node_id"]].noder_principal == "oolu-agent-news"
    # The editorial law rode the pave: both guards stand in the promoted
    # contract and render IN WORDS on /v1/paver/webs (the exit gate).
    webs = app.handle(_req("GET", "/v1/paver/webs", token=token)).body["webs"]
    gated = next(w for w in webs if w["web_id"] == web_id)
    gates = {
        (g["source"], g["target"]): g["guard"] for g in gated["gates"]
    }
    survey_gate = gates[(RESEARCH_DESK, SURVEY_DESK)]
    assert survey_gate["pointer"] == "result/press_research_bundle/open_questions"
    publish_gate = gates[(COMPOSITION_DESK, PUBLICATION_DESK)]
    assert publish_gate["pointer"] == "result/press_post_draft/sources_resolved"
    conn.close()


def test_one_trigger_runs_the_six_desks_end_to_end_with_gates(tmp_path):
    app, conn, ident, token, executor, calls, desks, web_id = _news_app(
        tmp_path
    )
    app._propagation_consent.grant(
        "t1", web_id, granted_by="user", now=NOW
    )
    anchor = desks[GENRE_DESK]
    calls.clear()
    app._web_router(NOW).on_trigger("t1", anchor, "run-1")
    app._drain_propagation(NOW)
    fired = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_fired"
    ]
    assert fired and fired[-1].payload["fired"] == 6
    # All six desks ran, in pipeline order — the slot flow IS the order.
    assert calls == [
        GENRE_DESK,
        TOPIC_DESK,
        RESEARCH_DESK,
        SURVEY_DESK,
        COMPOSITION_DESK,
        PUBLICATION_DESK,
    ]

    # Gates are enforced at TRIGGER time, not just rehearsal: research
    # leaving NO open question means the survey desk (and everything
    # downstream of its output) honestly does not run.
    executor._payloads[RESEARCH_DESK] = {
        "press_research_bundle": {
            **NEWS_PAYLOADS[RESEARCH_DESK]["press_research_bundle"],
            "open_questions": 0,
            "questions": [],
        }
    }
    calls.clear()
    app._web_router(NOW).on_trigger("t1", anchor, "run-2")
    app._drain_propagation(NOW)
    assert calls == [GENRE_DESK, TOPIC_DESK, RESEARCH_DESK]
    conn.close()


def test_an_editorial_hold_halts_and_resumes_on_release(tmp_path):
    app, conn, ident, token, executor, calls, desks, web_id = _news_app(
        tmp_path
    )
    app._propagation_consent.grant("t1", web_id, granted_by="user", now=NOW)
    anchor = desks[GENRE_DESK]
    # The human's law reserves the publication desk — every fire now
    # routes through the editorial hold.
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/paver/sops",
                token=token,
                body={
                    "sop": "sop: publication-hold\napproval:\n"
                    "  operations: ['press publication desk*']\n"
                },
            )
        ).status
        == 201
    )
    calls.clear()
    stamp = f"{anchor}:run-3"
    app._web_router(NOW).on_trigger("t1", anchor, "run-3")
    app._drain_propagation(NOW)
    # Held: nothing ran, the hold is a durable row the approver can see.
    assert calls == []
    held = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "paver.propagation_held"
    ]
    assert held and held[-1].payload["trigger_stamp"] == stamp
    holds = app.handle(_req("GET", "/v1/paver/holds", token=token)).body[
        "holds"
    ]
    assert [h["trigger_stamp"] for h in holds] == [stamp]
    assert holds[0]["reserved"] == [PUBLICATION_DESK]

    # A plain member cannot release the hold — the approver can.
    denied = app.handle(
        _req(
            "POST",
            f"/v1/paver/holds/{web_id}",
            token=token,
            body={"trigger_stamp": stamp, "approved": True},
        )
    )
    assert denied.status == 403
    _grant_approver(ident, "approver", "t1")
    released = app.handle(
        _req(
            "POST",
            f"/v1/paver/holds/{web_id}",
            token=ident.token("approver", "t1"),
            body={"trigger_stamp": stamp, "approved": True},
        )
    )
    assert released.status == 200 and released.body["resumed"] is True
    # The release re-staged the very message that halted: the drain
    # delivers it and the web COMPLETES — the wall is a gate again.
    app._drain_propagation(NOW)
    assert calls[-1] == PUBLICATION_DESK and len(calls) == 6
    assert app.handle(
        _req("GET", "/v1/paver/holds", token=token)
    ).body["holds"] == []

    # A denial settles the trigger: nothing fires, nothing re-stages.
    calls.clear()
    app._web_router(NOW).on_trigger("t1", anchor, "run-4")
    app._drain_propagation(NOW)
    assert calls == []
    denied_hold = app.handle(
        _req(
            "POST",
            f"/v1/paver/holds/{web_id}",
            token=ident.token("approver", "t1"),
            body={"trigger_stamp": f"{anchor}:run-4", "approved": False},
        )
    )
    assert denied_hold.status == 200 and denied_hold.body["resumed"] is False
    app._drain_propagation(NOW)
    assert calls == []
    conn.close()


def test_replacing_one_desk_repaves_without_touching_the_others(tmp_path):
    app, conn, ident, token, executor, calls, desks, web_id = _news_app(
        tmp_path
    )
    first = app._pave_store.paved("t1", web_id)
    # The research desk's implementation evolves — a successor VERSION
    # under the SAME node, exactly what an approved observer proposal
    # mandates the owner to do.
    desk = next(d for d in DESK_NODES if d.name == RESEARCH_DESK)
    app._nodeplace.contribute(
        noder_principal="oolu-agent-news",
        tenant_id="t1",
        skill=_script_skill(RESEARCH_DESK, desk.script + "\n# v2: stricter"),
        semver="1.1.0",
        title=desk.name,
        summary=desk.summary,
        node_id=desks[RESEARCH_DESK],
        consumes=desk.slot_consumes(),
        produces=desk.slot_produces(),
    )
    app._scheduled_survey_tick(NOW + timedelta(hours=7))
    second = app._pave_store.paved("t1", web_id)
    # Same web, new promotion; the predecessor retired under its own
    # (News-agent) name; the five other desks untouched.
    assert second["node_id"] != first["node_id"]
    by_id = {node.node_id: node for node in app._nodeplace.all_nodes()}
    assert by_id[first["node_id"]].revoked_at is not None
    for name, node_id in desks.items():
        assert by_id[node_id].revoked_at is None, name
    conn.close()


# --------------------------------------------------------------------------- #
# The observer seat: models propose, the owner disposes.                       #
# --------------------------------------------------------------------------- #
class _Voice:
    def __init__(self, reply):
        self._reply = reply

    def reply(self, messages):
        return self._reply


def test_the_observer_speaks_only_under_the_contract():
    found = observe_desk(
        "press genre desk",
        "rank 1: products (trial)",
        model=_Voice("ISSUE: the trial slot repeats one genre\nPLAN: widen the draw"),
    )
    assert found == (
        "the trial slot repeats one genre",
        "widen the draw",
    )
    # NOTHING, a broken contract, and a dead model all file nothing.
    assert observe_desk("d", "r", model=_Voice("NOTHING")) is None
    assert observe_desk("d", "r", model=_Voice("free prose")) is None
    assert observe_desk("d", "r", model=None) is None
    assert observe_desk("d", "", model=_Voice("ISSUE: x\nPLAN: y")) is None


def test_proposals_file_once_per_desk_and_the_owner_decides(tmp_path):
    conn = DurableConnection(tmp_path / "proposals.db")
    store = DeskProposalStore(conn)
    first = store.file(
        tenant="t1",
        desk="press genre desk",
        issue="the trial slot repeats",
        plan="widen the draw",
        evidence=["reading:press genre desk"],
    )
    assert first is not None
    # One open proposal per desk — never a pile.
    assert (
        store.file(tenant="t1", desk="press genre desk", issue="x", plan="y")
        is None
    )
    decided = store.decide(first, tenant="t1", approved=True, by="quinn")
    assert decided["status"] == "approved" and decided["decided_by"] == "quinn"
    # Decided once; a fresh proposal may then be filed.
    assert store.decide(first, tenant="t1", approved=False, by="x") is None
    assert (
        store.file(tenant="t1", desk="press genre desk", issue="x", plan="y")
        is not None
    )
    assert [p["status"] for p in store.list(tenant="t1")] == [
        "open",
        "approved",
    ]
    conn.close()


def test_the_proposal_door_is_owner_decided(tmp_path):
    app, conn, ident, registry, executor = _pave_app(tmp_path, payloads={})
    token = ident.token("user", "t1")
    proposal_id = app._desk_proposals.file(
        tenant="t1",
        desk="press genre desk",
        issue="the reading is stale",
        plan="replace the desk with a fresher rollup",
    )
    listed = app.handle(
        _req("GET", "/v1/press/desks/proposals", token=token)
    ).body["proposals"]
    assert [p["proposal_id"] for p in listed] == [proposal_id]
    # A plain member cannot dispose — the standing approval path can.
    assert (
        app.handle(
            _req(
                "POST",
                f"/v1/press/desks/proposals/{proposal_id}",
                token=token,
                body={"approved": True},
            )
        ).status
        == 403
    )
    _grant_approver(ident, "approver", "t1")
    decided = app.handle(
        _req(
            "POST",
            f"/v1/press/desks/proposals/{proposal_id}",
            token=ident.token("approver", "t1"),
            body={"approved": True},
        )
    )
    assert decided.status == 200 and decided.body["status"] == "approved"
    # No model on this host: an observation pass files nothing, honestly.
    observed = app.handle(
        _req("POST", "/v1/press/desks/observe", token=token)
    )
    assert observed.status == 200 and observed.body["filed"] == []
    conn.close()
