"""Phase 6 of the context-harness plan: multi-model strategies and
continuous evaluation.

The publish reviewer (its own seat, its own purpose, possibly a
different provider) judges the verified function before it lists —
availability advisory, verdict decisive; the build ledger's per-model
outcome history answers "who is earning the seat"; and the audition
scoreboard turns benchmark runs into a durable quality trend.
"""

from __future__ import annotations

import sys
from pathlib import Path

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import GOOD

from oolu.buildledger import BuildLedger
from oolu.durable.connection import DurableConnection
from oolu.providers.profiles import SEAT_PROFILES
from oolu.reviewer import review_node_function
from oolu.seats import SEATS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))


# --------------------------------------------------------------------------- #
# The reviewer's verdict protocol                                              #
# --------------------------------------------------------------------------- #
class _Says:
    def __init__(self, answer):
        self._answer = answer
        self.asked: list[list[dict]] = []

    def reply(self, messages):
        self.asked.append(messages)
        return self._answer


def test_a_pass_verdict_approves():
    approved, concern = review_node_function(
        _Says("Looks right.\nVERDICT: pass"), "goal", "script", {}
    )
    assert approved and concern == ""


def test_a_block_verdict_carries_its_reason():
    approved, concern = review_node_function(
        _Says("VERDICT: block — emits a hardcoded total instead of summing"),
        "goal",
        "script",
        {},
    )
    assert not approved
    assert "hardcoded total" in concern


def test_an_unreachable_or_mumbling_reviewer_never_blocks():
    class _Dead:
        def reply(self, messages):
            raise RuntimeError("no key")

    assert review_node_function(_Dead(), "g", "s", {}) == (True, "")
    # No verdict line at all → availability failure, not a block.
    assert review_node_function(_Says("interesting function"), "g", "s", {})[0]


def test_structured_delivery_is_preferred_when_the_model_speaks_it():
    class _Structured:
        def structured(self, messages, *, schema):
            assert schema["properties"]["verdict"]["enum"] == ["pass", "block"]
            return {"verdict": "block", "concern": "mints a synonym slot"}

        def reply(self, messages):  # pragma: no cover - must not be used
            raise AssertionError("structured was available")

    approved, concern = review_node_function(_Structured(), "g", "s", {})
    assert not approved and "synonym" in concern


def test_the_review_seat_is_declared_with_profile_and_registry():
    seat = SEATS["node.review"]
    assert seat.writes == () and seat.hands == ()  # judgement, never edits
    profile = SEAT_PROFILES["node.review"]
    assert profile.max_tokens == 2048


# --------------------------------------------------------------------------- #
# Draft → review at the build door                                             #
# --------------------------------------------------------------------------- #
def test_a_seated_reviewers_block_is_final_and_becomes_a_lesson(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        app._node_reviewer = lambda tenant: _Says(
            "VERDICT: block — declares result but computes nothing real"
        )

        reply = _chat(app, ident, f"build me a node that {GOAL}")

        assert "reviewer blocked" in reply.body["reply"]
        ledger = app._build_ledger()
        goal_key = app._function_skill_id("t1", GOAL)
        lessons = ledger.lessons_for("t1", goal_key)
        assert lessons and "reviewer blocked" in lessons[0]
        (attempt,) = ledger.attempts("t1", goal_key)
        assert attempt["status"] == "refused"
        assert any(
            state.startswith("review-blocked:") for state in attempt["states"]
        )
    finally:
        conn.close()


def test_a_reviewed_publish_records_the_review_state(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        app._node_reviewer = lambda tenant: _Says("VERDICT: pass")

        reply = _chat(app, ident, f"build me a node that {GOAL}")

        assert "reviewer blocked" not in reply.body["reply"]
        seat = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "model.seat"
            and r.payload.get("purpose") == "node.build"
        ]
        transaction = seat[-1].payload["transaction"]
        assert "reviewed" in transaction
        assert transaction[-1] == "published"
    finally:
        conn.close()


def test_no_reviewer_seated_publishes_exactly_as_before(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        # The rig has no model keys, so _node_reviewer resolves to None.
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert "reviewer blocked" not in reply.body["reply"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Performance-fed routing reads the ledger                                     #
# --------------------------------------------------------------------------- #
def test_the_seat_performance_board_ranks_models_by_outcomes(tmp_path):
    conn = DurableConnection(tmp_path / "ledger.db")
    try:
        ledger = BuildLedger(conn)
        for _ in range(3):
            ledger.record(
                "t1", "k1", "g", status="published", model="claude-sonnet-5"
            )
        ledger.record(
            "t1", "k1", "g", status="refused", problem="x", model="tiny-local"
        )
        ledger.record(
            "t1", "k2", "g2", status="refused", problem="y", model="tiny-local"
        )
        board = ledger.seat_performance("t1")
        assert board[0]["model"] == "claude-sonnet-5"
        assert board[0]["success_rate"] == 1.0
        assert board[-1]["model"] == "tiny-local"
        assert board[-1]["refused"] == 2
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The continuous-audition scoreboard                                           #
# --------------------------------------------------------------------------- #
def test_the_audition_scoreboard_accumulates_trend_rows(tmp_path):
    from node_authoring import (
        audition_history,
        record_report,
        run_bench,
        scripted_author,
    )

    report = run_bench(scripted_author(), name="incumbent")
    path = tmp_path / "auditions.jsonl"
    record_report(report, path, model="scripted", ceiling=16384)
    record_report(report, path, model="scripted", ceiling=1024)

    rows = audition_history(path)
    assert len(rows) == 2
    assert rows[0]["model"] == "scripted"
    assert rows[0]["fit"] is True
    assert rows[0]["cost_per_verified"] == 0.0  # scripted spends nothing
    assert rows[1]["ceiling"] == 1024
    assert audition_history(tmp_path / "absent.jsonl") == []
