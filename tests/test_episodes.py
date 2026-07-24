"""M2 of the memory-stack plan: episodic memory, summaries, overflow truth.

Episodes ride the M0 spine (no new store); summaries are extractive,
cite their episodes, supersede only each other, and NEVER serve stale
(read-side invalidation); the build door writes episodes for both
outcomes so an interrupted project restores from the stack; and the
chat window names its own truncation, carrying the earliest dropped
user asks verbatim.
"""

from __future__ import annotations

from test_chat_assistant import _FakeModel
from test_growth_trigger import GOAL, _chat, _req, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import CRASHING, _FailingScriptHand

from oolu.durable.connection import DurableConnection
from oolu.episodes import current_summary, record_episode, summarize
from oolu.memoryspine import MemorySpine


def _spine(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    return conn, MemorySpine(conn)


# --------------------------------------------------------------------------- #
# Summaries: derived, cited, superseding, never stale                          #
# --------------------------------------------------------------------------- #
def test_a_summary_cites_its_episodes_and_supersedes_its_prior(tmp_path):
    conn, spine = _spine(tmp_path)
    try:
        first = record_episode(
            spine, tenant="t1", subject="proj-a", kind="build",
            objective="normalize invoices", outcome="refused",
            unresolved=("KeyError: amount",),
        )
        s1 = summarize(spine, tenant="t1", subject="proj-a")
        assert f"memory:{first}" in spine.provenance(s1)
        record_episode(
            spine, tenant="t1", subject="proj-a", kind="build",
            objective="normalize invoices", outcome="published",
        )
        s2 = summarize(spine, tenant="t1", subject="proj-a")
        (only,) = spine.recall(("t1", "proj-a"), kinds=("summary",), limit=5)
        assert only["memory_id"] == s2  # the prior summary is superseded
    finally:
        conn.close()


def test_a_stale_summary_never_serves(tmp_path):
    conn, spine = _spine(tmp_path)
    try:
        record_episode(
            spine, tenant="t1", subject="p", kind="build",
            objective="the goal", outcome="refused",
            unresolved=("open question",),
        )
        summarize(spine, tenant="t1", subject="p")
        assert current_summary(spine, tenant="t1", subject="p") is not None
        # A newer episode invalidates the summary at READ time.
        record_episode(
            spine, tenant="t1", subject="p", kind="build",
            objective="the goal", outcome="published",
        )
        assert current_summary(spine, tenant="t1", subject="p") is None
        summarize(spine, tenant="t1", subject="p")
        assert current_summary(spine, tenant="t1", subject="p") is not None
    finally:
        conn.close()


def test_open_commitments_ride_the_summary_verbatim(tmp_path):
    conn, spine = _spine(tmp_path)
    try:
        record_episode(
            spine, tenant="t1", subject="p", kind="build",
            objective="parse the ledger", outcome="refused",
            unresolved=("ValueError: bad date format in column 3",),
        )
        summarize(spine, tenant="t1", subject="p")
        summary = current_summary(spine, tenant="t1", subject="p")
        assert "ValueError: bad date format in column 3" in summary["statement"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The build door writes episodes; interruption restores from the stack         #
# --------------------------------------------------------------------------- #
def test_an_interrupted_project_restores_from_the_stack(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _FailingScriptHand("undefined_name")}
        app._node_function_author = lambda tenant: FakeAuthor(CRASHING)
        _chat(app, ident, f"build me a node that {GOAL}")

        spine = app._memory_spine()
        goal_key = app._function_skill_id("t1", GOAL)
        summarize(spine, tenant="t1", subject=goal_key)
        restored = current_summary(spine, tenant="t1", subject=goal_key)
        # Weeks later, another process: objective, outcome, and the open
        # problem — from the stack, not a transcript.
        assert GOAL in restored["structured_value"]["objective"]
        assert restored["structured_value"]["latest_outcome"] == "refused"
        assert any(
            "undefined_name" in item or "NameError" in item
            for item in restored["structured_value"]["unresolved"]
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The window names its own truncation                                          #
# --------------------------------------------------------------------------- #
def _chat_history(app, ident, message, history):
    return app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=ident.token("user-1", "t1"),
            body={"message": message, "history": history},
        )
    )


def test_overflowed_history_carries_the_earliest_asks_verbatim(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        model = _FakeModel(['{"say": "noted", "task": null}'] * 2)
        app._tenant_model = lambda tenant: model

        long_history = [
            {"role": "user", "content": "always report totals in EUR please"}
        ] + [
            {"role": "assistant" if i % 2 else "user", "content": f"turn {i}"}
            for i in range(24)
        ]
        _chat_history(app, ident, "what did I ask at the start?", long_history)
        seen = str(model.calls[0])
        assert "beyond the visible window" in seen
        assert "always report totals in EUR" in seen

        # A short history stays exactly as before — no note, no noise.
        _chat_history(app, ident, "hello again", long_history[-5:])
        assert "beyond the visible window" not in str(model.calls[1])
    finally:
        conn.close()
