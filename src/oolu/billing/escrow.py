"""Escrow (M2): captured money waits in a liability until the world confirms.

The spec's discipline — prefer reversible authorization and escrow before
irreversible settlement — as posting arithmetic. Capture moves the buyer's
money into ``escrow_liability`` (the platform holds it, owing it to
someone); release moves it out to the seller, the fee account, and the tax
account only when a release condition holds: verified delivery evidence
plus buyer acceptance, the automatic acceptance timeout, or a dispute
resolution. Both moves are balanced transactions built here as pure
functions; the order machine decides WHEN, the ledger records THAT.

Exceptions are the spec's own: immediate digital delivery and low-value
trusted transactions may settle directly (the M1 single-split capture) —
an explicit policy choice, never a default.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .doubleentry import LedgerEntry


class EscrowPolicy(BaseModel):
    """When money may skip the escrow hold, and how long acceptance waits."""

    model_config = ConfigDict(frozen=True)

    # Below this total, a trusted seller settles directly. Zero = never.
    low_value_micros: int = Field(default=0, ge=0)
    trusted_sellers: tuple[str, ...] = ()
    # Immediate digital delivery may settle directly when the order says so.
    allow_direct_digital: bool = True
    # The automatic-acceptance clock: a delivered, evidenced, unaccepted
    # order releases on its own after this many days.
    acceptance_timeout_days: int = Field(default=7, ge=1)

    def holds(
        self, *, total_micros: int, seller_id: str, digital: bool = False
    ) -> bool:
        """True = capture into escrow; False = the spec's exceptions apply."""
        if digital and self.allow_direct_digital:
            return False
        if (
            self.low_value_micros
            and total_micros <= self.low_value_micros
            and seller_id in self.trusted_sellers
        ):
            return False
        return True


def capture_entries(*, total_micros: int) -> tuple[LedgerEntry, ...]:
    """The buyer's money arrives and is OWED, not earned: cash against
    the escrow liability, nothing split yet."""
    return (
        LedgerEntry(account="marketplace_cash", amount_micros=total_micros),
        LedgerEntry(account="escrow_liability", amount_micros=-total_micros),
    )


def release_entries(
    *,
    subtotal_micros: int,
    fees_micros: int,
    tax_micros: int,
    take_fee_micros: int,
) -> tuple[LedgerEntry, ...]:
    """The hold ends: escrow empties into the seller's payable, the
    platform's fee revenue, and the tax liability — balanced exactly."""
    total = subtotal_micros + fees_micros + tax_micros
    seller_share = subtotal_micros + fees_micros - take_fee_micros
    entries = [
        LedgerEntry(account="escrow_liability", amount_micros=total),
        LedgerEntry(account="seller_payable", amount_micros=-seller_share),
    ]
    if take_fee_micros:
        entries.append(
            LedgerEntry(
                account="marketplace_fee_revenue", amount_micros=-take_fee_micros
            )
        )
    if tax_micros:
        entries.append(
            LedgerEntry(account="tax_payable", amount_micros=-tax_micros)
        )
    return tuple(entries)
