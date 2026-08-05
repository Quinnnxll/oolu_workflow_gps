"""The web at scale (V5): indexed discovery, link dimensions, energy.

Exit gate (node-vitality-plan, phase V5): a synthetic registry at scale
answers discovery in bounded time (the inverted index, not the LIKE
scan); across 100 seeded assemblies a fresh fit node wins some and a
proven node wins most (the N1-style statistical pin, now over the
ENERGY selection); and the chosen web's energy and seed are recorded
and replayable.
"""

from __future__ import annotations

import time

from test_contract_run import _build
from test_gateway_market import _contribute_and_publish, _req

from oolu.durable import DurableConnection
from oolu.knowledge.traces import NodeObservation, TraceStore, route_node_key
from oolu.nodeplace import (
    Listing,
    ListingStatus,
    Node,
    NodeplaceService,
    NodeVersion,
    RegistryStore,
    Visibility,
    candidate_graph,
    cohesion_lookup,
    web_energy,
)
from oolu.nodeplace.energy import EnergyTerm

RAW = {"name": "raw", "value_type": "path", "role": "path"}
TIDY = {"name": "tidy", "value_type": "path", "role": "path"}


# --------------------------------------------------------------------------- #
# Indexed discovery: bounded-time search over a synthetic registry.            #
# --------------------------------------------------------------------------- #
def _synthetic_registry(conn, *, listings: int) -> RegistryStore:
    registry = RegistryStore(conn)
    with conn.transaction() as db:
        for i in range(listings):
            node = Node(
                noder_principal=f"noder-{i % 40}",
                tenant_id="t1",
                skill_id=f"fn-{i:08d}",
                visibility=Visibility.PUBLIC,
            )
            registry.add_node(node)
            version = NodeVersion(
                node_id=node.node_id,
                semver="1.0.0",
                content_hash=f"{i:064d}",
                sanitized_skill_json='{"actions": []}',
            )
            registry.add_version(version)
            registry.add_listing(
                Listing(
                    version_id=version.version_id,
                    title=f"node {i} widget number{i}",
                    summary=f"does chore {i % 97} for department {i % 13}",
                    tags=[f"tag{i % 29}"],
                    capabilities=[f"fn:verb{i % 61}", f"io:slot{i % 31}"],
                    status=ListingStatus.ACTIVE,
                )
            )
        del db  # the loop used the store's own doors; keep one txn scope
    return registry


def test_discover_answers_a_big_registry_through_the_index(tmp_path):
    conn = DurableConnection(tmp_path / "big.db")
    try:
        registry = _synthetic_registry(conn, listings=3000)
        # The query plan uses the FTS index, not a table scan.
        with conn.lock:
            plan = " ".join(
                str(dict(row))
                for row in conn.db.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT l.listing_id FROM listing_fts "
                    "JOIN listings l ON l.listing_id = listing_fts.listing_id "
                    "WHERE listing_fts MATCH ?",
                    ('"widget"*',),
                ).fetchall()
            )
        assert "listing_fts" in plan
        started = time.monotonic()
        found = registry.discover("number2999")
        elapsed = time.monotonic() - started
        assert [listing.title for listing in found] == [
            "node 2999 widget number2999"
        ]
        # Bounded-time is the exit criterion: one indexed lookup, not a
        # scan of every row — generous wall for slow CI machines.
        assert elapsed < 1.0, f"discovery took {elapsed:.3f}s"
        # Capability tokens are findable exactly as before.
        assert registry.discover("verb60")
        # Multi-word queries conjoin; prefixes still forgive morphology.
        assert registry.discover("widget number2999")
        assert registry.discover("widg")
    finally:
        conn.close()


