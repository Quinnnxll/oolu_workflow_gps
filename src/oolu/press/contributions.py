"""Contributions — publish, attribute, weigh (agents-expansion plan A1).

A :class:`ContentContribution` is a member's published material: text and
drawer-file references (refs, never copies), genre-keyed against the one
taxonomy, author-attributed, licensed, and durable. Publication walks a
gate, in order, and refuses loudly at the first failure:

1. **Consent + license.** Publishing without the explicit consent tick
   and a known license is refused — consent detached from the act is not
   consent (the growth-offer law, applied to publication).
2. **Words and bounds.** A contribution has a title and a body, within
   caps; junk is refused with the cap named.
3. **The taxonomy.** One to three genre keys from the versioned registry;
   an unknown key is refused by name.
4. **The scrub gate.** ``knowledge/scrubbing`` decides: a text that would
   need scrubbing is REFUSED with each leak named ("an email address, an
   API key…") — published prose with redaction holes is broken prose, so
   the author fixes the text; the platform never silently rewrites it.
5. **The neighbor check.** ``nodeplace/plagiarism.similarity`` over the
   tenant's live contributions: a near-duplicate is flagged with the
   original credited (``similar_to``), so composition and (later) the
   dividend can weigh the copy honestly. Flagged, not refused — retelling
   is human; uncredited retelling is the defect.

Unpublishing supersedes (the ``memoryspine`` WHERE-clause law): future
reads exclude the record, history is never erased, and the act lands on
the audit chain next to the publication it reverses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ..knowledge.scrubbing import scrub
from ..nodeplace.plagiarism import similarity
from .taxonomy import GENRES, TAXONOMY_VERSION

MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 20_000  # a piece, not a book (the message-cap kinship)
MAX_GENRES = 3
MAX_MEDIA = 6
# At or above this Jaccard similarity a contribution is flagged as a
# near-duplicate and credits the original. Flag, never refuse (gate 5).
SIMILARITY_FLAG = 0.60
# The neighbor scan reads the most recent live pieces, not the whole
# archive — the flag is about freshly retold material, and the scan cost
# stays bounded on old tenants.
_NEIGHBOR_SCAN_LIMIT = 500


class PressError(ValueError):
    """A refused press move — the message is the direction."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class License(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    terms: str


# The stated license a contribution publishes under. One license at A1 —
# named and versioned so a future second license is an addition, never a
# silent reinterpretation of standing records.
LICENSES: dict[str, License] = {
    lic.key: lic
    for lic in (
        License(
            key="oolu-members-1",
            name="OoLu members license v1",
            terms=(
                "Visible to members of your OoLu server. May be composed "
                "into stories and polls that always credit you by name "
                "and photo. You may unpublish at any time — future use "
                "stops; history is never erased. Eligible for the "
                "contributor revenue share when advertising arrives."
            ),
        ),
    )
}


class MediaRef(BaseModel):
    """A drawer file, by reference — never a copy. ``blob_ref`` is the
    content address (``sha256:…``) when the file is blob-backed, so the
    reference is self-verifying; inline drawer rows carry no blob."""

    model_config = ConfigDict(frozen=True)

    file_id: str
    blob_ref: str = ""
    media_type: str = ""
    name: str = ""


class ContentContribution(BaseModel):
    model_config = ConfigDict(frozen=True)

    contribution_id: str
    tenant_id: str
    author: str  # the byline resolves this principal
    title: str
    body: str
    genres: tuple[str, ...]
    license: str
    media: tuple[MediaRef, ...] = ()
    # The neighbor check's verdict: the credited original, when flagged.
    similar_to: str | None = None
    similarity: float | None = None
    taxonomy_version: int = TAXONOMY_VERSION
    created_at: datetime
    superseded_at: datetime | None = None


# The placeholders the scrubber introduces, named for humans — the loud
# refusal lists these so the author knows exactly what to remove.
_LEAK_NAMES: tuple[tuple[str, str], ...] = (
    ("<EMAIL>", "an email address"),
    ("<AWS_KEY>", "an AWS key"),
    ("<API_KEY>", "an API key"),
    ("<GH_TOKEN>", "a GitHub token"),
    ("<JWT>", "a signed token (JWT)"),
    ("<TOKEN>", "a bearer token"),
    ("<REDACTED>", "a KEY/SECRET/PASSWORD value"),
    ("<CREDS>", "credentials inside a URL"),
    ("<PATH>", "a machine file path"),
    ("<IP>", "an IP address"),
)


