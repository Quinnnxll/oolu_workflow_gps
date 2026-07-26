"""The six-decision policy ladder — deterministic rules over typed facts.

The spec's governing rule made executable: OoLu may create typed commercial
intents, but money movement and contractual commitment occur only after
deterministic policy evaluation. Both evaluators here are pure functions —
no clock, no store, no model call, no randomness. Same typed inputs, same
verdict, every time; the verdict carries its reasons and the policy version
that produced it, so every decision is reconstructable years later.

The ladder is evaluated strictest-first: deny, then dual control, then
strong approval, then approval, and only a run that trips nothing may
auto-execute (or execute-and-notify). Reasons ACCUMULATE within a rung —
like budget verdicts, the human sees every tripped trigger, not the first.

Risk facts (fraud score, first-time counterparty, benchmark) arrive as
typed inputs. In M0 the caller supplies them; later phases derive them from
history and provider signals — the evaluator neither knows nor cares, which
is what keeps it pure. Defaults are conservative: an unstated fact is the
riskier reading (first-time counterparty, unverified seller, unknown
destination), never the convenient one.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

_M = 1_000_000  # micros per currency unit


class Decision(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    EXECUTE_AND_NOTIFY = "execute_and_notify"
    REQUIRE_APPROVAL = "require_approval"
    REQUIRE_STRONG_APPROVAL = "require_strong_approval"
    REQUIRE_MULTIPLE_APPROVERS = "require_multiple_approvers"
    DENY = "deny"


class PurchasePolicy(BaseModel):
    """The buy side's typed limits — the user's stored values, versioned.

    ``policy_version`` rides into every digest: approving under one policy
    and executing under another is itself a material change.
    """

    model_config = ConfigDict(frozen=True)

    policy_id: str = "default-purchase"
    policy_version: str = "purchase-v1"
    auto_purchase_limit_micros: int = 50 * _M
    notify_limit_micros: int = 200 * _M
    strong_approval_limit_micros: int = 1_000 * _M
    daily_limit_micros: int = 200 * _M
    monthly_limit_micros: int = 2_000 * _M
    # None = not an organization; no dual-control rung.
    dual_control_limit_micros: int | None = None
    trusted_categories: tuple[str, ...] = ()
    trusted_counterparties: tuple[str, ...] = ()
    preapproved_destinations: tuple[str, ...] = ()
    prohibited_categories: tuple[str, ...] = ()
    regulated_categories: tuple[str, ...] = ()
    price_benchmark_percent: int = 10
    anomaly_notify_threshold: float = 0.25
    strong_fraud_score: float = 0.5
    fraud_deny_score: float = 0.85
    approval_ttl_minutes: int = 24 * 60


class PurchaseFacts(BaseModel):
    """Everything the purchase ladder reads, typed. Conservative defaults."""

    model_config = ConfigDict(frozen=True)

    amount_micros: int = Field(ge=0)
    currency: str = "USD"
    category: str = ""
    counterparty: str = ""
    first_time_counterparty: bool = True
    refundable: bool = True
    recurring: bool = False
    sensitive_data_required: bool = False
    delivery_destination: str = ""
    new_delivery_destination: bool = True
    price_benchmark_micros: int | None = None
    irreversible_delivery: bool = False
    international_payment: bool = False
    fraud_score: float = Field(default=0.0, ge=0.0, le=1.0)
    anomaly_score: float = Field(default=0.0, ge=0.0, le=1.0)
    daily_total_micros: int = Field(default=0, ge=0)
    monthly_total_micros: int = Field(default=0, ge=0)
    material_term_change: bool = False
    sanctions_match: bool = False
    # Delegation gaps arrive as reasons; any gap is a deny.
    agent_permission_gaps: tuple[str, ...] = ()
    budget_exceeded: bool = False
    seller_identity_verified: bool = False
    payout_destination_change: bool = False


class PolicyVerdict(BaseModel):
    """The ladder's output: decision, every reason, and the review shape."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    reasons: tuple[str, ...]
    required_approvers: int = 0
    # "none" | "normal" | "strong" — the authentication floor for approval.
    approval_strength: str = "none"
    approval_ttl_minutes: int = 0
    policy_version: str


def _money(micros: int, currency: str) -> str:
    return f"{micros / _M:.2f} {currency}"


