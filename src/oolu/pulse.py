"""The pulse — P0 of the personal-nodes plan: schedules that fire runs.

A schedule is a row with a rhythm: daily at a time, weekly on a day,
monthly on a date, yearly on one — owned by a person, naming the goal
it fires. Nothing here runs on a timer by itself: the gateway's lazy
tick (request traffic, one due-check per minute per host) asks this
module which occurrence is due, and a durable (schedule, occurrence)
CLAIM decides exactly-once across processes and restarts — the same
doctrine as the sweep scheduler, generalized.

Time is the owner's: every rhythm is expressed on the schedule's own
local clock (``tz_offset_minutes`` rides the row), and an occurrence
key is that local moment ("2026-07-27T09:00") — deterministic, human-
readable, and the claim table's natural key.

Catch-up is honest: a host waking from sleep fires each overdue
schedule ONCE, for its newest missed occurrence, and ``missed_between``
counts what was skipped so the fired run can NAME it — never a
fabricated backlog of runs that "should have" happened.

The floor: occurrences at or before ``floor_at`` never fire. Creation
sets it (a schedule made at 10am for "daily at 9" first fires
tomorrow), and re-enabling resets it (a paused rhythm resumes from
now, not from its backlog).
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

CADENCES = ("daily", "weekly", "monthly", "yearly")

# A life has rhythms, not a thousand alarms.
MAX_PER_PERSON = 100
MAX_GOAL_CHARS = 500
MAX_LABEL_CHARS = 200

# Catch-up counting is bounded: past this, the honest answer is "many".
_MISSED_CAP = 1500

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    schedule_id: str
    tenant: str
    principal: str
    cadence: str
    at_minute: int  # minutes past local midnight
    weekday: int | None = None  # weekly: 0 = Monday
    day_of_month: int | None = None  # monthly
    month: int | None = None  # yearly
    day: int | None = None  # yearly
    tz_offset_minutes: int = 0
    goal: str
    label: str = ""
    enabled: bool = True
    floor_at: datetime  # occurrences at/before this never fire
    created_at: datetime


# --------------------------------------------------------------------------- #
# Occurrence arithmetic — pure functions on the schedule's LOCAL clock.        #
# --------------------------------------------------------------------------- #
def _local(schedule: Schedule, moment: datetime) -> datetime:
    """A UTC moment on the schedule's own clock, naive."""
    return (
        moment + timedelta(minutes=schedule.tz_offset_minutes)
    ).replace(tzinfo=None)


def _clamped_day(year: int, month: int, day: int) -> int:
    return min(day, calendar.monthrange(year, month)[1])


def _on_or_before(schedule: Schedule, local: datetime) -> datetime:
    """The newest local occurrence at or before ``local`` — clamping
    month-length honestly (day 31 monthly lands on Feb 28/29)."""
    hh, mm = divmod(schedule.at_minute, 60)
    if schedule.cadence == "daily":
        candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate > local:
            candidate -= timedelta(days=1)
        return candidate
    if schedule.cadence == "weekly":
        candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidate -= timedelta(
            days=(candidate.weekday() - int(schedule.weekday or 0)) % 7
        )
        if candidate > local:
            candidate -= timedelta(days=7)
        return candidate
    if schedule.cadence == "monthly":
        wanted = int(schedule.day_of_month or 1)
        year, month = local.year, local.month
        candidate = datetime(
            year, month, _clamped_day(year, month, wanted), hh, mm
        )
        if candidate > local:
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
            candidate = datetime(
                year, month, _clamped_day(year, month, wanted), hh, mm
            )
        return candidate
    # yearly
    month, wanted = int(schedule.month or 1), int(schedule.day or 1)
    year = local.year
    candidate = datetime(
        year, month, _clamped_day(year, month, wanted), hh, mm
    )
    if candidate > local:
        year -= 1
        candidate = datetime(
            year, month, _clamped_day(year, month, wanted), hh, mm
        )
    return candidate


def _step_back(schedule: Schedule, occurrence: datetime) -> datetime:
    """One period earlier — re-clamped from the DECLARED day, so a
    monthly day-31 rhythm returns to the 31st after February."""
    hh, mm = divmod(schedule.at_minute, 60)
    if schedule.cadence == "daily":
        return occurrence - timedelta(days=1)
    if schedule.cadence == "weekly":
        return occurrence - timedelta(days=7)
    if schedule.cadence == "monthly":
        wanted = int(schedule.day_of_month or 1)
        year, month = occurrence.year, occurrence.month
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return datetime(year, month, _clamped_day(year, month, wanted), hh, mm)
    wanted = int(schedule.day or 1)
    year, month = occurrence.year - 1, int(schedule.month or 1)
    return datetime(year, month, _clamped_day(year, month, wanted), hh, mm)


