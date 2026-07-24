"""M3: negative knowledge — scoped, graduated, never a universal ban."""

from __future__ import annotations

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import CRASHING, _FailingScriptHand

from oolu.durable.connection import DurableConnection
from oolu.memoryspine import MemorySpine
from oolu.negative import negative_check, record_failure


def test_one_failure_warns_but_never_blocks(tmp_path):
    conn = DurableConnection(tmp_path / "n.db")
    try:
        spine = MemorySpine(conn)
        record_failure(
            spine, tenant="t1", subject="g1",
            problem="NameError: boom", applicability={"model": "m1"},
        )
        verdict = negative_check(
            spine, tenant="t1", subject="g1", context={"model": "m1"}
        )
        assert not verdict["blocked"]
    finally:
        conn.close()


def test_a_reproduced_failure_blocks_identity_allows_difference(tmp_path):
    conn = DurableConnection(tmp_path / "n.db")
    try:
        spine = MemorySpine(conn)
        for _ in range(2):
            record_failure(
                spine, tenant="t1", subject="g1",
                problem="NameError: boom", applicability={"model": "m1"},
                reopen_conditions=("sandbox upgraded",),
            )
        same = negative_check(
            spine, tenant="t1", subject="g1", context={"model": "m1"}
        )
        assert same["blocked"] and "2 times" in same["reason"]
        other = negative_check(
            spine, tenant="t1", subject="g1", context={"model": "m2"}
        )
        assert not other["blocked"] and "model changed" in other["difference"]
        reopened = negative_check(
            spine, tenant="t1", subject="g1",
            context={"model": "m1", "note": "the sandbox upgraded today"},
        )
        assert not reopened["blocked"] and "reopen" in reopened["difference"]
        # Mechanism-scoped twin exists for cross-goal retrieval.
        assert spine.recall(("t1", "mechanism:nameerror"), kinds=("failure",))
    finally:
        conn.close()


def test_the_gate_blocks_the_third_identical_attempt(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _FailingScriptHand("undefined_name")}
        author = FakeAuthor(CRASHING)
        app._node_function_author = lambda tenant: author
        for _ in range(2):
            reply = _chat(app, ident, f"build me a node that {GOAL}")
            assert "failed birth verification" in reply.body["reply"]
        calls_before = len(author.calls)

        third = _chat(app, ident, f"build me a node that {GOAL}")
        assert "already failed the same way" in third.body["reply"]
        assert len(author.calls) == calls_before  # no authoring spend

        # A materially different seat is allowed to retest.
        app._author_model_id = lambda a: "another-model"
        fourth = _chat(app, ident, f"build me a node that {GOAL}")
        assert "already failed the same way" not in fourth.body["reply"]
        assert len(author.calls) > calls_before
    finally:
        conn.close()


def test_a_publish_resolves_the_goals_failure_records(tmp_path):
    from oolu.buildledger import BuildLedger

    conn = DurableConnection(tmp_path / "n.db")
    try:
        spine = MemorySpine(conn)
        record_failure(spine, tenant="t1", subject="k1", problem="boom: x")
        record_failure(spine, tenant="t1", subject="k1", problem="boom: x")
        BuildLedger(conn, spine=spine).record(
            "t1", "k1", "the goal", status="published", node_id="n-1"
        )
        verdict = negative_check(spine, tenant="t1", subject="k1", context={})
        assert not verdict["blocked"]
        assert spine.recall(("t1", "k1"), kinds=("failure",)) == []
    finally:
        conn.close()
