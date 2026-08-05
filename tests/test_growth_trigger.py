"""The growth trigger: a failure that asks, instead of a wall that repeats.

Borrowed from n8n's editor: when the workflow is missing the node it
needs, the answer is a proposal to ADD that node — never the same refusal
again. Exit gate: a chat task the machine cannot execute ends with an
in-conversation offer — since V1 the ask is QUEUED, so the failure and
its offer arrive in the run's REPORT turn once the worker drives, not in
the reply that accepted the task; the user's plain "yes" IS the consent
(one goal, one build) — the node is built through the same gated path as
the interact window's build and the task re-fires through the node's own
function; a "no" (or any other message) withdraws the offer; and consent
is never assumed: no model to write the function means no offer, and the
yes builds exactly the offered goal, nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_chat_assistant import _FakeModel
from test_http_gateway import _autonomous, _Identity, _req
from test_node_interact import FakeAuthor
from test_node_rerun import _ScriptExec

from oolu.billing import BillingService, EarningsLedger
from oolu.chat import consent_answer
from oolu.durable import (
    DurableConnection,
    DurableWorkflowService,
    UserFileStore,
)
from oolu.gateway import GatewayApp, GatewayError
from oolu.metering.attribution import AttributionStore
from oolu.metering.store import MeteringLedger
from oolu.nodeplace import (
    NodeAccountStore,
    NodeplaceService,
    RegistryStore,
    WorkDesk,
)
from oolu.orchestrator import (
    ActionExecutorRouteRunner,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from oolu.social import AssistantHistoryStore

GOAL = "normalize invoice csv files"
TASK_TURN = '{"say": "On it!", "task": "' + GOAL + '"}'


def _rig(tmp_path, script_exec=None):
    """A gateway whose engine executes ONLY a goal's own node function.

    The stub scenario would happily run any plain intent, so the machine
    under test is made honest: a goal with no node function raises the
    same ``cannot_execute`` a real install raises when no capable node
    reaches it — the exact wall the growth trigger exists to open. V1
    moved the chat lane's wall to the worker, so the same honesty guards
    the DRIVE too: a queued run for a functionless goal FAILS in the same
    words, and the report turn carries the refusal and the offer.

    ``script_exec`` swaps the recording stub for a real script hand (a
    ``NodeScriptRunner``) where a test needs actual execute/repair
    behavior behind the same gateway."""
    ident = _Identity(tmp_path)
    brief, blueprint, executor, grounding = _autonomous()
    script_exec = script_exec if script_exec is not None else _ScriptExec()

    def build(events):
        return WorkflowOrchestrator(
            intaker=StaticIntaker(brief),
            grounder=CapabilityGrounder(grounding),
            optimizer=LeastCostRouteOptimizer([blueprint]),
            human_control=RiskBasedHumanControl(),
            executor=ActionExecutorRouteRunner(
                {"test": executor, "script": script_exec}
            ),
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=events,
        )

    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, build)
    registry = RegistryStore(conn)
    desk = WorkDesk(
        registry=registry,
        accounts=NodeAccountStore(conn),
        billing=BillingService(EarningsLedger(conn)),
        metering=MeteringLedger(conn),
        attribution=AttributionStore(conn),
        audit=durable.audit,
    )
    app = GatewayApp(
        durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
        files=UserFileStore(conn),
        # V1: the failure reports to the thread that asked — the report
        # turn is where the wall's words and the offer now live.
        assistant_history=AssistantHistoryStore(conn),
    )
    real = app._start_intent_run

    def picky(session, intent, **kwargs):
        if app._resolve_node_function(session, intent) is None:
            raise GatewayError(
                422, "cannot_execute", "no capable node reaches that goal"
            )
        return real(session, intent, **kwargs)

    app._start_intent_run = picky
    # The same honesty at the DRIVE (V1): the chat lane queues its ask,
    # so the wall falls under the worker's hands — a raised drive fails
    # the run in the exception's own words, which the report speaks.
    real_drive = durable._orch.run

    def picky_drive(state, **kwargs):
        drive_session = SimpleNamespace(
            tenant_id=state.contract.metadata.get("tenant_id"),
            principal_id=state.contract.submitted_by,
        )
        if app._resolve_node_function(drive_session, state.contract.intent) is None:
            raise RuntimeError("no capable node reaches that goal")
        return real_drive(state, **kwargs)

    durable._orch.run = picky_drive
    return app, conn, ident, desk, script_exec


def _chat(app, ident, message, *, principal="user-1"):
    return app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=ident.token(principal, "t1"),
            body={"message": message, "history": []},
        )
    )


def _speak_work(app, replies):
    """Give the tenant a scripted chat brain and a function author."""
    model = _FakeModel(replies)
    app._tenant_model = lambda tenant: model
    app._node_function_author = lambda tenant: FakeAuthor()
    return model


def _report(app, *, principal="user-1"):
    """The newest turn in the asker's thread — after a drive, the REPORT."""
    turns = app._assistant_history.history(tenant="t1", principal=principal)
    assert turns and turns[-1]["kind"] == "assistant"
    return turns[-1]["body"]


