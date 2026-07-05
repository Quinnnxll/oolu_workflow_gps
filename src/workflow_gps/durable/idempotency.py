"""An idempotency ledger for externally visible mutations.

Every mutation that has an effect outside the process — sending a notification,
calling a provider, charging a quota — is wrapped in ``run(key, fn)``. The first
call for a key executes ``fn`` and stores its (JSON-serializable) result; every
later call with the same key returns the stored result without running ``fn``
again. Re-delivering a task after a crash therefore cannot duplicate its effects.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .connection import DurableConnection


class IdempotencyLedger:
    def __init__(self, conn: DurableConnection):
        self._conn = conn

    def seen(self, key: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def get(self, key: str) -> Any | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT result_json FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def run(
        self,
        key: str,
        fn: Callable[[], Any],
        *,
        scope: str = "default",
    ) -> Any:
        """Run ``fn`` at most once per ``key``; return the first result thereafter.

        The claim insert and the result write bracket ``fn`` so a concurrent
        second caller that already claimed the key is detected before ``fn`` runs.
        """
        now = datetime.now(UTC)
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO idempotency (key, scope, result_json, created_at)"
                " VALUES (?, ?, NULL, ?)",
                (key, scope, now.isoformat()),
            )
            claimed = cursor.rowcount > 0
        if not claimed:
            return self.get(key)
        result = fn()
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE idempotency SET result_json = ? WHERE key = ?",
                (json.dumps(result), key),
            )
        return result

    def release(self, key: str) -> bool:
        """Drop a claim so the key may run again.

        For callers whose ``fn`` FAILED partway: ``run`` claims before it
        executes, so a raised exception leaves a claim with no result and
        every retry would return ``None`` forever. A caller that catches
        the failure, rolls back its own effects, and releases the key makes
        the operation honestly retryable instead of silently poisoned.
        """
        with self._conn.transaction() as db:
            cursor = db.execute("DELETE FROM idempotency WHERE key = ?", (key,))
            return cursor.rowcount > 0