def test_discover_keeps_its_filters_under_the_index(tmp_path):
    conn = DurableConnection(tmp_path / "filters.db")
    try:
        service = NodeplaceService(RegistryStore(conn))
        from test_nodeplace import _contribute

        result = _contribute(service)
        # DRAFT stays invisible; ACTIVE is found by its words.
        assert service.discover("deploy") == []
        service.publish(
            result.listing.listing_id,
            noder_principal="noder-1",
            tenant_id="t-noder",
        )
        found = service.discover("deploy")
        assert [listing.version_id for listing in found] == [
            result.version.version_id
        ]
    finally:
        conn.close()


def test_faces_backfill_and_replace_version_fetches(tmp_path):
    conn = DurableConnection(tmp_path / "faces.db")
    try:
        service = NodeplaceService(RegistryStore(conn))
        from test_nodeplace import _contribute

        _contribute(service)
        faces = service.own_faces("t-noder", "noder-1")
        assert len(faces) == 1
        face = faces[0]
        assert face["title"] == "deploy helper"
        assert face["goal"] == "does a thing"
        assert face["has_script"] is False  # a cli action, no script
        # A pre-V5 store (no face rows) heals at construction.
        with conn.transaction() as db:
            db.execute("DELETE FROM node_faces")
        service = NodeplaceService(RegistryStore(conn))
        healed = service.own_faces("t-noder", "noder-1")
        assert [f["title"] for f in healed] == ["deploy helper"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The link dimensions.                                                         #
# --------------------------------------------------------------------------- #
def test_version_cooccurrence_reads_runs_together(tmp_path):
    traces = TraceStore(tmp_path / "traces.db")
    traces.record_observation(
        goal="g",
        route="r",
        success=True,
        outcome_score=1.0,
        node_versions=["v-a", "v-b"],
    )
    traces.record_observation(
        goal="g",
        route="r",
        success=False,
        outcome_score=0.0,
        node_versions=["v-a", "v-b", "v-c"],
    )
    pairs = traces.version_cooccurrence()
    assert pairs[("v-a", "v-b")] == (2, 1)
    assert pairs[("v-a", "v-c")] == (1, 0)
    assert pairs[("v-b", "v-c")] == (1, 0)


def test_cohesion_rewards_pairs_that_finished_together(tmp_path):
    traces = TraceStore(tmp_path / "traces.db")
    for _ in range(3):
        traces.record_observation(
            goal="g",
            route="r",
            success=True,
            outcome_score=1.0,
            node_versions=["v-a", "v-b"],
        )
    cohesion = cohesion_lookup(traces, None)
    proven = cohesion(["v-a", "v-b"])
    assert proven == 1.0  # three shared verified runs: fully proven
    assert cohesion(["v-a", "v-x"]) == 0.0  # strangers
    assert cohesion(["v-a"]) == 0.0  # a single node has no pairs
    # The energy term: equal parts, the cohesive web reads lower.
    terms = [EnergyTerm(name="a", q=0.9), EnergyTerm(name="b", q=0.9)]
    assert (
        web_energy(terms, cohesion=proven).total
        < web_energy(terms, cohesion=0.0).total
    )


def test_candidate_graph_joins_the_three_dimensions(tmp_path):
    conn = DurableConnection(tmp_path / "graph.db")
    try:
        service = NodeplaceService(RegistryStore(conn))
        from test_nodeplace import _skill

        first = service.contribute(
            noder_principal="noder-1",
            tenant_id="t-noder",
            skill=_skill(),
            semver="1.0.0",
            title="Deploy Helper",
            summary="automates a deploy",
            tags=["deploy", "ops", "market:deploys"],
        )
        service.publish(
            first.listing.listing_id,
            noder_principal="noder-1",
            tenant_id="t-noder",
        )
        second = service.contribute(
            noder_principal="noder-2",
            tenant_id="t-noder",
            skill=_skill(description="derived deploy helper"),
            semver="1.0.0",
            title="Deploy Helper II",
            summary="automates a deploy, derived",
            # The shared market segment puts both in ONE class — the
            # class dimension's join key.
            tags=["deploy", "ops", "market:deploys"],
            derived_from=first.version.version_id,
        )
        service.publish(
            second.listing.listing_id,
            noder_principal="noder-2",
            tenant_id="t-noder",
        )
        traces = TraceStore(tmp_path / "traces.db")
        traces.record_observation(
            goal="deploy",
            route="r",
            success=True,
            outcome_score=1.0,
            node_versions=[
                first.version.version_id,
                second.version.version_id,
            ],
        )
        graph = candidate_graph(service, traces=traces, query="deploy")
        ids = {node.version_id for node in graph.nodes}
        assert ids == {first.version.version_id, second.version.version_id}
        kinds = {edge.kind for edge in graph.edges}
        # Co-occurrence (they ran together), lineage (derived_from), and
        # class (same slugged title class) — all three, one graph.
        assert kinds == {"cooccurrence", "lineage", "class"}
        lineage = [e for e in graph.edges if e.kind == "lineage"]
        assert lineage[0].source == first.version.version_id
        assert lineage[0].target == second.version.version_id
        co = [e for e in graph.edges if e.kind == "cooccurrence"]
        assert co[0].together == 1 and co[0].successes == 1
    finally:
        conn.close()


def test_semantic_near_misses_are_proposed_never_minted(tmp_path):
    from oolu.paver.discovery import SurveyNode, WebSurveyor
    from oolu.skills.contract import NodeContract, ScriptBody, Slot

    producer = SurveyNode(
        key="maker",
        contract=NodeContract(
            id="maker",
            name="maker",
            provenance="synthesized",
            produces=[Slot(name="invoice_document", value_type="path")],
            body=ScriptBody(goal="goal:maker"),
        ),
    )
    consumer = SurveyNode(
        key="taker",
        contract=NodeContract(
            id="taker",
            name="taker",
            provenance="synthesized",
            consumes=[Slot(name="invoice_file", value_type="path")],
            body=ScriptBody(goal="goal:taker"),
        ),
    )
    # No roles declared: the deterministic rule drops the pair as noise —
    # the two nodes stay unrelated, no web forms between them.
    plain = WebSurveyor().survey("t1", [producer, consumer])
    assert all(
        not web.near_misses and len(web.node_ids) < 2 for web in plain.webs
    )
    # The embedding dimension proposes it — as a NEAR-MISS, never an edge.
    semantic = WebSurveyor(similar=lambda a, b: 1.0).survey(
        "t1", [producer, consumer]
    )
    (web,) = [w for w in semantic.webs if len(w.node_ids) == 2]
    assert [n.reason for n in web.near_misses] == [
        "semantically close names, same type"
    ]
    assert web.edges == []  # law 4: advice proposes pairs, types dispose
    # A distant pair stays unproposed even with the hand wired.
    far = WebSurveyor(similar=lambda a, b: 0.0).survey(
        "t1", [producer, consumer]
    )
    assert all(
        not web.near_misses and len(web.node_ids) < 2 for web in far.webs
    )


# --------------------------------------------------------------------------- #
# The energy reading: seeded, recorded, replayable — and fair to the fresh.    #
# --------------------------------------------------------------------------- #
def _energy_market(tmp_path):
    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(tmp_path, trace_store=traces)
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="raw exporter",
        noder="noder-export",
        price=0.10,
        produces=[RAW],
        consumes=[],
    )
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="proven cleaner",
        noder="noder-proven",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="fresh cleaner",
        noder="noder-fresh",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )
    # The proven cleaner earned its posterior; the fresh one just arrived.
    # The exporter is proven too — it appears in EVERY candidate web, so
    # leaving it historyless would drown the cleaners' contest in the
    # noise of its own uniform draws.
    for _ in range(16):
        traces.record_run(
            goal="clean-the-books",
            steps=[
                NodeObservation(
                    node_key=route_node_key("proven cleaner"), ok=True
                ),
                NodeObservation(
                    node_key=route_node_key("raw exporter"), ok=True
                ),
            ],
            success=True,
            context="t2",
        )
    return app, conn, ident