# --------------------------------------------------------------------------- #
# The consent matcher.                                                         #
# --------------------------------------------------------------------------- #
def test_build_cost_note_reports_tokens_and_compute():
    from oolu.billing.model_calls import ModelCallMeter

    meter = ModelCallMeter()

    class _Telemetry:
        def __init__(self, tier, pt, ct):
            self.model, self.tier = "m", tier
            self.prompt_tokens, self.completion_tokens = pt, ct
            self.duration_s = 0.0

    before = len(meter.charges())
    meter.record("node.build", _Telemetry("fast", 900, 300))
    note = GatewayApp._build_cost_note(meter, before)
    assert "≈1,200 tokens" in note
    assert "$" in note  # a nonzero compute figure
    # A local (free) model reports the tokens, but says it was free.
    free = ModelCallMeter()
    free.record("node.build", _Telemetry("local", 1000, 500))
    assert "free" in GatewayApp._build_cost_note(free, 0)
    # No meter, or nothing metered -> no note (never a fake number).
    assert GatewayApp._build_cost_note(None, 0) == ""
    assert GatewayApp._build_cost_note(ModelCallMeter(), 0) == ""


def test_consent_answer_is_narrow():
    assert consent_answer("yes") == "yes"
    assert consent_answer("  Yes, please!  ") == "yes"
    assert consent_answer("go ahead") == "yes"
    assert consent_answer("OKAY.") == "yes"
    assert consent_answer("no") == "no"
    assert consent_answer("not now") == "no"
    # Anything that isn't an unmistakable yes or no spends no consent.
    assert consent_answer("yes and also email bob") is None
    assert consent_answer("maybe") is None
    assert consent_answer("what would the node do?") is None
    assert consent_answer("") is None


