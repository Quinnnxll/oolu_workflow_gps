"""F0 — the program substrate: spec refusals, tree-true birth verification,
transactional tree landing, the pre-publish freeze, and the round trip."""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.cache import LocalScriptCache
from oolu.durable import UserFileStore
from oolu.gateway import GatewayApp
from oolu.nodeplace import NodeplaceService, RegistryStore
from oolu.runtime import NodeScriptRunner
from oolu.runtime.bundle import BundleStore
from oolu.runtime.isolation import SubprocessBackend
from oolu.skills.models import ActionEvent
from oolu.skills.program import (
    MAX_PROGRAM_MODULES,
    ModuleSpec,
    ProgramSpec,
    UnifiedInterface,
    canonical_program_json,
    parse_program_spec,
)


# --------------------------------------------------------------------------- #
# Spec parse and refusals — by name, at the door.                              #
# --------------------------------------------------------------------------- #
def test_valid_spec_parses_and_reparses():
    spec, problem = parse_program_spec(
        {
            "modules": [
                {"path": "lib/ingest.py", "check": "tests/check_ingest.py"},
                {"path": "lib/report.py", "depends": ["lib/ingest.py"]},
            ],
            "operations": [{"name": "main", "entry": "lib.report:build"}],
            "interface": {"operation": "main", "ports": [{"name": "summary"}]},
        }
    )
    assert problem == "" and spec is not None
    again, problem2 = parse_program_spec(spec)
    assert problem2 == "" and again is spec


def test_interface_as_list_refuses_structurally():
    _, problem = parse_program_spec({"interface": []})
    assert "ONE unified interface" in problem


def test_module_ceiling_refuses_by_name():
    modules = [{"path": f"lib/m{i}.py"} for i in range(MAX_PROGRAM_MODULES + 1)]
    _, problem = parse_program_spec({"modules": modules})
    assert f"at most {MAX_PROGRAM_MODULES} modules" in problem


def test_dependency_cycle_refuses_by_name():
    _, problem = parse_program_spec(
        {
            "modules": [
                {"path": "lib/a.py", "depends": ["lib/b.py"]},
                {"path": "lib/b.py", "depends": ["lib/a.py"]},
            ]
        }
    )
    assert "form a cycle" in problem


def test_unknown_dependency_and_duplicate_paths_refuse():
    _, problem = parse_program_spec(
        {"modules": [{"path": "lib/a.py", "depends": ["lib/ghost.py"]}]}
    )
    assert "not a declared module" in problem
    _, dupes = parse_program_spec(
        {"modules": [{"path": "lib/a.py"}, {"path": "lib/a.py"}]}
    )
    assert "duplicate module path" in dupes


def test_escaping_paths_refuse():
    _, problem = parse_program_spec({"modules": [{"path": "../evil.py"}]})
    assert "escapes the tree" in problem
    _, absolute = parse_program_spec({"modules": [{"path": "/abs.py"}]})
    assert "escapes the tree" in absolute


def test_reserved_payload_key_port_refuses():
    for name in ("state", "files", "records"):
        _, problem = parse_program_spec(
            {"interface": {"ports": [{"name": name}]}}
        )
        assert "reserved payload key" in problem, name


def test_mechanism_flavored_port_label_refuses():
    _, problem = parse_program_spec(
        {
            "interface": {
                "ports": [{"name": "summary", "label": "Which output format?"}]
            }
        }
    )
    assert "plain words" in problem


def test_undeclared_interface_operation_and_state_refuse():
    _, problem = parse_program_spec(
        {
            "operations": [{"name": "build", "entry": "lib.x:run"}],
            "interface": {"operation": "main"},
        }
    )
    assert "not declared" in problem
    _, state = parse_program_spec(
        {"operations": [{"name": "main", "entry": "lib.x:run", "reads": ["ledger"]}]}
    )
    assert "state 'ledger'" in state


def test_canonical_serialization_is_stable_across_field_order():
    a = parse_program_spec(
        {
            "modules": [{"path": "lib/a.py", "purpose": "ingest"}],
            "interface": {"operation": "main", "ports": [{"name": "summary"}]},
        }
    )[0]
    b = parse_program_spec(
        {
            "interface": {"ports": [{"name": "summary"}], "operation": "main"},
            "modules": [{"purpose": "ingest", "path": "lib/a.py"}],
        }
    )[0]
    assert canonical_program_json(a) == canonical_program_json(b)

    from oolu.runtime.bundle import freeze_tree

    tree_a = {"program.json": canonical_program_json(a)}
    tree_b = {"program.json": canonical_program_json(b)}
    assert freeze_tree(tree_a)[0].bundle_id == freeze_tree(tree_b)[0].bundle_id


