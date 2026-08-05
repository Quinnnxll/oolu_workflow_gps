"""Find the standing node first (V4).

Exit gate (node-vitality-plan, phase V4): a reminder-work ask with a
standing reminder node SURFACES that node instead of silently routing to
the built-in service; a re-ask of a program node's goal finds the node
through its goal-derived alias; and a capability standing in the tenant
registry is found without naming the node — at the build door, at ask
time, and through the model's find_nodes hand.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_growth_trigger import (
    GOAL,
    PARAPHRASE,
    PARAPHRASE_TURN,
    _chat,
    _rig,
    _speak_work,
)
from test_http_gateway import NOW
from test_node_interact import FakeAuthor
from test_program_substrate import ENTRY, LIB, SPEC, _program_app
from test_task_loop import _settings
from test_verify_at_birth import GOOD

from oolu.chat import reminder_shaped
from oolu.gateway.app import TASK_DELEGATION_KEY
from oolu.reminders import ReminderStore

# A function whose CAPABILITY vocabulary (the derived fn: tokens) speaks
# reminder-work while its goal sentence shares no word with the asks that
# should find it — the exit gate's "found without naming the node".
CAPABILITY_FN = (
    "1. Schedule the check.\n"
    'IO: {"inputs": [], "outputs": [{"name": "note", "type": "str"}]}\n'
    "```python\n"
    "from _oolu_runtime import emit_result\n"
    "def schedule_reminder():\n"
    "    return ' '.join(['oven', 'check', 'scheduled'])\n"
    "emit_result({'note': schedule_reminder()})\n"
    "```\n"
)


def _session(principal="user-1"):
    return SimpleNamespace(tenant_id="t1", principal_id=principal)


def _built_node(app, ident, goal, *, principal="user-1", answer=GOOD):
    """Publish a function node through the explicit build door — static
    walls only (no script runtime), the same bench V3's loop used."""
    app._contract_executors = {}
    app._node_function_author = lambda tenant: FakeAuthor(answer)
    words = app._build_function_node(_session(principal), goal)
    assert "Built a NEW node" in words, words
    nodes = app._nodeplace.list_own_nodes(
        noder_principal=principal, tenant_id="t1"
    )
    return nodes[0].node_id


def _audits(app, event_type):
    return [
        r
        for r in app._durable.audit.records()
        if r.event_type == event_type
    ]


# --------------------------------------------------------------------------- #
# The deterministic door's shapes, exported for the which-door question.       #
# --------------------------------------------------------------------------- #
def test_reminder_shaped_matches_the_creation_shapes_only():
    assert reminder_shaped("remind me to check the oven in 20 minutes")
    assert reminder_shaped("remind me at 3pm to stretch")
    assert reminder_shaped("Remind me in 2 hours to drink water.")
    # Reads are not creations, and chat is neither.
    assert not reminder_shaped("my reminders")
    assert not reminder_shaped("show my reminders")
    assert not reminder_shaped("what's a good reminder app?")
    assert not reminder_shaped("")


