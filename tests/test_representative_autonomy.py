"""Representative mode, Phase 2: earned autonomy and the preference pass.

Exit gate (docs/representative-plan.md): "auto" is a mode anyone can set
but autonomy is EARNED per message — enough human verdicts, almost all
sent-as-written, plus the gate (grounded, no commitment); commitments
never auto-send, ever; auto-sent words never feed memory or the accept
rate; edits become DPO pairs and the worker stacks the preference pass on
the SFT adapter once the floor is met; and the friends surface auto-replies
end to end through the gateway.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.durable.queue import DurableTaskQueue
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.representative import (
    RepresentativeEngine,
    RepresentativeStore,
    build_dpo_dataset,
)
from oolu.representative.engine import AUTO_MIN_DECIDED
from oolu.representative.trainer import TRAIN_TASK_KIND, TrainedAdapter
from oolu.social import DirectMessageStore


class _Parrot:
    def __init__(self, text="sounds good — ship it"):
        self._text = text

    def reply(self, messages):
        return self._text


def _engine(model=None, **kwargs) -> RepresentativeEngine:
    return RepresentativeEngine(RepresentativeStore(":memory:"), model=model, **kwargs)


def _earn(engine, scope, *, sent=AUTO_MIN_DECIDED):
    """A history of trusted drafts: the user sent them all unedited."""
    for i in range(sent):
        draft = engine.draft(
            scope, conversation_id="peer", inbound_text=f"routine question {i}?",
            display_name="alice",
        )
        engine.decide(scope, draft.draft_id, action="send")


def _ground(engine, scope, text="are you around tomorrow morning?"):
    engine.ingest(scope, [("g1", text, "yep, around all day")])


# --------------------------------------------------------------------------- #
# Autonomy is earned, and every message re-earns it.                           #
# --------------------------------------------------------------------------- #
def test_auto_without_a_record_stays_a_draft():
    engine = _engine(model=_Parrot())
    engine.configure("s1", mode="auto")
    _ground(engine, "s1")
    draft = engine.auto_reply(
        "s1", conversation_id="bob", inbound_text="are you around tomorrow?",
        display_name="alice",
    )
    assert draft.status == "pending"  # no verdicts yet: not earned
    assert engine.status("s1")["auto_earned"] is False


def test_an_earned_grounded_reply_auto_sends_and_never_feeds_itself():
    engine = _engine(model=_Parrot("yep, around all day"))
    engine.configure("s1", mode="auto")
    _ground(engine, "s1")
    _earn(engine, "s1")
    exchanges_before = engine.status("s1")["exchanges"]

    draft = engine.auto_reply(
        "s1", conversation_id="bob",
        inbound_text="are you around tomorrow morning?", display_name="alice",
    )
    assert draft.status == "auto_sent"
    assert draft.final_text == "yep, around all day"
    status = engine.status("s1")
    assert status["auto_sent"] == 1 and status["auto_earned"] is True
    # Self-reinforcement is off: no new memory, no accept-rate credit.
    assert status["exchanges"] == exchanges_before
    assert status["drafts_decided"] == AUTO_MIN_DECIDED


def test_commitments_and_ungrounded_replies_never_auto_send():
    committed = _engine(model=_Parrot("Sure — see you at noon!"))
    committed.configure("s1", mode="auto")
    _ground(committed, "s1", "lunch at noon tomorrow?")
    _earn(committed, "s1")
    draft = committed.auto_reply(
        "s1", conversation_id="bob", inbound_text="lunch at noon tomorrow?",
        display_name="alice",
    )
    assert draft.status == "pending" and draft.gate.commitment

    ungrounded = _engine(model=_Parrot())
    ungrounded.configure("s1", mode="auto")
    _earn(ungrounded, "s1")  # earned — but nothing like this was ever said
    draft = ungrounded.auto_reply(
        "s1", conversation_id="bob",
        inbound_text="what is our quarterly hedging strategy?",
        display_name="alice",
    )
    assert draft.status == "pending" and draft.gate.score == 0.0


def test_edits_erode_the_accept_rate_below_the_bar():
    engine = _engine(model=_Parrot())
    engine.configure("s1", mode="auto")
    _ground(engine, "s1")
    _earn(engine, "s1", sent=AUTO_MIN_DECIDED)
    # A run of rewrites: the user is correcting the voice — trust drops.
    for i in range(10):
        draft = engine.draft(
            "s1", conversation_id="peer", inbound_text=f"tricky question {i}?",
            display_name="alice",
        )
        engine.decide(
            "s1", draft.draft_id, action="edit", text=f"actually, answer {i}"
        )
    assert engine.status("s1")["auto_earned"] is False
    draft = engine.auto_reply(
        "s1", conversation_id="bob",
        inbound_text="are you around tomorrow morning?", display_name="alice",
    )
    assert draft.status == "pending"


def test_a_muted_peer_never_gets_an_auto_reply():
    engine = _engine(model=_Parrot("yep, around all day"))
    engine.configure("s1", mode="auto")
    _ground(engine, "s1")
    _earn(engine, "s1")
    status = engine.set_peer_auto("s1", "boss", allowed=False)
    assert status["muted_peers"] == ["boss"]

    ask = {"inbound_text": "are you around tomorrow morning?", "display_name": "a"}
    muted = engine.auto_reply("s1", conversation_id="boss", **ask)
    assert muted.status == "pending"  # drafted, never sent
    open_peer = engine.auto_reply("s1", conversation_id="bob", **ask)
    assert open_peer.status == "auto_sent"

    # Un-muting restores the normal earned path; erasure clears rules too.
    engine.set_peer_auto("s1", "boss", allowed=True)
    assert engine.auto_reply("s1", conversation_id="boss", **ask).status == (
        "auto_sent"
    )
    engine.set_peer_auto("s1", "boss", allowed=False)
    engine.erase("s1")
    assert engine.peer_auto("s1", "boss") is True


def test_the_fallback_speaks_only_when_the_engine_signed_off():
    from oolu.replies import MessageEnvelope as Envelope

    engine = _engine(model=None)
    parrot = _Parrot("yep, around all day")
    fallback = engine.fallback(display_name="alice", model=parrot)
    envelope = Envelope(
        channel="telegram", conversation_id="c1", sender_id="peer",
        text="are you around tomorrow morning?", metadata={"reply_scope": "s1"},
    )
    engine.configure("s1", mode="auto")
    _ground(engine, "s1")
    assert fallback.reply(envelope, {}) is None  # not earned: files a draft
    [draft] = engine.pending("s1")
    engine.decide("s1", draft.draft_id, action="send")
    for i in range(AUTO_MIN_DECIDED - 1):
        d = engine.draft(
            "s1", conversation_id="peer", inbound_text=f"routine {i}?",
            display_name="alice", model=parrot,
        )
        engine.decide("s1", d.draft_id, action="send")
    assert fallback.reply(envelope, {}) == "yep, around all day"


# --------------------------------------------------------------------------- #
# The preference pass: edits become the reward.                                #
# --------------------------------------------------------------------------- #
def test_edit_pairs_become_a_scrubbed_dpo_dataset():
    engine = _engine(model=_Parrot("generic answer"))
    engine.configure("s1", mode="draft")
    for i, correction in enumerate(
        ["warmer answer", "generic answer", "mail me at quinn@mphepo.io"]
    ):
        draft = engine.draft(
            "s1", conversation_id="b", inbound_text=f"question {i}?",
            display_name="a",
        )
        engine.decide("s1", draft.draft_id, action="edit", text=correction)
    pairs = build_dpo_dataset(engine._store, "s1")
    # The no-op "edit" (same words back) teaches nothing and is dropped;
    # the email is scrubbed before it can reach a training file.
    assert [p["chosen"] for p in pairs] == ["warmer answer", "mail me at <EMAIL>"]
    assert all(p["rejected"] == "generic answer" for p in pairs)


def test_the_worker_stacks_dpo_once_the_floor_is_met(tmp_path):
    from test_representative_trainer import _FakeTrainer, _seed, _store, _worker

    class _FakeDpo:
        def __init__(self):
            self.configs = []

        def tune(self, config):
            self.configs.append(config)
            from pathlib import Path

            out = Path(config.output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "adapter_config.json").write_text("{}", encoding="utf-8")
            (out / "dpo-stamp").write_text("tuned", encoding="utf-8")
            return TrainedAdapter(adapter_dir=out, holdout_ppl=None)

    store = _store()
    queue = DurableTaskQueue(DurableConnection(tmp_path / "queue.db"))
    _seed(store, "s1", 10)
    # Two rewritten drafts on the books — a tiny dpo_floor makes them enough.
    for i, (generated, final) in enumerate(
        [("meh answer", "much better answer"), ("meh again", "better again")]
    ):
        from oolu.representative.models import Draft

        store.add_draft(
            Draft(
                draft_id=f"d{i}", scope="s1", conversation_id="bob",
                inbound_text=f"question {i}?", generated_text=generated,
                created_at=1.0,
            )
        )
        store.decide_draft(f"d{i}", status="edited", final_text=final)
    queue.enqueue(TRAIN_TASK_KIND, {"scope": "s1"})

    dpo = _FakeDpo()
    worker = _worker(tmp_path, store, queue, trainer=_FakeTrainer(ppl=4.0))
    worker._preference_trainer, worker._dpo_floor = dpo, 2
    result = worker.run_once()
    assert result["dpo_pairs"] == 2 and result["activated"] is True
    [config] = dpo.configs
    assert config.adapter_dir.endswith("adapter")  # stacked ON the SFT output
    # The artifact packs the DPO output, not the raw SFT adapter.
    import io
    import tarfile

    from oolu.durable.artifacts import FilesystemArtifactStore

    blob = FilesystemArtifactStore(tmp_path / "artifacts").get(
        result["artifact_ref"]
    )
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        assert "dpo-stamp" in archive.getnames()

    # Below the floor the pass is skipped entirely.
    _seed(store, "s2", 10)
    queue.enqueue(TRAIN_TASK_KIND, {"scope": "s2"})
    worker._dpo_floor = 300
    assert worker.run_once()["dpo_pairs"] == 0


# --------------------------------------------------------------------------- #
# The friends surface, end to end.                                             #
# --------------------------------------------------------------------------- #
def test_a_friend_message_gets_an_earned_auto_reply(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    engine = RepresentativeEngine(
        RepresentativeStore(":memory:"), model=_Parrot("yep, around all day")
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        direct_messages=DirectMessageStore(conn),
        representative=engine,
    )
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")

    # Alice's representative: auto mode, grounded, with an earned record.
    scope = "t1:alice"
    engine.configure(scope, mode="auto")
    _ground(engine, scope)
    _earn(engine, scope)

    sent = gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "are you around tomorrow morning?"})
    )
    assert sent.status == 201
    thread = gateway.handle(
        _req("GET", "/v1/friends/alice/messages", token=bob)
    ).body["items"]
    assert [m["text"] for m in thread][-2:] == [
        "are you around tomorrow morning?",
        "yep, around all day",
    ]
    assert thread[-1]["from"] == "alice"
    assert gateway.handle(
        _req("GET", "/v1/representative", token=alice)
    ).body["auto_sent"] == 1

    # An unearned or off representative never speaks: bob's own is off.
    reply = gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "ok!"})
    )
    assert reply.status == 201
    bobs_thread = gateway.handle(
        _req("GET", "/v1/friends/bob/messages", token=alice)
    ).body["items"]
    assert bobs_thread[-1]["text"] == "ok!"

    # Alice mutes bob: his next message drafts instead of auto-replying.
    muted = gateway.handle(
        _req("PUT", "/v1/representative/peers/bob", token=alice,
             body={"auto": False})
    )
    assert muted.status == 200 and muted.body["muted_peers"] == ["bob"]
    assert gateway.handle(
        _req("PUT", "/v1/representative/peers/bob", token=alice,
             body={"auto": "yes"})
    ).status == 400
    before = len(
        gateway.handle(
            _req("GET", "/v1/friends/alice/messages", token=bob)
        ).body["items"]
    )
    gateway.handle(
        _req("POST", "/v1/friends/alice/messages", token=bob,
             body={"text": "are you around tomorrow morning?"})
    )
    thread = gateway.handle(
        _req("GET", "/v1/friends/alice/messages", token=bob)
    ).body["items"]
    assert len(thread) == before + 1  # no auto-reply arrived
    assert gateway.handle(
        _req("GET", "/v1/representative", token=alice)
    ).body["drafts_pending"] >= 1
    conn.close()


def test_auto_mode_is_a_valid_setting_now():
    engine = _engine(model=_Parrot())
    assert engine.configure("s1", mode="auto")["mode"] == "auto"
    with pytest.raises(ValueError):
        engine.configure("s1", mode="firehose")
