"""The Paver's heartbeat — the survey's standing, consented Routine.

The same discipline as the CAS sweep, on its own table: ENABLING the
schedule is the approved, audited act; each survey tick runs under that
standing grant; ``claim_due`` fires exactly once across a fleet; revoking
stops the next survey cold. W1 surveys and persists the map — it writes
no code, so there is nothing here to approve beyond "keep the map fresh."
"""

from __future__ import annotations

from ..runtime.sweep import SweepScheduleStore

__all__ = ["PaverScheduleStore"]


class PaverScheduleStore(SweepScheduleStore):
    """``SweepScheduleStore`` on the ``paver_schedule`` table — every
    property inherited (durable single row, fleet-safe conditional claim,
    consent-first enable, revocable), a separate schedule so the Paver's
    cadence and the bundle sweep's never entangle."""

    def __init__(self, conn) -> None:
        super().__init__(conn, table="paver_schedule")
