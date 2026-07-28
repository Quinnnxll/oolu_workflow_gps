"""The News desk's intake — raw material becomes a piece through talk.

Exit gate: nobody fills a form. Raw material dropped into the News
thread is DETECTED (article-shaped text, or attachments at any length)
the way OoLu spots a buildable chore; the desk reviews in conversation
— one gathering question when the piece is thin, a loud named refusal
when the text would leak, the retelling credit named up front — and
makes a standing publish offer that renders the license terms. The
member's plain "yes" on the very next message publishes through the
same gate the manual door walks (the yes IS the consent); a plain "no"
drops it; anything else withdraws the offer. Whether the published
piece is worth composing is the RUBRIC's decision: the newsroom runs
after publication and the desk reports honestly which way it went.
Ordinary conversation never diverts — a question is a question.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    DROPPED,
    GATHER_ASK,
    ContributionStore,
    IntakeStore,
    PreferenceStore,
    PressDesk,
    StoryStore,
    draft_from_material,
    fold_answer,
    looks_like_material,
    review,
)
from oolu.press.intake import (
    GATHER_FLOOR_CHARS,
    MATERIAL_FLOOR_CHARS,
    derive_title,
    infer_genres,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

ARTICLE = (
    "After two years of repairs the harbor market reopened this morning "
    "with forty stalls and a queue down the pier. The fishmongers were "
    "back at the north end, the bakers at the south, and the harbormaster "
    "rang the old bell at eight to open the gates. By nine the ferry crews "
    "had come ashore for bread and the tables outside were full."
)
THIN = (
    "The harbor market reopened this morning with forty stalls and a "
    "queue down the pier. The old bell rang at eight and the gates opened "
    "right on time for once this year."
)


# --------------------------------------------------------------------------- #
# Detection: material the way OoLu spots a chore.                              #
# --------------------------------------------------------------------------- #
def test_detection_reads_shape_not_intent_words():
    assert len(ARTICLE) >= GATHER_FLOOR_CHARS  # the fixtures hold their roles
    assert MATERIAL_FLOOR_CHARS <= len(THIN) < GATHER_FLOOR_CHARS
    assert looks_like_material(ARTICLE)
    assert looks_like_material(THIN)
    # Short lines are conversation; questions are questions, however
    # long; attachments are material at ANY length.
    assert not looks_like_material("did anything happen at the harbor?")
    assert not looks_like_material(ARTICLE + " Was the bell always there?")
    assert not looks_like_material("hello")
    assert looks_like_material("my photo from the pier", has_media=True)


def test_titles_and_genres_derive_from_the_material_itself():
    assert derive_title("A short line\nand more") == "A short line"
    long_line = "word " * 40
    title = derive_title(long_line)
    assert len(title) <= 120 and title.endswith("…")
    assert infer_genres("the storm was research grade") == ("science",)
    assert infer_genres("nothing notable here") == ("local",)
    # A sound attachment hints culture — a song is culture first.
    assert infer_genres("for you", media_types=("audio/mpeg",)) == ("culture",)
    assert len(infer_genres("music from the match at the restaurant")) <= 2


# --------------------------------------------------------------------------- #
# The review: one question, loud refusals, the offer renders the terms.        #
# --------------------------------------------------------------------------- #
def _draft(message, **kw):
    return draft_from_material(
        tenant="t1", principal="alice", message=message, **kw
    )


def test_a_thin_piece_earns_one_question_then_the_offer_stands():
    staged, say = review(_draft(THIN), live_texts=[])
    assert staged.stage == "gathering" and staged.asked == 1
    assert say == GATHER_ASK
    # The answer folds in verbatim; the desk does not ask twice.
    grown = fold_answer(staged, "It happened at the north pier on Sunday.")
    assert "north pier on Sunday" in grown.body
    staged2, say2 = review(grown, live_texts=[])
    assert staged2.stage == "offered"
    assert "Ready to publish" in say2
    # The offer renders what the yes will mean: the license terms.
    assert "OoLu members license v1" in say2
    assert "Say “yes”" in say2


def test_leaks_refuse_loudly_and_the_offer_names_a_retelling():
    leaky, say = review(
        _draft(ARTICLE + " Mail me at bob@example.org for photos."),
        live_texts=[],
    )
    # Dropped, not parked: fixed words arrive as FRESH material, never
    # folded onto the leaky ones.
    assert leaky.stage == "dropped"
    assert "an email address" in say
    # A near-duplicate offers honestly: the credit is named up front.
    _, offer = review(
        _draft(ARTICLE),
        live_texts=[("c0", f"Harbor market reopened\n{ARTICLE}")],
        title_of=lambda cid: "Harbor market reopened",
    )
    assert "retells an earlier piece" in offer
    assert "Harbor market reopened" in offer


def test_the_store_holds_one_draft_and_spends_it_once(tmp_path):
    conn = DurableConnection(tmp_path / "intake.db")
    store = IntakeStore(conn)
    first, _ = review(_draft(ARTICLE), live_texts=[])
    store.put(first)
    replaced, _ = review(_draft("New material. " * 20), live_texts=[])
    store.put(replaced)  # the newest material is the one on the table
    standing = store.get(tenant="t1", principal="alice")
    assert standing is not None and standing.title.startswith("New material.")
    assert store.pop(tenant="t1", principal="alice") is not None
    assert store.pop(tenant="t1", principal="alice") is None  # spent once
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the thread is the door.                                         #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    from oolu.durable.files import UserFileStore

    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        intake=IntakeStore(conn),
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        assistant_history=AssistantHistoryStore(conn),
        press=press,
        files=UserFileStore(conn),
    )
    return gateway, conn, ident


def _say(gateway, token, message, file_ids=None):
    body = {"message": message, "agent": "news"}
    if file_ids is not None:
        body["file_ids"] = file_ids
    response = gateway.handle(_req("POST", "/v1/chat", token=token, body=body))
    assert response.status == 200
    return response.body


def test_raw_material_in_the_thread_reviews_publishes_and_composes(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")

    # A question is a question — the agent answers, nothing diverts.
    chat = _say(gateway, alice, "what can you do?")
    assert chat["source"] == "card"

    # Raw article-shaped text: the desk detects it and offers, terms
    # rendered — no form was ever involved.
    offer = _say(gateway, alice, ARTICLE)
    assert offer["source"] == "desk"
    assert "Ready to publish" in offer["reply"]
    assert "OoLu members license v1" in offer["reply"]

    # The plain yes IS the consent: published through the one gate, and
    # the rubric (not the desk's mood) judged it worth telling.
    done = _say(gateway, alice, "yes")
    assert "Published" in done["reply"]
    assert "worth telling" in done["reply"]
    shelf = gateway.handle(_req("GET", "/v1/press/contributions", token=alice))
    assert len(shelf.body["items"]) == 1
    assert shelf.body["items"][0]["license"] == "oolu-members-1"
    stories = gateway.handle(_req("GET", "/v1/press/stories", token=alice))
    assert len(stories.body["items"]) == 1
    # The audit chain carries the publication, same voice as the door.
    audited = [
        r
        for r in gateway._durable.audit.records()
        if r.event_type == "press.contribution_published"
    ]
    assert len(audited) == 1
    conn.close()


def test_thin_material_gathers_once_and_a_no_drops_it(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")

    ask = _say(gateway, alice, THIN)
    assert ask["reply"] == GATHER_ASK
    # A side question leaves the desk's question standing.
    aside = _say(gateway, alice, "what is your edition?")
    assert aside["source"] == "card"
    offer = _say(gateway, alice, "It was at the north pier, on Sunday morning.")
    assert "Ready to publish" in offer["reply"]
    # No means no: dropped, nothing on the shelf.
    dropped = _say(gateway, alice, "no")
    assert dropped["reply"] == DROPPED
    shelf = gateway.handle(_req("GET", "/v1/press/contributions", token=alice))
    assert shelf.body["items"] == []
    conn.close()


def test_attachments_ride_the_message_and_the_wall_holds(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice"), ident.token("bob")

    created = gateway.handle(
        _req(
            "POST",
            "/v1/files",
            token=alice,
            body={"name": "pier.jpg", "content": "x", "media_type": "image/jpeg"},
        )
    )
    file_id = created.body["file_id"]
    # Another member's file refuses loudly at the door — now, not later.
    walled = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=bob,
            body={"message": ARTICLE, "agent": "news", "file_ids": [file_id]},
        )
    )
    assert walled.status == 404

    offer = _say(gateway, alice, ARTICLE, file_ids=[file_id])
    assert "1 attachment" in offer["reply"]
    _say(gateway, alice, "yes")
    shelf = gateway.handle(_req("GET", "/v1/press/contributions", token=alice))
    [piece] = shelf.body["items"]
    assert [m["file_id"] for m in piece["media"]] == [file_id]
    conn.close()


def test_an_unrelated_message_withdraws_the_offer(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")

    offer = _say(gateway, alice, ARTICLE)
    assert "Ready to publish" in offer["reply"]
    # Neither yes nor no, and not material: the offer is withdrawn and
    # the conversation stands — an unrelated line never spends consent.
    aside = _say(gateway, alice, "thanks anyway")
    assert aside["source"] == "card"
    # A later plain "yes" answers NOTHING — no offer stands to spend.
    stray = _say(gateway, alice, "yes")
    assert stray["source"] == "card"
    shelf = gateway.handle(_req("GET", "/v1/press/contributions", token=alice))
    assert shelf.body["items"] == []
    conn.close()


def test_a_leak_is_refused_in_the_desk_words_and_fixed_text_offers(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")

    refusal = _say(
        gateway, alice, ARTICLE + " Reach me at alice@example.org for more."
    )
    assert "an email address" in refusal["reply"]
    # Fresh, fixed material replaces the draft and offers cleanly.
    offer = _say(gateway, alice, ARTICLE)
    assert "Ready to publish" in offer["reply"]
    conn.close()
