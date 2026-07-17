"""Model seats: the function lands in the drawer, whoever wrote it.

The bug this closes: node building "succeeded" — the model planned the
function, the node was created — but no source file ever appeared. The
function lived only inside the version's JSON snapshot; the drawer's
``src/`` folder was a run-time input nobody ever wrote. Exit gate:
building materializes ``src/main.py`` through the ``node.build`` seat
(scope-checked, attested, audited); the drawer copy is the function's
HOME (runs read it first, so editing the file edits the node — no stale
cache shadowing the edit); and the seat registry answers, in one place,
what every model call may touch, hold, produce, and answer to —
constant across whichever model sits down. docs/model-seats.md is the
architecture.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _req
from test_node_hands import WEB_GOAL, _grown_web_node

from oolu.durable import DurableConnection, UserFileStore
from oolu.seats import SEATS, DeskFiles, Seat, SeatViolation

# --------------------------------------------------------------------------- #
# The registry: one table, one vocabulary.                                     #
# --------------------------------------------------------------------------- #
ROUTER_PURPOSES = {
    "chat.turn",
    "plan.intake",
    "plan.route",
    "plan.synthesize",
    "plan.rebuild",
}


def test_every_seat_has_a_charge_and_purposes_are_the_meters_vocabulary():
    assert SEATS  # the table exists and is non-empty
    for purpose, seat in SEATS.items():
        assert seat.purpose == purpose  # keyed by its own name
        assert seat.charge.strip()  # a seat without a charge is a chair
    # The router's metering purposes all have seats — accounting and
    # governance agree on names.
    assert ROUTER_PURPOSES <= set(SEATS)


def test_the_build_seat_writes_code_and_only_code():
    seat = SEATS["node.build"]
    assert seat.writes == ("src/",)
    assert seat.consent_key == "account.autobuild_consent"
    assert seat.audited is True


# --------------------------------------------------------------------------- #
# DeskFiles: the seat's reach is the wall, whatever the model wants.           #
# --------------------------------------------------------------------------- #
def _desk(tmp_path, seat: Seat, **kwargs):
    conn = DurableConnection(tmp_path / "files.db")
    store = UserFileStore(conn)
    return (
        conn,
        store,
        DeskFiles(store, tenant="t1", node_id="n1", seat=seat, **kwargs),
    )


def test_desk_files_write_and_read_inside_the_seats_scope(tmp_path):
    conn, store, desk = _desk(
        tmp_path, SEATS["node.build"], consented=True
    )
    try:
        desk.write("src/main.py", "X = 1\n")
        desk.write("src/pkg/util.py", "Y = 2\n")
        assert desk.read("src/main.py") == "X = 1\n"
        assert desk.written == ["src/main.py", "src/pkg/util.py"]
        # A rewrite updates the same file, never a twin.
        desk.write("src/main.py", "X = 3\n")
        files = store.list(tenant="t1", node_id="n1")
        assert [f"{f.folder}/{f.name}" for f in files] == [
            "src/main.py",
            "src/pkg/util.py",
        ]
        assert files[0].content == "X = 3\n"
        assert files[0].media_type == "text/x-python"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "path", ["notes.md", "lessons/x.json", "../src/main.py", "/etc/pw", "srcx/a.py"]
)
def test_desk_files_refuse_out_of_scope_writes(tmp_path, path):
    conn, store, desk = _desk(tmp_path, SEATS["node.build"], consented=True)
    try:
        with pytest.raises(SeatViolation):
            desk.write(path, "nope")
        assert store.list(tenant="t1", node_id="n1") == []
    finally:
        conn.close()


def test_a_consent_gated_seat_refuses_without_the_attestation(tmp_path):
    conn = DurableConnection(tmp_path / "files.db")
    try:
        store = UserFileStore(conn)
        with pytest.raises(SeatViolation, match="consent"):
            DeskFiles(store, tenant="t1", node_id="n1", seat=SEATS["node.build"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Building materializes the function: src/main.py exists, audited.             #
# --------------------------------------------------------------------------- #
def test_building_a_node_writes_its_function_to_src_main(tmp_path):
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        assert "Built a NEW node" in agreed.body["reply"]
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        files = app._files.list(tenant="t1", node_id=node_id)
        [main] = [f for f in files if f.folder == "src" and f.name == "main.py"]
        # The drawer copy IS the authored function.
        assert "http_request" in main.content
        assert "emit_result" in main.content
        assert main.media_type == "text/x-python"
        # And the act is on the audit log, under the seat's purpose.
        seat_events = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "model.seat"
        ]
        [event] = seat_events
        assert event.payload["purpose"] == "node.build"
        assert event.payload["node_id"] == node_id
        assert event.payload["written"] == ["src/main.py"]
    finally:
        conn.close()


def test_editing_src_main_edits_the_node_with_no_stale_cache(tmp_path):
    """The drawer copy is the function's HOME: an edited main.py is what
    the next run executes — the old code's cache entry cannot shadow it,
    because the cache keys on the function's own fingerprint."""
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        files = app._files.list(tenant="t1", node_id=node_id)
        [main] = [f for f in files if f.folder == "src" and f.name == "main.py"]
        edited = (
            "from _oolu_runtime import emit_result\n"
            "emit_result('edited by hand')\n"
        )
        app._files.save(main.model_copy(update={"content": edited}))

        again = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=ident.token("user-1", "t1"),
                body={"message": WEB_GOAL, "history": []},
            )
        )
        assert again.status == 200, again.body
        action = script_exec.actions[-1]
        # The run executed the EDITED file — not the version snapshot.
        assert action.parameters["script"] == edited
        # And main.py is the script itself, never also a staged sibling.
        assert "main.py" not in (action.parameters.get("files") or {})
    finally:
        conn.close()


