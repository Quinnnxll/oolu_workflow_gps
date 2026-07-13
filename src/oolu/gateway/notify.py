"""Outbound webhooks: machine callers get told, they don't poll.

An API caller submits a run and moves on; when the run reaches a terminal
event, every webhook endpoint its tenant registered receives a signed POST.
The pipeline is derive-then-deliver, the same shape as metering:

- ``derive()`` scans the audit log past a durable cursor for terminal
  events (completed / failed / cancelled), resolves each run's tenant, and
  stages one durable delivery per (event, endpoint). Restart-safe and
  replay-safe: the cursor only advances, deliveries are keyed.
- ``deliver(transport)`` posts pending deliveries with the endpoint's own
  HMAC signature headers (the same signing scheme our inbound webhook
  verifier speaks), marking each delivered or failed (bounded attempts).

Nothing here runs on a timer by itself: the host calls pump() on whatever
cadence it likes — a scheduler, a loop, or a test calling it once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .webhooks import WebhookSigner

TERMINAL_EVENTS = frozenset(
    {"workflow.completed", "workflow.failed", "workflow.cancelled"}
)
MAX_ATTEMPTS = 5


class WebhookEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    tenant_id: str
    url: str
    secret: str  # shared HMAC secret; shown to the owner at registration
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Delivery(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: str = Field(default_factory=lambda: uuid4().hex)
    endpoint_id: str
    payload: dict
    status: str = "pending"  # pending | delivered | failed
    attempts: int = 0


class Transport(Protocol):
    """POST a JSON payload; return the HTTP status. Adapters own retries'
    transport concerns; the notifier owns attempt bookkeeping."""

    def post(self, url: str, payload: dict, headers: dict[str, str]) -> int: ...


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS webhook_endpoints (
        endpoint_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
        delivery_id TEXT PRIMARY KEY,
        endpoint_id TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_cursor (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_seq INTEGER NOT NULL
    )""",
)


class WebhookEndpointStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def add(self, endpoint: WebhookEndpoint) -> WebhookEndpoint:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO webhook_endpoints (endpoint_id, tenant_id, payload_json)"
                " VALUES (?, ?, ?)",
                (endpoint.endpoint_id, endpoint.tenant_id, endpoint.model_dump_json()),
            )
        return endpoint

    def list(self, *, tenant: str | None = None) -> list[WebhookEndpoint]:
        with self._conn.lock:
            if tenant is None:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM webhook_endpoints"
                ).fetchall()
            else:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM webhook_endpoints WHERE tenant_id = ?",
                    (tenant,),
                ).fetchall()
        endpoints = [
            WebhookEndpoint.model_validate_json(r["payload_json"]) for r in rows
        ]
        # Oldest-first by the record's clock — rowid is SQLite-only and an
        # UndefinedColumn on the PostgreSQL backend.
        endpoints.sort(key=lambda e: (e.created_at.isoformat(), e.endpoint_id))
        return endpoints

    def remove(self, endpoint_id: str, *, tenant: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM webhook_endpoints"
                " WHERE endpoint_id = ? AND tenant_id = ?",
                (endpoint_id, tenant),
            )
            return cursor.rowcount > 0


class RunEventNotifier:
    """Derive terminal run events into durable deliveries; deliver signed."""

    def __init__(self, *, audit, durable, endpoints: WebhookEndpointStore, conn):
        self._audit = audit
        self._durable = durable
        self._endpoints = endpoints
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Derivation.                                                          #
    # ------------------------------------------------------------------ #
    def _cursor(self) -> int:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT last_seq FROM webhook_cursor WHERE id = 1"
            ).fetchone()
        return int(row["last_seq"]) if row else 0

    def _advance(self, seq: int) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO webhook_cursor (id, last_seq) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_seq = excluded.last_seq",
                (seq,),
            )

    def _tenant_of(self, run_id: str) -> str | None:
        state = self._durable.runs.get(run_id)
        if state is None:
            return None
        return state.contract.metadata.get("tenant_id")

    def derive(self) -> int:
        """Stage deliveries for terminal events past the cursor. Returns
        how many deliveries were staged."""
        staged = 0
        last = self._cursor()
        for record in self._audit.records():
            if record.seq <= last:
                continue
            last = max(last, record.seq)
            if record.event_type not in TERMINAL_EVENTS or not record.run_id:
                continue
            tenant = self._tenant_of(record.run_id)
            if tenant is None:
                continue
            payload = {
                "type": record.event_type,
                "run_id": record.run_id,
                "at": record.at.isoformat(),
                "seq": record.seq,
            }
            for endpoint in self._endpoints.list(tenant=tenant):
                delivery = Delivery(
                    endpoint_id=endpoint.endpoint_id, payload=payload
                )
                with self._conn.transaction() as db:
                    db.execute(
                        "INSERT INTO webhook_deliveries"
                        " (delivery_id, endpoint_id, payload_json)"
                        " VALUES (?, ?, ?)",
                        (
                            delivery.delivery_id,
                            delivery.endpoint_id,
                            delivery.model_dump_json(),
                        ),
                    )
                staged += 1
        self._advance(last)
        return staged

    # ------------------------------------------------------------------ #
    # Delivery.                                                            #
    # ------------------------------------------------------------------ #
    def _deliveries(self, status: str) -> list[Delivery]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM webhook_deliveries"
            ).fetchall()
        parsed = [Delivery.model_validate_json(r["payload_json"]) for r in rows]
        # Deliveries follow the audit sequence they were derived from —
        # a real ordering both backends share, unlike SQLite's rowid.
        parsed.sort(key=lambda d: (int(d.payload.get("seq") or 0), d.delivery_id))
        return [d for d in parsed if d.status == status]

    def pending(self) -> list[Delivery]:
        return self._deliveries("pending")

    def _save(self, delivery: Delivery) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE webhook_deliveries SET payload_json = ?"
                " WHERE delivery_id = ?",
                (delivery.model_dump_json(), delivery.delivery_id),
            )

    def deliver(self, transport: Transport) -> dict[str, int]:
        endpoints = {e.endpoint_id: e for e in self._endpoints.list()}
        delivered = failed = 0
        for delivery in self.pending():
            endpoint = endpoints.get(delivery.endpoint_id)
            if endpoint is None:
                self._save(delivery.model_copy(update={"status": "failed"}))
                failed += 1
                continue
            headers = WebhookSigner(endpoint.secret).sign(
                delivery.payload, delivery_id=delivery.delivery_id
            )
            try:
                status = transport.post(endpoint.url, delivery.payload, headers)
            except Exception:  # noqa: BLE001 - a dead endpoint is an attempt
                status = 0
            attempts = delivery.attempts + 1
            if 200 <= status < 300:
                self._save(
                    delivery.model_copy(
                        update={"status": "delivered", "attempts": attempts}
                    )
                )
                delivered += 1
            else:
                new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                self._save(
                    delivery.model_copy(
                        update={"status": new_status, "attempts": attempts}
                    )
                )
                if new_status == "failed":
                    failed += 1
        return {"delivered": delivered, "failed": failed}

    def pump(self, transport: Transport) -> dict[str, int]:
        """One full cycle: derive then deliver — what a host loop calls."""
        staged = self.derive()
        result = self.deliver(transport)
        return {"staged": staged, **result}