def _step_forward(schedule: Schedule, occurrence: datetime) -> datetime:
    hh, mm = divmod(schedule.at_minute, 60)
    if schedule.cadence == "daily":
        return occurrence + timedelta(days=1)
    if schedule.cadence == "weekly":
        return occurrence + timedelta(days=7)
    if schedule.cadence == "monthly":
        wanted = int(schedule.day_of_month or 1)
        year, month = occurrence.year, occurrence.month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return datetime(year, month, _clamped_day(year, month, wanted), hh, mm)
    wanted = int(schedule.day or 1)
    year, month = occurrence.year + 1, int(schedule.month or 1)
    return datetime(year, month, _clamped_day(year, month, wanted), hh, mm)


def _key(occurrence: datetime) -> str:
    return occurrence.strftime("%Y-%m-%dT%H:%M")


def occurrence_at(schedule: Schedule, now: datetime) -> str | None:
    """The newest occurrence DUE at ``now`` (UTC) — strictly after the
    schedule's floor, at or before now, on its local clock — or None
    while nothing has come due yet."""
    local_now = _local(schedule, now)
    candidate = _on_or_before(schedule, local_now)
    if candidate <= _local(schedule, schedule.floor_at):
        return None
    return _key(candidate)


def next_occurrence(schedule: Schedule, now: datetime) -> str:
    """The next local occurrence strictly after ``now`` AND after the
    floor — what a listing shows as "fires next"."""
    local_now = _local(schedule, now)
    floor_local = _local(schedule, schedule.floor_at)
    candidate = _step_forward(
        schedule, _on_or_before(schedule, max(local_now, floor_local))
    )
    return _key(candidate)


def missed_between(
    schedule: Schedule, prior: str | None, occurrence: str
) -> int:
    """How many occurrences fell between the last fired one (or the
    floor, when nothing ever fired) and the occurrence firing NOW —
    exclusive both ends: the honest "missed while the host slept"
    count the fired run names. Bounded; past the cap the count stays
    at the cap ("many")."""
    floor_local = _local(schedule, schedule.floor_at)
    cursor = datetime.strptime(occurrence, "%Y-%m-%dT%H:%M")
    missed = 0
    while missed < _MISSED_CAP:
        cursor = _step_back(schedule, cursor)
        if cursor <= floor_local:
            break
        if prior is not None and _key(cursor) <= prior:
            break
        missed += 1
    return missed


# --------------------------------------------------------------------------- #
# The deterministic ear: "every day at 9 run …" → a schedule spec.             #
# --------------------------------------------------------------------------- #
_TIME = r"(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm)?"
_GOAL = r"\s*[,:]?\s+(?:run\s+|do\s+)?(?P<goal>.+?)"
_DAILY_RE = re.compile(rf"^every\s+day\s+at\s+{_TIME}{_GOAL}$", re.I)
_WEEKLY_RE = re.compile(
    rf"^every\s+(?P<wd>{'|'.join(WEEKDAYS)})\s+at\s+{_TIME}{_GOAL}$", re.I
)
_MONTHLY_RE = re.compile(
    r"^every\s+month\s+on\s+(?:the\s+)?(?P<dom>\d{1,2})(?:st|nd|rd|th)?"
    rf"\s+at\s+{_TIME}{_GOAL}$",
    re.I,
)
_YEARLY_RE = re.compile(
    rf"^every\s+year\s+on\s+(?P<mon>{'|'.join(MONTHS)})"
    rf"\s+(?P<dom>\d{{1,2}})(?:st|nd|rd|th)?\s+at\s+{_TIME}{_GOAL}$",
    re.I,
)


