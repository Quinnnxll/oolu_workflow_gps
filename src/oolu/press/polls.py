"""The poll floor — fun on the surface, instruments underneath (A3).

A poll is two comparable pieces from the same genre, each rendered with
its contributor's byline, and one question: which? Underneath the fun,
every consented vote is a typed preference event — a labeled pair in the
EXACT shape the standing DPO trainer consumes, and an engagement signal
the member's own edition ranking reads. The instruments never bend the
fun's honesty:

- **Vote first, see second.** Aggregates render only to members who
  voted, and only above the k-anonymity floor — below it the answer is
  "not enough votes yet", with no counts to reverse-engineer a
  neighbor's choice from.
- **One vote, once.** A vote is durable and idempotent: replaying it
  changes nothing; changing your mind is refused — the first vote is
  the vote.
- **The vote is the poll's; the learning is the member's.** The
  aggregate always counts the vote. The preference event (the pairwise
  row, the edition signal) is written ONLY under the member's
  `press.personalize` consent, and revoking consent stops future
  writes immediately. Export is scrubbed, per-member, consent-gated.

Genre scheduling explores: when the member names no genre, a Thompson
draw over per-genre engagement (served vs voted — the ``TraceStore``
pattern in miniature) picks the stream, so fun genres surface and stale
ones retire on evidence, never on an editor's hunch.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ..knowledge.scrubbing import scrub
from .contributions import ContentContribution, PressError
from .standards import agreement

# The k-anonymity floor: below this many votes, no aggregate renders.
# A working default — the shipped number is decision-log item 3.
K_FLOOR = 5
# The comparable band, on content-token agreement: enough shared subject
# to compare (>= MIN), below the near-duplicate territory (< MAX) where
# a "comparison" is really one piece twice. Working defaults.
PAIR_AGREE_MIN = 0.05
PAIR_AGREE_MAX = 0.60
# The DPO export's excerpt cap per side — a preference pair, not a book.
_EXCERPT_CHARS = 600
_CHOICES = ("left", "right")


class PollSide(BaseModel):
    model_config = ConfigDict(frozen=True)

    contribution_id: str
    author: str  # the byline resolves this principal
    title: str
    excerpt: str


class PollPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    pair_id: str
    tenant_id: str
    genre: str
    left: PollSide
    right: PollSide
    created_at: datetime


_PAIRS_SCHEMA = """CREATE TABLE IF NOT EXISTS poll_pairs (
    pair_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    genre TEXT NOT NULL,
    left_id TEXT NOT NULL,
    left_author TEXT NOT NULL,
    left_title TEXT NOT NULL,
    left_excerpt TEXT NOT NULL,
    right_id TEXT NOT NULL,
    right_author TEXT NOT NULL,
    right_title TEXT NOT NULL,
    right_excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""

_VOTES_SCHEMA = """CREATE TABLE IF NOT EXISTS poll_votes (
    pair_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    choice TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (pair_id, principal)
)"""

# Per-genre engagement: how often a served pair earned a vote. The
# Thompson draw reads exactly this.
_GENRE_SCHEMA = """CREATE TABLE IF NOT EXISTS poll_genre_stats (
    tenant_id TEXT NOT NULL,
    genre TEXT NOT NULL,
    served INTEGER NOT NULL DEFAULT 0,
    voted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, genre)
)"""

# The generic pairwise preference store — "this member chose A over B" —
# in the DPO trainer's own vocabulary, with the source named so polls,
# story feedback, and future instruments share one table.
_PAIRWISE_SCHEMA = """CREATE TABLE IF NOT EXISTS press_pairwise (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL,
    chosen TEXT NOT NULL,
    rejected TEXT NOT NULL,
    genres TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class PairwiseStore:
    """Members' pairwise preferences — consent-gated at the doors,
    per-member, scrub-checked again at export (defense in depth)."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_PAIRWISE_SCHEMA)

    def record(
        self,
        *,
        tenant: str,
        principal: str,
        source: str,
        prompt: str,
        chosen: str,
        rejected: str,
        genres: tuple[str, ...] | list[str] = (),
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_pairwise
                     (tenant_id, principal, source, prompt, chosen,
                      rejected, genres, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    principal,
                    source,
                    prompt,
                    chosen,
                    rejected,
                    json.dumps(list(genres)),
                    self._clock().isoformat(),
                ),
            )

    def export(
        self, *, tenant: str, principal: str, limit: int = 10_000
    ) -> list[dict]:
        """The member's OWN pairs in the DPO dataset shape — scrubbed
        once more on the way out, empty or degenerate rows dropped (the
        `build_dpo_dataset` discipline)."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT prompt, chosen, rejected FROM press_pairwise
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY at DESC LIMIT ?""",
                (tenant, principal, int(limit)),
            ).fetchall()
        pairs: list[dict] = []
        for row in rows:
            prompt = scrub(str(row["prompt"]))
            chosen = scrub(str(row["chosen"]))
            rejected = scrub(str(row["rejected"]))
            if not (prompt.strip() and chosen.strip() and rejected.strip()):
                continue
            if chosen.strip() == rejected.strip():
                continue
            pairs.append(
                {"prompt": prompt, "chosen": chosen, "rejected": rejected}
            )
        return pairs

    def erase(self, *, tenant: str, principal: str) -> int:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM press_pairwise"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)


