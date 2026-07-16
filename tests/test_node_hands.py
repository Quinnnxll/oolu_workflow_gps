"""OoLu's hands: web-capable, file-carrying, webhook-fireable nodes.

The complaint this closes: a task beyond the model's own reach (a web
search, an API call) ended in refusal — the sandbox has no network, so
the conversation was told never to build such nodes. The sandbox STAYS
severed; what changes is that a node's function now has honest hands:

* ``http_request`` (the shim) writes a request file into a bind-mounted
  exchange; a host-side broker answers it through the SAME guarded HTTP
  executor http actions use — machine allowlist, the node's egress grant,
  the always-on SSRF wall, every redirect re-checked. Exit gate: a
  granted script fetches through the broker inside a network-severed
  backend; an ungranted one is refused in words, never silently.
* A node carries its own programs: ``ExecutionRequest.files`` stages the
  drawer's ``src/`` documents next to the script; paths that try to
  escape the sandbox are refused loudly.
* A webhook fires the node: the owner mints one token-credentialed URL,
  an outside POST runs the node's own function with the payload staged
  at ``webhook_payload.json`` — under the owner's identity, quota, and
  egress consent. Wrong token and no hook answer the same 404.
* The prompts stopped lying: web work is named buildable, and the
  function writer is taught the one honest door to the web.
"""

from __future__ import annotations

import json

import httpx
import pytest
from test_chat_assistant import _FakeModel
from test_http_gateway import _app, _req
from test_node_interact import FakeAuthor

from oolu.durable import DurableConnection, NodeHookStore
from oolu.runtime import (
    ExecutionRequest,
    NodeScriptRunner,
    StubBackend,
    WebBroker,
    WebGrant,
)
from oolu.runtime.backend import BackendError, make_success
from oolu.runtime.isolation import SubprocessBackend
from oolu.runtime.sandbox_shim import WebGrantError, http_request
from oolu.runtime.webhand import (
    EXCHANGE_ENV,
    REQUEST_SUFFIX,
    RESPONSE_SUFFIX,
)
from oolu.skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy
from oolu.skills.models import ActionEvent, ExecutionStatus

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a resolver stub


def _guarded(handler=None, **policy):
    handler = handler or (lambda request: httpx.Response(200, text="ok"))
    return HttpActionExecutor(
        HttpExecutionPolicy(**policy),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=PUBLIC,
    )


def _stub_fetch(record: list | None = None, body: str = "hello from the web"):
    """A GuardedFetch double: records every call, answers a fixed page."""
    calls = record if record is not None else []

    def fetch(method, url, *, headers=None, body=None, grant=None, blocked=None):
        calls.append({"method": method, "url": url, "grant": grant, "blocked": blocked})
        return {
            "status": 200,
            "url": url,
            "content_type": "text/plain",
            "body": "hello from the web",
            "truncated": False,
            "error": None,
        }

    fetch.calls = calls
    return fetch


# --------------------------------------------------------------------------- #
# The wire: shim constants and the broker must never drift apart.              #
# --------------------------------------------------------------------------- #
def test_the_shim_and_the_broker_speak_one_protocol():
    from oolu.runtime import sandbox_shim, webhand

    assert sandbox_shim._WEB_EXCHANGE_ENV == webhand.EXCHANGE_ENV
    assert sandbox_shim._WEB_REQUEST_SUFFIX == webhand.REQUEST_SUFFIX
    assert sandbox_shim._WEB_RESPONSE_SUFFIX == webhand.RESPONSE_SUFFIX


def test_no_grant_means_the_honest_refusal_not_a_hang(monkeypatch):
    monkeypatch.delenv(EXCHANGE_ENV, raising=False)
    with pytest.raises(WebGrantError, match="no web grant"):
        http_request("https://api.example/x")


