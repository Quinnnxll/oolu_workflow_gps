"""Evidence — the three verified inputs a comparison may trust (A6).

Nothing here is self-declared (the standing marketplace lesson): a
review needs a finished order by its own buyer, the trust score derives
from the durable order book, and lab evidence is a live press
contribution (genre ``results``) its own author attached — byline-
labeled and, because it IS a contribution, dividend-eligible like any
other. Who may certify a lab beyond self-attachment is decision-log
item 6; v1 ships self-attached member results, honestly labeled as
member-measured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# An order pays at acceptance (M1) — that is also when its buyer has
# something real to review. Completed keeps the door open after escrow.
REVIEW_FINISHED_STATES = ("accepted", "completed")
MAX_REVIEW_CHARS = 2_000
# The pulse sentinel for followed interests: a schedule whose goal reads
# "explorer.brief:<category>:<mode>" fires a brief into the thread.
EXPLORER_BRIEF_PREFIX = "explorer.brief:"


class ExplorerError(ValueError):
    """A refused explorer move — the message is the direction."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ProductReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_id: str
    tenant_id: str
    listing_id: str
    reviewer: str  # the byline resolves this principal
    order_id: str  # the finished order that earned the voice
    rating: int  # 1..5
    words: str
    created_at: datetime


_REVIEWS_SCHEMA = """CREATE TABLE IF NOT EXISTS explorer_reviews (
    review_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    order_id TEXT NOT NULL,
    rating INTEGER NOT NULL,
    words TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, listing_id, reviewer)
)"""