def evaluate_purchase(policy: PurchasePolicy, facts: PurchaseFacts) -> PolicyVerdict:
    """The purchase ladder, strictest rung first. Pure."""

    def verdict(
        decision: Decision, reasons: list[str], *, approvers: int, strength: str
    ) -> PolicyVerdict:
        return PolicyVerdict(
            decision=decision,
            reasons=tuple(reasons),
            required_approvers=approvers,
            approval_strength=strength,
            approval_ttl_minutes=(policy.approval_ttl_minutes if approvers else 0),
            policy_version=policy.policy_version,
        )

    amount = _money(facts.amount_micros, facts.currency)

    deny: list[str] = []
    if facts.category and facts.category in policy.prohibited_categories:
        deny.append(f"category '{facts.category}' is prohibited")
    if facts.sanctions_match:
        deny.append("counterparty matches a sanctions list")
    deny.extend(facts.agent_permission_gaps)
    if facts.budget_exceeded:
        deny.append("the declared budget is exceeded")
    if not facts.seller_identity_verified:
        deny.append("seller identity is unverified")
    if facts.fraud_score >= policy.fraud_deny_score:
        deny.append(
            f"fraud score {facts.fraud_score:.2f} is at or above the deny "
            f"threshold {policy.fraud_deny_score:.2f}"
        )
    if deny:
        return verdict(Decision.DENY, deny, approvers=0, strength="none")

    multi: list[str] = []
    if (
        policy.dual_control_limit_micros is not None
        and facts.amount_micros > policy.dual_control_limit_micros
    ):
        multi.append(
            f"{amount} is above the dual-control limit "
            f"{_money(policy.dual_control_limit_micros, facts.currency)}"
        )
    if facts.payout_destination_change:
        multi.append("a payout-destination change always needs dual control")
    if multi:
        return verdict(
            Decision.REQUIRE_MULTIPLE_APPROVERS, multi, approvers=2, strength="strong"
        )

    strong: list[str] = []
    if facts.amount_micros > policy.strong_approval_limit_micros:
        strong.append(
            f"{amount} is above the strong-approval limit "
            f"{_money(policy.strong_approval_limit_micros, facts.currency)}"
        )
    if facts.category and facts.category in policy.regulated_categories:
        strong.append(f"category '{facts.category}' is regulated")
    if facts.irreversible_delivery:
        strong.append("delivery is irreversible")
    if facts.international_payment:
        strong.append("the payment crosses a border")
    if facts.fraud_score >= policy.strong_fraud_score:
        strong.append(f"fraud score {facts.fraud_score:.2f} is elevated")
    if strong:
        return verdict(
            Decision.REQUIRE_STRONG_APPROVAL, strong, approvers=1, strength="strong"
        )

    approval: list[str] = []
    if facts.amount_micros > policy.auto_purchase_limit_micros:
        approval.append(
            f"{amount} is above the auto-purchase limit "
            f"{_money(policy.auto_purchase_limit_micros, facts.currency)}"
        )
    if facts.first_time_counterparty:
        approval.append("first purchase from this counterparty")
    if not facts.refundable:
        approval.append("the purchase is non-refundable")
    if facts.recurring:
        approval.append("a recurring obligation always needs approval")
    if facts.price_benchmark_micros is not None and facts.price_benchmark_micros > 0:
        variance = abs(facts.amount_micros - facts.price_benchmark_micros)
        allowed = facts.price_benchmark_micros * policy.price_benchmark_percent // 100
        if variance > allowed:
            approval.append(
                f"price is outside the benchmark "
                f"{_money(facts.price_benchmark_micros, facts.currency)} by more "
                f"than {policy.price_benchmark_percent}%"
            )
    if facts.new_delivery_destination:
        approval.append("delivery goes to a new destination")
    if facts.sensitive_data_required:
        approval.append("the purchase requires sharing sensitive data")
    if facts.material_term_change:
        approval.append("a material term changed since the last review")
    if facts.amount_micros + facts.daily_total_micros > policy.daily_limit_micros:
        approval.append(
            f"would take today's total past the daily limit "
            f"{_money(policy.daily_limit_micros, facts.currency)}"
        )
    if facts.amount_micros + facts.monthly_total_micros > policy.monthly_limit_micros:
        approval.append(
            f"would take this month's total past the monthly limit "
            f"{_money(policy.monthly_limit_micros, facts.currency)}"
        )
    if approval:
        return verdict(
            Decision.REQUIRE_APPROVAL, approval, approvers=1, strength="normal"
        )

    # Nothing tripped. Auto-execution demands every positive requirement;
    # what is missing decides between silent execution and notify.
    auto_missing: list[str] = []
    if facts.category not in policy.trusted_categories:
        auto_missing.append(f"category '{facts.category}' is not trusted")
    if facts.counterparty not in policy.trusted_counterparties:
        auto_missing.append(f"counterparty '{facts.counterparty}' is not trusted")
    if facts.delivery_destination not in policy.preapproved_destinations:
        auto_missing.append("delivery destination is not pre-approved")
    if facts.price_benchmark_micros is None:
        auto_missing.append("no price benchmark is available")
    if not auto_missing:
        return verdict(
            Decision.AUTO_EXECUTE,
            ["every auto-execution requirement holds"],
            approvers=0,
            strength="none",
        )
    if (
        facts.amount_micros <= policy.notify_limit_micros
        and facts.anomaly_score < policy.anomaly_notify_threshold
    ):
        return verdict(
            Decision.EXECUTE_AND_NOTIFY,
            auto_missing
            + [
                f"{amount} is within the notify limit "
                f"{_money(policy.notify_limit_micros, facts.currency)}"
            ],
            approvers=0,
            strength="none",
        )
    return verdict(
        Decision.REQUIRE_APPROVAL,
        auto_missing + ["auto-execution requirements are not met"],
        approvers=1,
        strength="normal",
    )


