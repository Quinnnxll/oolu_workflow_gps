from __future__ import annotations

from .models import KycStatus, PayoutAccount, PayoutBatch, PayoutStatus

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS payout_accounts (
        noder_principal TEXT PRIMARY KEY,
        provider_account_id TEXT NOT NULL,
        kyc_status TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS payout_batches (
        batch_id TEXT PRIMARY KEY,
        noder_principal TEXT NOT NULL,
        status TEXT NOT NULL,
        provider_ref TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


class PayoutStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def save_account(self, account: PayoutAccount) -> None:
        with self._conn.transaction() as db:
            updated = db.execute(
                """UPDATE payout_accounts
                   SET provider_account_id = ?, kyc_status = ?, payload_json = ?
                   WHERE noder_principal = ?""",
                (
                    account.provider_account_id,
                    account.kyc_status.value,
                    account.model_dump_json(),
                    account.noder_principal,
                ),
            )
            if updated.rowcount == 0:
                db.execute(
                    """INSERT INTO payout_accounts
                       (noder_principal, provider_account_id, kyc_status, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (
                        account.noder_principal,
                        account.provider_account_id,
                        account.kyc_status.value,
                        account.model_dump_json(),
                    ),
                )

    def get_account(self, noder_principal: str) -> PayoutAccount | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM payout_accounts WHERE noder_principal = ?",
                (noder_principal,),
            ).fetchone()
        return PayoutAccount.model_validate_json(row["payload_json"]) if row else None

    def is_verified(self, noder_principal: str) -> bool:
        account = self.get_account(noder_principal)
        return account is not None and account.kyc_status == KycStatus.VERIFIED

    def add_batch(self, batch: PayoutBatch) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO payout_batches
                   (batch_id, noder_principal, status, provider_ref, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch.batch_id,
                    batch.noder_principal,
                    batch.status.value,
                    batch.provider_ref,
                    batch.model_dump_json(),
                    batch.created_at.isoformat(),
                ),
            )

    def update_batch(self, batch: PayoutBatch) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE payout_batches SET status = ?, provider_ref = ?, payload_json = ?
                   WHERE batch_id = ?""",
                (
                    batch.status.value,
                    batch.provider_ref,
                    batch.model_dump_json(),
                    batch.batch_id,
                ),
            )

    def get_batch(self, batch_id: str) -> PayoutBatch | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM payout_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        return PayoutBatch.model_validate_json(row["payload_json"]) if row else None

    def batches(self, noder_principal: str) -> list[PayoutBatch]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM payout_batches WHERE noder_principal = ?"
                " ORDER BY created_at ASC",
                (noder_principal,),
            ).fetchall()
        return [PayoutBatch.model_validate_json(row["payload_json"]) for row in rows]

    def batches_by_status(self, status: PayoutStatus) -> list[PayoutBatch]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM payout_batches WHERE status = ?"
                " ORDER BY created_at ASC",
                (status.value,),
            ).fetchall()
        return [PayoutBatch.model_validate_json(row["payload_json"]) for row in rows]
