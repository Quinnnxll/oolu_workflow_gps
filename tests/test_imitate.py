"""Imitate: a guided lesson in the node's window builds a capable node.

Exit gate (Issue 14): the platform owns no global mouse/keyboard capture
and no screen recording — so the lesson is taught through what it DOES
own. The user names the goal and describes each step in order; runs the
window logged while recording pair automatically from the audit-backed
activity feed; stop-and-build compiles the demonstration into ONE node
through the same gated build path as every other door, with the numbered
steps as the plan the model must follow; and the lesson persists verbatim
— rows in the store and a JSON data log in the built node's drawer — the
training record node creation was asked to become.
"""

from __future__ import annotations

import json

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.gateway import GatewayApp
from oolu.lessons import LessonStore
from oolu.nodeplace import (
    NodeAccountStore,
    NodeplaceService,
    RegistryStore,
    WorkDesk,
)
from oolu.nodeplace.desk import RunSteps
from oolu.nodeplace.models import Node, Visibility

GOAL = "normalize supplier invoices into one ledger csv"

FUNCTION_ANSWER = (
    "1. Read the demonstrated steps.\n"
    "```python\nfrom _oolu_runtime import emit_result\nemit_result(''.join(['o', 'k']))\n```"
)


class FakeAuthor:
    """The function-writing model: scripted answer, prompts recorded."""

    def __init__(self, answer=FUNCTION_ANSWER):
        self._answer = answer
        self.calls: list[list[dict]] = []

    def reply(self, messages):
        self.calls.append(messages)
        return self._answer


# --------------------------------------------------------------------------- #
# The store: one recording at a time, ordered steps, closed exactly once.      #
# --------------------------------------------------------------------------- #
def test_a_lesson_records_ordered_steps_and_closes_once(tmp_path):
    conn = DurableConnection(tmp_path / "l.db")
    try:
        store = LessonStore(conn)
        lesson = store.start(tenant="t", node_id="n1", owner="alice", goal=GOAL)
        assert lesson.status == "recording"
        # One recording per window: a second start is refused in words.
        with pytest.raises(ValueError, match="already recording"):
            store.start(tenant="t", node_id="n1", owner="alice", goal="other")
        # Steps keep the demonstrated ORDER — the order is the lesson.
        store.add_step(
            lesson.lesson_id, tenant="t", owner="alice", kind="say",
            text="open the supplier folder",
        )
        store.add_step(
            lesson.lesson_id, tenant="t", owner="alice", kind="say",
            text="  merge   the rows ",
        )
        active = store.active(tenant="t", node_id="n1", owner="alice")
        assert [s.text for s in active.steps] == [
            "open the supplier folder",
            "merge the rows",  # whitespace tidied, order kept
        ]
        # A step is validated: kind and words.
        with pytest.raises(ValueError, match="'say', 'run', or 'file'"):
            store.add_step(
                lesson.lesson_id, tenant="t", owner="alice",
                kind="click", text="x",
            )
        with pytest.raises(ValueError, match="needs words"):
            store.add_step(
                lesson.lesson_id, tenant="t", owner="alice",
                kind="say", text="  ",
            )
        # Closing is exactly-once; the record survives as data.
        closed = store.finish(
            lesson.lesson_id, tenant="t", owner="alice",
            status="built", built_node_id="node-9",
        )
        assert closed.status == "built" and closed.built_node_id == "node-9"
        assert store.finish(
            lesson.lesson_id, tenant="t", owner="alice", status="discarded"
        ) is None
        assert store.active(tenant="t", node_id="n1", owner="alice") is None
        # Erasure takes the account's lessons and their steps.
        assert store.erase(tenant="t", owner="alice") == 1
        assert store.get(lesson.lesson_id, tenant="t", owner="alice") is None
    finally:
        conn.close()


def test_a_lesson_never_records_a_strangers_window(tmp_path):
    conn = DurableConnection(tmp_path / "l.db")
    try:
        store = LessonStore(conn)
        lesson = store.start(tenant="t", node_id="n1", owner="alice", goal=GOAL)
        # Bob can neither see nor extend alice's lesson.
        assert store.get(lesson.lesson_id, tenant="t", owner="bob") is None
        with pytest.raises(ValueError, match="no lesson is recording"):
            store.add_step(
                lesson.lesson_id, tenant="t", owner="bob",
                kind="say", text="sabotage",
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the Imitate flow end to end.                                    #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
        files=UserFileStore(conn),
        lessons=LessonStore(conn),
    )
    author = FakeAuthor()
    gateway._node_function_author = lambda tenant: author
    # The classroom: a node on user-1's desk whose window teaches.
    node = Node(
        noder_principal="user-1",
        tenant_id="t1",
        skill_id="classroom.node",
        visibility=Visibility.PUBLIC,
    )
    registry.add_node(node)
    desk.create_account(node.node_id, principal="user-1", tenant="t1")
    return gateway, conn, ident, desk, author, node.node_id


def _post(gateway, ident, path, body=None, *, principal="user-1"):
    return gateway.handle(
        _req("POST", path, token=ident.token(principal, "t1"), body=body or {})
    )