class PollStore:
    """Pairs, votes, and the genre engagement book — durable and
    tenant-scoped. The honesty laws live in :class:`PollDesk`."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_PAIRS_SCHEMA)
            db.execute(_VOTES_SCHEMA)
            db.execute(_GENRE_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    # -- pairs --------------------------------------------------------- #
    def insert_pair(self, pair: PollPair) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO poll_pairs
                     (pair_id, tenant_id, genre,
                      left_id, left_author, left_title, left_excerpt,
                      right_id, right_author, right_title, right_excerpt,
                      created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pair.pair_id,
                    pair.tenant_id,
                    pair.genre,
                    pair.left.contribution_id,
                    pair.left.author,
                    pair.left.title,
                    pair.left.excerpt,
                    pair.right.contribution_id,
                    pair.right.author,
                    pair.right.title,
                    pair.right.excerpt,
                    pair.created_at.isoformat(),
                ),
            )

    def get_pair(self, pair_id: str, *, tenant: str) -> PollPair | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM poll_pairs"
                " WHERE pair_id = ? AND tenant_id = ?",
                (pair_id, tenant),
            ).fetchone()
        return self._pair(row) if row is not None else None

    def pairs_for_genre(self, *, tenant: str, genre: str) -> list[PollPair]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM poll_pairs
                   WHERE tenant_id = ? AND genre = ?
                   ORDER BY created_at DESC, pair_id DESC LIMIT 200""",
                (tenant, genre),
            ).fetchall()
        return [self._pair(row) for row in rows]

    def paired_ids(self, *, tenant: str, genre: str) -> set[frozenset]:
        """Which unordered contribution pairs already exist — mining
        never mints the same comparison twice."""
        return {
            frozenset((p.left.contribution_id, p.right.contribution_id))
            for p in self.pairs_for_genre(tenant=tenant, genre=genre)
        }

    # -- votes --------------------------------------------------------- #
    def vote_of(
        self, pair_id: str, *, tenant: str, principal: str
    ) -> str | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT choice FROM poll_votes
                   WHERE pair_id = ? AND tenant_id = ? AND principal = ?""",
                (pair_id, tenant, principal),
            ).fetchone()
        return str(row["choice"]) if row is not None else None

    def cast(
        self, pair_id: str, *, tenant: str, principal: str, choice: str
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO poll_votes
                     (pair_id, tenant_id, principal, choice, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (pair_id, tenant, principal, choice, self._clock().isoformat()),
            )

    def counts(self, pair_id: str, *, tenant: str) -> dict[str, int]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT choice, COUNT(*) AS n FROM poll_votes
                   WHERE pair_id = ? AND tenant_id = ?
                   GROUP BY choice""",
                (pair_id, tenant),
            ).fetchall()
        counts = {"left": 0, "right": 0}
        for row in rows:
            counts[str(row["choice"])] = int(row["n"])
        return counts

    def voted_pair_ids(self, *, tenant: str, principal: str) -> set[str]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT pair_id FROM poll_votes
                   WHERE tenant_id = ? AND principal = ?""",
                (tenant, principal),
            ).fetchall()
        return {str(row["pair_id"]) for row in rows}

    def erase_votes(self, *, tenant: str, principal: str) -> int:
        """The data subject's right outranks the aggregate: erased votes
        leave the counts, honestly smaller."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM poll_votes"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    # -- genre engagement ---------------------------------------------- #
    def bump_genre(
        self, *, tenant: str, genre: str, served: int = 0, voted: int = 0
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO poll_genre_stats (tenant_id, genre, served, voted)
                   VALUES (?, ?, 0, 0)
                   ON CONFLICT (tenant_id, genre) DO NOTHING""",
                (tenant, genre),
            )
            db.execute(
                """UPDATE poll_genre_stats
                   SET served = served + ?, voted = voted + ?
                   WHERE tenant_id = ? AND genre = ?""",
                (int(served), int(voted), tenant, genre),
            )

    def genre_stats(self, *, tenant: str) -> dict[str, tuple[int, int]]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT genre, served, voted FROM poll_genre_stats"
                " WHERE tenant_id = ?",
                (tenant,),
            ).fetchall()
        return {
            str(row["genre"]): (int(row["served"]), int(row["voted"]))
            for row in rows
        }

    @staticmethod
    def _pair(row) -> PollPair:
        return PollPair(
            pair_id=row["pair_id"],
            tenant_id=row["tenant_id"],
            genre=row["genre"],
            left=PollSide(
                contribution_id=row["left_id"],
                author=row["left_author"],
                title=row["left_title"],
                excerpt=row["left_excerpt"],
            ),
            right=PollSide(
                contribution_id=row["right_id"],
                author=row["right_author"],
                title=row["right_title"],
                excerpt=row["right_excerpt"],
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# --------------------------------------------------------------------------- #
# Mining and the desk.                                                         #
# --------------------------------------------------------------------------- #
def comparable(a: ContentContribution, b: ContentContribution) -> bool:
    """Two pieces compare when they share a subject band, come from
    different authors, and neither is the other's near-duplicate — a
    comparison of one piece with its own retelling is no comparison."""
    if a.author == b.author:
        return False
    if a.similar_to == b.contribution_id or b.similar_to == a.contribution_id:
        return False
    score = agreement(f"{a.title}\n{a.body}", f"{b.title}\n{b.body}")
    return PAIR_AGREE_MIN <= score < PAIR_AGREE_MAX


def _side(record: ContentContribution) -> PollSide:
    return PollSide(
        contribution_id=record.contribution_id,
        author=record.author,
        title=record.title,
        excerpt=record.body[:_EXCERPT_CHARS],
    )


class PollDesk:
    """The honesty laws, in one place: serving, voting, revealing."""

    def __init__(
        self,
        polls: PollStore,
        pairwise: PairwiseStore | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._polls = polls
        self._pairwise = pairwise
        self._rng = rng or random.Random()

    @property
    def store(self) -> PollStore:
        return self._polls

    @property
    def pairwise(self) -> PairwiseStore | None:
        return self._pairwise

    # -- serving ------------------------------------------------------- #
    def pick_genre(self, *, tenant: str, candidates: list[str]) -> str:
        """The Thompson draw over per-genre engagement: served vs voted
        as a Beta posterior, one sample per candidate, best sample wins.
        A genre with no evidence draws from the uniform prior — new
        streams get their chance."""
        if not candidates:
            raise PressError("no genre has comparable material yet", status=404)
        stats = self._polls.genre_stats(tenant=tenant)
        best, best_sample = candidates[0], -1.0
        for genre in candidates:
            served, voted = stats.get(genre, (0, 0))
            sample = self._rng.betavariate(
                voted + 1, max(served - voted, 0) + 1
            )
            if sample > best_sample:
                best, best_sample = genre, sample
        return best

    def next_pair(
        self,
        *,
        tenant: str,
        principal: str,
        corpus: list[ContentContribution],
        genre: str | None = None,
    ) -> PollPair:
        """A pair this member has not voted on — an existing one first,
        a freshly mined one second, an honest refusal third."""
        by_genre: dict[str, list[ContentContribution]] = {}
        for record in corpus:
            for key in record.genres:
                by_genre.setdefault(key, []).append(record)
        if genre is None:
            candidates = sorted(k for k, v in by_genre.items() if len(v) >= 2)
            genre = self.pick_genre(tenant=tenant, candidates=candidates)
        voted = self._polls.voted_pair_ids(tenant=tenant, principal=principal)
        for pair in self._polls.pairs_for_genre(tenant=tenant, genre=genre):
            if pair.pair_id not in voted:
                self._polls.bump_genre(tenant=tenant, genre=genre, served=1)
                return pair
        mined = self._mine(
            tenant=tenant, genre=genre, records=by_genre.get(genre, [])
        )
        if mined is None:
            raise PressError(
                f"not enough comparable material in {genre!r} yet — "
                "publish more contributions and ask again",
                status=404,
            )
        self._polls.insert_pair(mined)
        self._polls.bump_genre(tenant=tenant, genre=genre, served=1)
        return mined

    def _mine(
        self, *, tenant: str, genre: str, records: list[ContentContribution]
    ) -> PollPair | None:
        existing = self._polls.paired_ids(tenant=tenant, genre=genre)
        for i, a in enumerate(records):
            for b in records[i + 1 :]:
                key = frozenset((a.contribution_id, b.contribution_id))
                if key in existing:
                    continue
                if not comparable(a, b):
                    continue
                return PollPair(
                    pair_id=str(uuid4()),
                    tenant_id=tenant,
                    genre=genre,
                    left=_side(a),
                    right=_side(b),
                    created_at=self._polls.now(),
                )
        return None

    # -- voting and revealing ------------------------------------------ #
    def vote(
        self,
        pair_id: str,
        *,
        tenant: str,
        principal: str,
        choice: str,
        learning: bool = False,
    ) -> dict:
        """One idempotent vote. The aggregate always counts it; the
        pairwise preference row is written only when ``learning`` (the
        member's consent, checked at the door) is true."""
        if choice not in _CHOICES:
            raise PressError("choice must be left or right")
        pair = self._polls.get_pair(pair_id, tenant=tenant)
        if pair is None:
            raise PressError("no such poll", status=404)
        standing = self._polls.vote_of(
            pair_id, tenant=tenant, principal=principal
        )
        if standing is not None:
            if standing == choice:
                return self.reveal(pair_id, tenant=tenant, principal=principal)
            raise PressError(
                "your vote already stands — the first vote is the vote",
                status=409,
            )
        self._polls.cast(
            pair_id, tenant=tenant, principal=principal, choice=choice
        )
        self._polls.bump_genre(tenant=tenant, genre=pair.genre, voted=1)
        if learning and self._pairwise is not None:
            winner = pair.left if choice == "left" else pair.right
            loser = pair.right if choice == "left" else pair.left
            self._pairwise.record(
                tenant=tenant,
                principal=principal,
                source="poll",
                prompt=(
                    f"[{pair.genre}] Two member pieces on the same "
                    f"subject — which serves the reader better?\n"
                    f"A: {pair.left.title}\nB: {pair.right.title}"
                ),
                chosen=f"{winner.title}\n{winner.excerpt}",
                rejected=f"{loser.title}\n{loser.excerpt}",
                genres=(pair.genre,),
            )
        return self.reveal(pair_id, tenant=tenant, principal=principal)

    def reveal(
        self, pair_id: str, *, tenant: str, principal: str
    ) -> dict:
        """The floor laws, in one read: nothing before your vote, no
        counts below the floor — not even the total."""
        pair = self._polls.get_pair(pair_id, tenant=tenant)
        if pair is None:
            raise PressError("no such poll", status=404)
        mine = self._polls.vote_of(pair_id, tenant=tenant, principal=principal)
        if mine is None:
            return {
                "pair_id": pair_id,
                "voted": False,
                "revealed": False,
                "reason": "vote first — results follow your own choice",
            }
        counts = self._polls.counts(pair_id, tenant=tenant)
        total = counts["left"] + counts["right"]
        if total < K_FLOOR:
            return {
                "pair_id": pair_id,
                "voted": True,
                "choice": mine,
                "revealed": False,
                "reason": "not enough votes yet",
            }
        return {
            "pair_id": pair_id,
            "voted": True,
            "choice": mine,
            "revealed": True,
            "counts": counts,
            "total": total,
        }
