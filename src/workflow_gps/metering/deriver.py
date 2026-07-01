from __future__ import annotations

from ..durable.audit import DurableAuditLog
from ..skills.models import ExecutionStatus
from .models import MeteringEvent
from .store import MeteringLedger

_EXECUTED_EVENT = "workflow.executed"


class MeteringDeriver:
    def __init__(self, audit: DurableAuditLog, ledger: MeteringLedger) -> None:
        self._audit = audit
        self._ledger = ledger

    def derive(self) -> list[MeteringEvent]:
        recorded: list[MeteringEvent] = []
        for record in self._audit.records():
            if record.event_type != _EXECUTED_EVENT:
                continue
            if record.payload.get("status") != ExecutionStatus.SUCCEEDED.value:
                continue
            key = record.payload.get("idempotency_key") or f"audit:{record.seq}"
            event = MeteringEvent(
                idempotency_key=key,
                run_id=record.payload.get("run_id", ""),
                version_id=record.payload.get("version_id"),
                consumer_tenant=record.payload.get("consumer_tenant"),
                outcome=record.payload["status"],
                audit_seq=record.seq,
                occurred_at=record.at,
            )
            if self._ledger.record(event):
                recorded.append(event)
        return recorded
