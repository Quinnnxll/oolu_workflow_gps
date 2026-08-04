"""B4: the durable hand-off — outputs flow to the next node, run-cited.

Exit gate: running the downstream node alone OFFERS the upstream's
newest standing output as the default — in words, in the conversation —
and binds it only on a yes (a no runs the node exactly as before the
question); the bound value reaches the sandbox verbatim through the
exact-value binder; and the movement lands run-cited provenance on the
temporal graph — "what value moved between you, in which run" is one
query over the stores, answered with run ids — which the interact
window speaks when asked what the node produced.
"""

from __future__ import annotations

import json

from test_chat_assistant import _FakeModel
from test_growth_trigger import _chat, _rig
from test_http_gateway import _req
from test_node_interact import FakeAuthor

from oolu.cache import LocalScriptCache
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.runtime.backend import make_success
from oolu.values import ValueStore

GOAL_A = "collect the monthly invoice files from the mail inbox"
GOAL_B = "normalize invoice csv files"
TURN_A = '{"say": "On it!", "task": "' + GOAL_A + '"}'
TURN_B = '{"say": "On it!", "task": "' + GOAL_B + '"}'

# The producer declares the port; its run files a standing value there.
ANSWER_A = (
    "1. Collect the files.\n"
    'IO: {"inputs": [], "outputs": [{"name": "invoice_csv", "type": "str"}]}\n'
    "```python\nfrom _oolu_runtime import emit_result\n"
    "import glob\nemit_result(str(len(glob.glob('*.csv'))))\n```"
)
# The consumer declares the SAME slot as an input, plainly labeled.
ANSWER_B = (
    "1. Normalize the rows.\n"
    'IO: {"inputs": [{"name": "invoice_csv", "type": "str", '
    '"label": "Which file holds the invoices?"}], '
    '"outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\nfrom _oolu_runtime import emit_result\n"
    "import json\nrows = json.load(open('bindings.json'))\n"
    "emit_result(str(len(rows)))\n```"
)


def _handoff_rig(tmp_path):
    """The growth rig with a REAL script hand and a REAL value store —
    the same store the gateway files ports into is the one the runner's
    binder resolves ``output://`` edges through."""
    holder: dict = {}
    backend = StubBackend(
        [
            make_success({"invoice_csv": "invoices-06.csv"}),  # A's run
            make_success({"result": "3 rows"}),  # B's run
        ]
    )
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(":memory:"),
        value_resolver=lambda bindings, tenant: holder["store"].resolve_bindings(
            bindings, tenant=tenant
        ),
    )
    app, conn, ident, desk, _exec = _rig(tmp_path, script_exec=runner)
    store = ValueStore(conn)
    holder["store"] = store
    app._values = store
    return app, conn, ident, desk, backend


def _build(app, ident, answer, goal):
    app._node_function_author = lambda tenant: FakeAuthor(answer)
    built = _chat(app, ident, "build me a node that " + goal)
    assert "Built a NEW node" in built.body["reply"], built.body
    return built


def _run(app, ident, turn, goal):
    model = _FakeModel([turn])
    app._tenant_model = lambda tenant: model
    return _chat(app, ident, "please " + goal), model


def _producer_and_consumer(app):
    # Map roles by the LISTING's declared slots (a bare Node row has no io).
    by_role = {}
    for node in app._nodeplace.all_nodes():
        version = app._nodeplace.latest_version(node.node_id)
        listing = app._nodeplace.listing_for_version(version.version_id)
        slots = {s.name for s in (listing.consumes if listing else ())}
        by_role["consumer" if "invoice_csv" in slots else "producer"] = node
    assert set(by_role) == {"producer", "consumer"}
    return by_role["producer"], by_role["consumer"]


def _seed_producer(tmp_path):
    """Build A, ask to run it, drive the queued run so its port files,
    build B, ask to run B."""
    app, conn, ident, desk, backend = _handoff_rig(tmp_path)
    _build(app, ident, ANSWER_A, GOAL_A)
    ran, _model = _run(app, ident, TURN_A, GOAL_A)
    assert ran.status == 200 and ran.body["run_id"], ran.body
    # V1: the ask only ACCEPTED the run — a queued intake snapshot,
    # nothing driven, no port filed yet. The drive belongs to the
    # worker; here the test lends it hands.
    assert ran.body["run"]["phase"] == "intake", ran.body
    assert app.drive_queue() >= 1
    # The producer's standing output is on the port index.
    producers = app._values.producers_of("t1", "invoice_csv")
    assert producers, "the producer's run filed its port"
    _build(app, ident, ANSWER_B, GOAL_B)
    asked, _model = _run(app, ident, TURN_B, GOAL_B)
    assert asked.status == 200, asked.body
    return app, conn, ident, desk, backend, asked


