from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from .accounts import PayoutStore
from .guard import require_production_money
from .ledger import BalanceProjection, EarningsLedger
from .models import EarningsEntry, EarningsKind, PayoutBatch, PayoutStatus
from .payout import PaymentError, PayoutAdapter
from .policy import DEFAULT_MIN_PAYOUT_MICROS, DEFAULT_RESERVE_FRACTION


class SettlementService:
    def __init__(
        self,
        *,
        ledger: EarningsLedger,
        payout_store: PayoutStore,
        payout: PayoutAdapter,
        durable: Any,
        providers: Iterable[ProviderConfig],
        idempotency: IdempotencyLedger,
        reserve_fraction: float = DEFAULT_RESERVE_FRACTION,
        min_payout_micros: int = DEFAULT_MIN_PAYOUT_MICROS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._accounts = payout_store
        self._payout = payout
        self._durable = durable
        self._providers = list(providers)
        self._idem = idempotency
        self._reserve = reserve_fraction
        self._min_payout = min_payout_micros
        self._projection = BalanceProjection(ledger)
        self._clock = clock or (lambda: datetime.now(UTC))

    def settle(self, noder_principal: str, *, period_key: str) -> dict:
        """Settle one noder for a period, at most once — failure contained.

        A processor failure (``PaymentError``) is a first-class outcome,
        not a crash: the batch is marked FAILED for the record, the ledger
        is never debited (the money never moved), and the period's
        idempotency claim is released — so the same period is honestly
        retryable once the cause is fixed, while a period that PAID stays
        paid forever.
        """
        require_production_money(self._durable, self._providers)
        key = f"settle:{noder_principal}:{period_key}"
        failure: dict = {}

        def run() -> dict:
            now = self._clock()
            self._top_up_reserve(noder_principal, now)
            available = self._projection.balance(
                noder_principal, now=now
            ).available_micros

            if available < self._min_payout:
                return {
                    "paid": False,
                    "available_micros": available,
                    "reason": "below_threshold",
                }
            if not self._accounts.is_verified(noder_principal):
                return {
                    "paid": False,
                    "available_micros": available,
                    "reason": "kyc_pending",
                }

            account = self._accounts.get_account(noder_principal)
            batch = PayoutBatch(
                noder_principal=noder_principal, amount_micros=available
            )
            self._accounts.add_batch(batch)
            try:
                receipt = self._payout.payout(
                    idempotency_key=f"payout:{noder_principal}:{period_key}",
                    provider_account_id=account.provider_account_id,
                    amount_micros=available,
                    currency=account.currency,
                )
            except PaymentError as exc:
                # The batch records the attempt; nothing was debited.
                self._accounts.update_batch(
                    batch.model_copy(update={"status": PayoutStatus.FAILED})
                )
                failure.update(
                    {
                        "paid": False,
                        "reason": "payment_failed",
                        "batch_id": batch.batch_id,
                        "amount_micros": available,
                        "error": str(exc),
                    }
                )
                raise
            self._accounts.update_batch(
                batch.model_copy(
                    update={
                        "status": receipt.status,
                        "provider_ref": receipt.provider_ref,
                    }
                )
            )
            self._ledger.append(
                EarningsEntry(
                    noder_principal=noder_principal,
                    event_id=batch.batch_id,
                    amount_micros=-available,
                    kind=EarningsKind.PAYOUT,
                    available_at=now,
                )
            )
            return {
                "paid": True,
                "batch_id": batch.batch_id,
                "amount_micros": available,
                "provider_ref": receipt.provider_ref,
            }

        try:
            return self._idem.run(key, run, scope="settlement")
        except PaymentError:
            # Release the claim: the failure must not poison the period
            # (a claim without a result replays as None forever).
            self._idem.release(key)
            return failure

    def settle_all(self, *, period_key: str) -> dict:
        """One settlement cycle: every noder on the ledger, one period.

        Outcomes are per-noder and independent — one processor failure
        never blocks anyone else's payout. Re-running the same period is
        safe and is exactly how failures are retried: paid noders replay
        their cached receipts, failed ones get a fresh attempt. The cycle
        summary is appended to the durable audit log.
        """
        require_production_money(self._durable, self._providers)
        outcomes: dict[str, dict] = {}
        paid = failed = skipped = 0
        paid_micros = 0
        for noder in self._ledger.principals():
            outcome = self.settle(noder, period_key=period_key)
            if outcome is None:  # a claim left by a crashed run: surface it
                outcome = {"paid": False, "reason": "in_flight_or_crashed"}
            outcomes[noder] = outcome
            if outcome.get("paid"):
                paid += 1
                paid_micros += outcome["amount_micros"]
            elif outcome.get("reason") == "payment_failed":
                failed += 1
            else:
                skipped += 1
        summary = {
            "period_key": period_key,
            "noders": len(outcomes),
            "paid": paid,
            "paid_micros": paid_micros,
            "failed": failed,
            "skipped": skipped,
            "outcomes": outcomes,
        }
        audit = getattr(self._durable, "audit", None)
        if audit is not None:
            audit.append(
                "settlement.cycle",
                {key: value for key, value in summary.items() if key != "outcomes"},
            )
        return summary

    def _top_up_reserve(self, noder_principal: str, now: datetime) -> None:
        entries = self._ledger.entries(noder_principal)
        cleared_gross = sum(
            e.amount_micros
            for e in entries
            if e.kind == EarningsKind.ACCRUAL and e.available_at <= now
        )
        target = round(cleared_gross * self._reserve)
        held = sum(e.amount_micros for e in entries if e.kind == EarningsKind.RESERVE)
        delta = target - held
        if delta > 0:
            self._ledger.append(
                EarningsEntry(
                    noder_principal=noder_principal,
                    amount_micros=delta,
                    kind=EarningsKind.RESERVE,
                    available_at=now,
                )
            )
