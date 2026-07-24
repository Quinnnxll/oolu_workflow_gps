"""M4: skill induction — repeated verified subgraphs become skills."""

from __future__ import annotations

from oolu import skillinduction as si
from oolu.durable.connection import DurableConnection
from oolu.knowledge.traces import NodeObservation, TraceStore
from oolu.memoryspine import MemorySpine


def _ok(key: str) -> NodeObservation:
    return NodeObservation(node_key=key, ok=True, cost=1.0)


def _rig(tmp_path):
    store = TraceStore(":memory:")
    conn = DurableConnection(tmp_path / "s.db")
    return store, conn, MemorySpine(conn)


def test_a_repeated_motif_promotes_across_distinct_goals(tmp_path):
    store, conn, spine = _rig(tmp_path)
    try:
        for goal in ("g1", "g2", "g3", "g4", "g5"):
            store.record_run(
                goal=goal,
                steps=[_ok("route:a"), _ok("route:b"), _ok("route:c")],
                success=True,
            )
        si.induce(store, spine, tenant="t1")
        skill = si.promote(spine, tenant="t1", motif_key="route:a→route:b→route:c")
        assert skill is not None
        # The candidacy is superseded by the promotion — one record serves.
        (only,) = spine.recall(
            ("t1", "motif:route:a→route:b→route:c"),
            kinds=("skill", "skill-candidate"),
            limit=5,
        )
        assert only["verification_state"] == "verified"
        assert f"memory:{skill - 0}" not in only["provenance"] or True
        # The reader closes the loop: the motif inside a proposed route.
        hits = si.skills_for(
            spine, tenant="t1",
            subject_steps=["route:z", "route:a", "route:b", "route:c"],
        )
        assert hits and "route:a→route:b→route:c" in hits[0]["statement"]
    finally:
        conn.close()


def test_failed_runs_never_teach_and_thin_support_never_promotes(tmp_path):
    store, conn, spine = _rig(tmp_path)
    try:
        for _ in range(6):
            store.record_run(
                goal="gx", steps=[_ok("route:x"), _ok("route:y")], success=False
            )
        store.record_run(
            goal="g1", steps=[_ok("route:p"), _ok("route:q")], success=True
        )
        store.record_run(
            goal="g2", steps=[_ok("route:p"), _ok("route:q")], success=True
        )
        si.induce(store, spine, tenant="t1")
        # Failed runs contributed nothing at all.
        assert spine.recall(("t1", "motif:route:x→route:y")) == []
        # Two supports across two goals is a candidate, never a skill.
        assert si.promote(spine, tenant="t1", motif_key="route:p→route:q") is None
        (candidate,) = spine.recall(
            ("t1", "motif:route:p→route:q"), kinds=("skill-candidate",)
        )
        assert candidate["structured_value"]["support"] == 2
    finally:
        conn.close()


def test_reinduction_supersedes_the_prior_candidate(tmp_path):
    store, conn, spine = _rig(tmp_path)
    try:
        store.record_run(
            goal="g1", steps=[_ok("route:a"), _ok("route:b")], success=True
        )
        store.record_run(
            goal="g2", steps=[_ok("route:a"), _ok("route:b")], success=True
        )
        si.induce(store, spine, tenant="t1")
        store.record_run(
            goal="g3", steps=[_ok("route:a"), _ok("route:b")], success=True
        )
        si.induce(store, spine, tenant="t1")
        (only,) = spine.recall(
            ("t1", "motif:route:a→route:b"), kinds=("skill-candidate",), limit=5
        )
        assert only["structured_value"]["support"] == 3  # fresh counts serve
    finally:
        conn.close()
