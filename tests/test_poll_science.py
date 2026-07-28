"""The poll floor's social scientist (A3.1): patterns, on purpose.

Exit gate: hypotheses are typed conjectures over signals a pair's two
sides objectively differ in (evidence, depth, freshness); the strategic
deal serves — among honestly comparable candidates — the pair that
measures the least-evidenced hypothesis, and the fun never waits on the
science; only decided, k-anonymous comparisons enter the evidence; a
finding is worth words as a clear pattern or a genuine debate (never in
between), and each verdict is reported ONCE into the Poll thread; the
poll and the genre picking arrive as MESSAGE BLOCKS in the
conversation, and a genre named in words deals from that stream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.press import (
    K_FLOOR,
    ContentContribution,
    Finding,
    FindingStore,
    MediaRef,
    PollPair,
    PollSide,
    PollStore,
    judge,
)
from oolu.press.scientist import choose_pair, discriminates
from oolu.social import AssistantHistoryStore

T0 = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def _piece(cid, author, body, *, media=False, at=T0):
    return ContentContribution(
        contribution_id=cid,
        tenant_id="t1",
        author=author,
        title=f"Piece {cid}",
        body=body,
        genres=("local",),
        license="oolu-members-1",
        media=(MediaRef(file_id="f1"),) if media else (),
        created_at=at,
    )


# --------------------------------------------------------------------------- #
# Hypotheses discriminate on objective differences.                            #
# --------------------------------------------------------------------------- #
def test_hypotheses_discriminate_only_on_real_differences():
    plain = _piece("a", "alice", "x" * 100)
    shown = _piece("b", "bob", "y" * 100, media=True)
    deep = _piece("c", "carol", "z" * 200)
    old = _piece("d", "dan", "w" * 100, at=T0 - timedelta(days=3))
    assert discriminates("evidence", shown, plain) == "left"
    assert discriminates("evidence", plain, shown) == "right"
    assert discriminates("evidence", plain, plain) is None
    assert discriminates("depth", deep, plain) == "left"
    assert discriminates("depth", plain, deep) == "right"
    assert discriminates("depth", plain, shown) is None  # equal lengths
    assert discriminates("freshness", plain, old) == "left"
    assert discriminates("freshness", old, plain) == "right"
    assert discriminates("freshness", plain, shown) is None  # same hour


def test_the_strategic_deal_serves_the_least_evidenced_hypothesis():
    plain = _piece("a", "alice", "x" * 100)
    shown = _piece("b", "bob", "y" * 100, media=True)
    deep = _piece("c", "carol", "z" * 200)
    # Depth already holds evidence; the evidence hypothesis has none —
    # the (shown, plain) pair measures more.
    picked = choose_pair(
        [(deep, plain), (shown, plain)],
        decided_counts={"depth": 4, "evidence": 0, "freshness": 4},
    )
    assert picked == (shown, plain)
    # Nothing discriminating: the first candidate deals — fun never waits.
    assert choose_pair(
        [(plain, _piece("e", "eve", "v" * 100))], decided_counts={}
    ) == (plain, _piece("e", "eve", "v" * 100))


# --------------------------------------------------------------------------- #
# Judging: decided, k-anonymous comparisons only; verdicts worth words.        #
# --------------------------------------------------------------------------- #
def _pair(pid, left, right):
    return PollPair(
        pair_id=pid,
        tenant_id="t1",
        genre="local",
        left=PollSide(
            contribution_id=left.contribution_id,
            author=left.author,
            title=left.title,
            excerpt=left.body[:100],
        ),
        right=PollSide(
            contribution_id=right.contribution_id,
            author=right.author,
            title=right.title,
            excerpt=right.body[:100],
        ),
        created_at=T0,
    )


def test_judge_counts_only_decided_floors_and_names_the_verdict():
    shown = _piece("s", "alice", "x" * 100, media=True)
    plains = [_piece(f"p{i}", f"m{i}", "y" * 100) for i in range(4)]
    pairs = [_pair(f"pr{i}", shown, plains[i]) for i in range(4)]
    counts = {
        "pr0": {"left": 4, "right": 1},  # decided: evidence affirmed
        "pr1": {"left": 5, "right": 0},  # decided: affirmed
        "pr2": {"left": 3, "right": 2},  # decided: affirmed
        "pr3": {"left": 2, "right": 1},  # under the K_FLOOR: never counts
    }
    lookup = {p.contribution_id: p for p in [shown, *plains]}
    findings, decided = judge(
        pairs,
        counts_of=lambda pid: counts[pid],
        contribution_of=lambda cid: lookup.get(cid),
    )
    assert decided["evidence"] == 3  # pr3 stayed out
    [finding] = findings
    assert finding.hypothesis == "evidence" and finding.verdict == "affirmed"
    assert finding.support == 3 and finding.decided == 3
    assert "a pattern holds" in finding.report()
    # Flip two majorities to the plain side and decide the fourth: two
    # for, two against — an honest split is a debate, not a pattern.
    counts["pr0"] = {"left": 1, "right": 4}
    counts["pr2"] = {"left": 2, "right": 3}
    counts["pr3"] = {"left": 5, "right": 1}
    findings, _ = judge(
        pairs,
        counts_of=lambda pid: counts[pid],
        contribution_of=lambda cid: lookup.get(cid),
    )
    [debate] = findings
    assert debate.verdict == "debate"
    assert "a debate worth having" in debate.report()


def test_a_verdict_is_reported_once_until_it_changes(tmp_path):
    conn = DurableConnection(tmp_path / "sci.db")
    store = FindingStore(conn)
    finding = Finding(
        hypothesis="evidence",
        question="q?",
        verdict="affirmed",
        support=3,
        against=0,
        decided=3,
    )
    assert store.note(tenant="t1", finding=finding) is True
    assert store.note(tenant="t1", finding=finding) is False  # no ticker
    flipped = Finding(
        hypothesis="evidence",
        question="q?",
        verdict="debate",
        support=2,
        against=2,
        decided=4,
    )
    assert store.note(tenant="t1", finding=flipped) is True
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: message blocks in the thread, and the report pushed.            #
# --------------------------------------------------------------------------- #
def test_the_poll_thread_deals_blocks_and_reports_the_field_note(tmp_path):
    from oolu.press import (
        ContributionStore,
        IntakeStore,
        PairwiseStore,
        PollDesk,
        PreferenceStore,
        PressDesk,
        StoryStore,
        strategist,
    )

    app, conn, ident = _app(tmp_path)
    app._files = UserFileStore(conn)
    app._assistant_history = AssistantHistoryStore(conn)
    contributions = ContributionStore(conn)
    polls_store = PollStore(conn)
    app._press = PressDesk(
        contributions,
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        polls=PollDesk(
            polls_store,
            PairwiseStore(conn),
            strategist=strategist(polls_store, contributions),
        ),
        intake=IntakeStore(conn),
    )
    alice = ident.token("alice")

    # One piece WITH a photo and three without — same genre, a shared
    # subject band (the harbor, the morning), equal lengths: only the
    # evidence hypothesis discriminates.
    photo = app.handle(
        _req(
            "POST",
            "/v1/files",
            token=alice,
            body={"name": "pier.jpg", "content": "x", "media_type": "image/jpeg"},
        )
    )
    texts = {
        "shown": "The harbor market reopened with forty stalls this morning "
        "and a queue down the pier said everything about the wait.",
        "p1": "Rain reached the harbor valley terraces this morning and the "
        "runoff filled every channel before the market bells rang.",
        "p2": "The corner bakery by the harbor milled its own flour this "
        "morning and the first market loaves sold out before eight.",
        "p3": "Dolphins followed the morning ferry across the harbor bay "
        "and the market crossing slowed so passengers could watch.",
    }
    ids = {}
    for i, (key, body) in enumerate(texts.items()):
        # The photo is alice's own — her piece carries it; the drawer
        # wall would refuse it on anyone else's byline.
        author = alice if key == "shown" else ident.token(f"author-{i}")
        published = app.handle(
            _req(
                "POST",
                "/v1/press/contributions",
                token=author,
                body={
                    "title": f"Piece {key}",
                    "body": body,
                    "genres": ["local"],
                    "license": "oolu-members-1",
                    "consent": True,
                    **(
                        {"file_ids": [photo.body["file_id"]]}
                        if key == "shown"
                        else {}
                    ),
                },
            )
        )
        assert published.status == 201, published.body
        ids[key] = published.body["contribution_id"]

    # "poll" in the conversation deals a pair as a MESSAGE BLOCK; the
    # strategic deal picks one that discriminates (the shown piece on
    # one side — every candidate pair with it tests evidence).
    dealt = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "poll", "agent": "poll"},
        )
    )
    assert dealt.body["source"] == "desk"
    assert dealt.body["block"]["kind"] == "poll"
    assert ids["shown"] in (
        dealt.body["block"]["pair"]["left"]["contribution_id"],
        dealt.body["block"]["pair"]["right"]["contribution_id"],
    )
    # The genre chips arrive as a block too; naming a genre deals from it.
    chips = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "genres", "agent": "poll"},
        )
    )
    assert chips.body["block"]["kind"] == "genres"
    named = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "Around me", "agent": "poll"},
        )
    )
    assert named.body["block"]["kind"] == "poll"
    assert named.body["block"]["pair"]["genre"] == "local"
    # No evidence yet: the scientist says so, honestly.
    researching = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "findings", "agent": "poll"},
        )
    )
    assert "Still researching" in researching.body["reply"]

    # Three decided floors, built directly on the store (the doors would
    # take fifteen members); the LAST crossing vote walks the door — and
    # the field note lands in that voter's Poll thread.
    polls = PollStore(conn)
    shown_piece_id = ids["shown"]
    pair_rows = []
    for i, key in enumerate(("p1", "p2", "p3")):
        pair = PollPair(
            pair_id=f"sci-{i}",
            tenant_id="t1",
            genre="local",
            left=PollSide(
                contribution_id=shown_piece_id,
                author="author-0",
                title="Piece shown",
                excerpt="…",
            ),
            right=PollSide(
                contribution_id=ids[key],
                author=f"author-{key}",
                title=f"Piece {key}",
                excerpt="…",
            ),
            created_at=T0,
        )
        polls.insert_pair(pair)
        pair_rows.append(pair)
    for pair in pair_rows[:2]:
        for v in range(K_FLOOR):
            polls.cast(
                pair.pair_id, tenant="t1", principal=f"v{v}", choice="left"
            )
    for v in range(K_FLOOR - 1):
        polls.cast(
            pair_rows[2].pair_id, tenant="t1", principal=f"v{v}", choice="left"
        )
    voter = ident.token("closer")
    crossed = app.handle(
        _req(
            "POST",
            f"/v1/press/polls/{pair_rows[2].pair_id}/vote",
            token=voter,
            body={"choice": "left"},
        )
    )
    assert crossed.status == 200
    thread = app.handle(
        _req("GET", "/v1/chat/history", token=voter, query={"agent": "poll"})
    )
    notes = [t["body"] for t in thread.body["items"]]
    assert any("a pattern holds" in n for n in notes)
    assert any("SHOW something" in n for n in notes)
    # The same verdict never reports twice: another member's later vote
    # pushes nothing to THEIR thread.
    late = ident.token("latecomer")
    app.handle(
        _req(
            "POST",
            f"/v1/press/polls/{pair_rows[0].pair_id}/vote",
            token=late,
            body={"choice": "left"},
        )
    )
    silent = app.handle(
        _req("GET", "/v1/chat/history", token=late, query={"agent": "poll"})
    )
    assert silent.body["items"] == []
    # And "findings" reads the standing field note on demand.
    asked = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "findings", "agent": "poll"},
        )
    )
    assert "a pattern holds" in asked.body["reply"]
    conn.close()
