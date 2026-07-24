"""M6: multi-agent work over shared state — batons, leases, verdicts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oolu import handoffs as ho
from oolu.durable.connection import DurableConnection
from oolu.durable.queue import DurableTaskQueue
from oolu.episodes import record_episode
from oolu.knowledge.traces import TraceStore
from oolu.memoryspine import MemorySpine
from oolu.routelearning import context_bucket


def _rig(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    return conn, DurableTaskQueue(conn), MemorySpine(conn)


def _baton(**overrides) -> ho.Handoff:
    fields = {
        "tenant": "t1",
        "subject": "goal:quarterly-report",
        "objective": "assemble the quarterly report",
        "from_agent": "agent-a",
        "state_refs": ("runstate:r42",),
        "completed_refs": ("run:41",),
        "evidence": ("audit:e7",),
        "unresolved": ("the revenue figure for March is unconfirmed",),
        "acceptance": ("the report renders as PDF with all twelve figures",),
    }
    fields.update(overrides)
    return ho.Handoff(**fields)


def test_the_baton_is_typed_never_a_transcript():
    # A baton that references nothing hands nothing off.
    with pytest.raises(ValueError, match="hands nothing off"):
        _baton(state_refs=(), completed_refs=(), evidence=())
    # An item long enough to be a smuggled transcript is refused.
    with pytest.raises(ValueError, match="never a[ \n]+transcript"):
        _baton(unresolved=("chat log: " + "user said... " * 60,))
    # And the shape simply has no transcript field to fill.
    assert "transcript" not in ho.Handoff.model_fields
    assert "messages" not in ho.Handoff.model_fields


def test_an_agent_resumes_a_colleagues_task_from_events_alone(tmp_path):
    conn, queue, spine = _rig(tmp_path)
    try:
        subject = "goal:quarterly-report"
        # Agent A works, records episodes on the stack, then hands off.
        record_episode(
            spine, tenant="t1", subject=subject, kind="build",
            objective="assemble the quarterly report",
            outcome="draft assembled; figures 1-11 verified",
            unresolved=("figure 12 fails to render",),
            sources=("audit:e6",),
        )
        task_id = ho.hand_off(queue, spine, handoff=_baton())
        # Agent B claims and resumes — from the stack and the baton,
        # never from A's conversation.
        claimed = ho.claim(queue, agent="agent-b")
        assert claimed is not None
        task, baton = claimed
        assert task.task_id == task_id and task.lease_owner == "agent-b"
        context = ho.resume(spine, tenant="t1", subject=subject, handoff=baton)
        assert context["objective"] == "assemble the quarterly report"
        assert context["latest_outcome"].startswith("draft assembled")
        # BOTH commitment sources survive, verbatim: the stack's open
        # item and the colleague's last word.
        assert "figure 12 fails to render" in context["unresolved"]
        assert (
            "the revenue figure for March is unconfirmed"
            in context["unresolved"]
        )
        assert context["acceptance"] == [
            "the report renders as PDF with all twelve figures"
        ]
        assert context["handed_off_by"] == "agent-a"
        # The handoff itself is shared-state truth on the spine, its
        # provenance citing the evidence and the claimable task.
        (record,) = spine.recall(("t1", subject), kinds=("handoff",))
        assert f"task:{task_id}" in record["provenance"]
        assert "audit:e7" in record["provenance"]
    finally:
        conn.close()


def test_leases_prevent_duplicate_claims_and_death_releases_the_baton(tmp_path):
    conn, queue, spine = _rig(tmp_path)
    try:
        ho.hand_off(queue, spine, handoff=_baton())
        start = datetime.now(UTC)
        first = ho.claim(queue, agent="agent-b", lease_seconds=60, now=start)
        assert first is not None
        # A second claimant gets nothing while the lease stands.
        assert ho.claim(queue, agent="agent-c", now=start) is None
        # The claimant dies (no heartbeat, no completion): after the
        # lease expires the SAME claim call hands the baton to a
        # successor — reclaim rides every claim.
        later = start + timedelta(seconds=120)
        second = ho.claim(queue, agent="agent-c", now=later)
        assert second is not None
        assert second[0].lease_owner == "agent-c"
        assert second[1].from_agent == "agent-a"
    finally:
        conn.close()


def test_a_shared_family_queue_is_a_loud_mistake(tmp_path):
    conn, queue, spine = _rig(tmp_path)
    try:
        queue.enqueue("run", {"anything": True})
        with pytest.raises(ValueError, match="ride their own queue"):
            ho.claim(queue, agent="agent-b")
    finally:
        conn.close()


def test_conflicting_proposals_persist_separately_until_resolved(tmp_path):
    conn, queue, spine = _rig(tmp_path)
    try:
        a_id = ho.propose(
            spine, tenant="t1", subject="slot:march-revenue", agent="agent-a",
            statement="march revenue is 41.2k (from the ledger export)",
            value={"amount": 41200},
        )
        b_id = ho.propose(
            spine, tenant="t1", subject="slot:march-revenue", agent="agent-b",
            statement="march revenue is 39.8k (from the bank feed)",
            value={"amount": 39800},
        )
        # The disagreement IS the record: both stand, side by side.
        standing = ho.proposals(spine, tenant="t1", subject="slot:march-revenue")
        assert {row["memory_id"] for row in standing} == {a_id, b_id}
        # The resolver may not crown their own proposal.
        with pytest.raises(ValueError, match="cannot resolve in its own"):
            ho.resolve(
                spine, tenant="t1", subject="slot:march-revenue",
                winner_memory_id=b_id, by="agent-b",
            )
        # An evaluator resolves: one decision, citing the winner,
        # superseding every proposal — history kept, dispute closed.
        decision = ho.resolve(
            spine, tenant="t1", subject="slot:march-revenue",
            winner_memory_id=b_id, by="evaluator-c",
            sources=("audit:bank-feed-check",),
        )
        assert ho.proposals(spine, tenant="t1", subject="slot:march-revenue") == []
        (only,) = spine.recall(
            ("t1", "slot:march-revenue"), kinds=("decision",)
        )
        assert only["memory_id"] == decision
        assert f"memory:{b_id}" in only["provenance"]
        # Resolving a settled dispute is refused — no live disagreement.
        with pytest.raises(ValueError, match="live disagreement"):
            ho.resolve(
                spine, tenant="t1", subject="slot:march-revenue",
                winner_memory_id=a_id, by="evaluator-c",
            )
    finally:
        conn.close()


def test_the_producer_never_scores_its_own_deliverable(tmp_path):
    conn, queue, spine = _rig(tmp_path)
    try:
        with pytest.raises(ValueError, match="never verifies its own"):
            ho.record_verdict(
                spine, tenant="t1", subject="node:report", verifier="agent-a",
                producer="agent-a", verdict="pass",
            )
        row_id = ho.record_verdict(
            spine, tenant="t1", subject="node:report", verifier="agent-b",
            producer="agent-a", verdict="block",
            concern="figure 12 is a placeholder, not a computed chart",
        )
        (verdict,) = spine.recall(("t1", "node:report"), kinds=("verdict",))
        assert verdict["memory_id"] == row_id
        assert verdict["structured_value"]["verifier"] == "agent-b"
        # Routing applies the same law: the top expert verifies, unless
        # the top expert produced the work — then the next one does.
        board = [
            {"agent": "agent-a", "score": 0.9},
            {"agent": "agent-b", "score": 0.7},
        ]
        assert ho.assign_verifier(producer="agent-b", board=board) == "agent-a"
        assert ho.assign_verifier(producer="agent-a", board=board) == "agent-b"
        assert (
            ho.assign_verifier(
                producer="agent-a", board=[{"agent": "agent-a", "score": 0.9}]
            )
            is None
        )
    finally:
        conn.close()


def test_expertise_derives_from_seat_performance_and_trace_outcomes():
    store = TraceStore(":memory:")
    try:
        seat_rows = [
            {"model": "m1", "published": 8, "refused": 2, "success_rate": 0.8},
            {"model": "m2", "published": 1, "refused": 0, "success_rate": 1.0},
        ]
        # Volume beats luck: one lucky publish does not outrank a
        # proven 8-of-10 (the Beta posterior, not the raw rate).
        board = ho.expertise_board(seat_rows=seat_rows)
        assert [row["agent"] for row in board] == ["m1", "m2"]
        # Trace outcomes join in per subject, in the agent's own M5
        # bucket: m2's verified record ON THIS ROUTE flips the ranking.
        for _ in range(8):
            store.record_run(
                goal="quarterly-report", steps=[], success=True,
                context=context_bucket({"model": "m2"}),
            )
        board = ho.expertise_board(
            seat_rows=seat_rows, store=store, subject="quarterly-report"
        )
        assert [row["agent"] for row in board] == ["m2", "m1"]
        # No evidence sits at the uniform prior — explorable, never
        # presumed expert (and never credited with the global record).
        board = ho.expertise_board(
            seat_rows=seat_rows, store=store, subject="quarterly-report",
            agents=("m1", "m2", "m3"),
        )
        stranger = next(row for row in board if row["agent"] == "m3")
        assert stranger["score"] == 0.5 and stranger["evidence"] == 0
    finally:
        store.close()
