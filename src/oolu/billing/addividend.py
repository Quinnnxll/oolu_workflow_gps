"""The contributor ad dividend — verified delivery pays (A5).

ADR-0005's first client: contributors are earning principals, and a
story's lineage weights — recorded at composition time, long before any
money existed — become the split a verified ad impression pays over.
Everything rides the STANDING pipeline, exactly as the plan demands:

- the split is the same :class:`PricingEngine` the Nodeplace pays with
  (contributors in the noder seat, the ad commission α in the rho seat)
  — conservation is structural, `platform + Σ contributors == net`;
- accruals land in the same :class:`EarningsLedger` with the same
  holdback ``H``, so the standing settlement (reserve ``R``, threshold
  ``T``, risk window ``W``) and the standing :class:`DisputeService`
  clawback (reserve-first, shortfall as debt) work on ad earnings
  UNCHANGED — an ad event is just an event id with ``ad:`` in front;
- fraud gates run before money: the standing self-dealing exclusion
  (the viewer's own share never meters) hardened to the ad law — when
  the viewer is the ONLY contributor, no event settles at all — plus
  replay rejection via the idempotency ledger;
- recognition is a balanced posting on the double-entry book: the
  standing ``ad_budget_liability`` shrinks by the net, the platform's α
  lands in ``marketplace_fee_revenue``, the pool in
  ``contributor_payable`` — the A4 funding and the A5 recognition are
  two postings on ONE book that trial-balances to zero;
- and none of it moves on local-only infra: ``require_production_money``
  guards every settle, so a desktop host keeps its previews and a
  production host keeps its books.

Multipliers redistribute, never inflate (the rewards law): a clicked
placement's contributors carry a bounded engagement multiplier, an
injected reputation multiplier is clamped to ``mu_max``, and the engine
normalizes weight × multiplier within the pool — a multiplier changes a
contributor's slice, never the size of the pie.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable

from ..adhouse.delivery import AD_COMMISSION_ALPHA
from ..adhouse.placement import Placement
from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from ..metering.models import NoderShare
from .doubleentry import DoubleEntryLedger, LedgerEntry, LedgerTransaction
from .fraud import DefaultFraudSignals, FraudSignals
from .guard import require_production_money
from .ledger import EarningsLedger
from .models import EarningsEntry, EarningsKind
from .policy import DEFAULT_HOLDBACK_DAYS, DEFAULT_MU_MAX

# A clicked placement's content demonstrably engaged its reader — the
# bounded verified-engagement multiplier. Bounded and normalized: it
# shifts slices between contributors, never grows the pool.
ENGAGEMENT_MULTIPLIER = 1.25
_MU_FLOOR = 0.1  # the rewards clamp's floor, reused


def ad_event_id(placement_id: str) -> str:
    """The one naming convention: ledger rows, disputes, and audit lines
    for an ad settlement all key on ``ad:<placement_id>``."""
    return f"ad:{placement_id}"


class AdDividendService:
    """Verified impression → conserved accruals + balanced recognition."""

    def __init__(
        self,
        *,
        ledger: EarningsLedger,
        book: DoubleEntryLedger,
        durable: Any,  # anything with is_production_durable (the guard's law)
        providers: Iterable[ProviderConfig],
        idempotency: IdempotencyLedger,
        fraud: FraudSignals | None = None,
        alpha: float = AD_COMMISSION_ALPHA,
        holdback_days: int = DEFAULT_HOLDBACK_DAYS,
        mu_max: float = DEFAULT_MU_MAX,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from .pricing import PricingEngine

        self._ledger = ledger
        self._book = book
        self._durable = durable
        self._providers = list(providers)
        self._idem = idempotency
        self._fraud = fraud if fraud is not None else DefaultFraudSignals()
        self._engine = PricingEngine(rho=alpha)
        self._holdback = timedelta(days=holdback_days)
        self._mu_max = mu_max
        self._clock = clock or (lambda: datetime.now(UTC))

    def settle_impression(
        self,
        placement: Placement,
        *,
        shares: list[tuple[str, float]],  # the recorded lineage weights
        engaged: bool = False,  # a verified click stands behind this
        multiplier_of: Callable[[str], float] | None = None,  # reputation μ
    ) -> dict:
        """One verified impression becomes money — once, conserved,
        gated. Replays return the first result; local infra refuses."""
        require_production_money(self._durable, self._providers)
        event_id = ad_event_id(placement.placement_id)

        def run() -> dict:
            noder_shares = []
            for author, weight in shares:
                mu = 1.0
                if multiplier_of is not None:
                    mu = float(multiplier_of(author))
                if engaged:
                    mu *= ENGAGEMENT_MULTIPLIER
                mu = max(_MU_FLOOR, min(self._mu_max, mu))
                noder_shares.append(
                    NoderShare(
                        noder_principal=author, weight=weight, multiplier=mu
                    )
                )
            verdict = self._fraud.assess(
                idempotency_key=event_id,
                consumer_principal=placement.viewer,
                shares=noder_shares,
            )
            if not verdict.allowed:
                return {"settled": False, "reasons": verdict.reasons}
            if not verdict.shares:
                # The ad law, harder than the standing exclusion: when
                # the viewer is the ONLY contributor there is no
                # arms-length audience — no event settles at all.
                return {
                    "settled": False,
                    "reasons": [*verdict.reasons, "no_arms_length_share"],
                }
            result = self._engine.price(
                gross=placement.price_micros / 1_000_000,
                provider_cost=0.0,
                shares=verdict.shares,
            )
            available_at = placement.at + self._holdback
            for principal, amount_micros in result.noder_micros.items():
                self._ledger.append(
                    EarningsEntry(
                        noder_principal=principal,
                        event_id=event_id,
                        amount_micros=amount_micros,
                        kind=EarningsKind.ACCRUAL,
                        available_at=available_at,
                    )
                )
            pool = sum(result.noder_micros.values())
            # Recognition: the liability shrinks by exactly what was
            # earned — the platform's α and the contributors' pool — on
            # the same book the funding landed on. One book, in balance.
            self._book.post(
                LedgerTransaction(
                    txn_id=f"adrec-{placement.placement_id}",
                    idempotency_key=f"ad-recognize:{placement.placement_id}",
                    kind="ad.recognized",
                    order_id=placement.campaign_id,
                    entries=(
                        LedgerEntry(
                            account="ad_budget_liability",
                            amount_micros=result.net_micros,
                        ),
                        LedgerEntry(
                            account="marketplace_fee_revenue",
                            amount_micros=-result.platform_micros,
                        ),
                        LedgerEntry(
                            account="contributor_payable",
                            amount_micros=-pool,
                        ),
                    ),
                    memo=f"ad impression recognized for {placement.campaign_id}",
                    created_at=self._clock(),
                )
            )
            return {
                "settled": True,
                "event_id": event_id,
                "net_micros": result.net_micros,
                "platform_micros": result.platform_micros,
                "contributor_accruals": dict(result.noder_micros),
                "excluded": verdict.reasons,
            }

        return self._idem.run(
            f"ad-settle:{placement.placement_id}", run, scope="billing"
        )