# --------------------------------------------------------------------------- #
# The trigger: a failure asks, instead of repeating the wall.                  #
# --------------------------------------------------------------------------- #
def test_a_stuck_task_becomes_an_offer_not_a_wall(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        response = _chat(app, ident, "please tidy up my invoice files")
        assert response.status == 200, response.body
        # V1: the ask is ACCEPTED, not driven — the reply is the model's
        # bare ACK, the run stands QUEUED, and no wall has been hit yet.
        assert response.body["reply"] == "On it!"
        run_id = response.body["run_id"]
        assert run_id is not None
        assert response.body["run"]["phase"] == "intake"
        assert not response.body["run"]["result"]
        assert not response.body["run"]["failure"]
        assert app._growth_offers.get("t1", "user-1") is None
        # The worker drives; the wall falls at drive time; and the REPORT
        # turn asks, instead of repeating the refusal.
        assert app.drive_queue() >= 1
        assert app._durable.get(run_id).phase.value == "failed"
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        assert [t["kind"] for t in turns] == [
            "user", "assistant", "run", "assistant",
        ]
        report = turns[-1]["body"]
        assert "didn't make it" in report
        assert "The run failed — no capable node reaches that goal" in report
        assert "grow that missing piece" in report
        assert GOAL in report
        # The offer NAMES the node it will build (concise_name of the goal),
        # not just the goal sentence.
        assert "Normalize Invoice Csv" in report
        # The offer stands, keyed to the person, holding the exact goal.
        assert app._growth_offers.get("t1", "user-1") == ("build", GOAL, GOAL)
    finally:
        conn.close()


def test_explicit_build_creates_a_real_node_without_consulting_the_model(tmp_path):
    """Issue: OoLu narrated a build through the chat model without persisting
    a node. An explicit "build me a node …" now goes to the REAL builder — a
    node lands in My nodes — and the chat model is never even asked (so it
    cannot hallucinate the build)."""
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        # A model that would LIE about building if it were consulted.
        liar = _FakeModel(['{"say": "Done! I built your node. ✅", "task": null}'])
        app._tenant_model = lambda tenant: liar
        app._node_function_author = lambda tenant: FakeAuthor()

        response = _chat(app, ident, "build me a node that " + GOAL)
        assert response.status == 200, response.body
        reply = response.body["reply"]
        # The REAL builder's words, not the model's fabricated confirmation.
        assert "Built a NEW node" in reply
        assert "Done! I built your node" not in reply
        assert response.body["actions"] == [{"tool": "build_node"}]
        # The model was never consulted for this turn.
        assert liar.calls == []
        # And a real node is on the user's desk (My nodes).
        mine = desk.overview(principal="user-1", tenant="t1")
        assert [e.title for e in mine] == ["Normalize Invoice Csv Files"]
    finally:
        conn.close()


def test_a_build_shaped_task_from_the_model_reaches_the_writing_door(tmp_path):
    """Issue: a build ask the model routed into the task lane fired a
    WORKFLOW on the meta-sentence ("build a node to …") — nothing was
    written into any node. The gateway now recognizes a build-shaped task
    and hands it to the real builder: function authored, node on the desk,
    and no run started for the meta-sentence."""
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        # A phrasing the pre-model detector does not catch, answered by a
        # model that (correctly, per its prompt) keeps the build words in
        # the task lane.
        _speak_work(
            app,
            ['{"say": "On it!", "task": "build a node to ' + GOAL + '"}'],
        )
        response = _chat(
            app, ident, "hmm, so what I'm really after is a node doing " + GOAL
        )
        assert response.status == 200, response.body
        reply = response.body["reply"]
        assert "Built a NEW node" in reply
        # No workflow fired for the meta-sentence — building WAS the task.
        assert response.body["run_id"] is None
        assert {"tool": "build_node"} in response.body["actions"]
        mine = desk.overview(principal="user-1", tenant="t1")
        assert [e.title for e in mine] == ["Normalize Invoice Csv Files"]
    finally:
        conn.close()


def test_explicit_build_phrasings_reach_the_builder():
    from oolu.gateway.app import explicit_node_build_goal

    # Adjectives and ask-shaped forms are still explicit build requests.
    for message in (
        "build me a NEW node that cleans invoices",
        "create a weather-fetching node for daily emails",
        "i need a node that cleans invoices",
        "can you make a simple node to fetch the weather",
        "set up another node: archive receipts",
    ):
        assert explicit_node_build_goal(message), message
    # Ordinary work, questions, and definite references stay conversation.
    for message in (
        "build me a report on last month",
        "i need the node logs from yesterday",
        "what is a node?",
        "run my invoice node",
    ):
        assert explicit_node_build_goal(message) is None, message


def test_a_bare_build_request_asks_what_the_node_should_do(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor()
        response = _chat(app, ident, "build me a node")
        assert response.status == 200, response.body
        assert "tell me what the node should do" in response.body["reply"]
        assert desk.overview(principal="user-1", tenant="t1") == []
    finally:
        conn.close()


def test_yes_builds_the_node_and_refires_the_task(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        _chat(app, ident, "please tidy up my invoice files")
        # V1: the queued ask must be DRIVEN before any yes means anything
        # — the offer is written when the failed run reports.
        app.drive_queue()

        agreed = _chat(app, ident, "yes")
        assert agreed.status == 200, agreed.body
        reply = agreed.body["reply"]
        assert "Built a NEW node" in reply
        assert "own" in reply and "execution function" in reply
        assert agreed.body["actions"] == [{"tool": "build_node"}]
        assert agreed.body["source"] == "tool"
        # The consent was spent: no standing offer remains.
        assert app._growth_offers.get("t1", "user-1") is None
        # The node landed on the user's desk, born with its function.
        mine = desk.overview(principal="user-1", tenant="t1")
        assert [e.title for e in mine] == ["Normalize Invoice Csv Files"]
        # And the task re-fired THROUGH the node's own function: the run
        # executed the stored script on the script hand.
        assert agreed.body["run_id"] is not None
        [action] = script_exec.actions
        assert action.adapter == "script"
        assert "emit_result" in action.parameters["script"]
        assert action.parameters["goal"] == GOAL
    finally:
        conn.close()


def test_no_leaves_things_as_they_are(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        _chat(app, ident, "please tidy up my invoice files")
        app.drive_queue()  # the failed drive's report makes the offer

        declined = _chat(app, ident, "no thanks")
        assert "leaving it as is" in declined.body["reply"]
        assert app._growth_offers.get("t1", "user-1") is None
        assert desk.overview(principal="user-1", tenant="t1") == []
        assert script_exec.actions == []
    finally:
        conn.close()


def test_any_other_message_withdraws_the_offer(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        model = _speak_work(
            app, [TASK_TURN, '{"say": "Sunny all week!", "task": null}']
        )
        _chat(app, ident, "please tidy up my invoice files")
        app.drive_queue()  # the failed drive's report makes the offer

        # The user changes the subject: the offer is withdrawn — consent
        # detached from the question it answered is not consent — and the
        # conversation just continues.
        weather = _chat(app, ident, "how's the weather looking?")
        assert weather.body["reply"] == "Sunny all week!"
        assert app._growth_offers.get("t1", "user-1") is None

        # A later "yes" is an ordinary message, never a stale consent.
        assert len(model.calls) == 2
        assert desk.overview(principal="user-1", tenant="t1") == []
    finally:
        conn.close()


def test_no_offer_without_a_model_to_write_the_function(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        # Model-less: the message becomes the intent, the queued drive
        # refuses, and no offer is made — a build that cannot happen is
        # not offered.
        response = _chat(app, ident, GOAL)
        assert "On it!" in response.body["reply"]  # the fallback ACK
        app.drive_queue()
        report = _report(app)
        assert "The run failed — no capable node reaches that goal" in report
        assert "grow that missing piece" not in report
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_the_offer_survives_only_in_the_main_conversation(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, '{"say": "Hi there!", "task": null}'])
        _chat(app, ident, "please tidy up my invoice files")
        app.drive_queue()  # the failed drive's report makes the offer
        assert app._growth_offers.get("t1", "user-1") == ("build", GOAL, GOAL)
        # Another person's yes answers nothing — the offer is keyed to
        # the person who was asked.
        other = _chat(app, ident, "yes", principal="user-2")
        assert "Built" not in other.body["reply"]
        assert app._growth_offers.get("t1", "user-1") == ("build", GOAL, GOAL)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The twin guard: near-identical goals reuse the node, never mint a copy.      #
# --------------------------------------------------------------------------- #
PARAPHRASE = "normalize invoice csvs"
PARAPHRASE_TURN = '{"say": "On it!", "task": "' + PARAPHRASE + '"}'


def _built_first_node(app, ident):
    """Walk the plain growth flow once: the GOAL node exists afterwards."""
    _chat(app, ident, "please tidy up my invoice files")
    app.drive_queue()  # V1: the failed drive's report makes the offer
    agreed = _chat(app, ident, "yes")
    assert "Built a NEW node" in agreed.body["reply"]


def test_a_paraphrase_offers_reuse_instead_of_a_twin(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, PARAPHRASE_TURN])
        _built_first_node(app, ident)

        # The SAME work, said differently: the exact key misses, but the
        # twin guard now looks AT ASK TIME (V4) — the reply itself offers
        # to REUSE the node, before any doomed run is queued at all.
        runs_before = len(app._durable.runs.list())
        asked = _chat(app, ident, "tidy the invoice csvs for me")
        reply = asked.body["reply"]
        assert "answers for nearly this" in reply
        assert "Normalize Invoice Csv Files" in reply
        assert "grow that missing piece" not in reply
        # No run was spent asking the question: the offer fired FIRST.
        assert len(app._durable.runs.list()) == runs_before
        assert asked.body["run_id"] is None
        assert app._growth_offers.get("t1", "user-1") == (
            "reuse",
            GOAL,
            PARAPHRASE,
        )
    finally:
        conn.close()


def test_yes_to_reuse_runs_the_existing_node_and_mints_nothing(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, PARAPHRASE_TURN])
        _built_first_node(app, ident)
        runs_before = len(script_exec.actions)
        _chat(app, ident, "tidy the invoice csvs for me")
        # V4: the ask's own reply carried the reuse question — no drive.

        agreed = _chat(app, ident, "yes")
        reply = agreed.body["reply"]
        assert "already answers for this" in reply
        assert agreed.body["run_id"] is not None
        # The execution landed in the EXISTING node's log: the run executed
        # ITS stored function, under ITS goal — one node, one history.
        action = script_exec.actions[-1]
        assert action.parameters["goal"] == GOAL
        assert len(script_exec.actions) == runs_before + 1
        # And nothing new was minted: the desk still holds exactly one node.
        mine = desk.overview(principal="user-1", tenant="t1")
        assert [e.title for e in mine] == ["Normalize Invoice Csv Files"]
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_no_to_reuse_rolls_into_a_distinct_build_offer(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, PARAPHRASE_TURN])
        _built_first_node(app, ident)
        _chat(app, ident, "tidy the invoice csvs for me")
        # V4: the ask's own reply carried the reuse question — no drive.

        # "No" means this is different work — the plain build offer
        # follows, marked so the twin guard honors the user's answer.
        declined = _chat(app, ident, "no")
        assert "different work" in declined.body["reply"]
        assert PARAPHRASE in declined.body["reply"]
        assert app._growth_offers.get("t1", "user-1") == (
            "build_distinct",
            PARAPHRASE,
            PARAPHRASE,
        )

        # The consented build mints the SECOND node despite the near-match.
        agreed = _chat(app, ident, "yes")
        assert "Built a NEW node" in agreed.body["reply"]
        mine = desk.overview(principal="user-1", tenant="t1")
        assert len(mine) == 2
        assert script_exec.actions[-1].parameters["goal"] == PARAPHRASE
    finally:
        conn.close()


def test_the_build_door_refuses_a_twin_in_words(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        _built_first_node(app, ident)
        session = SimpleNamespace(tenant_id="t1", principal_id="user-1")

        refusal = app._build_function_node(session, PARAPHRASE)
        assert refusal.startswith("error:")
        assert "answers for nearly this" in refusal
        assert "Normalize Invoice Csv Files" in refusal
        assert "more distinctly" in refusal
        # The user's explicit "this is different work" opens the door.
        built = app._build_function_node(session, PARAPHRASE, allow_twin=True)
        assert "Built a NEW node" in built
        assert len(desk.overview(principal="user-1", tenant="t1")) == 2
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Durability: the question OoLu asked survives the process that asked it.      #
# --------------------------------------------------------------------------- #
def test_the_offer_survives_a_restart(tmp_path):
    from oolu.durable import GrowthOfferStore

    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        _chat(app, ident, "please tidy up my invoice files")
        app.drive_queue()  # V1: the offer is written at REPORT time
        assert app._growth_offers.get("t1", "user-1") == ("build", GOAL, GOAL)

        # The host restarts: a fresh store over the same durable connection
        # (what a new process sees) still holds the standing question.
        reborn = GrowthOfferStore(conn)
        assert reborn.get("t1", "user-1") == ("build", GOAL, GOAL)
        # The consent is spent exactly once, atomically — and spending it
        # in one process spends it in all of them.
        assert reborn.pop("t1", "user-1") == ("build", GOAL, GOAL)
        assert reborn.pop("t1", "user-1") is None
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_one_offer_per_person_and_the_newest_wins(tmp_path):
    from oolu.durable import DurableConnection, GrowthOfferStore

    conn = DurableConnection(tmp_path / "offers.db")
    try:
        offers = GrowthOfferStore(conn)
        offers.put("t1", "alice", kind="build", goal="old", original_goal="old")
        offers.put("t1", "alice", kind="reuse", goal="new", original_goal="ask")
        offers.put("t1", "bob", kind="build", goal="his", original_goal="his")
        # The newest question is the one on the table; people never share.
        assert offers.get("t1", "alice") == ("reuse", "new", "ask")
        assert offers.pop("t1", "bob") == ("build", "his", "his")
        assert offers.get("t1", "bob") is None
        assert offers.get("t2", "alice") is None  # tenant-walled
    finally:
        conn.close()


def test_a_message_to_a_friend_is_never_a_node_to_build(tmp_path):
    """Issue 9: 'reply to bob…' is delivered, never built for — the build
    door refuses in words, and the growth trigger never offers."""
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [])
        from test_http_gateway import NOW

        session = app._session_for(ident.token("user-1", "t1"), NOW)
        result = app._build_function_node(
            session, "reply to bob that I'm running late"
        )
        assert result.startswith("error:")
        assert "message to send" in result
        assert desk.overview(principal="user-1", tenant="t1") == []
        # The growth trigger stays silent on messaging intents too.
        say = app._offer_growth(
            "Hmm.", session, "tell bob the deploy finished", run=None
        )
        assert "yes" not in say.lower()
    finally:
        conn.close()


def test_the_operator_overview_lists_every_built_node(tmp_path):
    """GET /v1/nodes/overview: the tenant's nodes, whoever built them —
    the operator's field of view over a system its users created."""
    from oolu.identity import AuthorityGrant, Role

    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        ident.store.add_role(
            Role(
                tenant_id="t1", name="ops",
                permissions=frozenset({"users:manage"}),
            )
        )
        ident.store.add_grant(
            AuthorityGrant(
                tenant_id="t1", principal_id="ops-1", role_name="ops",
                granted_by="x",
            )
        )
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert built.status == 200, built.body

        # The builder's own view and the operator's agree on the node...
        walled = app.handle(
            _req("GET", "/v1/nodes/overview", token=ident.token("user-1", "t1"))
        )
        assert walled.status == 403  # building grants no oversight
        overview = app.handle(
            _req("GET", "/v1/nodes/overview", token=ident.token("ops-1", "t1"))
        )
        assert overview.status == 200, overview.body
        (item,) = overview.body["items"]
        assert item["title"] == "Normalize Invoice Csv Files"
        assert item["owner"] == "user-1"
        assert item["status"]
    finally:
        conn.close()


def test_the_inbox_carries_failures_not_only_approvals(tmp_path):
    """Issue: failed executions and failed node builds never reached
    the operator's inbox. The triggers are now defined events —
    workflow.failed (the engine's own) and node.build_failed (the
    build door's) — and GET /v1/inbox projects them beside the
    approval holds: the member sees their own, users:manage sees the
    tenant's, and every item leaves when its cause resolves."""
    from oolu.identity import AuthorityGrant, Role
    from oolu.skills.models import ExecutionOutcome, ExecutionStatus

    class _FailingExec:
        name = "script"

        def capabilities(self):
            return frozenset({"run"})

        def execute(self, action, *, idempotency_key):
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id=action.correlation_id,
                status=ExecutionStatus.FAILED,
                error="the sandbox exploded",
            )

        def cancel(self, idempotency_key):
            return None

    app, conn, ident, desk, _exec = _rig(tmp_path, script_exec=_FailingExec())
    try:
        ident.store.add_role(
            Role(
                tenant_id="t1", name="ops",
                permissions=frozenset({"users:manage"}),
            )
        )
        ident.store.add_grant(
            AuthorityGrant(
                tenant_id="t1", principal_id="ops-1", role_name="ops",
                granted_by="x",
            )
        )

        # A refused build: the author writes nothing usable, and the
        # DEFINED event lands on the hash-chained audit log.
        app._node_function_author = lambda tenant: FakeAuthor("I cannot.")
        refused = _chat(app, ident, "build me a node that polishes ledgers")
        assert refused.status == 200
        assert "couldn't build" in refused.body["reply"]
        failed_events = [
            e for e in app._durable.audit.records()
            if e.event_type == "node.build_failed"
        ]
        assert failed_events, "the refusal defines its event"
        assert failed_events[-1].payload["tenant"] == "t1"
        assert "polishes ledgers" in failed_events[-1].payload["goal"]

        # A failed execution: a healthy build, then the run explodes.
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert "Built a NEW node" in built.body["reply"]
        model = _FakeModel([TASK_TURN])
        app._tenant_model = lambda tenant: model
        ran = _chat(app, ident, "please " + GOAL)
        assert ran.status == 200
        # V1: the ask only queued the run — the explosion happens under
        # the worker's hands, so drive before reading the inbox.
        app.drive_queue()

        # The member's inbox: their own failed workflow, no tenant keys.
        mine = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("user-1", "t1"))
        )
        assert mine.status == 200, mine.body
        assert mine.body["scope"] == "mine"
        assert [r["intent"] for r in mine.body["failed_runs"]] == [GOAL]
        assert mine.body["failed_builds"] == []
        assert mine.body["holds"] == []

        # The operator's inbox: everything, named — and the refused
        # build stands while the published one never appears.
        ops = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("ops-1", "t1"))
        )
        assert ops.body["scope"] == "tenant"
        assert [r["submitted_by"] for r in ops.body["failed_runs"]] == ["user-1"]
        assert ops.body["failed_runs"][0]["phase"] in ("failed", "recovery") or \
            ops.body["failed_runs"][0]["awaiting"] == "incident"
        (build,) = ops.body["failed_builds"]
        assert "polishes ledgers" in build["goal"]
    finally:
        conn.close()
