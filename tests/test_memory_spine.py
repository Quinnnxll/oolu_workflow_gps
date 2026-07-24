"""M0 of the memory-stack plan: the atomic memory spine.

Admission is earned (evidence states require provenance), supersession
is a WHERE clause (a corrected value cannot re-enter a pack by
oversight), history is never erased, writers bridge (the BuildLedger
dual-writes its lessons with the attempt row and the audit chain as
provenance), and the loop closes through the real gateway: a refusal's
lesson reaches the retry's context pack FROM THE SPINE, and a publish
supersedes it everywhere at once.
"""

from __future__ import annotations

import pytest
from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import CRASHING, GOOD, _FailingScriptHand

from oolu.buildledger import BuildLedger
from oolu.durable.connection import DurableConnection
from oolu.memoryspine import MemorySpine


@pytest.fixture()
def spine(tmp_path):
    conn = DurableConnection(tmp_path / "spine.db")
    yield MemorySpine(conn)
    conn.close()


# --------------------------------------------------------------------------- #
# Admission is earned                                                          #
# --------------------------------------------------------------------------- #
def test_evidence_states_require_provenance(spine):
    with pytest.raises(ValueError):
        spine.admit(
            "lesson", "it failed", scope_ids=("t1",), verification_state="observed"
        )
    # Proposed may arrive bare; observed must show its receipts.
    bare = spine.admit("hunch", "maybe flaky", scope_ids=("t1",))
    backed = spine.admit(
        "lesson",
        "it failed",
        scope_ids=("t1",),
        verification_state="observed",
        provenance=("audit:abc123",),
    )
    assert spine.provenance(bare) == []
    assert spine.provenance(backed) == ["audit:abc123"]


def test_unscoped_or_empty_memories_are_refused(spine):
    with pytest.raises(ValueError):
        spine.admit("lesson", "   ", scope_ids=("t1",))
    with pytest.raises(ValueError):
        spine.admit("lesson", "real words", scope_ids=())
    with pytest.raises(ValueError):
        spine.admit(
            "lesson", "real words", scope_ids=("t1",), verification_state="maybe"
        )


# --------------------------------------------------------------------------- #
# Supersession is the query's shape                                            #
# --------------------------------------------------------------------------- #
def test_superseded_and_rejected_rows_never_recall(spine):
    old = spine.admit("lesson", "watch the KeyError", scope_ids=("t1", "g1"))
    fixed = spine.admit(
        "lesson-closed",
        "published — warnings closed",
        scope_ids=("t1", "g1"),
        verification_state="observed",
        provenance=("attempt:2",),
        supersedes=(old,),
    )
    spine.admit(
        "lesson",
        "never trust this",
        scope_ids=("t1", "g1"),
        verification_state="rejected",
        provenance=("audit:x",),
    )
    statements = [
        m["statement"] for m in spine.recall(("t1", "g1"), kinds=("lesson",))
    ]
    assert statements == []  # corrected and rejected rows are invisible
    # ...but history is never erased — the row still answers provenance.
    assert spine.provenance(old) == []
    assert fixed  # the closing record stands


def test_expired_memories_fall_out_of_recall(spine):
    spine.admit(
        "fact",
        "the offer stands until yesterday",
        scope_ids=("t1",),
        valid_until="2000-01-01T00:00:00+00:00",
    )
    assert spine.recall(("t1",), kinds=("fact",)) == []


def test_scope_walls_hold(spine):
    spine.admit("lesson", "tenant one's scar", scope_ids=("t1", "g1"))
    assert spine.recall(("t2", "g1"), kinds=("lesson",)) == []
    assert spine.recall(("t1", "g1"), kinds=("lesson",))


def test_recall_ranks_by_the_shared_scorer(spine):
    spine.admit("lesson", "csv parsing dropped the header row", scope_ids=("t1", "g"))
    spine.admit("lesson", "the webhook payload was oversized", scope_ids=("t1", "g"))
    top = spine.recall(("t1", "g"), "parse the csv rows", limit=1)
    assert "csv" in top[0]["statement"]


# --------------------------------------------------------------------------- #
# Writers bridge, never fork                                                   #
# --------------------------------------------------------------------------- #
def test_the_ledger_dual_writes_and_a_publish_closes_the_book(tmp_path):
    conn = DurableConnection(tmp_path / "l.db")
    try:
        spine = MemorySpine(conn)
        ledger = BuildLedger(conn, spine=spine)
        attempt = ledger.record(
            "t1",
            "k1",
            "the goal",
            status="refused",
            problem="NameError: boom",
            provenance=("audit:evt-9",),
        )
        (lesson,) = spine.recall(("t1", "k1"), kinds=("lesson",))
        assert "NameError" in lesson["statement"]
        assert lesson["source_seat"] == "node.build"
        # Provenance chains: the audit event AND the attempt row.
        assert f"build-attempt:{attempt}" in lesson["provenance"]
        assert "audit:evt-9" in lesson["provenance"]

        ledger.record("t1", "k1", "the goal", status="published", node_id="n-1")
        assert spine.recall(("t1", "k1"), kinds=("lesson",)) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The loop closes through the real gateway                                     #
# --------------------------------------------------------------------------- #
def test_the_retry_reads_its_lesson_from_the_spine(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _FailingScriptHand("undefined_name")}
        app._node_function_author = lambda tenant: FakeAuthor(CRASHING)
        first = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" in first.body["reply"]

        # The lesson is ON the spine, provenance resolving to the audit
        # chain the gateway appended to.
        spine = app._memory_spine()
        goal_key = app._function_skill_id("t1", GOAL)
        (lesson,) = spine.recall(("t1", goal_key), kinds=("lesson",))
        audit_refs = [p for p in lesson["provenance"] if p.startswith("audit:")]
        assert audit_refs
        chain_ids = {
            str(getattr(r, "entry_id", None) or getattr(r, "id", ""))
            for r in app._durable.audit.records()
        }
        assert audit_refs[0].split(":", 1)[1] in chain_ids

        # The retry's pack carries it — read spine-first by the gateway.
        author = FakeAuthor(GOOD)
        app._node_function_author = lambda tenant: author
        published = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" not in published.body["reply"]
        asked = str(author.calls[0][-1].get("content", ""))
        assert "previous attempt at this goal failed" in asked

        # And the publish closed the book on the spine too.
        assert spine.recall(("t1", goal_key), kinds=("lesson",)) == []
    finally:
        conn.close()
