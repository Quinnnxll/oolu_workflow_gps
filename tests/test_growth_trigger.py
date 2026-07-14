"""The growth trigger: a failure that asks, instead of a wall that repeats.

Borrowed from n8n's editor: when the workflow is missing the node it
needs, the answer is a proposal to ADD that node — never the same refusal
again. Exit gate: a chat task the machine cannot execute ends with an
in-conversation offer; the user's plain "yes" IS the consent (one goal,
one build) — the node is built through the same gated path as the
interact window's build and the task re-fires through the node's own
function; a "no" (or any other message) withdraws the offer; and consent
is never assumed: no model to write the function means no offer, and the
yes builds exactly the offered goal, nothing else.
"""

from __future__ import annotations

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

GOAL = "normalize invoice csv files"
TASK_TURN = '{"say": "On it!", "task": "' + GOAL + '"}'


def _rig(tmp_path):
    """A gateway whose engine executes ONLY a goal's own node function.

    The stub scenario would happily run any plain intent, so the machine
    under test is made honest: a goal with no node function raises the
    same ``cannot_execute`` a real install raises when no capable node
    reaches it — the exact wall the growth trigger exists to open."""
    ident = _Identity(tmp_path)
    brief, blueprint, executor, grounding = _autonomous()
    script_exec = _ScriptExec()

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
    )
    real = app._start_intent_run

    def picky(session, intent, **kwargs):
        if app._resolve_node_function(session, intent) is None:
            raise GatewayError(
                422, "cannot_execute", "no capable node reaches that goal"
            )
        return real(session, intent, **kwargs)

    app._start_intent_run = picky
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
        reply = response.body["reply"]
        assert "I can't run that on this machine yet" in reply
        assert "grow that missing piece" in reply
        assert GOAL in reply
        # The offer NAMES the node it will build (concise_name of the goal),
        # not just the goal sentence.
        assert "Normalize Invoice Csv" in reply
        assert response.body["run_id"] is None
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
        # Model-less: the message becomes the intent, the engine refuses,
        # and no offer is made — a build that cannot happen is not offered.
        response = _chat(app, ident, GOAL)
        reply = response.body["reply"]
        assert "I can't run that on this machine yet" in reply
        assert "grow that missing piece" not in reply
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_the_offer_survives_only_in_the_main_conversation(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, '{"say": "Hi there!", "task": null}'])
        _chat(app, ident, "please tidy up my invoice files")
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
    agreed = _chat(app, ident, "yes")
    assert "Built a NEW node" in agreed.body["reply"]


def test_a_paraphrase_offers_reuse_instead_of_a_twin(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN, PARAPHRASE_TURN])
        _built_first_node(app, ident)

        # The SAME work, said differently: the exact key misses, but the
        # twin guard finds the node and offers to REUSE it — not to build.
        response = _chat(app, ident, "tidy the invoice csvs for me")
        reply = response.body["reply"]
        assert "answers for nearly this" in reply
        assert "Normalize Invoice Csv Files" in reply
        assert "grow that missing piece" not in reply
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
    from types import SimpleNamespace

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