# --------------------------------------------------------------------------- #
# The single-file path shares the reserved-key wall.                           #
# --------------------------------------------------------------------------- #
def test_single_file_io_refuses_reserved_output_names():
    from oolu.chat import parse_node_io_checked

    _, problem = parse_node_io_checked(
        'IO: {"inputs": [], "outputs": [{"name": "state", "type": "str"}]}'
    )
    assert "reserved payload key" in problem
    io, ok = parse_node_io_checked(
        'IO: {"inputs": [], "outputs": [{"name": "summary", "type": "str"}]}'
    )
    assert ok == "" and io["outputs"][0]["name"] == "summary"


# --------------------------------------------------------------------------- #
# Tree-true birth verification on the real runner.                             #
# --------------------------------------------------------------------------- #
ENTRY = (
    "from _oolu_runtime import emit_result\n"
    "from lib.compute import answer\n"
    "emit_result({'summary': answer()})\n"
)
LIB = {
    "lib/__init__.py": "",
    "lib/compute.py": (
        "def answer():\n"
        "    return 'the ' + 'answer'\n"
    ),
    "tests/check_compute.py": (
        "from _oolu_runtime import emit_result\n"
        "from lib.compute import answer\n"
        "assert answer() == 'the answer'\n"
        "emit_result({'checked': 'compute'})\n"
    ),
}


def _runner(tmp_path) -> NodeScriptRunner:
    return NodeScriptRunner(
        SubprocessBackend(), LocalScriptCache(tmp_path / "scripts.db")
    )


def test_verify_function_judges_the_tree_as_itself(tmp_path):
    runner = _runner(tmp_path)
    # Without the tree, the entry cannot import its module — refused.
    bare = runner.verify_function(
        "compute", ENTRY, session_id="f0-bare", ports=[{"name": "summary"}]
    )
    assert not bare["ok"]
    # With the tree staged, the program is judged AS ITSELF and passes.
    staged = runner.verify_function(
        "compute",
        ENTRY,
        session_id="f0-tree",
        ports=[{"name": "summary"}],
        files=LIB,
    )
    assert staged["ok"], staged["error"]
    # A per-module check is just another script verified with the tree.
    check = runner.verify_function(
        "check module lib/compute.py",
        LIB["tests/check_compute.py"],
        session_id="f0-check",
        files=LIB,
    )
    assert check["ok"], check["error"]


# --------------------------------------------------------------------------- #
# The internal publish door, end to end.                                       #
# --------------------------------------------------------------------------- #
def _program_app(tmp_path, *, with_files=True, with_bundles=True):
    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    runner = _runner(tmp_path)
    cas = FilesystemArtifactStore(tmp_path / "cas") if with_bundles else None
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        contract_executors={"script": runner},
        files=UserFileStore(conn, artifacts=cas) if with_files else None,
        bundle_store=BundleStore(conn, cas) if with_bundles else None,
    )
    return app, conn, ident, runner


def _session(app, ident):
    # The internal door reads the session's identity fields only.
    from types import SimpleNamespace

    return SimpleNamespace(tenant_id="t1", principal_id="noder")


SPEC = ProgramSpec(
    modules=[
        ModuleSpec(path="lib/compute.py", check="tests/check_compute.py"),
        ModuleSpec(path="lib/__init__.py"),
    ],
    interface=UnifiedInterface(operation="main", ports=[{"name": "summary"}]),
)