class SalesPolicy(BaseModel):
    """The sell side's signed boundaries — what a seller agent may accept."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = "default-sales"
    policy_version: str = "sales-v1"
    # The seller's working floor: below it, a human reviews.
    price_floor_micros: int = 0
    # The absolute floor: below it, the offer is DENIED without model
    # discretion — the spec's own acceptance test.
    absolute_floor_micros: int = 0
    auto_accept_price_micros: int = 0
    maximum_discount_percent: int = 20
    automated_quantity_limit: int = 10
    seller_review_limit_micros: int = 1_000 * _M


class SaleFacts(BaseModel):
    """Everything the sales ladder reads. Conservative defaults again."""

    model_config = ConfigDict(frozen=True)

    final_price_micros: int = Field(ge=0)
    currency: str = "USD"
    list_price_micros: int | None = None
    quantity: int = Field(default=1, gt=0)
    inventory_available: bool = False
    fulfillment_capacity_available: bool = False
    buyer_risk_acceptable: bool = False
    custom_product: bool = False
    custom_warranty: bool = False
    custom_terms: bool = False
    accelerated_delivery: bool = False
    exclusivity_requested: bool = False
    restricted_destination: bool = False
    fraudulent_buyer: bool = False


def evaluate_sale(policy: SalesPolicy, facts: SaleFacts) -> PolicyVerdict:
    """The order-acceptance ladder for the seller agent. Pure."""

    def verdict(
        decision: Decision, reasons: list[str], *, approvers: int, strength: str
    ) -> PolicyVerdict:
        return PolicyVerdict(
            decision=decision,
            reasons=tuple(reasons),
            required_approvers=approvers,
            approval_strength=strength,
            approval_ttl_minutes=(24 * 60 if approvers else 0),
            policy_version=policy.policy_version,
        )

    price = _money(facts.final_price_micros, facts.currency)

    deny: list[str] = []
    if facts.final_price_micros < policy.absolute_floor_micros:
        deny.append(
            f"{price} is below the absolute price floor "
            f"{_money(policy.absolute_floor_micros, facts.currency)}"
        )
    if not facts.inventory_available:
        deny.append("inventory is unavailable")
    if facts.restricted_destination:
        deny.append("the destination is restricted")
    if facts.fraudulent_buyer:
        deny.append("the buyer is flagged as fraudulent")
    if deny:
        return verdict(Decision.DENY, deny, approvers=0, strength="none")

    review: list[str] = []
    if facts.final_price_micros < policy.auto_accept_price_micros:
        review.append(
            f"{price} is below the auto-accept price "
            f"{_money(policy.auto_accept_price_micros, facts.currency)}"
        )
    if facts.list_price_micros:
        discount = (
            (facts.list_price_micros - facts.final_price_micros)
            * 100
            // facts.list_price_micros
        )
        if discount > policy.maximum_discount_percent:
            review.append(
                f"discount {discount}% exceeds the automated maximum "
                f"{policy.maximum_discount_percent}%"
            )
    if facts.custom_product:
        review.append("the product is custom")
    if facts.custom_warranty:
        review.append("the warranty is custom")
    if facts.accelerated_delivery:
        review.append("delivery is accelerated")
    if facts.exclusivity_requested:
        review.append("the buyer requested exclusivity")
    if facts.final_price_micros * facts.quantity > policy.seller_review_limit_micros:
        review.append(
            f"order value exceeds the seller review limit "
            f"{_money(policy.seller_review_limit_micros, facts.currency)}"
        )
    if review:
        return verdict(
            Decision.REQUIRE_APPROVAL, review, approvers=1, strength="normal"
        )

    missing: list[str] = []
    if facts.final_price_micros < policy.price_floor_micros:
        missing.append(
            f"{price} is below the seller price floor "
            f"{_money(policy.price_floor_micros, facts.currency)}"
        )
    if facts.quantity > policy.automated_quantity_limit:
        missing.append(
            f"quantity {facts.quantity} exceeds the automated limit "
            f"{policy.automated_quantity_limit}"
        )
    if not facts.fulfillment_capacity_available:
        missing.append("fulfillment capacity is not confirmed")
    if not facts.buyer_risk_acceptable:
        missing.append("buyer risk is not confirmed acceptable")
    if facts.custom_terms:
        missing.append("the order carries custom terms")
    if missing:
        return verdict(
            Decision.REQUIRE_APPROVAL, missing, approvers=1, strength="normal"
        )
    return verdict(
        Decision.AUTO_EXECUTE,
        ["every automated-acceptance requirement holds"],
        approvers=0,
        strength="none",
    )