def test_the_shim_asks_and_the_broker_answers(tmp_path, monkeypatch):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    monkeypatch.setenv(EXCHANGE_ENV, str(exchange))
    fetch = _stub_fetch()
    broker = WebBroker(fetch=fetch, grant=WebGrant(hosts=("api.example",)))
    broker.start(exchange)
    try:
        answer = http_request(
            "https://api.example/rates",
            method="POST",
            body={"base": "USD"},
            timeout_s=10,
        )
    finally:
        broker.stop()
    assert answer["status"] == 200
    assert answer["body"] == "hello from the web"
    assert answer["error"] is None
    # The broker handed the node's grant to the guarded fetch — the wall
    # travels with every call.
    [call] = fetch.calls
    assert call["method"] == "POST"
    assert call["grant"] == frozenset({"api.example"})
    # A JSON body earned its content-type; the honest record kept the call.
    request_files = list(exchange.glob(f"*{REQUEST_SUFFIX}"))
    sent = json.loads(request_files[0].read_text())
    assert sent["headers"]["Content-Type"] == "application/json"
    assert broker.calls[0]["status"] == 200


def test_a_broken_request_file_gets_words_back(tmp_path):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    (exchange / f"bad{REQUEST_SUFFIX}").write_text("this is not json")
    broker = WebBroker(fetch=_stub_fetch(), grant=WebGrant())
    broker.sweep(exchange, set())
    answer = json.loads((exchange / f"bad{RESPONSE_SUFFIX}").read_text())
    assert answer["status"] == 0
    assert "unreadable request" in answer["error"]


def test_the_per_run_call_cap_refuses_the_flood(tmp_path):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    for i in range(3):
        (exchange / f"c{i}{REQUEST_SUFFIX}").write_text(
            json.dumps({"method": "GET", "url": "https://api.example/x"})
        )
    broker = WebBroker(
        fetch=_stub_fetch(), grant=WebGrant(hosts=("api.example",), max_calls=2)
    )
    broker.sweep(exchange, set())
    answers = [
        json.loads((exchange / f"c{i}{RESPONSE_SUFFIX}").read_text()) for i in range(3)
    ]
    refused = [a for a in answers if a["error"] and "cap" in a["error"]]
    served = [a for a in answers if a["error"] is None]
    assert len(served) == 2 and len(refused) == 1


# --------------------------------------------------------------------------- #
# The guarded request: one enforcement point, reads and writes alike.          #
# --------------------------------------------------------------------------- #
def test_a_granted_post_passes_and_an_ungranted_one_dies_first():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, text="posted")

    executor = _guarded(handler)
    ok = executor.request(
        "POST",
        "https://api.example/ingest",
        body='{"x": 1}',
        grant=frozenset({"api.example"}),
    )
    assert ok["status"] == 200 and ok["error"] is None
    blocked = executor.request(
        "POST",
        "https://elsewhere.example/ingest",
        grant=frozenset({"api.example"}),
    )
    assert blocked["status"] == 0
    assert "granted hosts" in blocked["error"]
    assert hits == ["https://api.example/ingest"]


def test_an_empty_grant_fails_closed_with_the_words_to_fix_it():
    executor = _guarded()
    answer = executor.request("GET", "https://api.example/x", grant=frozenset())
    assert answer["status"] == 0
    assert "no network grant" in answer["error"]


def test_a_write_never_follows_a_redirect():
    def bouncing(request):
        return httpx.Response(302, headers={"location": "https://exfil.example/sink"})

    executor = _guarded(bouncing)
    answer = executor.request(
        "POST", "https://api.example/x", grant=frozenset({"api.example"})
    )
    assert answer["status"] == 0
    assert "redirect" in answer["error"]


def test_strange_methods_and_oversized_bodies_are_refused():
    executor = _guarded()
    assert "not supported" in executor.request("BREW", "https://api.example/x")["error"]
    too_big = "x" * 300_000
    assert (
        "exceeds"
        in executor.request("POST", "https://api.example/x", body=too_big)["error"]
    )


def test_the_open_web_regime_still_honors_the_org_blocks():
    executor = _guarded()
    blocked = executor.request(
        "GET",
        "https://gambling.example/odds",
        grant=None,
        blocked=frozenset({"gambling.example"}),
    )
    assert "blocked by this node's Supernode" in blocked["error"]
    open_ok = executor.request(
        "GET", "https://news.example/today", grant=None, blocked=frozenset()
    )
    assert open_ok["status"] == 200


