"""Phase 5 of the context-harness plan: memory and continuity.

One retrieval scorer serves every recall site (words + character
trigrams behind the Embedder seam); the build ledger makes a failed
build durable across turns, restarts, and processes — its lessons enter
the next attempt's context pack, and a publish supersedes them; and the
spec's acceptance scenario holds end to end: daily chat interrupts a
build, and the retry resumes knowing exactly what already failed.
"""

from __future__ import annotations

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import CRASHING, GOOD, _FailingScriptHand

from oolu.buildledger import BuildLedger
from oolu.durable.connection import DurableConnection
from oolu.retrieval import LexicalEmbedder, cosine, score, shares_words


# --------------------------------------------------------------------------- #
# The one scorer                                                               #
# --------------------------------------------------------------------------- #
def test_identical_texts_score_one_and_disjoint_score_zero():
    assert abs(score("normalize invoice csv", "normalize invoice csv") - 1.0) < 1e-9
    assert score("normalize invoice csv", "water the plants") < 0.05
    assert score("", "anything") == 0.0


def test_trigrams_catch_morphology_where_token_overlap_went_blind():
    related = score("normalizing the invoices", "normalize invoice csv files")
    unrelated = score("normalizing the invoices", "send a short notification")
    assert related > unrelated
    assert related > 0.2  # word overlap alone is zero here bar 'the'


def test_the_silence_gate_is_separate_from_the_scorer():
    # Trigrams alone must never manufacture a memory.
    assert not shares_words("running", "jumping")
    assert shares_words("normalize invoices", "invoices arrived")


def test_the_embedder_seam_is_the_upgrade_path():
    class Doubler:
        def embed(self, text):
            return {"x": 2.0} if text else {}

    # Any Embedder slots into the same call — this is what a model-backed
    # index implements to upgrade every consumer at once.
    assert score("a", "b", embedder=Doubler()) == 1.0
    assert cosine(LexicalEmbedder().embed("slug"), {}) == 0.0


# --------------------------------------------------------------------------- #
# The build ledger                                                             #
# --------------------------------------------------------------------------- #
def test_a_refusal_admits_a_lesson_with_provenance(tmp_path):
    conn = DurableConnection(tmp_path / "ledger.db")
    try:
        ledger = BuildLedger(conn)
        attempt = ledger.record(
            "t1",
            "skill-1",
            "normalize invoice csv files",
            status="refused",
            script="broken",
            problem="NameError: undefined_name",
            states=("proposed", "generated", "repair:NameError"),
        )
        lessons = ledger.lessons_for("t1", "skill-1")
        assert len(lessons) == 1
        assert "NameError" in lessons[0]
        failure = ledger.last_failure("t1", "skill-1")
        assert failure["attempt_id"] == attempt
        assert failure["script"] == "broken"
    finally:
        conn.close()


def test_a_publish_supersedes_the_goals_lessons(tmp_path):
    conn = DurableConnection(tmp_path / "ledger.db")
    try:
        ledger = BuildLedger(conn)
        ledger.record(
            "t1", "skill-1", "the goal", status="refused", problem="boom"
        )
        ledger.record(
            "t1", "skill-1", "the goal", status="published", node_id="n-1"
        )
        # Corrections beat stale warnings: nothing enters future packs...
        assert ledger.lessons_for("t1", "skill-1") == []
        assert ledger.last_failure("t1", "skill-1") is None
        # ...but the ledger never forgets — it only supersedes.
        history = ledger.attempts("t1", "skill-1")
        assert [row["status"] for row in history] == ["refused", "published"]
    finally:
        conn.close()


def test_the_ledger_survives_a_new_connection(tmp_path):
    first = DurableConnection(tmp_path / "ledger.db")
    BuildLedger(first).record(
        "t1", "skill-9", "the goal", status="refused", problem="TypeError: drift"
    )
    first.close()
    second = DurableConnection(tmp_path / "ledger.db")
    try:
        assert BuildLedger(second).lessons_for("t1", "skill-9")
    finally:
        second.close()


def test_lessons_are_tenant_walled(tmp_path):
    conn = DurableConnection(tmp_path / "ledger.db")
    try:
        ledger = BuildLedger(conn)
        ledger.record("t1", "skill-1", "goal", status="refused", problem="x")
        assert ledger.lessons_for("t2", "skill-1") == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The acceptance scenario: chat interrupts coding, the work survives           #
# --------------------------------------------------------------------------- #
class _EavesdroppingAuthor(FakeAuthor):
    """Always answers with the crashing function; records every prompt
    so the retry's context pack is inspectable."""

    def __init__(self):
        super().__init__(CRASHING)
        self.user_contents: list[str] = []

    def reply(self, messages):
        system = str(messages[0].get("content", ""))
        if system.startswith("You are the function writer"):
            self.user_contents.append(str(messages[-1].get("content", "")))
        return CRASHING


def test_a_failed_build_teaches_the_retry_across_unrelated_turns(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {
            "script": _FailingScriptHand("undefined_name")
        }
        author = _EavesdroppingAuthor()
        app._node_function_author = lambda tenant: author

        # The build fails birth verification and refuses to publish.
        first = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" in first.body["reply"]
        # First attempt wrote nearly blind: no lesson yet.
        assert "previous attempt" not in author.user_contents[0]

        # Daily chat interrupts — unrelated turns, the offer machinery,
        # anything; the build state lives in the LEDGER, not the window.
        _chat(app, ident, "thanks, unrelated question about my day")

        # The retry resumes knowing exactly what already failed.
        second = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" in second.body["reply"]
        retry_prompt = author.user_contents[-1]
        assert "previous attempt at this goal failed" in retry_prompt
        assert "NameError" in retry_prompt
    finally:
        conn.close()


def test_a_publish_clears_the_warning_from_future_packs(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {
            "script": _FailingScriptHand("undefined_name")
        }

        crashing = FakeAuthor(CRASHING)
        app._node_function_author = lambda tenant: crashing
        _chat(app, ident, f"build me a node that {GOAL}")

        good = FakeAuthor(GOOD)
        app._node_function_author = lambda tenant: good
        built = _chat(app, ident, f"build me a node that {GOAL}")
        assert "failed birth verification" not in built.body["reply"]

        ledger = app._build_ledger()
        goal_key = app._function_skill_id("t1", GOAL)
        assert ledger.lessons_for("t1", goal_key) == []
        statuses = [row["status"] for row in ledger.attempts("t1", goal_key)]
        assert statuses == ["refused", "published"]
    finally:
        conn.close()
