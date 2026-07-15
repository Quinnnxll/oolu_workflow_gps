"""OoLu's outbox: messages to friends and nodes, on the user's behalf.

Exit gate: from the chat (Life) and a node's interact window (Work),
OoLu sends messages to friends, to the user's own nodes, and to nodes
under the same Supernode. The user names the destination in their own
words; resolution finds the best compatible target — exact name first,
then substring, ties broken by the user's own HABITS (who they actually
talk to) — and the backend delivers to the exact id: a friend gets a
real server message, a node gets a document in its own drawer. Every
delivery is marked as forwarded via OoLu from the user — the recipient
always sees WHO sent it; OoLu never impersonates.
"""

from __future__ import annotations

from test_http_gateway import _Identity

from oolu.chat import (
    VIA_OOLU_MARK,
    ChatAssistant,
    GatewayChatTools,
    NodeChatTools,
    resolve_message_target,
)
from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.nodeplace import (
    NodeAccountStore,
    RegistryStore,
    WorkDesk,
)
from oolu.nodeplace.models import Node, Visibility
from oolu.social import DirectMessageStore


# --------------------------------------------------------------------------- #
# Resolution: the best compatible target, never a guess.                       #
# --------------------------------------------------------------------------- #
def _t(kind: str, id: str, name: str, habit: float = 0.0) -> dict:
    return {"kind": kind, "id": id, "name": name, "habit": habit}


def test_exact_name_wins_over_every_habit():
    targets = [
        _t("friend", "bob", "bob", habit=0.0),
        _t("friend", "bobby", "bobby", habit=9.0),
    ]
    assert resolve_message_target(targets, "Bob") == [targets[0]]


def test_habits_break_substring_ties_only_when_clear():
    home = _t("friend", "anna-home", "anna-home", habit=2.0)
    work = _t("friend", "anna-work", "anna-work", habit=1.0)
    # The user talks to anna-home more recently: "anna" goes there.
    assert resolve_message_target([work, home], "anna") == [home]
    # Equal habits stay ambiguous — the caller asks, never guesses.
    tied = [_t("friend", "a1", "anna-home"), _t("friend", "a2", "anna-work")]
    assert len(resolve_message_target(tied, "anna")) == 2


def test_word_match_and_exact_lookup_fallback():
    node = _t("node", "n1", "Normalize Invoice Csv Files")
    assert resolve_message_target([node], "invoice files") == [node]
    # Nothing listed matches, but an exact account name still resolves —
    # a host is never a directory, yet a name you know reaches its person.
    found = _t("friend", "carol", "carol")
    assert resolve_message_target([], "carol", exact_lookup=lambda n: found) == [
        found
    ]
    assert resolve_message_target([], "", exact_lookup=lambda n: found) == []


