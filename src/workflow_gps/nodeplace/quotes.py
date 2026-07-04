"""Consumer quoting — what a workflow will cost before it runs, honestly.

``QuoteEngine`` picks one candidate per workflow step by mode-weighted
economic utility (verified quality per retry-adjusted dollar, from
``market.rank_candidates``), clears every price through the ``PriceBook``,
and renders the result the way a consumer needs to see it:

- **Subscription coverage vs pass-through.** Commodity and workflow nodes are
  covered by the plan (their line shows value delivered, amount 0.00);
  regulated fees and professional services are outside-plan lines the user
  pays at face value, per vendor.
- **Budget projection with *accumulating* warnings.** Every exceeded limit
  (automation budget, CLI quota, API quota) is reported; none overwrites
  another.
- **Retry-adjusted expected cost.** The projection charges the platform's own
  budget with ``cost / p(success)``, so an unreliable route visibly eats the
  plan budget faster than a proven one.
- **Payout previews, never payouts.** The quote shows what each noder would
  earn *if the run verifies*, computed with the same class-aware commission
  and reward multipliers the settlement pipeline will use — but money only
  moves when the metering deriver sees a platform-verified success. A quote
  is a forecast, not a ledger entry.
"""

from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..billing.pricing import PricingEngine
from .market import (
    CandidateEconomics,
    ClearedPrice,
    NodeClass,
    PriceBook,
    QuoteMode,
    rank_candidates,
)
from .rewards import (
    LineageLink,
    RewardSignals,
    commission_rate,
    lineage_shares,
    reward_multiplier,
)

QUOTES_SCHEMA_VERSION = 1


class Coverage(str, Enum):
    SUBSCRIPTION = "subscription"
    OUTSIDE_PLAN = "outside_plan"


class SubscriptionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    monthly_price: float
    automation_cost_budget: float
    included_cli_calls: int
    included_api_calls: int

    def covers(self, node_class: NodeClass) -> Coverage:
        if node_class in {NodeClass.REGULATED, NodeClass.PROFESSIONAL}:
            return Coverage.OUTSIDE_PLAN
        return Coverage.SUBSCRIPTION


class ConsumerAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    plan: SubscriptionPlan
    used_automation_budget: float = 0.0
    used_cli_calls: int = 0
    used_api_calls: int = 0


class StepCandidates(BaseModel):
    """One workflow step plus the marketplace candidates that can perform it."""

    model_config = ConfigDict(frozen=True)

    name: str
    candidates: list[CandidateEconomics]
    signals: dict[str, RewardSignals] = Field(default_factory=dict)  # by version_id
    ancestors: dict[str, list[LineageLink]] = Field(default_factory=dict)
    cli_calls: int = 0
    api_calls: int = 0
    vendor: str | None = None
    minutes_saved: float = 0.0


class InvoiceLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    amount: float
    coverage: Coverage
    vendor: str | None = None
    version_id: str | None = None


class PayoutPreview(BaseModel):
    """What a noder would earn if this step's run verifies. A forecast only."""

    model_config = ConfigDict(frozen=True)

    noder_principal: str
    version_id: str
    amount: float
    reason: str = "preview: accrues only on platform-verified success"


class StepQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: str
    chosen: CandidateEconomics
    cleared: ClearedPrice
    coverage: Coverage
    expected_automation_cost: float  # retry-adjusted platform cost
    payout_previews: list[PayoutPreview] = Field(default_factory=list)
    platform_margin_preview: float = 0.0


class WorkflowQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = QUOTES_SCHEMA_VERSION
    quote_id: str
    mode: QuoteMode
    steps: list[StepQuote]
    invoice_lines: list[InvoiceLine]
    payout_previews: list[PayoutPreview]
    subscription_covered_value: float
    outside_plan_due: float
    total_user_due_now: float
    expected_automation_cost: float
    platform_margin_preview: float
    warnings: list[str]


