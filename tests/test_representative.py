"""Representative mode, Phase 0: drafts in the user's voice, never auto-sent.

Exit gate (docs/representative-plan.md): a user with history toggles draft
mode and gets drafts grounded in their own past replies; every
send/edit/discard lands an outcome row (sent and edited words become
memory — the flywheel); a user with the mode off is unaffected; commitments
and ungrounded replies are marked by the gate; and erasing the scope
removes settings, memory, and drafts in one call. All pure Python: a fake
model, in-memory SQLite.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.chat import ModelUnavailable
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.replies import MessageEnvelope
from oolu.representative import (
    RepresentativeEngine,
    RepresentativeStore,
    StoreExchangeMemory,
    commitment_marker,
    judge,
    pair_exchanges,
)
from oolu.representative.memory import _tokens
from oolu.representative.models import RecallHit
from oolu.social import DirectMessageStore


class _Parrot:
    """A model that always answers the same words and remembers the asks."""

    def __init__(self, text="sounds good — ship it"):
        self.calls: list[list[dict]] = []
        self._text = text

    def reply(self, messages):
        self.calls.append(messages)
        return self._text


class _DeadModel:
    def reply(self, messages):
        raise ModelUnavailable("no brain today")


def _engine(model=None, **kwargs) -> RepresentativeEngine:
    return RepresentativeEngine(
        RepresentativeStore(":memory:"), model=model, **kwargs
    )


# --------------------------------------------------------------------------- #
# Memory: what the user has actually said.                                     #
# --------------------------------------------------------------------------- #
def test_recall_ranks_by_overlap_and_respects_scope():
    store = RepresentativeStore(":memory:")
    memory = StoreExchangeMemory(store)
    memory.remember(
        "s1", key="a", prompt="how do you deploy the app?", reply="push to main"
    )
    memory.remember("s1", key="b", prompt="lunch tomorrow?", reply="can't, busy")
    memory.remember("s2", key="c", prompt="deploy the app?", reply="other person")

    hits = memory.recall("s1", "what's the deploy process for the app?", k=2)
    assert hits and hits[0].reply_text == "push to main"
    assert all(hit.reply_text != "other person" for hit in hits)
    # Re-remembering the same key refreshes, never duplicates.
    memory.remember("s1", key="a", prompt="how do you deploy the app?", reply="push!")
    assert memory.count("s1") == 2
    # Junk in, nothing out.
    assert memory.recall("s1", "???", k=2) == [] or _tokens("???") == frozenset()
    store.close()


def test_pair_exchanges_folds_a_thread_into_prompt_reply_pairs():
    turns = [
        ("m1", "alice", "hi bob"),  # own message with no inbound: skipped
        ("m2", "bob", "how do you deploy?"),
        ("m3", "alice", "push to main"),
        ("m4", "alice", "the action does the rest"),  # follow-up: skipped
        ("m5", "bob", "  "),  # blank: ignored
        ("m6", "bob", "thanks!"),
        ("m7", "alice", "anytime"),
    ]
    assert pair_exchanges(turns, me="alice") == [
        ("m3", "how do you deploy?", "push to main"),
        ("m7", "thanks!", "anytime"),
    ]


# --------------------------------------------------------------------------- #
# The gate: commitments always draft, ungrounded replies are marked.           #
# --------------------------------------------------------------------------- #
def test_the_gate_flags_commitments_whatever_the_grounding():
    assert commitment_marker("Deal! I'll be there at 5.") is not None
    assert commitment_marker("I will pay you back Friday") is not None
    assert commitment_marker("the deal fell through, sadly") is None
    strong = [RecallHit(prompt_text="x", reply_text="y", score=0.9)]
    verdict = judge("Sure — see you at noon!", strong)
    assert verdict.commitment and not verdict.auto_ok and verdict.reasons


def test_the_gate_marks_ungrounded_replies():
    verdict = judge("probably fine", [])
    assert verdict.score == 0.0 and not verdict.auto_ok
    assert any("ungrounded" in reason for reason in verdict.reasons)
    grounded = judge(
        "probably fine", [RecallHit(prompt_text="x", reply_text="y", score=0.5)]
    )
    assert grounded.auto_ok


# --------------------------------------------------------------------------- #
# The engine: draft, decide, remember.                                         #
# --------------------------------------------------------------------------- #
def test_a_draft_is_grounded_in_the_users_own_replies():
    parrot = _Parrot()
    engine = _engine(model=parrot)
    engine.configure("s1", mode="draft", about="engineer, keeps replies short")
    engine.ingest("s1", [("k1", "how do you deploy?", "push to main")])

    draft = engine.draft(
        "s1",
        conversation_id="bob",
        inbound_text="what's your deploy process?",
        display_name="alice",
    )
    assert draft.status == "pending" and draft.adapter_version == "base"
    [messages] = parrot.calls
    system = messages[0]["content"]
    # The persona card, the user's real exchange, and the hard rules all ride.
    assert "alice" in system and "push to main" in system
    assert "engineer, keeps replies short" in system
    assert "Never agree to spend money" in system
    assert messages[-1] == {"role": "user", "content": "what's your deploy process?"}
    assert engine.pending("s1") == [draft]
    assert engine.status("s1")["drafts_pending"] == 1


def test_deciding_a_draft_records_the_outcome_and_feeds_memory():
    engine = _engine(model=_Parrot("on it"))
    engine.configure("s1", mode="draft")
    draft = engine.draft(
        "s1", conversation_id="bob", inbound_text="can you review my PR?",
        display_name="alice",
    )
    decided = engine.decide("s1", draft.draft_id, action="edit", text="on it — today")
    assert decided.status == "edited" and decided.final_text == "on it — today"
    # The approved words are the user's own now: recall finds them.
    [hit] = engine._memory.recall("s1", "review my PR?", k=1)
    assert hit.reply_text == "on it — today"
    # A decision is spent exactly once.
    with pytest.raises(ValueError, match="already decided"):
        engine.decide("s1", draft.draft_id, action="discard")
    status = engine.status("s1")
    assert status["drafts_pending"] == 0 and status["drafts_decided"] == 1


def test_the_engine_refuses_what_it_should():
    engine = _engine(model=_Parrot())
    with pytest.raises(ValueError, match="mode must be one of"):
        engine.configure("s1", mode="firehose")
    engine.configure("s1", mode="draft")
    with pytest.raises(ValueError, match="nothing to reply to"):
        engine.draft("s1", conversation_id="b", inbound_text="  ", display_name="a")
    draft = engine.draft(
        "s1", conversation_id="b", inbound_text="hey", display_name="a"
    )
    with pytest.raises(ValueError, match="edited words"):
        engine.decide("s1", draft.draft_id, action="edit", text="  ")
    with pytest.raises(ValueError, match="send, edit, or discard"):
        engine.decide("s1", draft.draft_id, action="yolo")
    # Another scope's drafts are indistinguishable from missing.
    with pytest.raises(KeyError):
        engine.decide("s2", draft.draft_id, action="send")
    # No model anywhere means a plain refusal, not a stack trace.
    with pytest.raises(ModelUnavailable):
        _engine().draft(
            "s1", conversation_id="b", inbound_text="hey", display_name="a"
        )


def test_erasing_a_scope_removes_the_whole_representative():
    engine = _engine(model=_Parrot())
    engine.configure("s1", mode="draft", about="me")
    engine.ingest("s1", [("k", "q", "a")])
    engine.draft("s1", conversation_id="b", inbound_text="hey", display_name="a")
    assert engine.erase("s1") == 3
    status = engine.status("s1")
    assert status == {
        "mode": "off",
        "about": "",
        "exchanges": 0,
        "drafts_pending": 0,
        "drafts_decided": 0,
        "sent_unedited": 0,
        "auto_sent": 0,
        "accept_rate": None,
        "auto_earned": False,
        "adapter": "base",
    }


# --------------------------------------------------------------------------- #
# The channels seam: a fallback that files drafts and never speaks.            #
# --------------------------------------------------------------------------- #
def test_the_fallback_drafts_when_on_stays_silent_always():
    engine = _engine()
    fallback = engine.fallback(display_name="alice", model=_Parrot())

    def envelope(text="you around?"):
        return MessageEnvelope(
            channel="telegram", conversation_id="c1", sender_id="peer", text=text,
            metadata={"reply_scope": "s1"},
        )

    # Mode off: silent AND inert — not a single draft.
    assert fallback.reply(envelope(), {}) is None
    assert engine.pending("s1") == []
    engine.configure("s1", mode="draft")
    assert fallback.reply(envelope(), {}) is None
    [draft] = engine.pending("s1")
    assert draft.conversation_id == "c1" and draft.inbound_text == "you around?"
    # A dead model never breaks the polling loop.
    dead = engine.fallback(display_name="alice", model=_DeadModel())
    assert dead.reply(envelope("still there?"), {}) is None


# --------------------------------------------------------------------------- #
# The gateway: real accounts behind the routes.                                #
# --------------------------------------------------------------------------- #
def _host(tmp_path, model=None):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    engine = RepresentativeEngine(RepresentativeStore(":memory:"), model=model)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        direct_messages=DirectMessageStore(conn),
        representative=engine,
    )
    return gateway, conn, ident


def test_the_representative_flow_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path, model=_Parrot("on it — will look today"))
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")

    # Off by default; drafting while off is refused.
    assert gateway.handle(
        _req("GET", "/v1/representative", token=alice)
    ).body["mode"] == "off"
    refused = gateway.handle(
        _req("POST", "/v1/representative/drafts", token=alice, body={"peer": "bob"})
    )
    assert refused.status == 409 and refused.body["error"]["code"] == "representative_off"

    # Toggle on, with a persona note. Junk modes are refused.
    assert gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "firehose"})
    ).status == 400
    configured = gateway.handle(
        _req("PUT", "/v1/representative", token=alice,
             body={"mode": "draft", "about": "keeps replies short"})
    )
    assert configured.status == 200 and configured.body["mode"] == "draft"

    # A real thread: alice has replied to bob before, then bob asks again.
    for sender, recipient, text in (
        ("bob", "alice", "how do you deploy the app?"),
        ("alice", "bob", "push to main, the action does the rest"),
        ("bob", "alice", "nice — can you review my PR tomorrow?"),
    ):
        token = alice if sender == "alice" else bob
        assert gateway.handle(
            _req("POST", f"/v1/friends/{recipient}/messages", token=token,
                 body={"text": text})
        ).status == 201

    drafted = gateway.handle(
        _req("POST", "/v1/representative/drafts", token=alice, body={"peer": "bob"})
    )
    assert drafted.status == 201, drafted.body
    assert drafted.body["generated_text"] == "on it — will look today"
    assert drafted.body["status"] == "pending"
    # The thread was folded into memory on the way.
    assert gateway.handle(
        _req("GET", "/v1/representative", token=alice)
    ).body["exchanges"] == 1

    # The inbox lists it; deciding "send" delivers to bob and spends it.
    [pending] = gateway.handle(
        _req("GET", "/v1/representative/drafts", token=alice)
    ).body["items"]
    decided = gateway.handle(
        _req("POST", f"/v1/representative/drafts/{pending['draft_id']}",
             token=alice, body={"action": "send"})
    )
    assert decided.status == 200 and decided.body["status"] == "sent"
    assert decided.body["delivered"]["message_id"]
    thread = gateway.handle(
        _req("GET", "/v1/friends/alice/messages", token=bob)
    ).body["items"]
    assert thread[-1]["text"] == "on it — will look today"
    assert not thread[-1]["mine"]

    # Spent is spent; and now the last word is alice's — nothing to answer.
    assert gateway.handle(
        _req("POST", f"/v1/representative/drafts/{pending['draft_id']}",
             token=alice, body={"action": "discard"})
    ).status == 400
    again = gateway.handle(
        _req("POST", "/v1/representative/drafts", token=alice, body={"peer": "bob"})
    )
    assert again.status == 409 and again.body["error"]["code"] == "nothing_to_answer"

    # Another account can't see or spend alice's drafts.
    assert gateway.handle(
        _req("GET", "/v1/representative/drafts", token=bob)
    ).body["items"] == []
    assert gateway.handle(
        _req("POST", f"/v1/representative/drafts/{pending['draft_id']}",
             token=bob, body={"action": "send"})
    ).status == 404
    conn.close()


def test_no_model_is_a_plain_503_and_no_service_is_404(tmp_path):
    gateway, conn, ident = _host(tmp_path, model=None)
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob, body={"text": "hey"})
    )
    unavailable = gateway.handle(
        _req("POST", "/v1/representative/drafts", token=alice, body={"peer": "bob"})
    )
    assert unavailable.status == 503
    assert unavailable.body["error"]["code"] == "model_unavailable"

    # A host without the service: the routes say so, everything else works.
    plain, plain_conn, plain_ident = _app(tmp_path, path=tmp_path / "plain.db")
    token = plain_ident.token("user-1")
    assert plain.handle(_req("GET", "/v1/representative", token=token)).status == 404
    conn.close()
    plain_conn.close()
