"""The calendar record model — events, busy intervals, consented sharing.

The agents-expansion survey's hard wall, closed: before this module the
codebase had no ``Event`` or ``TimeSlot`` type anywhere — the word
"calendar" was a starter-node goal string. This is the one calendar
OoLu's conversation, the starter shelf, and the travel desk (A7) share.

Two privacy laws are structural:

- **The free-busy read is shaped, not filtered.** ``busy_intervals``
  returns merged (start, end) blocks and NOTHING else — titles never
  enter the function's return type, so a leak is a type error, not a
  bug class.
- **Sharing is a standing grant, per peer, revocable.**
  :class:`FreeBusyGrants` records who may read whose busy/free; reading
  without a grant refuses by name. Event contents never cross — there
  is no door that shares them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

MAX_EVENT_TITLE_CHARS = 120
MAX_EVENTS_PER_PERSON = 2_000


class RecordsError(ValueError):
    """A refused records move — the message is the direction."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class CalendarEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    tenant_id: str
    owner: str
    title: str
    starts_at: datetime  # stored UTC; the client renders local
    ends_at: datetime
    source: str = "member"  # "member" | "trip" (a confirmed booking)
    created_at: datetime


_EVENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS calendar_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""

_GRANTS_SCHEMA = """CREATE TABLE IF NOT EXISTS freebusy_grants (
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    peer TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner, peer)
)"""


class CalendarStore:
    """Durable, tenant-scoped, owner-walled events."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_EVENTS_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def add(
        self,
        *,
        tenant: str,
        owner: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        source: str = "member",
    ) -> CalendarEvent:
        title = str(title or "").strip()
        if not title:
            raise RecordsError("an event needs a title")
        if len(title) > MAX_EVENT_TITLE_CHARS:
            raise RecordsError(
                f"the title is at most {MAX_EVENT_TITLE_CHARS} chars"
            )
        if ends_at <= starts_at:
            raise RecordsError("an event ends after it starts")
        if source not in ("member", "trip"):
            raise RecordsError("source is member or trip")
        if (
            len(self.between(
                tenant=tenant,
                owner=owner,
                start=datetime(1970, 1, 1, tzinfo=UTC),
                end=datetime(2200, 1, 1, tzinfo=UTC),
            ))
            >= MAX_EVENTS_PER_PERSON
        ):
            raise RecordsError("that's a full calendar — clear something first")
        event = CalendarEvent(
            event_id=str(uuid4()),
            tenant_id=tenant,
            owner=owner,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            source=source,
            created_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO calendar_events
                     (event_id, tenant_id, owner, title, starts_at,
                      ends_at, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    tenant,
                    owner,
                    title,
                    starts_at.isoformat(),
                    ends_at.isoformat(),
                    source,
                    event.created_at.isoformat(),
                ),
            )
        return event

    def between(
        self, *, tenant: str, owner: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        """The owner's events overlapping the window, oldest first."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM calendar_events
                   WHERE tenant_id = ? AND owner = ?
                     AND starts_at < ? AND ends_at > ?
                   ORDER BY starts_at, event_id""",
                (tenant, owner, end.isoformat(), start.isoformat()),
            ).fetchall()
        return [self._record(r) for r in rows]

    def delete(self, event_id: str, *, tenant: str, owner: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """DELETE FROM calendar_events
                   WHERE event_id = ? AND tenant_id = ? AND owner = ?""",
                (event_id, tenant, owner),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)

    def erase(self, *, tenant: str, owner: str) -> int:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM calendar_events"
                " WHERE tenant_id = ? AND owner = ?",
                (tenant, owner),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    @staticmethod
    def _record(row) -> CalendarEvent:
        return CalendarEvent(
            event_id=row["event_id"],
            tenant_id=row["tenant_id"],
            owner=row["owner"],
            title=row["title"],
            starts_at=datetime.fromisoformat(row["starts_at"]),
            ends_at=datetime.fromisoformat(row["ends_at"]),
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class FreeBusyGrants:
    """Who may read whose busy/free — standing, per peer, revocable."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_GRANTS_SCHEMA)

    def grant(self, *, tenant: str, owner: str, peer: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM freebusy_grants WHERE tenant_id = ?"
                " AND owner = ? AND peer = ?",
                (tenant, owner, peer),
            )
            db.execute(
                """INSERT INTO freebusy_grants (tenant_id, owner, peer, at)
                   VALUES (?, ?, ?, ?)""",
                (tenant, owner, peer, self._clock().isoformat()),
            )

    def revoke(self, *, tenant: str, owner: str, peer: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM freebusy_grants WHERE tenant_id = ?"
                " AND owner = ? AND peer = ?",
                (tenant, owner, peer),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)

    def allows(self, *, tenant: str, owner: str, peer: str) -> bool:
        if owner == peer:
            return True  # your own time is always yours to read
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM freebusy_grants WHERE tenant_id = ?"
                " AND owner = ? AND peer = ?",
                (tenant, owner, peer),
            ).fetchone()
        return row is not None

    def granted_by(self, *, tenant: str, owner: str) -> list[str]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT peer FROM freebusy_grants WHERE tenant_id = ?"
                " AND owner = ? ORDER BY peer",
                (tenant, owner),
            ).fetchall()
        return [str(r["peer"]) for r in rows]

    def erase(self, *, tenant: str, principal: str) -> int:
        """Both directions: grants they gave and grants given to them."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM freebusy_grants WHERE tenant_id = ?"
                " AND (owner = ? OR peer = ?)",
                (tenant, principal, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)


# --------------------------------------------------------------------------- #
# The interval arithmetic — pure, and shaped so titles cannot leak.            #
# --------------------------------------------------------------------------- #
Interval = tuple[datetime, datetime]


def busy_intervals(
    events: list[CalendarEvent], *, start: datetime, end: datetime
) -> list[Interval]:
    """Merged (start, end) busy blocks clipped to the window — and
    nothing else. The return type IS the privacy wire shape."""
    clipped = sorted(
        (max(e.starts_at, start), min(e.ends_at, end))
        for e in events
        if e.starts_at < end and e.ends_at > start
    )
    merged: list[Interval] = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def common_free(
    busy_by_member: dict[str, list[Interval]],
    *,
    start: datetime,
    end: datetime,
    min_length: timedelta,
) -> list[Interval]:
    """The group's shared free slots of at least ``min_length`` — the
    gaps left when EVERY member's busy blocks are laid on one line."""
    everything: list[Interval] = sorted(
        block for blocks in busy_by_member.values() for block in blocks
    )
    merged: list[Interval] = []
    for s, e in everything:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    free: list[Interval] = []
    cursor = start
    for s, e in merged:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        free.append((cursor, end))
    return [slot for slot in free if slot[1] - slot[0] >= min_length]