# --------------------------------------------------------------------------- #
# The tools: real deliveries, by exact id, always attributed.                  #
# --------------------------------------------------------------------------- #
def _rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    ident = _Identity(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "carol"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    files = UserFileStore(conn)
    messages = DirectMessageStore(conn)
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    tools = GatewayChatTools(
        files,
        tenant="t1",
        principal="alice",
        desk=desk,
        accounts=accounts,
        direct_messages=messages,
    )
    return conn, tools, files, messages, registry, desk, accounts


def _add_node(registry, desk, *, title_words: str, principal="alice"):
    node = Node(
        noder_principal=principal,
        tenant_id="t1",
        skill_id=title_words.replace(" ", "."),
        visibility=Visibility.PUBLIC,
    )
    registry.add_node(node)
    desk.create_account(node.node_id, principal=principal, tenant="t1")
    return node


def test_a_friend_gets_a_real_marked_delivery(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        result = tools.deliver_message("friend", "bob", "lunch at noon?")
        assert result == "sent to bob"
        [message] = messages.between(tenant="t1", me="bob", peer="alice")
        # Bob sees WHO forwarded it — the mark, then the words verbatim.
        assert message.body == f"{VIA_OOLU_MARK} alice:\nlunch at noon?"
        assert message.sender == "alice" and message.recipient == "bob"
    finally:
        conn.close()


def test_a_node_gets_a_document_in_its_own_drawer(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        node = _add_node(registry, desk, title_words="invoice cleaner")
        [target] = [
            t for t in tools.message_targets() if t["kind"] == "node"
        ]
        result = tools.deliver_message("node", target["id"], "check row 42")
        assert "delivered to the node's drawer" in result
        [doc] = files.list(tenant="t1", node_id=node.node_id)
        assert doc.folder == "messages"
        assert f"{VIA_OOLU_MARK} alice" in doc.content
        assert "check row 42" in doc.content
    finally:
        conn.close()


def test_unreachable_destinations_are_refused(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        # A node id nobody resolved to — not on this desk, not reachable.
        refused = tools.deliver_message("node", "ghost-node", "hello?")
        assert refused.startswith("error:")
        # A disabled account stops resolving by exact name.
        accounts.set_disabled("carol", True)
        assert tools.exact_friend("carol") is None
        # And yourself is never a destination.
        assert tools.exact_friend("alice") is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The chat commands: deterministic floor and the model's tool.                 #
# --------------------------------------------------------------------------- #
def test_send_to_command_resolves_by_habit_and_delivers(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        # Alice talks to bob (recently) and carol (long ago): habits.
        messages.send(tenant="t1", sender="carol", recipient="alice", body="old")
        messages.send(tenant="t1", sender="bob", recipient="alice", body="new")
        assistant = ChatAssistant()

        turn = assistant.respond(
            "send the go to market plan to bob", tools=tools
        )
        assert turn.source == "tool"
        assert "Sent to bob" in turn.say
        assert "forwarded via OoLu" in turn.say
        assert turn.actions == [{"tool": "send_message", "name": "bob"}]
        thread = messages.between(tenant="t1", me="bob", peer="alice")
        # The greedy split: a message containing "to" still lands whole.
        assert thread[-1].body == f"{VIA_OOLU_MARK} alice:\nthe go to market plan"

        # "message <name>: <words>" is the other shape.
        told = assistant.respond("message carol: see you at five", tools=tools)
        assert "Sent to carol" in told.say
    finally:
        conn.close()


def test_a_name_nothing_matches_falls_through_to_work(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        assistant = ChatAssistant()
        turn = assistant.respond("send the report to accounting", tools=tools)
        # No friend, node, or exact account: not a message command after
        # all — the sentence becomes the run intent, exactly as before.
        assert turn.source == "intent"
        assert turn.task == "send the report to accounting"
    finally:
        conn.close()


def test_the_model_sends_through_the_same_tool(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        class _Model:
            def __init__(self):
                self.replies = [
                    '{"tool": "send_message", "args": {"to": "bob", "text": "on my way"}}',
                    '{"say": "Told Bob you\'re on your way!", "task": null}',
                ]

            def reply(self, _messages):
                return self.replies.pop(0)

        assistant = ChatAssistant(model=_Model())
        # A conversational shape (the deterministic patterns skip a
        # "could you…?" ask) reaches the model, whose send_message tool
        # goes through the same delivery door.
        turn = assistant.respond(
            "could you let bob know I'm on my way?", tools=tools
        )
        assert turn.actions == [{"tool": "send_message", "name": "bob"}]
        [message] = messages.between(tenant="t1", me="bob", peer="alice")
        assert message.body == f"{VIA_OOLU_MARK} alice:\non my way"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The Work window: the node's org is reachable too.                            #
# --------------------------------------------------------------------------- #
def test_siblings_under_the_same_supernode_are_reachable(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        # A Supernode with two members: exporting and cleaning.
        supernode = Node(
            noder_principal="alice",
            tenant_id="t1",
            skill_id="finance.division",
            visibility=Visibility.PUBLIC,
        )
        registry.add_node(supernode)
        desk.create_account(
            supernode.node_id, principal="alice", tenant="t1", is_supernode=True
        )
        members = {}
        for words in ("raw exporter", "invoice cleaner"):
            node = Node(
                noder_principal="alice",
                tenant_id="t1",
                skill_id=words.replace(" ", "."),
                visibility=Visibility.PUBLIC,
            )
            registry.add_node(node)
            desk.create_account(
                node.node_id,
                principal="alice",
                tenant="t1",
                supernode_id=supernode.node_id,
                authority_level=1,
            )
            members[words] = node

        exporter = members["raw exporter"]
        cleaner = members["invoice cleaner"]
        assert [m["node_id"] for m in desk.siblings(exporter.node_id, tenant="t1")] == [
            cleaner.node_id
        ]

        node_tools = NodeChatTools(
            files,
            tenant="t1",
            principal="alice",
            desk=desk,
            accounts=accounts,
            direct_messages=messages,
            node={"node_id": exporter.node_id, "title": "Raw Exporter"},
            holds_list=lambda: [],
            holds_decide=lambda *a: "done",
            holds_reply=lambda *a: "done",
            builder=lambda goal: "error: not here",
        )
        assistant = ChatAssistant()
        turn = assistant.respond("send batch 7 is clean to invoice cleaner", tools=node_tools)
        assert "Sent to Invoice Cleaner" in turn.say, turn.say
        [doc] = files.list(tenant="t1", node_id=cleaner.node_id)
        assert doc.folder == "messages"
        assert f"{VIA_OOLU_MARK} alice" in doc.content
        assert "batch 7 is clean" in doc.content
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Issue 9: a message-shaped sentence delivers DIRECTLY — no model, no task,    #
# no node. WHO comes from the user's real friends and nodes; WHAT is the       #
# user's own words, marked as forwarded via OoLu.                              #
# --------------------------------------------------------------------------- #
class _NeverSpeaks:
    def reply(self, _messages):  # pragma: no cover - the point is silence
        raise AssertionError("a message command must not reach the model")


def test_message_shaped_sentences_deliver_without_the_model(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        assistant = ChatAssistant(model=_NeverSpeaks())
        for sentence, expected in (
            ("tell bob I'll be late", "I'll be late"),
            ("reply to bob that we're still coming", "we're still coming"),
            ("let bob know the meeting moved to 4", "the meeting moved to 4"),
            ("send the go to market plan to bob", "the go to market plan"),
        ):
            turn = assistant.respond(sentence, tools=tools)
            # Never a task, never a node — a delivery, confirmed.
            assert turn.task is None and turn.source == "tool", sentence
            assert turn.actions == [{"tool": "send_message", "name": "bob"}]
            latest = messages.between(tenant="t1", me="bob", peer="alice")[-1]
            assert latest.body == f"{VIA_OOLU_MARK} alice:\n{expected}"
    finally:
        conn.close()


def test_a_name_matching_nobody_still_falls_through_to_the_model(tmp_path):
    conn, tools, files, messages, registry, desk, accounts = _rig(tmp_path)
    try:
        class _Chatty:
            def reply(self, _messages):
                return '{"say": "Here is a joke!", "task": null}'

        turn = ChatAssistant(model=_Chatty()).respond(
            "tell me a joke", tools=tools
        )
        assert turn.say == "Here is a joke!"
        assert messages.between(tenant="t1", me="bob", peer="alice") == []
    finally:
        conn.close()


def test_messaging_intents_never_mint_nodes():
    from oolu.chat import messaging_intent

    assert messaging_intent("reply to quinnnxll that I'm on my way")
    assert messaging_intent("tell mom the dinner is at eight")
    assert messaging_intent("let kai know that the deploy finished")
    assert messaging_intent("send the report to bob")
    # Ordinary work stays work.
    assert not messaging_intent("normalize invoice csv files")
    assert not messaging_intent("convert the report to pdf")
    assert not messaging_intent("")