# --------------------------------------------------------------------------- #
# End to end through a real backend: severed, yet the web answers.             #
# --------------------------------------------------------------------------- #
WEB_SCRIPT = """\
from _oolu_runtime import emit_result, http_request
answer = http_request("https://api.example/rates", timeout_s=15)
emit_result({"fetched": answer["body"], "status": answer["status"]})
"""


def test_a_granted_script_reaches_the_web_through_the_broker():
    fetch = _stub_fetch()
    backend = SubprocessBackend(web_fetch=fetch)
    result = backend.run(
        ExecutionRequest(script=WEB_SCRIPT, web=WebGrant(hosts=("api.example",)))
    )
    assert result.contract_ok, result.stderr
    assert result.contract_payload == {
        "fetched": "hello from the web",
        "status": 200,
    }
    [call] = fetch.calls
    assert call["grant"] == frozenset({"api.example"})


UNGRANTED_SCRIPT = """\
from _oolu_runtime import emit_result, http_request, WebGrantError
try:
    http_request("https://api.example/rates")
    emit_result({"reached": True})
except WebGrantError as exc:
    emit_result({"reached": False, "why": str(exc)})
"""


def test_an_ungranted_script_gets_the_refusal_in_words():
    backend = SubprocessBackend(web_fetch=_stub_fetch())
    result = backend.run(ExecutionRequest(script=UNGRANTED_SCRIPT))
    assert result.contract_ok, result.stderr
    assert result.contract_payload["reached"] is False
    assert "no web grant" in result.contract_payload["why"]


FILES_SCRIPT = """\
from _oolu_runtime import emit_result
import pathlib
emit_result({"helper": pathlib.Path("src/helper.py").read_text()})
"""


def test_a_nodes_own_files_stage_next_to_its_script():
    backend = SubprocessBackend()
    result = backend.run(
        ExecutionRequest(script=FILES_SCRIPT, files={"src/helper.py": "WISDOM = 42\n"})
    )
    assert result.contract_ok, result.stderr
    assert result.contract_payload == {"helper": "WISDOM = 42\n"}


@pytest.mark.parametrize(
    "path", ["../evil.py", "/etc/owned", "user_script.py", "a/../../b.py"]
)
def test_a_staged_path_that_escapes_or_shadows_is_refused(path):
    backend = SubprocessBackend()
    with pytest.raises(BackendError):
        backend.run(ExecutionRequest(script="x=1", files={path: "x"}))


# --------------------------------------------------------------------------- #
# The script hand carries the stamp: grant and files ride the action.          #
# --------------------------------------------------------------------------- #
def _script_action(**params):
    params.setdefault("goal", "fetch the rates")
    params.setdefault("script", "from _oolu_runtime import emit_result\nemit_result(1)")
    return ActionEvent(
        correlation_id="c1", adapter="script", operation="run", parameters=params
    )