def test_running_the_downstream_node_alone_offers_the_standing_output(tmp_path):
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        producer, consumer = _producer_and_consumer(app)
        reply = asked.body["reply"]
        # The question, in words: the plain label, the producer's name,
        # and the exact standing value — and NO run fired yet.
        assert "Use that for this run?" in reply
        assert "Which file holds the invoices?" in reply
        assert "invoices-06.csv" in reply
        assert asked.body["run_id"] is None
        # The offer stands, keyed to the person, for exactly one message.
        assert app._growth_offers.get("t1", "user-1") == (
            "handoff",
            GOAL_B,
            GOAL_B,
        )
    finally:
        conn.close()


def test_yes_binds_the_output_and_the_graph_carries_the_run_citation(tmp_path):
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        producer, consumer = _producer_and_consumer(app)
        agreed = _chat(app, ident, "yes")
        assert agreed.status == 200, agreed.body
        run_id = agreed.body["run_id"]
        assert run_id, agreed.body
        assert "Bound" in agreed.body["reply"]
        # The consent was spent — no standing offer remains.
        assert app._growth_offers.get("t1", "user-1") is None

        # The EXACT standing value reached the sandbox: the binder
        # resolved the output:// edge to the producer's filed value.
        staged = json.loads(backend.requests[-1].files["bindings.json"])
        assert staged == {"invoice_csv": "invoices-06.csv"}

        # The movement is one query over the stores, cited with the run:
        # a handoff edge, producer → consumer, run id and port aboard.
        graph = app._temporal_graph()
        (edge,) = graph.neighbors(
            consumer.node_id, edge_types=("handoff",)
        )
        assert edge["source_id"] == producer.node_id
        assert edge["target_id"] == consumer.node_id
        attrs = edge["attributes"]
        assert attrs["run_id"] == run_id
        assert attrs["port"] == "invoice_csv"
        assert attrs["value_ref"].startswith("value://t1/")

        # B3's drawer record agrees: the run's inputs.json holds the
        # resolved value verbatim.
        files = {
            (f.folder, f.name): f
            for f in app._files.list(tenant="t1", node_id=consumer.node_id)
        }
        inputs = json.loads(files[(f"runs/{run_id}", "inputs.json")].content)
        assert inputs["bindings"] == {"invoice_csv": "invoices-06.csv"}

        # And the chain is VISIBLE: the interact window names what the
        # node received, from whom, in which run — no model consulted.
        window = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=ident.token("user-1", "t1"),
                body={
                    "message": "what did you produce last",
                    "history": [],
                    "node_id": consumer.node_id,
                },
            )
        )
        assert window.status == 200, window.body
        said = window.body["reply"]
        assert "received" in said
        assert "invoice_csv" in said
        assert run_id[:8] in said
        assert window.body["source"] == "tool"
    finally:
        conn.close()


def test_no_runs_the_node_without_the_default_and_lands_no_edge(tmp_path):
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        producer, consumer = _producer_and_consumer(app)
        declined = _chat(app, ident, "no")
        assert declined.status == 200, declined.body
        # The run still fires — exactly as it would have before the
        # question — but nothing was bound.
        assert declined.body["run_id"], declined.body
        assert "without" in declined.body["reply"]
        staged = backend.requests[-1].files.get("bindings.json")
        assert staged is None or json.loads(staged) == {}
        graph = app._temporal_graph()
        assert graph.neighbors(consumer.node_id, edge_types=("handoff",)) == []
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_any_other_message_withdraws_the_handoff_offer(tmp_path):
    app, conn, ident, desk, backend, asked = _seed_producer(tmp_path)
    try:
        model = _FakeModel(['{"say": "Sunny!", "task": null}'])
        app._tenant_model = lambda tenant: model
        weather = _chat(app, ident, "how's the weather?")
        assert weather.body["reply"] == "Sunny!"
        # Consent detached from the question it answered is not consent.
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()