# --------------------------------------------------------------------------- #
# Built-ins yield: the reported defect, pinned.                                #
# --------------------------------------------------------------------------- #
def test_a_reminder_ask_surfaces_the_standing_node_first(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._reminders = ReminderStore(conn, clock=lambda: NOW)
        _built_node(app, ident, "reminds me about important things")

        asked = _chat(app, ident, "remind me to check the oven in 20 minutes")
        reply = asked.body["reply"]
        # The ask SURFACES the node — a question, not a silent row.
        assert "node can take this" in reply
        assert "built-in" in reply
        assert asked.body["run_id"] is None
        # Nothing was filed and nothing ran: the choice is the user's.
        assert app._reminders.upcoming(tenant="t1", principal="user-1") == []
        offer = app._growth_offers.get("t1", "user-1")
        assert offer == (
            "reminder_route",
            "remind me to check the oven in 20 minutes",
            "remind me to check the oven in 20 minutes",
        )
    finally:
        conn.close()


def test_yes_routes_the_ask_through_the_standing_node(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._reminders = ReminderStore(conn, clock=lambda: NOW)
        node_id = _built_node(app, ident, "reminds me about important things")
        ask = "remind me to check the oven in 20 minutes"
        _chat(app, ident, ask)

        agreed = _chat(app, ident, "yes")
        assert "Running" in agreed.body["reply"]
        run_id = agreed.body["run_id"]
        assert run_id is not None
        # The run keeps the USER'S words as its intent and is FORCED
        # through the chosen node's own function.
        state = app._durable.get(run_id)
        assert state.contract.intent == ask
        function = state.contract.metadata.get("node_function")
        assert function is not None and function["node_id"] == node_id
        # The plain row was NOT filed — the node took the ask.
        assert app._reminders.upcoming(tenant="t1", principal="user-1") == []
        routes = _audits(app, "node.builtin_route")
        assert routes and routes[-1].payload["choice"] == "node"
        # And the worker really executes THROUGH the node's function —
        # the promise is a run in the node's log, not queued words.
        app.drive_queue()
        expected = app._function_skill_id(
            "t1", "reminds me about important things"
        )
        action = script_exec.actions[-1]
        assert action.parameters["node_key"] == f"node:{expected}"
    finally:
        conn.close()


def test_no_files_the_plain_row_through_the_old_door(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._reminders = ReminderStore(conn, clock=lambda: NOW)
        _built_node(app, ident, "reminds me about important things")
        _chat(app, ident, "remind me to check the oven in 20 minutes")

        declined = _chat(app, ident, "no")
        assert "bring it up here" in declined.body["reply"]
        rows = app._reminders.upcoming(tenant="t1", principal="user-1")
        assert [r.text for r in rows] == ["check the oven"]
        routes = _audits(app, "node.builtin_route")
        assert routes and routes[-1].payload["choice"] == "builtin"
    finally:
        conn.close()


def test_without_a_standing_node_the_builtin_stays_deterministic(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._reminders = ReminderStore(conn, clock=lambda: NOW)
        asked = _chat(app, ident, "remind me to check the oven in 20 minutes")
        # No node stands, so no question: the row is filed directly and
        # the confirmation reads from the store — the old door, intact.
        assert "bring it up here" in asked.body["reply"]
        rows = app._reminders.upcoming(tenant="t1", principal="user-1")
        assert [r.text for r in rows] == ["check the oven"]
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Program nodes become findable: the goal-derived alias row.                   #
# --------------------------------------------------------------------------- #
def test_a_program_goal_re_ask_resolves_through_the_alias(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = SimpleNamespace(tenant_id="t1", principal_id="noder")
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result
    function = app._resolve_node_function(session, "compute the answer")
    assert function is not None
    assert function["node_id"] == result["node_id"]
    assert function["skill_id"].startswith("program-")
    # Another member's session never resolves someone else's alias.
    other = SimpleNamespace(tenant_id="t1", principal_id="stranger")
    assert app._resolve_node_function(other, "compute the answer") is None
    conn.close()


def test_a_rebuilt_program_goal_repoints_the_alias(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = SimpleNamespace(tenant_id="t1", principal_id="noder")
    first = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    second = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY + "\n# rebuilt\n",
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert first["ok"] and second["ok"]
    assert first["node_id"] != second["node_id"]
    function = app._resolve_node_function(session, "compute the answer")
    assert function is not None and function["node_id"] == second["node_id"]
    conn.close()


def test_the_similar_scan_sees_program_nodes(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    session = SimpleNamespace(tenant_id="t1", principal_id="noder")
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result
    # A reworded goal finds the program node as a near-twin…
    similar = app._find_similar_function_node(session, "compute the answers")
    assert similar is not None and similar["node_id"] == result["node_id"]
    # …but the EXACT goal is resolution's find, never a "twin".
    assert app._find_similar_function_node(session, "compute the answer") is None
    conn.close()


# --------------------------------------------------------------------------- #
# Search-at-ask: standing work answers before a bare contract queues.          #
# --------------------------------------------------------------------------- #
def test_under_delegation_a_near_twin_runs_at_ask(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        settings.set("t1", TASK_DELEGATION_KEY, True, "user-1")
        _built_node(app, ident, GOAL)
        _speak_work(app, [PARAPHRASE_TURN])

        asked = _chat(app, ident, "tidy the invoice csvs for me")
        reply = asked.body["reply"]
        assert "You already have" in reply
        assert asked.body["run_id"] is not None
        # The queued run routes through the standing node's OWN goal.
        state = app._durable.get(asked.body["run_id"])
        assert state.contract.intent == GOAL
        decisions = _audits(app, "node.reuse_decision")
        assert decisions and decisions[-1].payload["decision"] == "reuse_at_ask"
        # The worker drives it into the node's log.
        app.drive_queue()
        assert script_exec.actions[-1].parameters["goal"] == GOAL
    finally:
        conn.close()


def test_at_ask_a_tenant_kin_is_named_not_run(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        # Even a DELEGATED asker gets the pointer, never a silent
        # duplicate: surfacing outranks the auto-build.
        settings.set("t1", TASK_DELEGATION_KEY, True, "user-2")
        _built_node(app, ident, GOAL, principal="user-1")
        runs_before = len(app._durable.runs.list())
        _speak_work(app, [PARAPHRASE_TURN])

        asked = _chat(app, ident, "tidy the invoice csvs for me", principal="user-2")
        reply = asked.body["reply"]
        assert "in your tenant's registry" in reply
        assert "by user-1" in reply
        assert asked.body["run_id"] is None
        assert len(app._durable.runs.list()) == runs_before
        # And nothing was minted on the asker's own desk.
        assert (
            app._nodeplace.list_own_nodes(
                noder_principal="user-2", tenant_id="t1"
            )
            == []
        )
        # The naming is never a dead end: the consent door stands.
        assert app._growth_offers.get("t1", "user-2") == (
            "build_despite_kin",
            PARAPHRASE,
            PARAPHRASE,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The build door's field widens to the tenant registry.                        #
# --------------------------------------------------------------------------- #
def test_the_build_door_names_a_tenant_members_standing_kin(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _built_node(app, ident, GOAL, principal="user-1")

        refusal = app._build_function_node(_session("user-2"), PARAPHRASE)
        assert refusal.startswith("error:")
        assert "tenant's registry" in refusal
        assert "by user-1" in refusal
        # The explicit "different work" answer still builds a private one,
        # with the considered node on the log.
        built = app._build_function_node(
            _session("user-2"), PARAPHRASE, allow_twin=True
        )
        assert "Built a NEW node" in built
        decisions = _audits(app, "node.reuse_decision")
        assert decisions[-1].payload["considered_owner"] == "user-1"
    finally:
        conn.close()


def test_a_tenant_capability_is_found_without_naming_the_node(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        # The node's goal shares no word with the asks that should find
        # it — only its derived fn: tokens (schedule_reminder) speak.
        _built_node(
            app,
            ident,
            "keep the kitchen timer honest",
            principal="user-1",
            answer=CAPABILITY_FN,
        )
        hits = app._find_tenant_capability(
            _session("user-2"), "schedule a reminder for me"
        )
        assert hits, "the capability search should reach the fn: tokens"
        assert hits[0]["owner"] == "user-1"
        assert "kitchen" in hits[0]["title"].lower()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The model can look: the find_nodes hand.                                     #
# --------------------------------------------------------------------------- #
def test_find_nodes_speaks_own_and_tenant_standing_work(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _built_node(app, ident, GOAL, principal="user-1")
        own = app._find_nodes_words(_session("user-1"), "normalize invoices")
        assert "yours" in own and "Normalize Invoice Csv Files" in own
        theirs = app._find_nodes_words(_session("user-2"), "normalize invoices")
        assert "in your tenant's registry by user-1" in theirs
        nothing = app._find_nodes_words(
            _session("user-1"), "launch a weather balloon"
        )
        assert "no standing node matches" in nothing
        assert app._find_nodes_words(_session("user-1"), "") .startswith(
            "error:"
        )
    finally:
        conn.close()


def test_find_nodes_dispatches_as_a_chat_tool(tmp_path):
    from oolu.chat import GatewayChatTools, _run_tool, _ToolCall
    from oolu.durable import DurableConnection, UserFileStore

    conn = DurableConnection(tmp_path / "tools.db")
    try:
        tools = GatewayChatTools(
            UserFileStore(conn),
            tenant="t1",
            principal="user-1",
            node_search=lambda query: f"“Timer” — yours, built for “{query}”",
        )
        words, action = _run_tool(
            tools, _ToolCall(name="find_nodes", args={"query": "timers"})
        )
        assert words == "“Timer” — yours, built for “timers”"
        assert action == {"tool": "find_nodes"}
        # Without the hand, the tool answers in honest words.
        bare = GatewayChatTools(
            UserFileStore(conn), tenant="t1", principal="user-1"
        )
        refused, _action = _run_tool(
            bare, _ToolCall(name="find_nodes", args={"query": "timers"})
        )
        assert refused.startswith("error:")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The alias store itself.                                                      #
# --------------------------------------------------------------------------- #
def test_the_alias_survives_in_the_registry_store(tmp_path):
    from oolu.durable import DurableConnection
    from oolu.nodeplace import NodeplaceService, RegistryStore
    from oolu.nodeplace.models import Node, Visibility

    conn = DurableConnection(tmp_path / "alias.db")
    try:
        service = NodeplaceService(RegistryStore(conn))
        node = Node(
            noder_principal="user-1",
            tenant_id="t1",
            skill_id="program-abc123",
            visibility=Visibility.PUBLIC,
        )
        service._store.add_node(node)
        service.add_alias("t1", "user-1", "fn-deadbeef", node.node_id)
        found = service.node_by_alias("t1", "user-1", "fn-deadbeef")
        assert found is not None and found.node_id == node.node_id
        # Owner-scoped: another tenant OR another member sees nothing.
        assert service.node_by_alias("t2", "user-1", "fn-deadbeef") is None
        assert service.node_by_alias("t1", "user-2", "fn-deadbeef") is None
        # And unknown aliases answer honestly.
        assert service.node_by_alias("t1", "user-1", "fn-unknown") is None
    finally:
        conn.close()


def test_a_siblings_same_goal_publish_never_breaks_the_owners_alias(tmp_path):
    """The alias is OWNER-keyed: Bob publishing Alice's goal writes HIS
    row — Alice's re-ask still resolves her node, and neither sees the
    other's as a twin of their own exact goal."""
    app, conn, ident, runner = _program_app(tmp_path)
    alice = SimpleNamespace(tenant_id="t1", principal_id="alice")
    bob = SimpleNamespace(tenant_id="t1", principal_id="bob")
    io = {"outputs": [{"name": "summary", "type": "str"}]}
    first = app.publish_program_node(
        alice,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io=io,
    )
    second = app.publish_program_node(
        bob,
        goal="compute the answer",
        script=ENTRY + "\n# bob's own\n",
        files=dict(LIB),
        program=SPEC,
        io=io,
    )
    assert first["ok"] and second["ok"]
    mine = app._resolve_node_function(alice, "compute the answer")
    theirs = app._resolve_node_function(bob, "compute the answer")
    assert mine is not None and mine["node_id"] == first["node_id"]
    assert theirs is not None and theirs["node_id"] == second["node_id"]
    # The exact goal is resolution's find for each owner — never a twin.
    assert app._find_similar_function_node(alice, "compute the answer") is None
    conn.close()


def test_a_pre_alias_program_node_reuse_still_runs_through_the_node(tmp_path):
    """A program node published before the alias table exists has no
    alias row — the reuse 'yes' must still FORCE the route through the
    node (via_node), never ride its own words on a bare plan."""
    app, conn, ident, runner = _program_app(tmp_path)
    session = SimpleNamespace(tenant_id="t1", principal_id="noder")
    result = app.publish_program_node(
        session,
        goal="compute the answer",
        script=ENTRY,
        files=dict(LIB),
        program=SPEC,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result
    with conn.transaction() as db:
        db.execute("DELETE FROM node_aliases")  # the pre-V4 world
    assert app._resolve_node_function(session, "compute the answer") is None
    similar = app._find_similar_function_node(session, "compute the answer")
    assert similar is not None and similar["node_id"] == result["node_id"]

    turn, run = app._reuse_node_and_run(session, "compute the answer")
    assert run is not None
    state = app._durable.get(run["run_id"])
    function = state.contract.metadata.get("node_function")
    assert function is not None and function["node_id"] == result["node_id"]
    assert "already answers for this" in turn.say
    conn.close()


def test_unlisted_nodes_stay_out_of_the_tenant_registry(tmp_path):
    from oolu.nodeplace.models import Visibility

    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        node_id = _built_node(app, ident, GOAL, principal="user-1")
        assert app._find_tenant_capability(_session("user-2"), PARAPHRASE)
        registry = app._nodeplace._store
        node = registry.get_node(node_id)
        registry.update_node(
            node.model_copy(update={"visibility": Visibility.UNLISTED})
        )
        # Unlisted keeps its word to tenant siblings too.
        assert app._find_tenant_capability(_session("user-2"), PARAPHRASE) == []
    finally:
        conn.close()


def test_yes_after_the_kin_naming_builds_your_own(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _built_node(app, ident, GOAL, principal="user-1")
        _speak_work(app, [PARAPHRASE_TURN])
        asked = _chat(
            app, ident, "tidy the invoice csvs for me", principal="user-2"
        )
        assert "build your own" in asked.body["reply"]
        agreed = _chat(app, ident, "yes", principal="user-2")
        assert "Built a NEW node" in agreed.body["reply"]
        assert app._nodeplace.list_own_nodes(
            noder_principal="user-2", tenant_id="t1"
        )
    finally:
        conn.close()


def test_the_explicit_build_door_stands_the_kin_offer(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _built_node(app, ident, GOAL, principal="user-1")
        refused = _chat(
            app,
            ident,
            f"build me a node that can {PARAPHRASE}",
            principal="user-2",
        )
        assert "I couldn't build that node" in refused.body["reply"]
        assert "tenant's registry" in refused.body["reply"]
        # The refusal's promised yes-door really builds.
        agreed = _chat(app, ident, "yes", principal="user-2")
        assert "Built a NEW node" in agreed.body["reply"]
    finally:
        conn.close()


def test_the_seeded_starter_shelf_never_hijacks_the_reminder_ask(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._reminders = ReminderStore(conn, clock=lambda: NOW)
        seeded = app._seed_starter_shelf("t1", "user-1")
        assert seeded, "the shelf should land"
        mine = desk.overview(principal="user-1", tenant="t1")
        assert any("remind" in e.title.lower() for e in mine)
        # The SEEDED reminders node earns no question — the built-in
        # stays deterministic; the question belongs to built nodes.
        asked = _chat(app, ident, "remind me to check the oven in 20 minutes")
        assert "bring it up here" in asked.body["reply"]
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_a_partial_capability_echo_never_blocks_work(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _built_node(
            app,
            ident,
            "keep the kitchen timer honest",
            principal="user-1",
            answer=CAPABILITY_FN,
        )
        # Guard posture: 2 of 3 asked words echoing a capability is NOT
        # "the same work" — nothing is blocked or rerouted by it.
        assert (
            app._find_tenant_capability(
                _session("user-2"), "create a reminder for standup"
            )
            == []
        )
        # Search posture still surfaces it for the model's find_nodes.
        words = app._find_nodes_words(
            _session("user-2"), "create a reminder for standup"
        )
        assert "Kitchen" in words
    finally:
        conn.close()
