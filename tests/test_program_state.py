"""F2 — program state + the program limits profile.

The program keeps its own books (state as DATA outside the cache key,
the records precedent applied to programs); the sandbox stays hostile
(the program profile widens the walls only to CLAMPED ceilings — a
requested 10-hour wall still runs under 180 seconds).
"""

from __future__ import annotations

import json

import pytest

from oolu.cache import LocalScriptCache
from oolu.runtime.backend import (
    PROGRAM_LIMITS,
    PROGRAM_LIMITS_MAX,
    ResourceLimits,
    StubBackend,
    clamp_limits,
    make_success,
)
from oolu.runtime.script_node import NodeScriptRunner
from oolu.skills.models import ActionEvent, ExecutionStatus
from oolu.skills.program import MAX_STATE_ROWS, merge_state_rows, parse_program_spec

SCRIPT = "from _oolu_runtime import emit_result\nemit_result(1)"


def _action(**params):
    params.setdefault("goal", "keep the ledger")
    params.setdefault("script", SCRIPT)
    params.setdefault("node_key", "node:prog")
    return ActionEvent(
        correlation_id="c1", adapter="script", operation="run", parameters=params
    )


def _runner(backend):
    return NodeScriptRunner(backend, LocalScriptCache(":memory:"))


# --------------------------------------------------------------------------- #
# State stages as DATA, outside the cache key.                                 #
# --------------------------------------------------------------------------- #
def test_state_stages_as_state_json():
    backend = StubBackend([make_success({"result": 1})])
    state_blob = json.dumps({"ledger": [{"at": "2026-01-01", "label": "a"}]})
    outcome = _runner(backend).execute(
        _action(_state=state_blob), idempotency_key="k1"
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED, outcome.error
    [request] = backend.requests
    assert request.files["state.json"] == state_blob


def test_state_change_replays_the_cached_script():
    # The replay-determinism pin: state is DATA, never part of the cache
    # key — run 2 with a DIFFERENT state must HIT the cache entry run 1
    # verified, replaying the same script against the current state.
    backend = StubBackend([make_success({"result": 1}), make_success({"result": 2})])
    runner = _runner(backend)
    first = runner.execute(
        _action(_state=json.dumps({"ledger": []})), idempotency_key="k1"
    )
    assert first.evidence["cache"] == "provided"  # verified and cached
    second = runner.execute(
        _action(_state=json.dumps({"ledger": [{"at": "x", "label": "y"}]})),
        idempotency_key="k2",
    )
    assert second.status is ExecutionStatus.SUCCEEDED
    assert second.evidence["cache"] == "hit"  # same key across state change
    # And the second run STAGED the new state — fresh data, cached code.
    assert json.loads(backend.requests[1].files["state.json"])["ledger"]


# --------------------------------------------------------------------------- #
# The limits profile — widened only to clamped ceilings.                       #
# --------------------------------------------------------------------------- #
def test_program_profile_widens_the_wall_to_180s():
    backend = StubBackend([make_success({"result": 1})])
    _runner(backend).execute(
        _action(_limits_profile="program"), idempotency_key="k1"
    )
    [request] = backend.requests
    assert request.limits.wall_timeout_s == 180.0
    assert request.limits.memory_mb == PROGRAM_LIMITS.memory_mb


def test_a_requested_ten_hour_wall_clamps_to_180s():
    # The plan's clamp test: requested 10 h wall => 180 s. The stamp is a
    # REQUEST; PROGRAM_LIMITS_MAX is the wall.
    backend = StubBackend([make_success({"result": 1})])
    _runner(backend).execute(
        _action(_limits_profile={"wall_timeout_s": 36000.0, "memory_mb": 999999}),
        idempotency_key="k1",
    )
    [request] = backend.requests
    assert request.limits.wall_timeout_s == 180.0
    assert request.limits.memory_mb == PROGRAM_LIMITS_MAX.memory_mb


def test_an_unstamped_action_keeps_the_step_limits():
    backend = StubBackend([make_success({"result": 1})])
    _runner(backend).execute(_action(), idempotency_key="k1")
    [request] = backend.requests
    assert request.limits.wall_timeout_s == 30.0  # the hostile step default


def test_clamp_never_widens_and_never_unlocks_the_rootfs():
    clamped = clamp_limits(
        ResourceLimits(
            cpus=64.0,
            memory_mb=1_000_000,
            pids_limit=100_000,
            wall_timeout_s=999_999.0,
            install_timeout_s=999_999.0,
            read_only_rootfs=False,  # asked to unlock — refused
            writable_scratch_mb=100_000,
        )
    )
    assert clamped.cpus == PROGRAM_LIMITS_MAX.cpus
    assert clamped.memory_mb == PROGRAM_LIMITS_MAX.memory_mb
    assert clamped.pids_limit == PROGRAM_LIMITS_MAX.pids_limit
    assert clamped.wall_timeout_s == PROGRAM_LIMITS_MAX.wall_timeout_s
    assert clamped.install_timeout_s == PROGRAM_LIMITS_MAX.install_timeout_s
    assert clamped.writable_scratch_mb == PROGRAM_LIMITS_MAX.writable_scratch_mb
    assert clamped.read_only_rootfs is True  # the posture never widens
    # A modest request stays as asked (clamp is a ceiling, not a floor).
    modest = clamp_limits(ResourceLimits(wall_timeout_s=60.0))
    assert modest.wall_timeout_s == 60.0


# --------------------------------------------------------------------------- #
# The rows discipline.                                                         #
# --------------------------------------------------------------------------- #
def test_merge_state_rows_applies_the_lifebooks_discipline():
    standing = [{"at": "2026-01-01", "label": "a", "value": 1, "note": ""}]
    emitted = [
        {"at": "2026-01-01", "label": "a", "value": 99},  # dup: standing wins
        {"at": "2026-01-02", "label": "b", "value": 2},
        "not-a-row",  # garbage is dropped, never crashes
    ]
    merged = merge_state_rows(standing, emitted)
    assert [r["label"] for r in merged] == ["a", "b"]
    assert merged[0]["value"] == 1  # the standing book won the dedup
    assert merged[1] == {"at": "2026-01-02", "label": "b", "value": 2, "note": ""}


def test_merge_state_rows_caps_at_the_book_size():
    emitted = [
        {"at": f"2026-01-{i:02d}T{j:02d}", "label": f"r{i}-{j}"}
        for i in range(1, 26)
        for j in range(24)
    ] * 5  # far past the cap
    merged = merge_state_rows([], emitted)
    assert len(merged) <= MAX_STATE_ROWS


# --------------------------------------------------------------------------- #
# Polyglot timeout alignment.                                                  #
# --------------------------------------------------------------------------- #
def test_polyglot_wrapper_timeout_sits_under_the_program_wall():
    from oolu.runtime.polyglot import POLYGLOT_STEP_TIMEOUT_S, polyglot_wrapper

    assert POLYGLOT_STEP_TIMEOUT_S < PROGRAM_LIMITS_MAX.wall_timeout_s
    wrapper = polyglot_wrapper("main.js")
    assert f"timeout={POLYGLOT_STEP_TIMEOUT_S!r}" in wrapper
    import ast

    ast.parse(wrapper)  # the generated wrapper is valid python


# --------------------------------------------------------------------------- #
# The walls stand.                                                             #
# --------------------------------------------------------------------------- #
def test_a_bundle_member_may_not_shadow_the_state_channel():
    from oolu.runtime.bundle import pack_tar
    from oolu.runtime.isolation import BackendError, _unpack_into

    for name in ("state.json", "records.json", "bindings.json"):
        archive = pack_tar({name: b"hostile"})
        with pytest.raises(BackendError, match="shadow"):
            _unpack_into(__import__("pathlib").Path("/tmp/never-used"), archive)


def test_a_port_named_state_still_refuses_at_parse():
    spec, problem = parse_program_spec(
        {
            "modules": [
                {"path": "lib/a.py"},
                {"path": "lib/b.py"},
            ],
            "operations": [{"name": "main", "entry": "lib.a:run"}],
            "interface": {
                "operation": "main",
                "ports": [{"name": "state", "type": "json"}],
            },
        }
    )
    assert spec is None and "reserved" in problem


# --------------------------------------------------------------------------- #
# The gateway circle: state lands, copies under runs/*, and stages next run.   #
# --------------------------------------------------------------------------- #
STATE_ENTRY = (
    "import json, os\n"
    "from _oolu_runtime import emit_result\n"
    "from lib.ledger import next_row\n"
    "standing = {}\n"
    "if os.path.exists('state.json'):\n"
    "    with open('state.json', encoding='utf-8') as fh:\n"
    "        standing = json.load(fh)\n"
    "rows = list(standing.get('ledger') or [])\n"
    "row = next_row(len(rows))\n"
    "emit_result({'summary': f'{len(rows) + 1} rows', "
    "'state': {'ledger': rows + [row], 'cursor': len(rows) + 1}})\n"
)
STATE_LIB = {
    "lib/__init__.py": "",
    "lib/ledger.py": (
        "def next_row(count):\n"
        "    return {'at': f'2026-01-{count + 1:02d}', "
        "'label': f'entry-{count + 1}', 'value': count + 1}\n"
    ),
}


def _completed_run(node_id, run_id, payload):
    """A COMPLETED node-function RunState, shaped as the completion hooks
    read it — the minimal honest stand-in."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from oolu.orchestrator.state import Phase
    from oolu.skills.models import ExecutionOutcome
    from oolu.skills.models import ExecutionStatus as XS

    now = datetime.now(UTC)
    outcome = ExecutionOutcome(
        idempotency_key=run_id,
        skill_id="s",
        status=XS.SUCCEEDED,
        evidence={"result": payload},
        started_at=now,
        completed_at=now,
    )
    return SimpleNamespace(
        run_id=run_id,
        phase=Phase.COMPLETED,
        execution=SimpleNamespace(action_outcomes=[outcome]),
        contract=SimpleNamespace(
            metadata={
                "node_function": {"node_id": node_id},
                "tenant_id": "t1",
            }
        ),
        updated_at=now,
    )


def _publish_state_program(tmp_path):
    from test_program_substrate import _program_app, _session

    from oolu.skills.program import ProgramSpec, StateSpec
    from oolu.skills.program import ModuleSpec as MS
    from oolu.skills.program import UnifiedInterface as UI

    spec = ProgramSpec(
        modules=[MS(path="lib/ledger.py"), MS(path="lib/__init__.py")],
        interface=UI(operation="main", ports=[{"name": "summary"}]),
        state=[
            StateSpec(name="ledger", kind="rows"),
            StateSpec(name="cursor", kind="value"),
        ],
    )
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)
    result = app.publish_program_node(
        session,
        goal="keep the ledger",
        script=STATE_ENTRY,
        files=dict(STATE_LIB),
        program=spec,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result.get("problem")
    return app, conn, session, result["node_id"]


def _drawer_file(app, node_id, folder, name):
    return next(
        (
            f
            for f in app._files.list(tenant="t1", node_id=node_id)
            if f.folder == folder and f.name == name
        ),
        None,
    )


def test_state_lands_copies_history_and_stages_the_next_run(tmp_path):
    app, conn, session, node_id = _publish_state_program(tmp_path)
    try:
        # Run 1 completes: a row and a cursor, plus an UNDECLARED key.
        row1 = {"at": "2026-01-01", "label": "entry-1", "value": 1}
        app._land_state(
            _completed_run(
                node_id,
                "run-1",
                {
                    "summary": "1 rows",
                    "state": {
                        "ledger": [row1],
                        "cursor": 1,
                        "undeclared": "dropped",
                    },
                },
            )
        )
        landed = _drawer_file(app, node_id, "state", "state.json")
        assert landed is not None
        standing = json.loads(landed.content)
        assert standing["cursor"] == 1  # value-kind replaced whole
        assert [r["label"] for r in standing["ledger"]] == ["entry-1"]
        assert "undeclared" not in standing  # only DECLARED names land
        # History: run 1's STAGED state (before the update) was empty.
        history1 = _drawer_file(app, node_id, "runs/run-1/state", "state.json")
        assert history1 is not None and json.loads(history1.content) == {}

        # Run 2 READS run 1's state: the extras stage the landed file.
        extras = app._node_function_extras(session, node_id)
        assert json.loads(extras["_state"]) == standing

        # Run 2 completes: the standing row re-emitted (dedup) + a new one.
        row2 = {"at": "2026-01-02", "label": "entry-2", "value": 2}
        app._land_state(
            _completed_run(
                node_id,
                "run-2",
                {
                    "summary": "2 rows",
                    "state": {
                        "ledger": [
                            {**row1, "value": 99},  # dup (at,label): standing wins
                            row2,
                        ],
                        "cursor": 2,
                    },
                },
            )
        )
        merged = json.loads(
            _drawer_file(app, node_id, "state", "state.json").content
        )
        assert [r["label"] for r in merged["ledger"]] == ["entry-1", "entry-2"]
        assert merged["ledger"][0]["value"] == 1  # the standing book won
        assert merged["cursor"] == 2
        # Run 2's history holds what run 2 READ — run 1's landed state.
        history2 = _drawer_file(app, node_id, "runs/run-2/state", "state.json")
        assert json.loads(history2.content) == standing
        # Idempotent per run: re-landing run 2 changes nothing.
        before = _drawer_file(app, node_id, "state", "state.json").content
        app._land_state(
            _completed_run(
                node_id, "run-2", {"summary": "2 rows", "state": {"cursor": 2}}
            )
        )
        assert (
            _drawer_file(app, node_id, "state", "state.json").content == before
        )

        # The state file never joins the frozen tree — the bundle id (and
        # so the cache key) is identical across state changes.
        assert "state.json" not in app._node_src_bundle_tree("t1", node_id)
    finally:
        conn.close()


def test_program_profile_is_stamped_frozen_and_surfaced(tmp_path):
    from test_program_substrate import _program_app, _session

    from oolu.skills.pack import ReusableSkill
    from oolu.skills.program import ModuleSpec as MS
    from oolu.skills.program import ProgramSpec
    from oolu.skills.program import UnifiedInterface as UI

    spec = ProgramSpec(
        modules=[MS(path="lib/ledger.py"), MS(path="lib/__init__.py")],
        interface=UI(operation="main", ports=[{"name": "summary"}]),
        limits_profile="program",
    )
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        result = app.publish_program_node(
            _session(app, ident),
            goal="keep the ledger",
            script=STATE_ENTRY.replace("state.json", "unused.json"),
            files=dict(STATE_LIB),
            program=spec,
            io={"outputs": [{"name": "summary", "type": "str"}]},
        )
        assert result["ok"], result.get("problem")
        # The consent is SURFACED in the build notes, in numbers.
        assert any("program limits profile" in n for n in result["notes"])
        # The stamp rides the FROZEN action — a drawer edit cannot mint it.
        version = app._nodeplace.latest_version(result["node_id"])
        skill = ReusableSkill.model_validate_json(version.sanitized_skill_json)
        action = next(a for a in skill.actions if a.adapter == "script")
        assert action.parameters["_limits_profile"] == "program"

        from oolu.gateway.app import _limits_profile_stamp

        assert _limits_profile_stamp(action) == {"_limits_profile": "program"}
    finally:
        conn.close()


def test_a_state_tree_key_is_refused_at_publish(tmp_path):
    from test_program_substrate import _program_app, _session

    from oolu.skills.program import ModuleSpec as MS
    from oolu.skills.program import ProgramSpec
    from oolu.skills.program import UnifiedInterface as UI

    spec = ProgramSpec(
        modules=[MS(path="lib/ledger.py"), MS(path="lib/__init__.py")],
        interface=UI(operation="main", ports=[{"name": "summary"}]),
    )
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        result = app.publish_program_node(
            _session(app, ident),
            goal="keep the ledger",
            script=STATE_ENTRY,
            files={**STATE_LIB, "state.json": "{}"},
            program=spec,
        )
        assert not result["ok"] and "reserved" in result["problem"]
    finally:
        conn.close()
