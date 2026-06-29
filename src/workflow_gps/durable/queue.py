"""A durable task queue with leases, heartbeats, cancellation, and retry.

A task is leased to one worker for a bounded time. A worker extends its lease with
heartbeats while it makes progress; if the worker dies, the lease expires and the
task becomes available again so another worker can reclaim it. Acknowledging a task
stores its result; failing it either reschedules (with backoff) until ``max_attempts``
or moves it to ``dead``. Combined with the idempotency ledger, this gives
exactly-once *effects* across restarts even though delivery is at-least-once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .connection import DurableConnection


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


class TaskStatus(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    CANCELLED = "cancelled"
    DEAD = "dead"


class Task(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    kind: str
    payload: dict[str, Any]
    status: TaskStatus
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 5
    idempotency_key: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class TaskQueue(Protocol):
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> Task: ...

    def lease(
        self, owner: str, *, lease_seconds: float = 30.0, now: datetime | None = None
    ) -> Task | None: ...

    def heartbeat(
        self,
        task_id: str,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> bool: ...

    def complete(
        self, task_id: str, owner: str, *, result: dict[str, Any] | None = None
    ) -> bool: ...

    def fail(
        self,
        task_id: str,
        owner: str,
        *,
        error: str,
        retry_in_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> Task | None: ...

    def cancel(self, task_id: str) -> bool: ...

    def reclaim_expired(self, *, now: datetime | None = None) -> list[str]: ...

    def get(self, task_id: str) -> Task | None: ...


_COLUMNS = (
    "task_id, kind, payload_json, status, available_at, lease_owner, "
    "lease_expires_at, heartbeat_at, attempts, max_attempts, idempotency_key, "
    "result_json, error, created_at, updated_at"
)


class DurableTaskQueue:
    def __init__(self, conn: DurableConnection):
        self._conn = conn

    # ------------------------------------------------------------------ #
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> Task:
        # Idempotent enqueue: a duplicate key returns the existing task instead of
        # creating a second one (no duplicate work after a retried submission).
        if idempotency_key is not None:
            existing = self._by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        now = _now()
        when = available_at or now
        task_id = uuid4().hex

        with self._conn.transaction() as db:
            db.execute(
                f"INSERT INTO tasks ({_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, NULL, NULL, ?, ?)",
                (
                    task_id,
                    kind,
                    json.dumps(payload),
                    TaskStatus.PENDING.value,
                    _iso(when),
                    max_attempts,
                    idempotency_key,
                    _iso(now),
                    _iso(now),
                ),
            )
        task = self.get(task_id)
        assert task is not None
        return task

    def lease(
        self, owner: str, *, lease_seconds: float = 30.0, now: datetime | None = None
    ) -> Task | None:
        moment = now or _now()
        expiry = moment + timedelta(seconds=lease_seconds)
        with self._conn.transaction() as db:
            # Pick the oldest available task: pending, or a leased task whose lease
            # has expired. The UPDATE ... WHERE re-checks status so two leasers
            # cannot both win the same row.
            row = db.execute(
                """SELECT task_id FROM tasks
                   WHERE status = ? AND available_at <= ?
                   ORDER BY available_at ASC LIMIT 1""",
                (TaskStatus.PENDING.value, _iso(moment)),
            ).fetchone()
            if row is None:
                return None
            task_id = row["task_id"]
            cursor = db.execute(
                """UPDATE tasks
                   SET status = ?, lease_owner = ?, lease_expires_at = ?,
                       heartbeat_at = ?, attempts = attempts + 1, updated_at = ?
                   WHERE task_id = ? AND status = ?""",
                (
                    TaskStatus.LEASED.value,
                    owner,
                    _iso(expiry),
                    _iso(moment),
                    _iso(moment),
                    task_id,
                    TaskStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get(task_id)

    def heartbeat(
        self,
        task_id: str,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> bool:
        moment = now or _now()
        expiry = moment + timedelta(seconds=lease_seconds)
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE tasks
                   SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                   WHERE task_id = ? AND lease_owner = ? AND status = ?""",
                (
                    _iso(expiry),
                    _iso(moment),
                    _iso(moment),
                    task_id,
                    owner,
                    TaskStatus.LEASED.value,
                ),
            )
        return cursor.rowcount > 0

    def complete(
        self, task_id: str, owner: str, *, result: dict[str, Any] | None = None
    ) -> bool:

        now = _now()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE tasks
                   SET status = ?, result_json = ?, lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE task_id = ? AND lease_owner = ? AND status = ?""",
                (
                    TaskStatus.DONE.value,
                    json.dumps(result) if result is not None else None,
                    _iso(now),
                    task_id,
                    owner,
                    TaskStatus.LEASED.value,
                ),
            )
        return cursor.rowcount > 0

    def fail(
        self,
        task_id: str,
        owner: str,
        *,
        error: str,
        retry_in_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> Task | None:
        moment = now or _now()
        task = self.get(task_id)
        if (
            task is None
            or task.lease_owner != owner
            or task.status is not TaskStatus.LEASED
        ):
            return None
        if task.attempts >= task.max_attempts:
            with self._conn.transaction() as db:
                db.execute(
                    """UPDATE tasks SET status = ?, error = ?, lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ? WHERE task_id = ?""",
                    (TaskStatus.DEAD.value, error, _iso(moment), task_id),
                )
        else:
            available = moment + timedelta(seconds=retry_in_seconds)
            with self._conn.transaction() as db:
                db.execute(
                    """UPDATE tasks SET status = ?, error = ?, available_at = ?,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                       WHERE task_id = ?""",
                    (
                        TaskStatus.PENDING.value,
                        error,
                        _iso(available),
                        _iso(moment),
                        task_id,
                    ),
                )
        return self.get(task_id)

    def cancel(self, task_id: str) -> bool:
        now = _now()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE tasks SET status = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ?
                   WHERE task_id = ? AND status IN (?, ?)""",
                (
                    TaskStatus.CANCELLED.value,
                    _iso(now),
                    task_id,
                    TaskStatus.PENDING.value,
                    TaskStatus.LEASED.value,
                ),
            )
        return cursor.rowcount > 0

    def reclaim_expired(self, *, now: datetime | None = None) -> list[str]:
        """Return leased tasks whose lease has expired to the pending pool.

        This is how a restarted worker pool recovers in-flight work a crashed
        worker was holding: no task is lost.
        """
        moment = now or _now()
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT task_id FROM tasks
                   WHERE status = ? AND lease_expires_at IS NOT NULL
                   AND lease_expires_at < ?""",
                (TaskStatus.LEASED.value, _iso(moment)),
            ).fetchall()
            reclaimed = [row["task_id"] for row in rows]
            for task_id in reclaimed:
                db.execute(
                    """UPDATE tasks SET status = ?, lease_owner = NULL,
                       lease_expires_at = NULL, available_at = ?, updated_at = ?
                       WHERE task_id = ?""",
                    (TaskStatus.PENDING.value, _iso(moment), _iso(moment), task_id),
                )
        return reclaimed

    def get(self, task_id: str) -> Task | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    # ------------------------------------------------------------------ #
    def _by_idempotency_key(self, key: str) -> Task | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    @staticmethod
    def _row_to_task(row) -> Task:

        def _dt(value):
            return datetime.fromisoformat(value) if value else None

        return Task(
            task_id=row["task_id"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            status=TaskStatus(row["status"]),
            available_at=datetime.fromisoformat(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=_dt(row["lease_expires_at"]),
            heartbeat_at=_dt(row["heartbeat_at"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            idempotency_key=row["idempotency_key"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