def test_a_deleted_drawer_copy_falls_back_to_the_version_snapshot(tmp_path):
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        [main] = [
            f
            for f in app._files.list(tenant="t1", node_id=node_id)
            if f.folder == "src" and f.name == "main.py"
        ]
        app._files.delete(main.file_id, tenant="t1")

        again = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=ident.token("user-1", "t1"),
                body={"message": WEB_GOAL, "history": []},
            )
        )
        assert again.status == 200, again.body
        action = script_exec.actions[-1]
        # The snapshot inside the version still answers.
        assert "http_request" in action.parameters["script"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The cache never shadows new code: the key carries the script fingerprint.    #
# --------------------------------------------------------------------------- #
def test_a_changed_provided_script_misses_the_old_cache():
    from oolu.cache import LocalScriptCache
    from oolu.runtime import NodeScriptRunner, StubBackend
    from oolu.runtime.backend import make_success
    from oolu.skills.models import ActionEvent

    backend = StubBackend(
        [
            make_success({"result": 1}),
            make_success({"result": 2}),
            # A cache hit still EXECUTES (replay is a real run) — it just
            # skips synthesis; the stub answers that run too.
            make_success({"result": 2}),
        ]
    )
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))

    def action(script):
        return ActionEvent(
            correlation_id="c1",
            adapter="script",
            operation="run",
            parameters={"goal": "g", "node_key": "node:x", "script": script},
        )

    first = runner.execute(action("A = 1"), idempotency_key="k1")
    assert first.evidence["cache"] == "provided"
    second = runner.execute(action("A = 2"), idempotency_key="k2")
    # The edit EXECUTED (a fresh verification), never a replay of A = 1.
    assert second.evidence["cache"] == "provided"
    assert backend.requests[-1].script == "A = 2"
    # Same code again: now the cache answers — one node, one entry per code.
    third = runner.execute(action("A = 2"), idempotency_key="k3")
    assert third.evidence["cache"] == "hit"
