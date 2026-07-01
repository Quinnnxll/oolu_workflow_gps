from __future__ import annotations

from ..durable.audit import DurableAuditLog
from ..skills.models import ExecutionStatus
from .attribution import AttributionStore
from .models import AttributionRecord, MeteringEvent
from .store import MeteringLedger

_EXECUTED_EVENT = "workflow.executed"


class MeteringDeriver:
    def __init__(
        self,
        audit: DurableAuditLog,
        ledger: MeteringLedger,
        attribution: AttributionStore | None = None,
    ) -> None:
        self._audit = audit
        self._ledger = ledger
        self._attribution = attribution

    def derive(self) -> list[MeteringEvent]:
        recorded: list[MeteringEvent] = []
        for record in self._audit.records():
            if record.event_type != _EXECUTED_EVENT:
                continue
            if record.payload.get("status") != ExecutionStatus.SUCCEEDED.value:
                continue
            run_id = record.payload.get("run_id", "")
            key = record.payload.get("idempotency_key") or f"audit:{record.seq}"
            binding = (
                self._attribution.get_binding(run_id) if self._attribution else None
            )
            event = MeteringEvent(
                idempotency_key=key,
                run_id=run_id,
                version_id=binding.version_id if binding else None,
                consumer_tenant=binding.consumer_tenant if binding else None,
                consumer_principal=binding.consumer_principal if binding else None,
                outcome=record.payload["status"],
                gross=binding.gross if binding else None,
                provider_cost=binding.provider_cost if binding else None,
                audit_seq=record.seq,
                occurred_at=record.at,
            )
            if self._ledger.record(event):
                recorded.append(event)
                if binding is not None and self._attribution is not None:
                    for share in binding.shares:
                        self._attribution.add_attribution(
                            AttributionRecord(
                                event_id=event.event_id,
                                noder_principal=share.noder_principal,
                                weight=share.weight,
                                multiplier=share.multiplier,
                            )
                        )
        return recorded