def _energy_reading(app, ident, *, seed, beam=2):
    resp = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={
                "goal": {"name": "clean-the-books", "want": [TIDY]},
                "explore": True,
                "beam": beam,
                "seed": seed,
            },
        )
    )
    assert resp.status == 200, resp.body
    return resp.body


def test_the_energy_selection_keeps_the_fresh_node_alive(tmp_path):
    """The N1-style statistical pin, over the energy selection: across
    100 seeded readings the proven node wins most — and the freshly
    published fit node keeps earning draws, exactly as the desk
    doctrine demands. Fixed seeds: the outcome is deterministic."""
    app, conn, ident = _energy_market(tmp_path)
    try:
        leaders = []
        for seed in range(100):
            body = _energy_reading(app, ident, seed=seed)
            (chosen,) = {"proven cleaner", "fresh cleaner"} & set(
                body["selected"]
            )
            leaders.append(chosen)
        proven = leaders.count("proven cleaner")
        fresh = leaders.count("fresh cleaner")
        assert proven > 70, f"proven won only {proven}/100"
        assert fresh > 0, "the fresh node never earned a draw"
    finally:
        conn.close()


def test_the_energy_reading_replays_from_its_recorded_seed(tmp_path):
    app, conn, ident = _energy_market(tmp_path)
    try:
        first = _energy_reading(app, ident, seed=41)
        again = _energy_reading(app, ident, seed=41)
        assert first["seed"] == again["seed"] == 41
        assert first["selected"] == again["selected"]
        assert first["energy"] == again["energy"]
        assert first["alternatives"] == again["alternatives"]
        # The reading names its parts: one term per picked node, and
        # every alternative the beam weighed, the chosen one marked.
        assert len(first["energy_terms"]) == len(first["selected"])
        assert any(alt["chosen"] for alt in first["alternatives"])
        # And it landed on the audit chain with its seed.
        readings = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "assembly.energy"
        ]
        assert readings and readings[-1].payload["seed"] == 41
        assert readings[-1].payload["energy"] == again["energy"]
    finally:
        conn.close()


