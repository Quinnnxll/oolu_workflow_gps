"""Plan §5: the conversational-building quality metrics, live.

Exit gate: the five building-quality numbers ride the standing investor
catalog and collect from the REAL stores — after a full hand-off circle
(B3 io stored, B4 edge cited) the shares read 100 and the divergence 0;
deleting a drawer copy moves the divergence; a standing clarification
splits into mechanism questions (target zero, the B0 wall's law) and
plain value asks (the honest cost of conversation-first); and every key
is a cataloged spec, so the panel names, thresholds, and charts it
without edits.
"""

from __future__ import annotations

from test_growth_trigger import _chat
from test_handoff import _seed_producer

from oolu.orchestrator.state import (
    PauseKind,
    PauseToken,
    RunState,
    TaskContract,
)
from oolu.telemetry.investor import METRIC_CATALOG, MetricsSnapshotStore

BUILD_KEYS = {
    "build.mechanism_questions_open",
    "build.value_asks_open",
    "build.src_divergence_pct",
    "build.runs_with_stored_io_pct",
    "build.handoff_citation_pct",
}


def _service(app, conn):
    app._metrics_store = MetricsSnapshotStore(conn)
    return app._investor_metrics()


def test_the_catalog_carries_all_five_with_their_contracts():
    by_key = {s.key: s for s in METRIC_CATALOG}
    assert BUILD_KEYS <= set(by_key)
    # The plan's own targets ride the specs: zero mechanism questions,
    # zero divergence, everything stored and cited.
    assert by_key["build.mechanism_questions_open"].direction == "down"
    assert by_key["build.mechanism_questions_open"].target == 0.0
    assert by_key["build.src_divergence_pct"].direction == "down"
    assert by_key["build.runs_with_stored_io_pct"].target == 100.0
    assert by_key["build.handoff_citation_pct"].target == 100.0
    for key in BUILD_KEYS:
        assert by_key[key].source == "auto"
        assert by_key[key].formula


def test_the_full_circle_reads_clean(tmp_path):
    # The B4 rig end to end: producer ran, consumer bound the standing
    # output on a yes — every phase's number should read healthy.
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        agreed = _chat(app, ident, "yes")
        assert agreed.body["run_id"], agreed.body
        collected = _service(app, conn).collect()
        assert collected["build.runs_with_stored_io_pct"] == 100.0
        assert collected["build.handoff_citation_pct"] == 100.0
        assert collected["build.src_divergence_pct"] == 0.0
        assert collected["build.mechanism_questions_open"] == 0.0
        assert collected["build.value_asks_open"] == 0.0
    finally:
        conn.close()


def test_divergence_and_standing_asks_move_the_needles(tmp_path):
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        agreed = _chat(app, ident, "yes")
        assert agreed.body["run_id"], agreed.body

        # One of the two drawer copies vanishes: divergence reads 50 —
        # and heals on that node's next run, so the number can fall.
        mains = [
            f
            for node in app._nodeplace.all_nodes()
            for f in app._files.list(tenant="t1", node_id=node.node_id)
            if f.folder == "src" and f.name == "main.py"
        ]
        assert len(mains) == 2
        app._files.delete(mains[0].file_id, tenant="t1")

        # A run pauses on clarification: one mechanism ask (the B0
        # wall's debt, target zero) and one plain value ask.
        state = RunState(
            intent="export the ledger",
            contract=TaskContract(
                intent="export the ledger",
                submitted_by="user-1",
                metadata={"tenant_id": "t1"},
            ),
            pause=PauseToken(
                kind=PauseKind.CLARIFICATION,
                prompt="answers required",
                payload={
                    "questions": [
                        {
                            "parameter": "format",
                            "question": (
                                "Which file format should the export use?"
                            ),
                        },
                        {
                            "parameter": "folder",
                            "question": "Which folder are the invoices in?",
                        },
                    ]
                },
            ),
        )
        app._durable.runs.save(state)

        collected = _service(app, conn).collect()
        assert collected["build.src_divergence_pct"] == 50.0
        assert collected["build.mechanism_questions_open"] == 1.0
        assert collected["build.value_asks_open"] == 1.0
    finally:
        conn.close()