class ReviewStore:
    """One voice per member per product, durable and tenant-scoped."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_REVIEWS_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def insert(self, review: ProductReview) -> None:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO explorer_reviews
                     (review_id, tenant_id, listing_id, reviewer,
                      order_id, rating, words, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review.review_id,
                    review.tenant_id,
                    review.listing_id,
                    review.reviewer,
                    review.order_id,
                    review.rating,
                    review.words,
                    review.created_at.isoformat(),
                ),
            )
        if not (getattr(cursor, "rowcount", 0) or 0):
            raise ExplorerError(
                "you already reviewed this — one voice per product", status=409
            )

    def for_listing(self, listing_id: str, *, tenant: str) -> list[ProductReview]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM explorer_reviews
                   WHERE tenant_id = ? AND listing_id = ?
                   ORDER BY created_at DESC, review_id DESC""",
                (tenant, listing_id),
            ).fetchall()
        return [
            ProductReview(
                review_id=r["review_id"],
                tenant_id=r["tenant_id"],
                listing_id=r["listing_id"],
                reviewer=r["reviewer"],
                order_id=r["order_id"],
                rating=int(r["rating"]),
                words=r["words"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]


class ReviewDesk:
    """The verified-buyer gate: the ratings.py verified-run law, applied
    to commerce. The desk holds the rules; the gateway resolves the
    order and the listing and appends the audit line."""

    def __init__(self, store: ReviewStore) -> None:
        self._store = store

    @property
    def store(self) -> ReviewStore:
        return self._store

    def review(
        self,
        *,
        tenant: str,
        reviewer: str,
        listing,  # marketplace.catalog.Listing
        order,  # marketplace.orders.StoredOrder | None
        rating: int,
        words: str = "",
    ) -> ProductReview:
        if order is None:
            raise ExplorerError(
                "a review needs a finished order — buy it, receive it, "
                "then say what it was like",
                status=404,
            )
        if order.state not in REVIEW_FINISHED_STATES:
            raise ExplorerError(
                f"the order is {order.state!r} — reviews follow acceptance"
            )
        if order.record.buyer_principal != reviewer:
            raise ExplorerError("that is not your order", status=403)
        if order.record.offer.item_id != listing.listing_id:
            raise ExplorerError("that order bought something else")
        if reviewer == listing.seller_principal:
            # The standing self-dealing law: your own shop earns no voice.
            raise ExplorerError("no self-reviews", status=403)
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ExplorerError("the rating is 1 to 5")
        words = str(words or "").strip()
        if len(words) > MAX_REVIEW_CHARS:
            raise ExplorerError(
                f"a review speaks, it does not lecture (cap "
                f"{MAX_REVIEW_CHARS} chars)"
            )
        review = ProductReview(
            review_id=str(uuid4()),
            tenant_id=tenant,
            listing_id=listing.listing_id,
            reviewer=reviewer,
            order_id=order.record.order_id,
            rating=rating,
            words=words,
            created_at=self._store.now(),
        )
        self._store.insert(review)
        return review


def feedback_of(reviews: list[ProductReview]) -> dict:
    """The feedback factor: verified voices only, honestly counted.
    No reviews = no claim — the factor sits at the neutral 0.5, exactly
    the no-evidence posture the reliability multiplier takes."""
    if not reviews:
        return {"count": 0, "mean": None, "factor": 0.5}
    mean = sum(r.rating for r in reviews) / len(reviews)
    return {
        "count": len(reviews),
        "mean": round(mean, 2),
        "factor": round(mean / 5.0, 4),
    }


def trust_from_book(*, finished: int, refunded: int, disputed: int) -> dict:
    """The browsable trust score: the PurchaseFacts derivations made a
    persistent, explainable read model. Derived from the durable order
    book, never from a seller's claim; a fresh seller sits at the
    no-evidence neutral, and trouble weighs double."""
    finished, refunded, disputed = int(finished), int(refunded), int(disputed)
    if finished + refunded + disputed == 0:
        return {
            "score": 0.5,
            "finished": 0,
            "refunded": 0,
            "disputed": 0,
            "basis": "no order history",
        }
    score = finished / (finished + 2 * (refunded + disputed) + 1)
    return {
        "score": round(score, 4),
        "finished": finished,
        "refunded": refunded,
        "disputed": disputed,
        "basis": "derived from the order book",
    }


class LabReport(BaseModel):
    """A measured result bound to a listing — the evidence IS a press
    contribution (genre ``results``), so the byline, the provenance,
    and the dividend all come for free."""

    model_config = ConfigDict(frozen=True)

    report_id: str
    tenant_id: str
    listing_id: str
    contribution_id: str  # the live press contribution the numbers live in
    author: str  # the contribution's author — the byline
    score: int  # 0..100, the report's overall verdict
    metrics: dict  # named measurements, free-form but typed at the door
    created_at: datetime


_LAB_SCHEMA = """CREATE TABLE IF NOT EXISTS explorer_lab_reports (
    report_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    author TEXT NOT NULL,
    score INTEGER NOT NULL,
    metrics TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, listing_id, contribution_id)
)"""


class LabStore:
    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_LAB_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def insert(self, report: LabReport) -> None:
        import json

        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO explorer_lab_reports
                     (report_id, tenant_id, listing_id, contribution_id,
                      author, score, metrics, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.report_id,
                    report.tenant_id,
                    report.listing_id,
                    report.contribution_id,
                    report.author,
                    report.score,
                    json.dumps(report.metrics),
                    report.created_at.isoformat(),
                ),
            )
        if not (getattr(cursor, "rowcount", 0) or 0):
            raise ExplorerError(
                "that contribution already backs a report on this listing",
                status=409,
            )

    def for_listing(self, listing_id: str, *, tenant: str) -> list[LabReport]:
        import json

        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM explorer_lab_reports
                   WHERE tenant_id = ? AND listing_id = ?
                   ORDER BY created_at DESC, report_id DESC""",
                (tenant, listing_id),
            ).fetchall()
        return [
            LabReport(
                report_id=r["report_id"],
                tenant_id=r["tenant_id"],
                listing_id=r["listing_id"],
                contribution_id=r["contribution_id"],
                author=r["author"],
                score=int(r["score"]),
                metrics=json.loads(r["metrics"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]


def lab_of(reports: list[LabReport]) -> dict:
    """The lab factor: member-measured results, honestly counted; no
    reports = the neutral 0.5, never a manufactured verdict."""
    if not reports:
        return {"count": 0, "mean_score": None, "factor": 0.5}
    mean = sum(r.score for r in reports) / len(reports)
    return {
        "count": len(reports),
        "mean_score": round(mean, 1),
        "factor": round(mean / 100.0, 4),
    }


def lab_report(
    *,
    tenant: str,
    listing_id: str,
    contribution,  # press.ContentContribution — resolved LIVE by the door
    caller: str,
    score: int,
    metrics: dict | None = None,
    now: datetime,
) -> LabReport:
    """The attachment gate: the evidence must be a LIVE results
    contribution, attached by its OWN author — provenance and byline are
    the contribution's, not this record's claim."""
    if contribution is None:
        raise ExplorerError(
            "the lab evidence must be a live contribution — publish the "
            "results first (genre: Lab & tests)",
            status=404,
        )
    if "results" not in contribution.genres:
        raise ExplorerError(
            "lab evidence lives in the 'results' genre — publish it there"
        )
    if contribution.author != caller:
        raise ExplorerError(
            "only the author may attach their results", status=403
        )
    score = int(score)
    if not 0 <= score <= 100:
        raise ExplorerError("the overall score is 0 to 100")
    return LabReport(
        report_id=str(uuid4()),
        tenant_id=tenant,
        listing_id=listing_id,
        contribution_id=contribution.contribution_id,
        author=contribution.author,
        score=score,
        metrics=dict(metrics or {}),
        created_at=now,
    )
