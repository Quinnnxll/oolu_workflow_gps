from __future__ import annotations

from collections.abc import Callable

from .errors import RatingError, UnverifiedRunError
from .models import Rating
from .reputation import mu_from_ratings

VerifiedRun = Callable[[str, str], bool]  # (version_id, rater_principal) -> bool

_SCHEMA = """CREATE TABLE IF NOT EXISTS ratings (
    rating_id TEXT PRIMARY KEY,
    subject_version_id TEXT NOT NULL,
    rater_principal TEXT NOT NULL,
    score INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (subject_version_id, rater_principal)
)"""


class RatingStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def add(self, rating: Rating) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO ratings
                   (rating_id, subject_version_id, rater_principal, score,
                    payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    rating.rating_id,
                    rating.subject_version_id,
                    rating.rater_principal,
                    rating.score,
                    rating.model_dump_json(),
                    rating.created_at.isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def for_version(self, version_id: str) -> list[Rating]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM ratings WHERE subject_version_id = ?"
                " ORDER BY created_at ASC",
                (version_id,),
            ).fetchall()
        return [Rating.model_validate_json(row["payload_json"]) for row in rows]

    def stats(self, version_id: str) -> tuple[int, float]:
        ratings = self.for_version(version_id)
        if not ratings:
            return 0, 0.0
        return len(ratings), sum(r.score for r in ratings) / len(ratings)


class RatingService:
    def __init__(self, store: RatingStore, *, verified_run: VerifiedRun) -> None:
        self._store = store
        self._verified_run = verified_run

    def rate(
        self, *, rater_principal: str, version_id: str, score: int, text: str = ""
    ) -> Rating:
        if not 1 <= score <= 5:
            raise RatingError("score must be between 1 and 5")
        if not self._verified_run(version_id, rater_principal):
            raise UnverifiedRunError(
                "only a rater with a verified successful run of this version may rate it"
            )
        rating = Rating(
            subject_version_id=version_id,
            rater_principal=rater_principal,
            score=score,
            text=text,
            verified_run=True,
        )
        if not self._store.add(rating):
            raise RatingError("this principal has already rated this version")
        return rating

    def ratings(self, version_id: str) -> list[Rating]:
        return self._store.for_version(version_id)

    def reputation(self, version_id: str, *, mu_max: float = 2.0) -> float:
        count, average = self._store.stats(version_id)
        return mu_from_ratings(average, count, mu_max=mu_max)