def test_the_runner_hands_grant_and_files_to_the_backend(tmp_path):
    from oolu.cache import LocalScriptCache

    backend = StubBackend([make_success({"result": 1})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(
        _script_action(_egress_hosts=["api.example"], files={"src/helper.py": "X=1"}),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED, outcome.error
    [request] = backend.requests
    assert request.web == WebGrant(hosts=("api.example",))
    assert request.files == {"src/helper.py": "X=1"}


def test_the_open_stamp_becomes_the_open_grant(tmp_path):
    from oolu.cache import LocalScriptCache

    backend = StubBackend([make_success({"result": 1})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    runner.execute(
        _script_action(_egress_open=True, _egress_blocked=["bad.example"]),
        idempotency_key="k1",
    )
    [request] = backend.requests
    assert request.web == WebGrant(open_web=True, blocked_hosts=("bad.example",))


def test_an_unstamped_action_mounts_no_web_at_all(tmp_path):
    from oolu.cache import LocalScriptCache

    backend = StubBackend([make_success({"result": 1})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    runner.execute(_script_action(), idempotency_key="k1")
    [request] = backend.requests
    assert request.web is None


def test_stamping_covers_script_bodied_children_too():
    from oolu.nodeplace import compile_contract, stamp_egress_grants
    from oolu.skills.contract import NodeContract, ScriptBody

    contract = NodeContract(name="fetch-rates", body=ScriptBody(goal="fetch the rates"))
    stamped = stamp_egress_grants(
        contract,
        compile_contract(contract),
        {contract.id: ("api.example",)},
    )
    [item] = [i for i in stamped.blueprint.actions if i.action.adapter == "script"]
    assert item.action.parameters["_egress_hosts"] == ["api.example"]


# --------------------------------------------------------------------------- #
# The prompts stopped lying.                                                   #
# --------------------------------------------------------------------------- #
def test_the_function_writer_is_taught_the_web_hand():
    from oolu.chat import NODE_FUNCTION_PROMPT

    assert "http_request" in NODE_FUNCTION_PROMPT
    assert "IS executable work" in NODE_FUNCTION_PROMPT
    assert "webhook_payload.json" in NODE_FUNCTION_PROMPT


def test_the_search_note_no_longer_forbids_web_tasks():
    from oolu.chat import WEB_SEARCH_NOTE, WEB_TASK_NOTE

    assert "can only fail" not in WEB_SEARCH_NOTE
    assert "REPEATABLE web work" in WEB_SEARCH_NOTE
    assert "task" in WEB_TASK_NOTE and "granted" in WEB_TASK_NOTE


def test_every_chat_turn_carries_the_engines_web_truth(tmp_path):
    from oolu.chat import WEB_TASK_NOTE

    app, conn, ident = _app(tmp_path)
    model = _FakeModel(['{"say": "Sure!", "task": null}'])
    app._tenant_model = lambda tenant: model
    try:
        response = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=ident.token("user-1", "t1"),
                body={"message": "can you watch a webpage for me?", "history": []},
            )
        )
        assert response.status == 200, response.body
        [messages] = model.calls
        notes = [m["content"] for m in messages if m.get("role") == "system"]
        assert any(WEB_TASK_NOTE in note for note in notes)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The consent chain, end to end: build in chat, run through the node's own     #
# function, the grant stamped onto the executed action.                        #
# --------------------------------------------------------------------------- #
WEB_GOAL = "fetch today's exchange rates from the web"
WEB_FUNCTION_ANSWER = (
    "1. Fetch the rates.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\nfrom _oolu_runtime import emit_result, http_request\n"
    'answer = http_request("https://api.example/rates")\n'
    'emit_result(answer["body"])\n```'
)


def _grown_web_node(tmp_path):
    """The growth flow walked once for a WEB goal: the node exists after."""
    from test_growth_trigger import _chat, _rig

    app, conn, ident, desk, script_exec = _rig(tmp_path)
    task_turn = '{"say": "On it!", "task": "' + WEB_GOAL + '"}'
    # Two scripted turns: the growth walk, and one later re-ask of the
    # same goal (which then runs the built node's own function).
    model = _FakeModel([task_turn, task_turn])
    app._tenant_model = lambda tenant: model
    app._node_function_author = lambda tenant: FakeAuthor(WEB_FUNCTION_ANSWER)
    _chat(app, ident, "get me today's fx rates")
    agreed = _chat(app, ident, "yes")
    return app, conn, ident, desk, script_exec, agreed


def test_a_web_task_grows_a_node_and_the_grant_rides_the_run(tmp_path):
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        reply = agreed.body["reply"]
        assert "Built a NEW node" in reply
        # Born reaching for the web: the reply says to grant hosts.
        assert "grant the exact hosts" in reply
        # The run executed the node's OWN function, and the action carries
        # the fail-closed stamp: an account with no granted hosts yet.
        action = script_exec.actions[-1]
        assert action.adapter == "script"
        assert "http_request" in action.parameters["script"]
        assert action.parameters["_egress_hosts"] == []
    finally:
        conn.close()


def test_drawer_src_files_ride_the_nodes_runs(tmp_path):
    from oolu.durable import UserFile

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        mine = desk.overview(principal="user-1", tenant="t1")
        node_id = mine[0].node_id
        app._files.save(
            UserFile(
                tenant_id="t1",
                node_id=node_id,
                folder="src",
                name="helper.py",
                content="WISDOM = 42\n",
            )
        )
        from test_growth_trigger import _chat

        again = _chat(app, ident, WEB_GOAL)
        assert again.status == 200, again.body
        action = script_exec.actions[-1]
        assert action.parameters["files"] == {"helper.py": "WISDOM = 42\n"}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The webhook door.                                                            #
# --------------------------------------------------------------------------- #
def test_the_hook_store_mints_verifies_rotates_and_revokes(tmp_path):
    conn = DurableConnection(tmp_path / "hooks.db")
    try:
        hooks = NodeHookStore(conn)
        token = hooks.mint("node-1", tenant="t1", principal="alice")
        assert hooks.verify("node-1", token).principal == "alice"
        assert hooks.verify("node-1", "wrong") is None
        assert hooks.verify("node-2", token) is None
        # Only the digest is stored — a database read yields no usable token.
        row = conn.db.execute(
            "SELECT token_sha256 FROM node_hooks WHERE node_id = 'node-1'"
        ).fetchone()
        assert row["token_sha256"] != token
        # Minting again ROTATES: the old token dies with the new birth.
        fresh = hooks.mint("node-1", tenant="t1", principal="alice")
        assert hooks.verify("node-1", token) is None
        assert hooks.verify("node-1", fresh) is not None
        assert hooks.revoke("node-1") is True
        assert hooks.verify("node-1", fresh) is None
    finally:
        conn.close()


def test_the_webhook_fires_the_nodes_own_function_with_the_payload(tmp_path):
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        owner = ident.token("user-1", "t1")

        # A stranger cannot mint; the owner can, once the function exists.
        minted = app.handle(_req("POST", f"/v1/work/nodes/{node_id}/hook", token=owner))
        assert minted.status == 201, minted.body
        token = minted.body["token"]
        assert token in minted.body["path"]

        runs_before = len(script_exec.actions)
        fired = app.handle(
            _req(
                "POST",
                f"/v1/hooks/nodes/{node_id}/{token}",
                body={"event": "rates.updated", "base": "USD"},
            )
        )
        assert fired.status == 202, fired.body
        assert fired.body["run_id"] is not None
        action = script_exec.actions[-1]
        assert len(script_exec.actions) == runs_before + 1
        payload = json.loads(action.parameters["files"]["webhook_payload.json"])
        assert payload == {"event": "rates.updated", "base": "USD"}
        # The fired run still wears the node's egress consent.
        assert action.parameters["_egress_hosts"] == []

        # A wrong token and a hookless node answer the SAME 404.
        wrong = app.handle(
            _req("POST", f"/v1/hooks/nodes/{node_id}/not-the-token", body={})
        )
        assert wrong.status == 404

        # Revocation closes the door.
        revoked = app.handle(
            _req("DELETE", f"/v1/work/nodes/{node_id}/hook", token=owner)
        )
        assert revoked.status == 200 and revoked.body["revoked"] is True
        after = app.handle(_req("POST", f"/v1/hooks/nodes/{node_id}/{token}", body={}))
        assert after.status == 404
    finally:
        conn.close()


def test_a_hook_needs_a_function_and_an_owned_node(tmp_path):
    from test_growth_trigger import _rig

    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        nobody = app.handle(
            _req(
                "POST",
                "/v1/work/nodes/ghost/hook",
                token=ident.token("user-1", "t1"),
            )
        )
        assert nobody.status == 404
    finally:
        conn.close()


def test_an_oversized_webhook_payload_is_refused(tmp_path):
    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        minted = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/hook",
                token=ident.token("user-1", "t1"),
            )
        )
        token = minted.body["token"]
        huge = {"blob": "x" * 70_000}
        refused = app.handle(
            _req("POST", f"/v1/hooks/nodes/{node_id}/{token}", body=huge)
        )
        assert refused.status == 400
        assert "exceeds" in refused.body["error"]["message"]
    finally:
        conn.close()