class QuoteEngine:
    """Turns step candidates into a priced, explainable workflow quote."""

    def __init__(
        self,
        price_book: PriceBook,
        *,
        hourly_rate: float = 60.0,
    ):
        self._book = price_book
        self._hourly_rate = hourly_rate

    def quote(
        self,
        account: ConsumerAccount,
        steps: list[StepCandidates],
        *,
        mode: QuoteMode = QuoteMode.STANDARD,
        days_elapsed: float = 30.0,
        commit_prices: bool = True,
    ) -> WorkflowQuote:
        # ``commit_prices=False`` quotes without moving the price book's
        # reference prices — for quote-shopping surfaces; a binding quote
        # (about to execute) commits.
        step_quotes: list[StepQuote] = []
        invoice_lines: list[InvoiceLine] = []
        previews: list[PayoutPreview] = []
        warnings: list[str] = []

        covered_value = 0.0
        outside_due = 0.0
        expected_cost = 0.0
        margin_preview = 0.0

        for step in steps:
            if not step.candidates:
                raise ValueError(f"no candidates for step: {step.name}")
            chosen = rank_candidates(step.candidates, mode)[0]
            signals = step.signals.get(
                chosen.version_id,
                RewardSignals(node_class=chosen.node_class),
            )
            user_value = (
                None
                if step.minutes_saved <= 0
                else step.minutes_saved / 60.0 * self._hourly_rate
            )
            cleared = self._book.clear(
                class_key=chosen.class_key,
                node_class=chosen.node_class,
                ask=chosen.cleared_price,
                cost=chosen.cost,
                substitutes=signals.substitutes,
                quality_parity=signals.quality_parity,
                user_value=user_value,
                days_elapsed=days_elapsed,
                commit=commit_prices,
            )
            chosen = chosen.model_copy(update={"cleared_price": cleared.cleared})

            coverage = account.plan.covers(chosen.node_class)
            step_cost = chosen.cost.automation_cost / max(chosen.success_mean, 0.05)
            expected_cost += step_cost

            step_previews, step_margin = self._preview_split(chosen, signals, step)
            previews.extend(step_previews)
            margin_preview += step_margin

            if coverage is Coverage.SUBSCRIPTION:
                covered_value += cleared.cleared
                invoice_lines.append(
                    InvoiceLine(
                        label=f"Included automation: {step.name}",
                        amount=0.0,
                        coverage=coverage,
                        version_id=chosen.version_id,
                    )
                )
            else:
                due = cleared.cleared + chosen.cost.external_invoice
                outside_due += due
                invoice_lines.append(
                    InvoiceLine(
                        label=f"Outside-plan: {step.name}",
                        amount=due,
                        coverage=coverage,
                        vendor=step.vendor or "third-party provider",
                        version_id=chosen.version_id,
                    )
                )

            step_quotes.append(
                StepQuote(
                    step=step.name,
                    chosen=chosen,
                    cleared=cleared,
                    coverage=coverage,
                    expected_automation_cost=step_cost,
                    payout_previews=step_previews,
                    platform_margin_preview=step_margin,
                )
            )

        projected = account.used_automation_budget + expected_cost
        if projected > account.plan.automation_cost_budget:
            warnings.append(
                "automation budget exceeded by "
                f"{projected - account.plan.automation_cost_budget:.2f}: route to "
                "cheaper nodes, defer non-urgent steps, or add automation credits"
            )
        cli_total = account.used_cli_calls + sum(s.cli_calls for s in steps)
        if cli_total > account.plan.included_cli_calls:
            warnings.append(
                f"included CLI-call quota exceeded ({cli_total}/"
                f"{account.plan.included_cli_calls})"
            )
        api_total = account.used_api_calls + sum(s.api_calls for s in steps)
        if api_total > account.plan.included_api_calls:
            warnings.append(
                f"included API-call quota exceeded ({api_total}/"
                f"{account.plan.included_api_calls})"
            )

        return WorkflowQuote(
            quote_id=uuid4().hex,
            mode=mode,
            steps=step_quotes,
            invoice_lines=invoice_lines,
            payout_previews=previews,
            subscription_covered_value=covered_value,
            outside_plan_due=outside_due,
            total_user_due_now=outside_due,
            expected_automation_cost=expected_cost,
            platform_margin_preview=margin_preview,
            warnings=warnings,
        )

    def settle_usage(
        self,
        account: ConsumerAccount,
        quote: WorkflowQuote,
        steps: list[StepCandidates],
    ) -> ConsumerAccount:
        """Charge the plan's usage counters for an executed quote."""
        return account.model_copy(
            update={
                "used_automation_budget": account.used_automation_budget
                + quote.expected_automation_cost,
                "used_cli_calls": account.used_cli_calls
                + sum(s.cli_calls for s in steps),
                "used_api_calls": account.used_api_calls
                + sum(s.api_calls for s in steps),
            }
        )

    # ------------------------------------------------------------------ #
    # Preview split — the same math settlement will use, clearly labeled.  #
    # ------------------------------------------------------------------ #
    def _preview_split(
        self,
        chosen: CandidateEconomics,
        signals: RewardSignals,
        step: StepCandidates,
    ) -> tuple[list[PayoutPreview], float]:
        if chosen.node_class is NodeClass.REGULATED:
            return [], 0.0  # pass-through: no pool, no commission
        breakdown = reward_multiplier(signals)
        scarcity_bonus = 0.5 / (1.0 + max(0, signals.substitutes))
        rho = commission_rate(chosen.node_class, scarcity_bonus=scarcity_bonus)
        shares = lineage_shares(
            chosen.noder_principal,
            list(step.ancestors.get(chosen.version_id, [])),
            executing_multiplier=breakdown.multiplier,
        )
        result = PricingEngine(rho=rho).price(
            gross=chosen.cleared_price,
            provider_cost=chosen.cost.automation_cost,
            shares=shares,
        )
        previews = [
            PayoutPreview(
                noder_principal=principal,
                version_id=chosen.version_id,
                amount=micros / 1_000_000,
            )
            for principal, micros in sorted(result.noder_micros.items())
        ]
        return previews, result.platform_micros / 1_000_000
