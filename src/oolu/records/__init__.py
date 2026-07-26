"""Personal records — typed, durable, and privacy-shaped by design.

A7's prerequisite made real: the calendar record model the codebase
never had (`calendar.py` — timezone-aware events, merged busy
intervals, consented free-busy sharing that reveals busy/free ONLY).
OoLu's conversation, the starter-shelf calendar node, and the travel
desk all read the same records — one calendar, many readers.
"""

from .calendar import (
    MAX_EVENT_TITLE_CHARS,
    CalendarEvent,
    CalendarStore,
    FreeBusyGrants,
    RecordsError,
    busy_intervals,
    common_free,
)

__all__ = [
    "CalendarEvent",
    "CalendarStore",
    "FreeBusyGrants",
    "MAX_EVENT_TITLE_CHARS",
    "RecordsError",
    "busy_intervals",
    "common_free",
]