def test_program_node_publishes_and_round_trips(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)

    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result.get("problem")
    assert result["receipt_note"] == ""  # the tree landed, quietly
    node_id = result["node_id"]

    # The drawer holds the WHOLE tree: entry, modules, checks, spec.
    tree = app._node_src_tree("t1", node_id)
    assert set(tree) >= {
        "main.py",
        "lib/compute.py",
        "tests/check_compute.py",
        "program.json",
    }
    assert tree["program.json"] == canonical_program_json(SPEC)

    # Round trip: drawer -> finalize (freeze) -> run -> cache hit.
    files = {p: c for p, c in tree.items()}
    function = app._finalize_function(
        {
            "node_id": node_id,
            "script": "",
            "node_key": f"node:{result['skill_id']}",
            "goal": "compute the answer",
            "files": files,
        },
    )
    assert function["script"]  # main.py promoted to THE function
    assert function.get("bundle")  # the tree froze to a content-addressed id

    app_runner = NodeScriptRunner(
        SubprocessBackend(),
        LocalScriptCache(tmp_path / "run-scripts.db"),
        bundle_resolver=app._bundle_store.prepare,
    )
    action = ActionEvent(
        correlation_id="function",
        adapter="script",
        operation="run",
        parameters={
            "goal": "compute the answer",
            "script": function["script"],
            "node_key": function["node_key"],
            "bundle": function["bundle"],
        },
    )
    first = app_runner.execute(action, idempotency_key="f0-run-1")
    assert first.status.value == "succeeded", first.error
    assert first.evidence["result"]["summary"] == "the answer"
    second = app_runner.execute(action, idempotency_key="f0-run-2")
    assert second.status.value == "succeeded"
    assert second.evidence.get("cache") == "hit"
    conn.close()


def test_program_door_refuses_failing_checks_and_smelly_modules(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)

    broken = dict(LIB)
    broken["tests/check_compute.py"] = (
        "from _oolu_runtime import emit_result\n"
        "from lib.compute import answer\n"
        "assert answer() == 'wrong'\n"
        "emit_result({'checked': 'compute'})\n"
    )
    failing = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=broken,
        program=SPEC,
    )
    assert not failing["ok"]
    assert "failed its check" in failing["problem"]

    missing = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files={k: v for k, v in LIB.items() if k != "lib/compute.py"},
        program=SPEC,
    )
    assert not missing["ok"]
    assert "not in the tree" in missing["problem"]
    conn.close()


def test_program_door_lands_loudly_without_a_file_store(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path, with_files=False)
    session = _session(app, ident)
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
    )
    # Verified and published — but the drawer miss is LOUD: the receipt
    # says so and node.src_unlanded stands on the audit chain.
    assert result["ok"]
    assert "did not land" in result["receipt_note"]
    events = [
        r
        for r in app._durable.audit.records()
        if r.event_type == "node.src_unlanded"
    ]
    assert events and events[-1].payload["node_id"] == result["node_id"]
    conn.close()


def test_over_wall_tree_freezes_before_publish(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)
    # 40 filler modules push the tree past the 32-file inline wall; the
    # door freezes it pre-publish and verifies via the bundle.
    filler = {f"lib/filler_{i}.py": f"VALUE_{i} = {i}\n" for i in range(40)}
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files={**LIB, **filler},
        program=SPEC,
    )
    assert result["ok"], result.get("problem")
    assert result["bundle_id"]  # pre-publish frozen, judged via bundle=
    conn.close()


def test_over_wall_tree_refuses_honestly_without_a_bundle_store(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path, with_bundles=False)
    session = _session(app, ident)
    filler = {f"lib/filler_{i}.py": f"VALUE_{i} = {i}\n" for i in range(40)}
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files={**LIB, **filler},
        program=SPEC,
    )
    assert not result["ok"]
    assert "no bundle store" in result["problem"]
    conn.close()


# --------------------------------------------------------------------------- #
# F0.1 — hardening (adversarial review of the F0 diff).                        #
# --------------------------------------------------------------------------- #
def test_door_refuses_reserved_and_escaping_tree_keys(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)
    for bad_key in ("main.py", "_oolu_runtime.py", "user_script.py",
                    "bindings.json", "program.json", "state/x.json",
                    "../evil.py", "/abs.py"):
        result = app.publish_program_node(
            session,
            goal="compute the answer",
            script=ENTRY,
            files={**LIB, bad_key: "print('x')\n"},
            program=SPEC,
        )
        assert not result["ok"], bad_key
        assert (
            "reserved" in result["problem"] or "escapes" in result["problem"]
        ), bad_key
    conn.close()


