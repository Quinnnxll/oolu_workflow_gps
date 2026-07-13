"""The representative knows WHO it's talking to (the register), and the
sweep drafts a reply for every waiting friend so the user only filters.

Exit gate: a same-peer memory outranks an equally similar cross-peer one;
the drafting prompt names the addressee and labels same-peer examples;
the training corpus (SFT and DPO alike) carries the same conditioner so
ONE adapter learns per-person registers; and the sweep is idempotent per
message — a message that ever had a draft is never drafted again,
whatever the user decided about it.
"""

from __future__ import annotations

from test_http_gateway import _req
from test_representative import _host

from oolu.representative import (
    RepresentativeEngine,
    RepresentativeStore,
    build_dpo_dataset,
    build_sft_dataset,
)
from oolu.representative.models import Draft


class _Parrot:
    def __init__(self, text="on it"):
        self.calls: list[list[dict]] = []
        self._text = text

    def reply(self, messages):
        self.calls.append(messages)
        return self._text


def _engine(model=None):
    return RepresentativeEngine(RepresentativeStore(":memory:"), model=model)


# --------------------------------------------------------------------------- #
# Register-aware recall and prompting.                                         #
# --------------------------------------------------------------------------- #
def test_a_same_peer_memory_outranks_an_equal_cross_peer_one():
    engine = _engine()
    engine.ingest(
        "s1", [("a", "lunch tomorrow at noon?", "can't — deadline day, rain check?")],
        peer="boss",
    )
    engine.ingest(
        "s1", [("b", "lunch tomorrow at noon?", "yesss 🍜 usual place")],
        peer="ming",
    )
    hits = engine._memory.recall("s1", "lunch tomorrow?", k=2, peer="ming")
    assert hits[0].peer == "ming" and "🍜" in hits[0].reply_text
    assert hits[1].peer == "boss" and hits[1].score < hits[0].score
    # And the other way around: the boss's register wins for the boss.
    hits = engine._memory.recall("s1", "lunch tomorrow?", k=2, peer="boss")
    assert hits[0].peer == "boss"


def test_the_drafting_prompt_names_the_addressee_and_their_register():
    parrot = _Parrot()
    engine = _engine(model=parrot)
    engine.configure("s1", mode="draft")
    engine.ingest(
        "s1", [("a", "lunch tomorrow at noon?", "can't — deadline day")],
        peer="boss",
    )
    engine.ingest(
        "s1", [("b", "lunch tomorrow at noon?", "yesss 🍜")], peer="ming"
    )
    engine.draft(
        "s1", conversation_id="ming", inbound_text="lunch tomorrow?",
        display_name="alice",
    )
    [messages] = parrot.calls
    system = messages[0]["content"]
    assert "The reply is TO ming" in system
    # Same-peer examples are named; cross-peer ones stay anonymous.
    assert 'When ming said: "lunch tomorrow at noon?"' in system
    assert 'When someone said: "lunch tomorrow at noon?"' in system
    assert "When boss said" not in system


def test_the_training_corpus_carries_the_register_conditioner():
    store = RepresentativeStore(":memory:")
    store.remember_exchange(
        "s1", key="a", prompt="lunch tomorrow?", reply="can't — deadline day",
        peer="boss",
    )
    store.remember_exchange(
        "s1", key="b", prompt="ship it?", reply="ship it 🚀"
    )  # peer unknown: no conditioner, the average voice
    train, holdout, _ = build_sft_dataset(store, "s1", holdout_fraction=0.0)
    by_reply = {ex["messages"][-1]["content"]: ex["messages"] for ex in train}
    boss = by_reply["can't — deadline day"]
    assert boss[0] == {"role": "system", "content": "Replying to boss."}
    assert by_reply["ship it 🚀"][0]["role"] == "user"  # no system line

    # DPO pairs ride the same conditioner, from the draft's conversation.
    store.add_draft(
        Draft(
            draft_id="d1", scope="s1", conversation_id="boss",
            inbound_text="lunch tomorrow?", generated_text="sure!",
            created_at=1.0,
        )
    )
    store.decide_draft("d1", status="edited", final_text="can't — deadline day")
    [pair] = build_dpo_dataset(store, "s1")
    assert pair["prompt"].startswith("Replying to boss. ")
    store.close()


# --------------------------------------------------------------------------- #
# The sweep: every waiting friend gets a draft, exactly once.                  #
# --------------------------------------------------------------------------- #
def test_the_sweep_drafts_each_waiting_message_exactly_once(tmp_path):
    gateway, conn, ident = _host(tmp_path, model=_Parrot("on it — soon!"))
    alice = ident.token("alice", "t1")
    bob = ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )

    # The sweep with nothing waiting drafts nothing.
    empty = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    )
    assert empty.status == 200 and empty.body["drafted"] == []

    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "are you around tomorrow?"})
    )
    first = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    )
    [draft] = first.body["drafted"]
    assert draft["conversation_id"] == "bob"
    assert first.body["pending"] == 1

    # Idempotent: polling again costs nothing and drafts nothing.
    again = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    )
    assert again.body["drafted"] == [] and again.body["pending"] == 1

    # Even a DISCARDED draft pins its message: no re-draft nagging.
    gateway.handle(
        _req("POST", f"/v1/representative/drafts/{draft['draft_id']}",
             token=alice, body={"action": "discard"})
    )
    assert gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    ).body["drafted"] == []

    # A NEW message from bob is fresh work.
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "ok how about friday then?"})
    )
    fresh = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    )
    assert [d["inbound_text"] for d in fresh.body["drafted"]] == [
        "ok how about friday then?"
    ]

    # Mode off refuses plainly.
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "off"})
    )
    assert gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    ).status == 409
    conn.close()


def test_a_dead_model_stops_the_sweep_with_words(tmp_path):
    gateway, conn, ident = _host(tmp_path, model=None)  # no model anywhere
    alice = ident.token("alice", "t1")
    bob = ident.token("bob", "t1")
    gateway.handle(
        _req("PUT", "/v1/representative", token=alice, body={"mode": "draft"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "ping"})
    )
    swept = gateway.handle(
        _req("POST", "/v1/representative/sweep", token=alice, body={})
    )
    assert swept.status == 200 and swept.body["drafted"] == []
    assert "model" in (swept.body["model_error"] or "")
    conn.close()