def _spoken_minute(match) -> int | None:
    hour = int(match.group("h"))
    minute = int(match.group("mi") or 0)
    meridiem = (match.group("ap") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def spoken_schedule(text: str) -> dict | None:
    """The ``add()`` kwargs a spoken rhythm means, or None when the
    sentence is not a schedule — anything doubtful falls through to
    ordinary conversation rather than becoming a surprise alarm."""
    text = (text or "").strip().rstrip(".!")
    for pattern, extras in (
        (_DAILY_RE, lambda m: {"cadence": "daily"}),
        (
            _WEEKLY_RE,
            lambda m: {
                "cadence": "weekly",
                "weekday": WEEKDAYS[m.group("wd").lower()],
            },
        ),
        (
            _MONTHLY_RE,
            lambda m: {
                "cadence": "monthly",
                "day_of_month": int(m.group("dom")),
            },
        ),
        (
            _YEARLY_RE,
            lambda m: {
                "cadence": "yearly",
                "month": MONTHS[m.group("mon").lower()],
                "day": int(m.group("dom")),
            },
        ),
    ):
        match = pattern.match(text)
        if match is None:
            continue
        minute = _spoken_minute(match)
        if minute is None:
            return None
        return {
            **extras(match),
            "at_minute": minute,
            "goal": match.group("goal").strip(),
        }
    return None


def speak_rhythm(schedule: Schedule) -> str:
    """The rhythm in the words a listing shows."""
    hh, mm = divmod(schedule.at_minute, 60)
    at = f"{hh:02d}:{mm:02d}"
    if schedule.cadence == "daily":
        return f"every day at {at}"
    if schedule.cadence == "weekly":
        names = {v: k for k, v in WEEKDAYS.items()}
        return f"every {names[int(schedule.weekday or 0)].capitalize()} at {at}"
    if schedule.cadence == "monthly":
        return f"every month on day {int(schedule.day_of_month or 1)} at {at}"
    names = {v: k for k, v in MONTHS.items()}
    return (
        f"every year on {names[int(schedule.month or 1)].capitalize()} "
        f"{int(schedule.day or 1)} at {at}"
    )


# --------------------------------------------------------------------------- #
# The durable store: rows and claims.                                          #
# --------------------------------------------------------------------------- #
_SCHEMA = """CREATE TABLE IF NOT EXISTS pulse_schedules (
    schedule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    cadence TEXT NOT NULL,
    at_minute INTEGER NOT NULL,
    weekday INTEGER,
    day_of_month INTEGER,
    month INTEGER,
    day INTEGER,
    tz_offset_minutes INTEGER NOT NULL DEFAULT 0,
    goal TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    floor_at TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""

# The exactly-once ledger: one row per fired (schedule, occurrence) —
# INSERT OR IGNORE is the whole election, across processes and restarts.
_CLAIMS_SCHEMA = """CREATE TABLE IF NOT EXISTS pulse_claims (
    schedule_id TEXT NOT NULL,
    occurrence TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (schedule_id, occurrence)
)"""


class PulseStore:
    """Schedules and their fired-occurrence claims, durable."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(_CLAIMS_SCHEMA)

    # -- rows ------------------------------------------------------------ #
    def add(
        self,
        tenant: str,
        principal: str,
        *,
        cadence: str,
        at_minute: int,
        goal: str,
        weekday: int | None = None,
        day_of_month: int | None = None,
        month: int | None = None,
        day: int | None = None,
        tz_offset_minutes: int = 0,
        label: str = "",
        enabled: bool = True,
        now: datetime | None = None,
    ) -> Schedule:
        if cadence not in CADENCES:
            raise ValueError(
                f"the rhythm must be one of: {', '.join(CADENCES)}"
            )
        goal = " ".join(str(goal or "").split())
        if not goal:
            raise ValueError("a schedule needs a goal to run")
        if len(goal) > MAX_GOAL_CHARS:
            raise ValueError("a schedule's goal is one sentence")
        at_minute = int(at_minute)
        if not 0 <= at_minute <= 1439:
            raise ValueError("the time must fall inside a day")
        if cadence == "weekly":
            if weekday is None or not 0 <= int(weekday) <= 6:
                raise ValueError("a weekly rhythm names its weekday")
        if cadence == "monthly":
            if day_of_month is None or not 1 <= int(day_of_month) <= 31:
                raise ValueError("a monthly rhythm names its day (1–31)")
        if cadence == "yearly":
            if (
                month is None
                or day is None
                or not 1 <= int(month) <= 12
                or not 1 <= int(day) <= 31
            ):
                raise ValueError("a yearly rhythm names its month and day")
        if len(self.list_for(tenant, principal)) >= MAX_PER_PERSON:
            raise ValueError(
                "that's enough standing schedules — cancel one first"
            )
        moment = now or self._clock()
        schedule = Schedule(
            schedule_id=uuid4().hex,
            tenant=tenant,
            principal=principal,
            cadence=cadence,
            at_minute=at_minute,
            weekday=int(weekday) if weekday is not None else None,
            day_of_month=(
                int(day_of_month) if day_of_month is not None else None
            ),
            month=int(month) if month is not None else None,
            day=int(day) if day is not None else None,
            tz_offset_minutes=int(tz_offset_minutes),
            goal=goal,
            label=str(label or "")[:MAX_LABEL_CHARS],
            enabled=bool(enabled),
            floor_at=moment,
            created_at=moment,
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO pulse_schedules
                       (schedule_id, tenant_id, principal, cadence,
                        at_minute, weekday, day_of_month, month, day,
                        tz_offset_minutes, goal, label, enabled,
                        floor_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    schedule.schedule_id, tenant, principal, cadence,
                    at_minute, schedule.weekday, schedule.day_of_month,
                    schedule.month, schedule.day,
                    schedule.tz_offset_minutes, goal, schedule.label,
                    1 if schedule.enabled else 0,
                    schedule.floor_at.isoformat(),
                    schedule.created_at.isoformat(),
                ),
            )
        return schedule

    def get(
        self, schedule_id: str, *, tenant: str, principal: str
    ) -> Schedule | None:
        """The caller's own schedule — a stranger's is indistinguishable
        from missing."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT * FROM pulse_schedules WHERE schedule_id = ?
                     AND tenant_id = ? AND principal = ?""",
                (schedule_id, tenant, principal),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_for(self, tenant: str, principal: str) -> list[Schedule]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM pulse_schedules
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY created_at ASC, schedule_id ASC""",
                (tenant, principal),
            ).fetchall()
        return [self._row(row) for row in rows]

    def all_enabled(self) -> list[Schedule]:
        """Every enabled schedule on the host — the tick's field of
        view; the fire itself happens as each row's OWNER."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT * FROM pulse_schedules WHERE enabled = 1"
                " ORDER BY created_at ASC, schedule_id ASC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def set_enabled(
        self,
        schedule_id: str,
        *,
        tenant: str,
        principal: str,
        enabled: bool,
        now: datetime | None = None,
    ) -> Schedule | None:
        """Pause or resume. Resuming RESETS the floor to now: a paused
        rhythm continues from the present, never from its backlog."""
        current = self.get(schedule_id, tenant=tenant, principal=principal)
        if current is None:
            return None
        moment = now or self._clock()
        with self._conn.transaction() as db:
            if enabled:
                db.execute(
                    """UPDATE pulse_schedules SET enabled = 1, floor_at = ?
                       WHERE schedule_id = ?""",
                    (moment.isoformat(), schedule_id),
                )
            else:
                db.execute(
                    "UPDATE pulse_schedules SET enabled = 0"
                    " WHERE schedule_id = ?",
                    (schedule_id,),
                )
        return self.get(schedule_id, tenant=tenant, principal=principal)

    def delete(
        self, schedule_id: str, *, tenant: str, principal: str
    ) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """DELETE FROM pulse_schedules WHERE schedule_id = ?
                     AND tenant_id = ? AND principal = ?""",
                (schedule_id, tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def erase(self, *, tenant: str, principal: str) -> int:
        """Data-subject erasure: the account's rhythms, gone."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM pulse_schedules"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    # -- claims ----------------------------------------------------------- #
    def claim(self, schedule_id: str, occurrence: str) -> bool:
        """Fire election: True exactly once per (schedule, occurrence),
        across every process that shares the durable connection."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO pulse_claims
                       (schedule_id, occurrence, fired_at, run_id)
                   VALUES (?, ?, ?, '')""",
                (schedule_id, occurrence, self._clock().isoformat()),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def newest_claim(
        self, schedule_id: str, *, before: str | None = None
    ) -> dict | None:
        """The newest fired occurrence (optionally strictly before one)
        — what catch-up counting and listings read."""
        query = (
            "SELECT occurrence, fired_at, run_id FROM pulse_claims"
            " WHERE schedule_id = ?"
        )
        args: list = [schedule_id]
        if before is not None:
            query += " AND occurrence < ?"
            args.append(before)
        query += " ORDER BY occurrence DESC LIMIT 1"
        with self._conn.lock:
            row = self._conn.db.execute(query, args).fetchone()
        if row is None:
            return None
        return {
            "occurrence": row["occurrence"],
            "fired_at": row["fired_at"],
            "run_id": row["run_id"],
        }

    def set_claim_run(
        self, schedule_id: str, occurrence: str, run_id: str
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE pulse_claims SET run_id = ?
                   WHERE schedule_id = ? AND occurrence = ?""",
                (run_id, schedule_id, occurrence),
            )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _row(row) -> Schedule:
        return Schedule(
            schedule_id=row["schedule_id"],
            tenant=row["tenant_id"],
            principal=row["principal"],
            cadence=row["cadence"],
            at_minute=int(row["at_minute"]),
            weekday=row["weekday"],
            day_of_month=row["day_of_month"],
            month=row["month"],
            day=row["day"],
            tz_offset_minutes=int(row["tz_offset_minutes"]),
            goal=row["goal"],
            label=row["label"],
            enabled=bool(row["enabled"]),
            floor_at=datetime.fromisoformat(row["floor_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
