"""The representative gathers what it needs FROM THE USER, in conversation.

A reply the model cannot honestly write is never a draft full of questions
for the peer: the model answers NEED_INFO and the questions go to the USER
— in their own OoLu conversation, one at a time, with no reply forced the
moment the toggle flips. The user's answer redrafts; their "ignore it"
marks the message read. And a DISCARDED draft postpones instead of
burying: the same message is drafted again when the peer writes anew, when
the representative is toggled back on, or after a day still unread.
"""

from __future__ import annotations

from test_http_gateway import _app, _req
from test_representative import _Parrot

from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.representative import RepresentativeEngine, RepresentativeStore
from oolu.representative.models import REDRAFT_AFTER_S, PersonaCard
from oolu.representative.persona import build_system_prompt
from oolu.social import AssistantHistoryStore, DirectMessageStore


class _Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now


class _Script:
    """A model that answers from a script, one reply per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[list[dict]] = []

    def reply(self, messages):
        self.calls.append(messages)
        return self.answers.pop(0) if self.answers else "ok"


def _engine(model=None, clock=None) -> RepresentativeEngine:
    clock = clock or _Clock()
    return RepresentativeEngine(
        RepresentativeStore(":memory:", clock=clock), model=model, clock=clock
    )


# --------------------------------------------------------------------------- #
# The persona: questions are for the user, never words in the draft.           #
# --------------------------------------------------------------------------- #
def test_the_prompt_forbids_asking_the_user_inside_the_draft():
    prompt = build_system_prompt(PersonaCard(display_name="quinn"), [])
    assert "never to quinn" in prompt
    assert "NEED_INFO:" in prompt
    assert "questions inside the reply" in prompt
    assert "Never ask quinn" in prompt
    # The user's answer rides the prompt on a redraft.
    informed = build_system_prompt(
        PersonaCard(display_name="quinn"),
        [],
        info_note="the gathering is Saturday's BBQ; say we're coming",
    )
    assert "Saturday's BBQ" in informed
    assert "do not ask again" in informed


# --------------------------------------------------------------------------- #
# The engine: NEED_INFO files a WAITING draft, the answer supersedes it.       #
# --------------------------------------------------------------------------- #
def test_need_info_files_a_waiting_draft_not_a_pending_one():
    engine = _engine(
        model=_Parrot("NEED_INFO: which gathering, and what are your plans?")
    )
    engine.configure("s1", mode="draft")
    draft = engine.draft(
        "s1",
        conversation_id="bob",
        inbound_text="are you coming to the gathering?",
        display_name="quinn",
    )
    assert draft.status == "needs_info"
    assert draft.generated_text == "which gathering, and what are your plans?"
    # NOT in the review inbox — the peer-facing block never shows questions.
    assert engine.pending("s1") == []
    assert [w.draft_id for w in engine.waiting("s1")] == [draft.draft_id]
    assert engine.status("s1")["drafts_waiting"] == 1
    assert engine.status("s1")["drafts_pending"] == 0
    # The message counts as handled: the sweep will not re-draft it.
    assert engine.has_draft_for(
        "s1", "bob", "are you coming to the gathering?"
    )


def test_the_users_answer_redrafts_and_supersedes_the_question():
    model = _Script(
        "NEED_INFO: which gathering?",
        "yes — we'll be at the BBQ on Saturday!",
    )
    engine = _engine(model=model)
    engine.configure("s1", mode="draft")
    asking = engine.draft(
        "s1",
        conversation_id="bob",
        inbound_text="coming to the gathering?",
        display_name="quinn",
    )
    answered = engine.draft(
        "s1",
        conversation_id="bob",
        inbound_text="coming to the gathering?",
        display_name="quinn",
        extra_context="it's Saturday's BBQ, tell him we're in",
    )
    # The user's information rode the prompt...
    assert "Saturday's BBQ" in model.calls[1][0]["content"]
    # ...the fresh draft is a normal reviewable one...
    assert answered.status == "pending"
    assert [d.draft_id for d in engine.pending("s1")] == [answered.draft_id]
    # ...and the question it answered is settled, not a second ask.
    assert engine.waiting("s1") == []
    assert engine.get("s1", asking.draft_id).status == "answered"


def test_ignore_settles_both_kinds_and_is_not_a_quality_verdict():
    engine = _engine(model=_Parrot("drafted words"))
    engine.configure("s1", mode="draft")
    draft = engine.draft(
        "s1", conversation_id="bob", inbound_text="hey", display_name="q"
    )
    ignored = engine.decide("s1", draft.draft_id, action="ignore")
    assert ignored.status == "ignored"
    # Ignoring never erodes (or feeds) the accept-rate.
    assert engine.accept_rate("s1") is None
    # An ignored message stays ignored: no redraft, ever.
    assert engine.has_draft_for("s1", "bob", "hey")
    # ignore_conversation settles a WAITING question the same way.
    asking = _engine(model=_Parrot("NEED_INFO: what do I say?"))
    asking.configure("s2", mode="draft")
    asking.draft(
        "s2", conversation_id="eve", inbound_text="so?", display_name="q"
    )
    assert asking.ignore_conversation("s2", "eve") == 1
    assert asking.waiting("s2") == []


# --------------------------------------------------------------------------- #
# Discard postpones, never buries.                                             #
# --------------------------------------------------------------------------- #
def test_a_discard_blocks_only_until_a_day_passes():
    clock = _Clock()
    engine = _engine(model=_Parrot("drafted words"), clock=clock)
    engine.configure("s1", mode="draft")
    draft = engine.draft(
        "s1", conversation_id="bob", inbound_text="ping", display_name="q"
    )
    engine.decide("s1", draft.draft_id, action="discard")
    # Freshly discarded: the sweep must not immediately redraft.
    assert engine.has_draft_for("s1", "bob", "ping")
    # A day later, still unread: the message earns a fresh draft.
    clock.now += REDRAFT_AFTER_S
    assert not engine.has_draft_for("s1", "bob", "ping")


def test_toggling_the_representative_back_on_forgives_a_discard():
    clock = _Clock()
    engine = _engine(model=_Parrot("drafted words"), clock=clock)
    engine.configure("s1", mode="draft")
    draft = engine.draft(
        "s1", conversation_id="bob", inbound_text="ping", display_name="q"
    )
    clock.now += 10
    engine.decide("s1", draft.draft_id, action="discard")
    assert engine.has_draft_for("s1", "bob", "ping")
    # Off and on again: the user asked for a fresh pass.
    clock.now += 10
    engine.configure("s1", mode="off")
    clock.now += 10
    engine.configure("s1", mode="draft")
    assert not engine.has_draft_for("s1", "bob", "ping")
    # Sent and ignored verdicts are NOT forgiven by a toggle.
    sent = engine.draft(
        "s1", conversation_id="bob", inbound_text="pong", display_name="q"
    )
    engine.decide("s1", sent.draft_id, action="send")
    engine.configure("s1", mode="off")
    engine.configure("s1", mode="draft")
    assert engine.has_draft_for("s1", "bob", "pong")


# --------------------------------------------------------------------------- #
# The gateway: the question lands in the OoLu conversation, once.              #
# --------------------------------------------------------------------------- #
def _host(tmp_path, model=None):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    engine = RepresentativeEngine(RepresentativeStore(":memory:"), model=model)
    history = AssistantHistoryStore(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        direct_messages=DirectMessageStore(conn),
        representative=engine,
        assistant_history=history,
    )
    return gateway, conn, ident, history


def test_the_sweep_asks_the_user_in_their_own_conversation(tmp_path):
    gateway, conn, ident, history = _host(
        tmp_path, model=_Parrot("NEED_INFO: which gathering do they mean?")
    )
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )
    assert gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "are you coming to the gathering?"})
    ).status == 201

    swept = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice)
    )
    assert swept.status == 200, swept.body
    # No peer-facing draft was filed — the questions went to the USER.
    assert swept.body["drafted"] == [] and swept.body["pending"] == 0
    assert swept.body["waiting"] == 1
    asked = swept.body["asked"]
    assert asked is not None and asked["peer"] == "bob"
    assert "which gathering do they mean?" in asked["text"]
    assert "ignore it" in asked["text"]
    # The question lives in alice's OoLu conversation now.
    turns = history.history(tenant="t1", principal="alice")
    assert turns and turns[-1]["kind"] == "assistant"
    assert "which gathering do they mean?" in turns[-1]["body"]
    # Asked ONCE: the next sweep stays quiet about the same question.
    again = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice)
    )
    assert again.body["asked"] is None
    assert len(history.history(tenant="t1", principal="alice")) == 1
    # The drafts route shows it as waiting, not as a reviewable draft.
    drafts = gateway.handle(
        _req("GET", "/v1/representative/drafts", token=alice)
    ).body
    assert drafts["items"] == []
    assert [w["conversation_id"] for w in drafts["waiting"]] == ["bob"]
    conn.close()


def test_ignoring_marks_the_friends_message_read(tmp_path):
    gateway, conn, ident, history = _host(tmp_path, model=_Parrot("a draft"))
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "pssst"})
    )
    swept = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice)
    )
    [drafted] = swept.body["drafted"]
    unread = gateway.handle(
        _req("GET", "/v1/friends", token=alice)
    ).body["items"]
    assert unread and unread[0]["unread"] == 1
    decided = gateway.handle(
        _req("POST", f"/v1/representative/drafts/{drafted['draft_id']}",
             token=alice, body={"action": "ignore"})
    )
    assert decided.status == 200 and decided.body["status"] == "ignored"
    assert decided.body["delivered"] is None
    # "The message is read": it stops counting as waiting.
    after = gateway.handle(
        _req("GET", "/v1/friends", token=alice)
    ).body["items"]
    assert after[0]["unread"] == 0
    conn.close()


def test_the_chat_hands_answer_and_ignore(tmp_path):
    gateway, conn, ident, history = _host(
        tmp_path,
        model=_Script(
            "NEED_INFO: which gathering?",
            "yes, we're in for Saturday!",
        ),
    )
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "coming to the gathering?"})
    )
    gateway.handle(_req("POST", "/v1/representative/sweep", token=alice))

    session = gateway.authorize_chat_stream(
        _req("GET", "/v1/chat", token=alice)
    )
    hands = gateway._representative_chat_hands(session)
    assert hands is not None
    [item] = hands.waiting()
    assert item["peer"] == "bob" and "which gathering" in item["questions"]

    # The user's answer, relayed by OoLu: a fresh reviewable draft.
    said = hands.answer("bob", "it's Saturday's BBQ — tell him we're in")
    assert not said.startswith("error:") and "drafted" in said
    drafts = gateway.handle(
        _req("GET", "/v1/representative/drafts", token=alice)
    ).body
    assert len(drafts["items"]) == 1 and drafts["waiting"] == []

    # "Ignore bob's message": standing drafts settle and the thread reads.
    said = hands.ignore("bob")
    assert not said.startswith("error:")
    assert gateway.handle(
        _req("GET", "/v1/representative/drafts", token=alice)
    ).body["items"] == []
    assert gateway.handle(
        _req("GET", "/v1/friends", token=alice)
    ).body["items"][0]["unread"] == 0

    # Off means no hands at all — the chat tools answer in words instead.
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "off"})
    )
    assert gateway._representative_chat_hands(session) is None
    conn.close()