def test_bundle_member_cannot_shadow_the_harness(tmp_path):
    # The over-wall path packs the tree into a bundle; a member named
    # like the harness must be refused at the sandbox boundary, not
    # overwrite the shim after it is written.
    from oolu.runtime.bundle import freeze_tree
    from oolu.runtime.isolation import _unpack_into
    import pytest

    manifest, blobs = freeze_tree(
        {"lib/ok.py": "X = 1\n", "_oolu_runtime.py": "evil = True\n"}
    )
    # Reassemble the tar the store would ship and unpack it — the boundary
    # refuses the harness-shadowing member by name.
    from oolu.runtime.bundle import pack_tar

    archive = pack_tar(
        {"lib/ok.py": b"X = 1\n", "_oolu_runtime.py": b"evil = True\n"}
    )
    with pytest.raises(Exception) as exc:
        _unpack_into(tmp_path, archive)
    assert "shadow the harness" in str(exc.value)


def test_check_may_not_be_a_module_source():
    _, problem = parse_program_spec(
        {
            "modules": [
                {"path": "lib/fetch.py", "check": "lib/fetch.py"},
                {"path": "lib/other.py"},
            ]
        }
    )
    assert "names a module source" in problem


def test_operation_entry_format_is_enforced():
    _, problem = parse_program_spec(
        {"operations": [{"name": "main", "entry": "not-dotted"}]}
    )
    assert "dotted.module:function" in problem


def test_partial_landing_names_what_did_and_did_not_land(tmp_path):
    # A file-store that accepts the first write then refuses the second
    # leaves a half-written drawer — the miss must name the tree honestly,
    # never read as a lone main.py miss.
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)

    class HalfStore:
        def __init__(self, real):
            self._real = real
            self._writes = 0

        def __getattr__(self, name):
            return getattr(self._real, name)

        def save(self, *args, **kwargs):
            self._writes += 1
            if self._writes >= 2:
                raise RuntimeError("disk full")
            return self._real.save(*args, **kwargs)

    app._files = HalfStore(app._files)
    note = app._land_tree(
        session,
        "node-x",
        {"src/main.py": "a\n", "src/lib/helper.py": "b\n", "src/program.json": "{}"},
        goal="g",
    )
    assert "program's drawer tree" in note
    events = [
        r for r in app._durable.audit.records()
        if r.event_type == "node.src_unlanded"
    ]
    assert events
    payload = events[-1].payload
    assert "landed" in payload and "missed" in payload
    assert payload["missed"]  # something did not land, and it is named
    conn.close()


def test_verify_backend_failure_refuses_not_crashes(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)

    class ExplodingRunner:
        def verify_function(self, *args, **kwargs):
            raise RuntimeError("backend down")

    app._contract_executors["script"] = ExplodingRunner()
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
    )
    assert not result["ok"]
    assert "verification failed" in result["problem"]
    conn.close()


# --------------------------------------------------------------------------- #
# F1.1 — review fixes.                                                         #
# --------------------------------------------------------------------------- #
INPUT_ENTRY = (
    "import json\n"
    "from _oolu_runtime import emit_result\n"
    "from lib.compute import answer\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    bindings = json.load(fh)\n"
    "emit_result({'summary': answer(bindings.get('source', 'default'))})\n"
)
INPUT_LIB = {
    "lib/__init__.py": "",
    "lib/compute.py": "def answer(source):\n    return 'from ' + str(source)\n",
}
INPUT_SPEC = ProgramSpec(
    modules=[ModuleSpec(path="lib/compute.py"), ModuleSpec(path="lib/__init__.py")],
    interface=UnifiedInterface(operation="main", ports=[{"name": "summary"}]),
)


def test_inputs_declaring_program_births_with_a_staged_bindings_file(tmp_path):
    # The dispatcher reads bindings.json; birth must stage one, or the
    # entry dies on open() before it dispatches or checks a port.
    app, conn, ident, runner = _program_app(tmp_path)
    session = _session(app, ident)
    result = app.publish_program_node(
        session,
        goal="compute from a source",
        script=INPUT_ENTRY,
        files=dict(INPUT_LIB),
        program=INPUT_SPEC,
        io={
            "inputs": [{"name": "source", "type": "str", "label": "Which source?"}],
            "outputs": [{"name": "summary", "type": "str"}],
        },
    )
    # The entry actually ran the real dispatch path (read bindings, called
    # the module, emitted the port) — not silently defeated as honest_error.
    assert result["ok"], result.get("problem")
    assert not any("birth ran honest" in n for n in result.get("notes", []))
    conn.close()


def test_reserved_tree_key_uses_the_real_shim_name():
    from oolu.runtime.contract import SHIM_MODULE_NAME

    # The shim's own name is a reserved tree key derived from the constant.
    assert SHIM_MODULE_NAME == "_oolu_runtime"
