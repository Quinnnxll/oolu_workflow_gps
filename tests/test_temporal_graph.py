"""M1 of the memory-stack plan: the temporal graph and projections.

Edges carry validity intervals, provenance, and supersession; every
read is time-scoped ("what depended on X when Y happened" is one
query); a closed edge never contributes proximity; and state cards are
projections — derived on every call, rebuild-equals-read by shape.
"""

from __future__ import annotations

import pytest
from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import GOOD

from oolu.durable.connection import DurableConnection
from oolu.temporalgraph import TemporalGraph


@pytest.fixture()
def graph(tmp_path):
    conn = DurableConnection(tmp_path / "graph.db")
    yield TemporalGraph(conn)
    conn.close()


def test_an_edge_without_provenance_is_refused(graph):
    with pytest.raises(ValueError):
        graph.connect("depends_on", "a", "b", provenance=())


def test_dependents_at_is_one_time_scoped_query(graph):
    from datetime import UTC, datetime

    early = graph.connect("depends_on", "app", "lib", provenance=("e:1",))
    mid = datetime.now(UTC).isoformat()
    graph.close(early)
    graph.connect("depends_on", "tool", "lib", provenance=("e:2",))

    now_deps = {d["source_id"] for d in graph.dependents_at("lib", datetime.now(UTC).isoformat())}
    then_deps = {d["source_id"] for d in graph.dependents_at("lib", mid)}
    assert now_deps == {"tool"}
    assert then_deps == {"app"}  # history answers as of THEN


def test_a_closed_edge_never_contributes_proximity(graph):
    edge = graph.connect("produces", "node-a", "slot:rows", provenance=("e:1",))
    assert "slot:rows" in graph.neighborhood("node-a")
    graph.close(edge)
    assert graph.neighborhood("node-a") == set()


def test_neighborhood_walks_hops_and_excludes_the_seed(graph):
    graph.connect("produces", "node-a", "slot:rows", provenance=("e:1",))
    graph.connect("consumes", "node-b", "slot:rows", provenance=("e:2",))
    one_hop = graph.neighborhood("node-a", hops=1)
    two_hop = graph.neighborhood("node-a", hops=2)
    assert one_hop == {"slot:rows"}
    assert two_hop == {"slot:rows", "node-b"}
    assert "node-a" not in two_hop


def test_a_publish_lands_its_relations_and_the_card_projects(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" not in reply.body["reply"]

        graph = app._temporal_graph()
        goal_key = app._function_skill_id("t1", GOAL)
        satisfied = graph.neighbors(f"goal:{goal_key}", edge_types=("satisfies",))
        assert satisfied, "the publish landed no satisfies edge"
        node_id = satisfied[0]["source_id"]
        # The GOOD fixture declares one output: result.
        assert "slot:result" in graph.neighborhood(node_id)

        session = type("S", (), {"tenant_id": "t1", "principal_id": "u1"})()
        first = app._node_state_card(session, node_id)
        second = app._node_state_card(session, node_id)
        assert first == second  # a projection, not stored state
        assert any(
            r["edge_type"] == "satisfies" for r in first["relations"]
        )
    finally:
        conn.close()