def test_the_imitate_flow_teaches_and_builds(tmp_path):
    gateway, conn, ident, desk, author, node_id = _host(tmp_path)
    try:
        base = f"/v1/work/nodes/{node_id}/imitate"
        # Nothing is recording until the button says so.
        empty = gateway.handle(
            _req("GET", base, token=ident.token("user-1", "t1"))
        )
        assert empty.status == 200 and empty.body["lesson"] is None

        started = _post(gateway, ident, base, {"goal": GOAL})
        assert started.status == 201
        assert started.body["lesson"]["status"] == "recording"

        # Building with zero demonstrated steps is refused — a lesson
        # teaches by showing, not by title alone.
        early = _post(gateway, ident, f"{base}/stop", {"build": True})
        assert early.status == 400
        assert "at least one demonstrated step" in early.body["error"]["message"]

        for text in ("download the supplier csvs", "merge rows into one ledger"):
            stepped = _post(gateway, ident, f"{base}/step", {"text": text})
            assert stepped.status == 200
        assert len(stepped.body["lesson"]["steps"]) == 2

        # Runs the window logged while recording pair automatically: one
        # after the lesson opened counts; one from before does not.
        desk.activity = lambda node_id, tenant: [
            RunSteps(
                run_id="fresh123",
                steps=[
                    {
                        "seq": 1,
                        "event_type": "workflow.completed",
                        "at": "9999-01-01T00:00:00+00:00",
                    }
                ],
            ),
            RunSteps(
                run_id="ancient9",
                steps=[
                    {
                        "seq": 1,
                        "event_type": "workflow.completed",
                        "at": "1970-01-01T00:00:00+00:00",
                    }
                ],
            ),
        ]
        built = _post(gateway, ident, f"{base}/stop", {"build": True})
        assert built.status == 200, built.body
        assert "Built a NEW node" in built.body["say"]
        lesson = built.body["lesson"]
        assert lesson["status"] == "built" and lesson["built_node_id"]

        # The model was handed the demonstration AS the plan — the
        # user's numbered steps plus the paired run, imitated exactly.
        [call] = author.calls
        prompt = call[1]["content"]
        assert "DEMONSTRATED" in prompt
        assert "1. download the supplier csvs" in prompt
        assert "2. merge rows into one ledger" in prompt
        assert "(observed: run fresh123" in prompt
        assert "ancient9" not in prompt  # ran before the lesson — not it

        # The paired run persisted ON the lesson too: the stored record
        # is the full demonstration, words and logs together.
        kinds = [s["kind"] for s in lesson["steps"]]
        assert kinds == ["say", "say", "run"]

        # The lesson rode into the BUILT node's drawer as a data log —
        # node creation requirements as a training record.
        files = gateway._files.list(
            tenant="t1", node_id=lesson["built_node_id"]
        )
        [log] = [f for f in files if f.folder == "lessons"]
        record = json.loads(log.content)
        assert record["goal"] == GOAL
        assert record["taught_in_node"] == node_id
        assert [s["text"] for s in record["steps"]][:2] == [
            "download the supplier csvs",
            "merge rows into one ledger",
        ]
    finally:
        conn.close()


def test_a_refused_build_keeps_the_lesson_recording(tmp_path):
    gateway, conn, ident, desk, author, node_id = _host(tmp_path)
    try:
        base = f"/v1/work/nodes/{node_id}/imitate"
        gateway._node_function_author = lambda tenant: FakeAuthor("NO_TASK")
        _post(gateway, ident, base, {"goal": GOAL})
        _post(gateway, ident, f"{base}/step", {"text": "one step"})
        refused = _post(gateway, ident, f"{base}/stop", {"build": True})
        assert refused.status == 200
        assert refused.body["say"].startswith("error:")
        # Nothing recorded is lost to a refusal: still recording.
        assert refused.body["lesson"]["status"] == "recording"
    finally:
        conn.close()


def test_discard_closes_but_keeps_the_record(tmp_path):
    gateway, conn, ident, desk, author, node_id = _host(tmp_path)
    try:
        base = f"/v1/work/nodes/{node_id}/imitate"
        started = _post(gateway, ident, base, {"goal": GOAL})
        lesson_id = started.body["lesson"]["lesson_id"]
        dropped = _post(gateway, ident, f"{base}/stop", {"build": False})
        assert dropped.status == 200
        assert dropped.body["lesson"]["status"] == "discarded"
        # Gone from the window, kept as data.
        assert gateway.handle(
            _req("GET", base, token=ident.token("user-1", "t1"))
        ).body["lesson"] is None
        kept = gateway._lessons.get(lesson_id, tenant="t1", owner="user-1")
        assert kept is not None and kept.status == "discarded"
        # No author call ever happened.
        assert author.calls == []
    finally:
        conn.close()


def test_imitate_demands_the_callers_own_desk(tmp_path):
    gateway, conn, ident, desk, author, node_id = _host(tmp_path)
    try:
        stranger = _post(
            gateway,
            ident,
            f"/v1/work/nodes/{node_id}/imitate",
            {"goal": GOAL},
            principal="user-2",
        )
        assert stranger.status == 404
    finally:
        conn.close()