def test_an_unseeded_energy_reading_mints_and_echoes_a_seed(tmp_path):
    app, conn, ident = _energy_market(tmp_path)
    try:
        resp = app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=ident.token("consumer", "t2"),
                body={
                    "goal": {"name": "clean-the-books", "want": [TIDY]},
                    "explore": True,
                    "beam": 2,
                },
            )
        )
        assert resp.status == 200, resp.body
        minted = resp.body["seed"]
        assert isinstance(minted, int)
        # The echoed seed replays the exact same reading.
        replay = _energy_reading(app, ident, seed=minted)
        assert replay["selected"] == resp.body["selected"]
        assert replay["energy"] == resp.body["energy"]
    finally:
        conn.close()


def test_beam_validation_refuses_nonsense(tmp_path):
    app, conn, ident = _energy_market(tmp_path)
    try:
        for bad in ({"beam": 0}, {"beam": 99}, {"beam": "wide"}, {"seed": "x"}):
            resp = app.handle(
                _req(
                    "POST",
                    "/v1/market/assemble",
                    token=ident.token("consumer", "t2"),
                    body={
                        "goal": {"name": "g", "want": [TIDY]},
                        **bad,
                    },
                )
            )
            assert resp.status == 400, (bad, resp.body)
    finally:
        conn.close()


def test_the_links_door_serves_the_candidate_graph(tmp_path):
    app, conn, ident = _energy_market(tmp_path)
    try:
        resp = app.handle(
            _req(
                "GET",
                "/v1/market/links",
                token=ident.token("consumer", "t2"),
                query={"q": "cleaner"},
            )
        )
        assert resp.status == 200, resp.body
        titles = {node["title"] for node in resp.body["nodes"]}
        assert {"proven cleaner", "fresh cleaner"} <= titles
        # Same slugged class ("workflow:<title>" differs per title), so no
        # class edge is required here — the door's shape is the pin.
        assert "edges" in resp.body
    finally:
        conn.close()
