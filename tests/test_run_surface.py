"""The run surface tells the truth (V0): durable markers, executing snapshots.

Exit gate (node-vitality-plan, phase V0): a chat turn's ask and a
durable ``working`` marker land in server history BEFORE the work, and
the reply RESOLVES the marker — so any device, reload, or return
mid-turn shows the ask and an honest working state, node threads
included (they persist under ``node:<id>`` now, losing nothing on
unmount). A marker outliving its process is swept into an honest
interruption note, never left promising work nobody is doing. And the
durable service stages an EXECUTING snapshot before and during the
drive, so a status poll answers honestly WHILE a run executes — and a
resumed run's decision lands durably before the drive, ending the
stale "needs a decision" window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import _app, _req

from oolu.durable import DurableConnection, DurableWorkflowService
from oolu.gateway import GatewayApp
from oolu.gateway.app import _WORKING_INTERRUPTED_NOTE
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.orchestrator import (
    PauseKind,
    Phase,
    ResumeInput,
    TaskContract,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The marker book: append, resolve, sweep.                                     #
# --------------------------------------------------------------------------- #
def test_the_working_marker_lives_resolves_and_never_lies(tmp_path):
    conn = DurableConnection(tmp_path / "turns.db")
    store = AssistantHistoryStore(conn, clock=lambda: NOW)
    store.append(tenant="t1", principal="alice", kind="user", body="do it")
    seq = store.append(
        tenant="t1", principal="alice", kind="working", body=""
    )
    turns = store.history(tenant="t1", principal="alice")
    assert [t["kind"] for t in turns] == ["user", "working"]
    # The reply resolves the marker — consumed exactly once.
    assert store.resolve_working(tenant="t1", principal="alice", seq=seq)
    assert not store.resolve_working(tenant="t1", principal="alice", seq=seq)
    store.append(tenant="t1", principal="alice", kind="assistant", body="done")
    assert [t["kind"] for t in store.history(tenant="t1", principal="alice")] == [
        "user",
        "assistant",
    ]
    conn.close()


def test_the_sweep_converts_dead_markers_and_spares_live_ones(tmp_path):
    conn = DurableConnection(tmp_path / "turns.db")
    old = AssistantHistoryStore(conn, clock=lambda: NOW - timedelta(hours=1))
    old.append(tenant="t1", principal="alice", kind="user", body="stuck ask")
    old.append(tenant="t1", principal="alice", kind="working", body="")
    fresh = AssistantHistoryStore(conn, clock=lambda: NOW)
    fresh.append(tenant="t1", principal="bob", kind="working", body="")
    # Only the marker past the bound is swept — into honest words, in
    # place, so the thread explains itself instead of promising work.
    swept = fresh.sweep_working(
        older_than=NOW - timedelta(minutes=15), note="— interrupted."
    )
    assert swept == 1
    alice = fresh.history(tenant="t1", principal="alice")
    assert alice[-1]["kind"] == "assistant"
    assert alice[-1]["body"] == "— interrupted."
    assert [t["kind"] for t in fresh.history(tenant="t1", principal="bob")] == [
        "working"
    ]
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the turn is durable from its first breath.                      #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    accounts.create_user("alice", "alice-password-1", tenant="t1")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        assistant_history=AssistantHistoryStore(conn),
    )
    return gateway, conn, ident


def test_a_finished_turn_leaves_the_reply_where_the_marker_stood(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    reply = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=ident.token("alice"),
            body={"message": "hello", "history": []},
        )
    )
    assert reply.status == 200
    turns = gateway._assistant_history.history(
        tenant="t1", principal="alice"
    )
    # The ask and the reply, in order — and NO marker left behind.
    assert [t["kind"] for t in turns] == ["user", "assistant"]
    assert turns[0]["body"] == "hello"
    conn.close()


def test_a_node_thread_persists_server_side_under_its_own_agent(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")
    reply = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "hello", "history": [], "node_id": "n-77"},
        )
    )
    assert reply.status == 200
    # The node's thread is a real thread now: the history door answers
    # for it, and leaving the window mid-turn loses nothing.
    door = gateway.handle(
        _req("GET", "/v1/chat/history", token=alice, query={"agent": "node:n-77"})
    )
    assert door.status == 200
    assert [t["kind"] for t in door.body["items"]] == ["user", "assistant"]
    # The main thread stayed untouched — threads never bleed.
    assert (
        gateway._assistant_history.history(tenant="t1", principal="alice")
        == []
    )
    conn.close()


def test_a_raised_turn_resolves_its_marker_instead_of_promising_work(
    tmp_path, monkeypatch
):
    gateway, conn, ident = _host(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("the turn died mid-work")

    monkeypatch.setattr(gateway, "_chat_turn_work", boom)
    try:
        gateway.handle(
            _req(
                "POST",
                "/v1/chat",
                token=ident.token("alice"),
                body={"message": "do a thing", "history": []},
            )
        )
    except RuntimeError:
        pass
    turns = gateway._assistant_history.history(
        tenant="t1", principal="alice"
    )
    # The ask stands (it happened); the marker does NOT (nothing is
    # working it) — the error went to the client in its own words.
    assert [t["kind"] for t in turns] == ["user"]
    conn.close()


def test_a_fresh_process_sweeps_markers_no_process_is_working(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    gateway._assistant_history.append(
        tenant="t1", principal="alice", kind="user", body="long ask"
    )
    gateway._assistant_history.append(
        tenant="t1", principal="alice", kind="working", body=""
    )
    # A second process over the same durable store: its boot resolves
    # the dead marker into the honest interruption note.
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    reborn = GatewayApp(
        gateway._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=gateway._settings,
        assistant_history=gateway._assistant_history,
    )
    turns = reborn._assistant_history.history(
        tenant="t1", principal="alice"
    )
    assert [t["kind"] for t in turns] == ["user", "assistant"]
    assert turns[-1]["body"] == _WORKING_INTERRUPTED_NOTE
    conn.close()


# --------------------------------------------------------------------------- #
# The executing snapshot: a poll answers honestly DURING the drive.            #
# --------------------------------------------------------------------------- #
def _service(tmp_path, *, mode):
    from test_durable_runtime import (
        ScriptedActionExecutor,
        _blueprint,
        _factory,
        _param,
    )

    from oolu.skills.requirements import (
        AuthorizationGrant,
        RequirementBrief,
    )

    brief = RequirementBrief(
        intent="summarize",
        parameters=[_param("format", value="md")],
        authorization=AuthorizationGrant(mode=mode),
    )
    factory = _factory(
        brief=brief,
        blueprint=_blueprint(
            operation="render", capability="render", reserved=False,
            risk="write",
        ),
        executor=ScriptedActionExecutor({"render"}),
        grounding_map={"format": "render"},
    )
    conn = DurableConnection(tmp_path / "runs.db")
    return conn, DurableWorkflowService(conn, factory)


def test_the_run_row_exists_and_moves_while_the_drive_runs(tmp_path):
    from oolu.skills.requirements import AuthorizationMode

    conn, service = _service(
        tmp_path, mode=AuthorizationMode.FULLY_DELEGATED
    )
    observed: list[str] = []

    def on_progress(state):
        # A CONCURRENT poll's view, mid-drive: the row exists and names
        # the phase the run actually stands in — never "no such run".
        stored = service.get(state.run_id)
        assert stored is not None
        observed.append(stored.phase.value)

    state = service.submit(
        TaskContract(intent="summarize"), on_progress=on_progress
    )
    assert state.phase is Phase.COMPLETED
    # The drive narrated real, pre-terminal phases as it went.
    assert len(observed) >= 3
    assert observed[-1] == Phase.COMPLETED.value
    assert any(p != Phase.COMPLETED.value for p in observed)
    conn.close()


def test_a_resumed_decision_lands_durably_before_the_drive(tmp_path):
    from oolu.skills.requirements import AuthorizationMode

    conn, service = _service(tmp_path, mode=AuthorizationMode.GUIDED)
    state = service.submit(TaskContract(intent="summarize"))
    assert state.pause is not None
    assert state.pause.kind is PauseKind.CONFIRMATION
    run_id = state.run_id

    seen_pause: list[bool] = []

    def on_progress(_state):
        stored = service.get(run_id)
        seen_pause.append(stored.pause is not None)

    done = service.resume(
        run_id,
        ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True),
        on_progress=on_progress,
    )
    assert done.phase is Phase.COMPLETED
    # From the FIRST mid-drive look onward the stored row shows the
    # decision consumed — the stale "needs a decision" window is gone.
    assert seen_pause and not any(seen_pause)
    conn.close()