def leak_report(text: str) -> list[str]:
    """What the scrub gate would remove, in words. Empty = safe."""
    scrubbed = scrub(text)
    if scrubbed == text:
        return []
    found = []
    for placeholder, name in _LEAK_NAMES:
        if placeholder in scrubbed and placeholder not in text:
            found.append(name)
    # Scrubbing changed the text but no known placeholder surfaced (a
    # future scrubber shape): still refuse, generically named.
    return found or ["secret-shaped material"]


_SCHEMA = """CREATE TABLE IF NOT EXISTS press_contributions (
    contribution_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    genres TEXT NOT NULL,
    license TEXT NOT NULL,
    media TEXT NOT NULL,
    similar_to TEXT,
    similarity REAL,
    taxonomy_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT
)"""


class ContributionStore:
    """Durable, tenant-scoped contributions. Supersession is a WHERE
    clause: default reads exclude superseded rows in SQL; nothing here
    can erase a row."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def insert(self, record: ContentContribution) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_contributions
                     (contribution_id, tenant_id, author, title, body,
                      genres, license, media, similar_to, similarity,
                      taxonomy_version, created_at, superseded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.contribution_id,
                    record.tenant_id,
                    record.author,
                    record.title,
                    record.body,
                    json.dumps(list(record.genres)),
                    record.license,
                    json.dumps([m.model_dump() for m in record.media]),
                    record.similar_to,
                    record.similarity,
                    record.taxonomy_version,
                    record.created_at.isoformat(),
                    None,
                ),
            )

    def get(
        self,
        contribution_id: str,
        *,
        tenant: str,
        include_superseded: bool = False,
    ) -> ContentContribution | None:
        where = "contribution_id = ? AND tenant_id = ?"
        if not include_superseded:
            where += " AND superseded_at IS NULL"
        with self._conn.lock:
            row = self._conn.db.execute(
                f"SELECT * FROM press_contributions WHERE {where}",
                (contribution_id, tenant),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(
        self,
        *,
        tenant: str,
        genre: str | None = None,
        author: str | None = None,
        limit: int = 100,
        include_superseded: bool = False,
    ) -> list[ContentContribution]:
        """Newest first. The genre filter joins on the taxonomy key."""
        where = "tenant_id = ?"
        args: list = [tenant]
        if author is not None:
            where += " AND author = ?"
            args.append(author)
        if not include_superseded:
            where += " AND superseded_at IS NULL"
        with self._conn.lock:
            rows = self._conn.db.execute(
                f"""SELECT * FROM press_contributions WHERE {where}
                    ORDER BY created_at DESC, contribution_id DESC LIMIT ?""",
                (*args, int(limit) if genre is None else _NEIGHBOR_SCAN_LIMIT),
            ).fetchall()
        records = [self._record(row) for row in rows]
        if genre is not None:
            records = [r for r in records if genre in r.genres][: int(limit)]
        return records

    def supersede(
        self, contribution_id: str, *, tenant: str
    ) -> ContentContribution | None:
        """Stamp the moment; report the record as it now stands. A second
        supersession is a no-op that returns None — the first stamp is
        the truth."""
        stamp = self._clock().isoformat()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE press_contributions SET superseded_at = ?
                    WHERE contribution_id = ? AND tenant_id = ?
                      AND superseded_at IS NULL""",
                (stamp, contribution_id, tenant),
            )
        if not (getattr(cursor, "rowcount", 0) or 0):
            return None
        return self.get(contribution_id, tenant=tenant, include_superseded=True)

    def live_texts(self, *, tenant: str) -> list[tuple[str, str]]:
        """(id, title+body) of the most recent live pieces — the neighbor
        check's corpus."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT contribution_id, title, body FROM press_contributions
                   WHERE tenant_id = ? AND superseded_at IS NULL
                   ORDER BY created_at DESC, contribution_id DESC LIMIT ?""",
                (tenant, _NEIGHBOR_SCAN_LIMIT),
            ).fetchall()
        return [
            (row["contribution_id"], f"{row['title']}\n{row['body']}")
            for row in rows
        ]

    def _record(self, row) -> ContentContribution:
        return ContentContribution(
            contribution_id=row["contribution_id"],
            tenant_id=row["tenant_id"],
            author=row["author"],
            title=row["title"],
            body=row["body"],
            genres=tuple(json.loads(row["genres"])),
            license=row["license"],
            media=tuple(
                MediaRef(**m) for m in json.loads(row["media"] or "[]")
            ),
            similar_to=row["similar_to"],
            similarity=row["similarity"],
            taxonomy_version=int(row["taxonomy_version"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            superseded_at=(
                datetime.fromisoformat(row["superseded_at"])
                if row["superseded_at"]
                else None
            ),
        )


class PressDesk:
    """The publication gate, walked in order — and the unpublish door.

    The desk owns the RULES; the gateway owns the doors and the audit
    appends (one audit voice, like every other surface). From A2 the
    desk also carries the newsroom's stores (stories, preferences) —
    optional, so a pre-A2 composition stays a working press."""

    def __init__(
        self,
        store: ContributionStore,
        *,
        stories=None,
        preferences=None,
        polls=None,
        intake=None,
    ) -> None:
        self._store = store
        self._stories = stories  # newsroom.StoryStore | None
        self._preferences = preferences  # editions.PreferenceStore | None
        self._polls = polls  # polls.PollDesk | None
        self._intake = intake  # intake.IntakeStore | None

    @property
    def store(self) -> ContributionStore:
        return self._store

    @property
    def stories(self):
        return self._stories

    @property
    def preferences(self):
        return self._preferences

    @property
    def polls(self):
        return self._polls

    @property
    def intake(self):
        return self._intake

    def publish(
        self,
        *,
        tenant: str,
        author: str,
        title: str,
        body: str,
        genres: tuple[str, ...] | list[str],
        license: str,
        consent: bool,
        media: tuple[MediaRef, ...] | list[MediaRef] = (),
    ) -> ContentContribution:
        # Gate 1 — consent + license.
        if consent is not True:
            raise PressError(
                "publishing needs your explicit consent — tick the consent "
                "box after reading the license terms"
            )
        if license not in LICENSES:
            known = ", ".join(sorted(LICENSES))
            raise PressError(
                f"unknown license: {license!r} — publish under one of: {known}"
            )
        # Gate 2 — words and bounds.
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title:
            raise PressError("a contribution needs a title")
        if len(title) > MAX_TITLE_CHARS:
            raise PressError(
                f"the title is too long ({len(title)} chars; the cap is "
                f"{MAX_TITLE_CHARS})"
            )
        if not body:
            raise PressError("a contribution needs words")
        if len(body) > MAX_BODY_CHARS:
            raise PressError(
                f"the body is too long ({len(body)} chars; the cap is "
                f"{MAX_BODY_CHARS})"
            )
        # Gate 3 — the taxonomy.
        keys = tuple(str(g).strip() for g in genres if str(g).strip())
        if not keys:
            raise PressError("pick at least one genre")
        if len(keys) > MAX_GENRES:
            raise PressError(f"at most {MAX_GENRES} genres")
        for key in keys:
            if key not in GENRES:
                known = ", ".join(GENRES)
                raise PressError(
                    f"unknown genre: {key!r} — the taxonomy has: {known}"
                )
        media = tuple(media)
        if len(media) > MAX_MEDIA:
            raise PressError(f"at most {MAX_MEDIA} attached files")
        # Gate 4 — the scrub gate, loud.
        leaks = leak_report(f"{title}\n{body}")
        if leaks:
            raise PressError(
                "publishing this would leak "
                + ", ".join(leaks)
                + " — remove it from the text and try again (the platform "
                "never rewrites your words)"
            )
        # Gate 5 — the neighbor check: flag, credit, never refuse.
        similar_to: str | None = None
        best = 0.0
        text = f"{title}\n{body}"
        for other_id, other_text in self._store.live_texts(tenant=tenant):
            score = similarity(text, other_text)
            if score > best:
                best, similar_to = score, other_id
        if best < SIMILARITY_FLAG:
            similar_to, best = None, 0.0
        record = ContentContribution(
            contribution_id=str(uuid4()),
            tenant_id=tenant,
            author=author,
            title=title,
            body=body,
            genres=keys,
            license=license,
            media=media,
            similar_to=similar_to,
            similarity=round(best, 4) if similar_to else None,
            taxonomy_version=TAXONOMY_VERSION,
            created_at=self._store.now(),
        )
        self._store.insert(record)
        return record

    def unpublish(
        self, contribution_id: str, *, tenant: str, author: str
    ) -> ContentContribution:
        record = self._store.get(
            contribution_id, tenant=tenant, include_superseded=True
        )
        if record is None:
            raise PressError("no such contribution", status=404)
        if record.author != author:
            # Someone else's byline is someone else's decision.
            raise PressError(
                "only the author may unpublish this", status=403
            )
        if record.superseded_at is not None:
            return record  # already resting; the first stamp is the truth
        superseded = self._store.supersede(contribution_id, tenant=tenant)
        return superseded if superseded is not None else record
